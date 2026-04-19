import pytest


@pytest.fixture(autouse=True)
def _bypass_preflight(monkeypatch, app_modules):
    """Skip real claude/codex/at probing in operations tests."""
    from schedule_agent import environment, preflight

    probe = environment.AgentProbe(
        agent="claude",
        resolved_path="/fake/claude",
        version="2.1.112",
        version_known_good=True,
        help_ok=True,
        error=None,
    )
    report = preflight.PreflightReport(results=[])
    monkeypatch.setattr(
        app_modules.operations,
        "_submit_preflight",
        lambda agent: (report, probe),
    )


def _base_job(app_modules):
    return app_modules.transitions.make_job(
        job_id="job1",
        title="Ship scheduler overhaul",
        agent="claude",
        session_mode="new",
        session_id=None,
        prompt_file="/tmp/prompt.md",
        scheduled_for="2026-04-18T09:00:00+0000",
        cwd="/tmp/project",
        log_dir="/tmp/project/logs/job1",
    )


def _scheduled_job(app_modules, at_job_id="42"):
    return app_modules.transitions.on_submit(_base_job(app_modules), at_job_id)


def _completed_job(app_modules):
    running = app_modules.transitions.on_start(
        _scheduled_job(app_modules),
        started_at="2026-04-18T09:00:00+0000",
        log_file="/tmp/project/logs/job1/run.log",
    )
    return app_modules.transitions.on_success(
        running,
        finished_at="2026-04-18T09:05:00+0000",
        exit_code=0,
        log_file="/tmp/project/logs/job1/run.log",
    )


def test_create_job_persists_unsent_job_with_derived_title_and_prompt_file(
    app_modules, monkeypatch
):
    operations = app_modules.operations
    monkeypatch.setattr(operations, "now_iso", lambda: "2026-04-17T10:00:00+0000")
    monkeypatch.setattr(
        operations,
        "resolve_schedule_spec",
        lambda spec: "2026-04-18T11:30:00+0000",
    )

    job = operations.create_job(
        agent="claude",
        session_mode="new",
        session_id=None,
        prompt_text="\n  Release checklist\n\nbody",
        schedule_spec="tomorrow 11:30",
        cwd="/tmp/project",
        submit=False,
    )

    assert job["title"] == "Release checklist"
    assert job["submission"] == "queued"
    assert job["scheduled_for"] == "2026-04-18T11:30:00+0000"
    assert job["id"].startswith("claude-20260417T100000")
    assert app_modules.persistence.load_jobs()[0]["id"] == job["id"]
    assert (
        app_modules.persistence.Path(job["prompt_file"])
        .read_text(encoding="utf-8")
        .startswith("\n  Release checklist")
    )


def test_create_job_surfaces_submit_failure_but_keeps_job_for_repair(app_modules, monkeypatch):
    operations = app_modules.operations
    monkeypatch.setattr(operations, "now_iso", lambda: "2026-04-17T10:00:00+0000")
    monkeypatch.setattr(
        operations,
        "resolve_schedule_spec",
        lambda spec: "2026-04-18T11:30:00+0000",
    )
    monkeypatch.setattr(
        operations,
        "submit_job",
        lambda job: (_ for _ in ()).throw(RuntimeError("at unavailable")),
    )

    with pytest.raises(operations.OperationError, match="at unavailable"):
        operations.create_job(
            agent="claude",
            session_mode="new",
            session_id=None,
            prompt_text="Release checklist",
            schedule_spec="tomorrow 11:30",
            cwd="/tmp/project",
            submit=True,
        )

    persisted = app_modules.persistence.load_jobs()
    assert len(persisted) == 1
    assert persisted[0]["submission"] == "queued"
    assert persisted[0]["at_job_id"] is None


def test_refresh_prompt_errors_when_file_is_missing(app_modules):
    operations = app_modules.operations
    job = _base_job(app_modules)
    job["prompt_file"] = "/tmp/does-not-exist.md"
    app_modules.persistence.save_jobs([job])

    with pytest.raises(operations.OperationError, match="Prompt file not found"):
        operations.refresh_prompt("job1")


def test_change_session_resubmits_scheduled_job(app_modules, monkeypatch):
    operations = app_modules.operations
    app_modules.persistence.save_jobs([_scheduled_job(app_modules)])

    removed = []
    submitted = []

    monkeypatch.setattr(
        operations,
        "remove_at_job",
        lambda at_job_id: (removed.append(at_job_id) or True, ""),
    )

    def fake_submit(job):
        submitted.append((job["submission"], job["session_mode"], job["session_id"]))
        return "99", "job 99 at Fri Apr 18 09:00:00 2026"

    def fake_query(at_job_id):
        if at_job_id == "99":
            return type("Entry", (), {"scheduled_for": "2026-04-18T09:00:00+0000"})(), None
        return None, None

    monkeypatch.setattr(operations, "submit_job", fake_submit)
    monkeypatch.setattr(operations, "query_atq_entry", fake_query)

    updated = operations.change_session("job1", "sess-999")

    assert removed == ["42"]
    assert submitted == [("queued", "resume", "sess-999")]
    assert updated["session_mode"] == "resume"
    assert updated["session_id"] == "sess-999"
    assert updated["submission"] == "scheduled"
    assert updated["at_job_id"] == "99"


def test_submit_or_repair_rejects_non_submittable_job(app_modules):
    operations = app_modules.operations
    app_modules.persistence.save_jobs([_completed_job(app_modules)])

    with pytest.raises(operations.OperationError, match="not submittable"):
        operations.submit_or_repair_job("job1")


def test_retry_job_resets_terminal_state_and_resubmits(app_modules, monkeypatch):
    operations = app_modules.operations
    app_modules.persistence.save_jobs([_completed_job(app_modules)])
    monkeypatch.setattr(
        operations,
        "resolve_schedule_spec",
        lambda spec: "2026-04-19T08:00:00+0000",
    )
    monkeypatch.setattr(
        operations,
        "submit_job",
        lambda job: ("77", "job 77 at Sat Apr 19 08:00:00 2026"),
    )
    monkeypatch.setattr(
        operations,
        "query_atq_entry",
        lambda at_job_id: (
            type("Entry", (), {"scheduled_for": "2026-04-19T08:05:00+0000"})(),
            None,
        ),
    )

    updated = operations.retry_job("job1", "tomorrow 08:00")

    assert updated["submission"] == "scheduled"
    assert updated["execution"] == "pending"
    assert updated["scheduled_for"] == "2026-04-19T08:05:00+0000"
    assert updated["last_run_at"] is None
    assert updated["last_exit_code"] is None


def test_format_job_summary_and_list_rows_include_scheduler_metadata(app_modules, monkeypatch):
    operations = app_modules.operations
    app_modules.persistence.save_jobs([_scheduled_job(app_modules)])
    monkeypatch.setattr(
        operations,
        "query_atq",
        lambda *args, **kwargs: (
            {"42": type("Entry", (), {"scheduled_for": "2026-04-18T09:30:00+0000"})()},
            None,
        ),
    )

    view = operations.get_job_view("job1")
    summary = operations.format_job_summary(view)
    monkeypatch.setattr(
        operations,
        "list_job_views",
        lambda filter_name="all": ([view], "atq warning"),
    )
    rows, error = operations.list_rows()

    assert "atq_run_at:" in summary
    assert "drift_reason:" in summary
    assert rows[0].startswith("Ship scheduler overhaul | Scheduled | Drifted |")
    assert error == "atq warning"
