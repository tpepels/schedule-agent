import json
import os
from pathlib import Path

import pytest


def _write_job(app_modules, **overrides):
    job = app_modules.transitions.make_job(
        job_id="job1",
        title="Do the thing",
        agent="claude",
        session_mode="new",
        session_id=None,
        prompt_file="/tmp/prompt.md",
        scheduled_for="2026-04-18T09:00:00+0100",
        cwd="/tmp/project",
        log_dir="/tmp/project/logs/job1",
    )
    job.update(overrides)
    app_modules.persistence.save_jobs([job])
    return job


def _write_jsonl(path: Path, *entries: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(entry) for entry in entries), encoding="utf-8")


def test_list_job_views_reports_missing_and_drifted_scheduler_states(app_modules, monkeypatch):
    operations = app_modules.operations
    _write_job(app_modules, submission="scheduled", at_job_id="42")

    monkeypatch.setattr(
        operations,
        "query_atq",
        lambda *args, **kwargs: (
            {"42": type("Entry", (), {"scheduled_for": "2026-04-18T09:05:00+0100"})()},
            None,
        ),
    )
    jobs, error = operations.list_job_views()
    assert error is None
    assert jobs[0]["scheduler_status"] == "drifted"

    monkeypatch.setattr(operations, "query_atq", lambda *args, **kwargs: ({}, None))
    jobs, _ = operations.list_job_views()
    assert jobs[0]["scheduler_status"] == "missing"
    assert jobs[0]["display_state"] == "scheduled"


def test_reschedule_of_scheduled_job_removes_old_at_job_and_resubmits(app_modules, monkeypatch):
    operations = app_modules.operations
    _write_job(app_modules, submission="scheduled", at_job_id="42")

    removed = []
    submitted = []

    monkeypatch.setattr(
        operations, "remove_at_job", lambda at_job_id: (removed.append(at_job_id) or True, "")
    )
    monkeypatch.setattr(operations, "query_atq_entry", lambda at_job_id: (None, None))

    def fake_submit(job):
        submitted.append(job["scheduled_for"])
        return "99", "job 99 at Fri Apr 18 10:00:00 2026"

    monkeypatch.setattr(operations, "submit_job", fake_submit)
    monkeypatch.setattr(
        operations, "resolve_schedule_spec", lambda spec: "2026-04-18T10:00:00+0100"
    )

    updated = operations.reschedule_job("job1", "now + 1 hour")
    assert removed == ["42"]
    assert submitted == ["2026-04-18T10:00:00+0100"]
    assert updated["submission"] == "scheduled"
    assert updated["at_job_id"] == "99"


def test_submit_or_repair_replaces_stale_scheduler_membership(app_modules, monkeypatch):
    operations = app_modules.operations
    _write_job(app_modules, submission="scheduled", at_job_id="42")

    removed = []
    submitted = []

    monkeypatch.setattr(
        operations,
        "remove_at_job",
        lambda at_job_id: (removed.append(at_job_id) or False, "atrm: job 42 not found"),
    )

    def fake_query(at_job_id):
        if at_job_id == "42":
            return None, None
        return type("Entry", (), {"scheduled_for": "2026-04-18T09:10:00+0100"})(), None

    monkeypatch.setattr(operations, "query_atq_entry", fake_query)

    def fake_submit(job):
        submitted.append((job["submission"], job["scheduled_for"], job["at_job_id"]))
        return "99", "job 99 at Fri Apr 18 09:10:00 2026"

    monkeypatch.setattr(operations, "submit_job", fake_submit)

    updated = operations.submit_or_repair_job("job1")
    assert removed == ["42"]
    assert submitted == [("queued", "2026-04-18T09:00:00+0100", None)]
    assert updated["submission"] == "scheduled"
    assert updated["at_job_id"] == "99"
    assert updated["scheduled_for"] == "2026-04-18T09:10:00+0100"


def test_reschedule_job_leaves_unscheduled_job_when_resubmit_fails(app_modules, monkeypatch):
    operations = app_modules.operations
    _write_job(app_modules, submission="scheduled", at_job_id="42")

    monkeypatch.setattr(operations, "remove_at_job", lambda at_job_id: (True, ""))
    monkeypatch.setattr(
        operations, "resolve_schedule_spec", lambda spec: "2026-04-18T10:00:00+0100"
    )
    monkeypatch.setattr(
        operations,
        "submit_job",
        lambda job: (_ for _ in ()).throw(RuntimeError("at unavailable")),
    )

    with pytest.raises(operations.OperationError, match="at unavailable"):
        operations.reschedule_job("job1", "now + 1 hour")

    jobs = app_modules.persistence.load_jobs()
    assert jobs[0]["submission"] == "queued"
    assert jobs[0]["at_job_id"] is None
    assert jobs[0]["scheduled_for"] == "2026-04-18T10:00:00+0100"


def test_refresh_prompt_updates_title_and_resubmits_when_scheduled(
    app_modules, monkeypatch, tmp_path
):
    operations = app_modules.operations
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("New prompt title\n\nbody", encoding="utf-8")
    _write_job(app_modules, submission="scheduled", at_job_id="42", prompt_file=str(prompt_path))

    monkeypatch.setattr(operations, "remove_at_job", lambda at_job_id: (True, ""))
    monkeypatch.setattr(operations, "query_atq_entry", lambda at_job_id: (None, None))
    monkeypatch.setattr(
        operations, "submit_job", lambda job: ("77", "job 77 at Fri Apr 18 09:00:00 2026")
    )

    updated = operations.refresh_prompt("job1")
    assert updated["title"] == "New prompt title"
    assert updated["at_job_id"] == "77"


def test_unschedule_and_delete_block_running_jobs(app_modules):
    operations = app_modules.operations
    _write_job(app_modules, submission="running", execution="running")

    try:
        operations.unschedule_job("job1")
    except operations.OperationError as exc:
        assert "cannot remove from queue while running" in str(exc)
    else:
        raise AssertionError("expected OperationError")

    try:
        operations.delete_job("job1")
    except operations.OperationError as exc:
        assert "cannot delete while running" in str(exc)
    else:
        raise AssertionError("expected OperationError")


def test_mark_finished_updates_dependents(app_modules):
    operations = app_modules.operations
    parent = app_modules.transitions.make_job(
        job_id="parent",
        title="Parent",
        agent="claude",
        session_mode="new",
        session_id=None,
        prompt_file="/tmp/parent.md",
        scheduled_for="2026-04-18T09:00:00+0100",
        cwd="/tmp/project",
        log_dir="/tmp/project/logs/parent",
    )
    parent = app_modules.transitions.on_start(
        parent, "2026-04-18T09:00:00+0100", "/tmp/project/logs/parent/run.log"
    )
    child = app_modules.transitions.make_job(
        job_id="child",
        title="Child",
        agent="claude",
        session_mode="new",
        session_id=None,
        prompt_file="/tmp/child.md",
        scheduled_for="2026-04-18T10:00:00+0100",
        cwd="/tmp/project",
        log_dir="/tmp/project/logs/child",
        depends_on="parent",
    )
    app_modules.persistence.save_jobs([parent, child])

    operations.mark_finished(
        "parent",
        finished_at="2026-04-18T09:05:00+0100",
        exit_code=0,
        log_file="/tmp/project/logs/parent/run.log",
    )
    jobs = app_modules.persistence.load_jobs()
    child = next(job for job in jobs if job["id"] == "child")
    assert child["readiness"] == "ready"


def test_mark_finished_failure_blocks_dependents(app_modules):
    operations = app_modules.operations
    parent = app_modules.transitions.make_job(
        job_id="parent",
        title="Parent",
        agent="claude",
        session_mode="new",
        session_id=None,
        prompt_file="/tmp/parent.md",
        scheduled_for="2026-04-18T09:00:00+0100",
        cwd="/tmp/project",
        log_dir="/tmp/project/logs/parent",
    )
    parent = app_modules.transitions.on_start(
        parent, "2026-04-18T09:00:00+0100", "/tmp/project/logs/parent/run.log"
    )
    child = app_modules.transitions.make_job(
        job_id="child",
        title="Child",
        agent="claude",
        session_mode="new",
        session_id=None,
        prompt_file="/tmp/child.md",
        scheduled_for="2026-04-18T10:00:00+0100",
        cwd="/tmp/project",
        log_dir="/tmp/project/logs/child",
        depends_on="parent",
    )
    app_modules.persistence.save_jobs([parent, child])

    operations.mark_finished(
        "parent",
        finished_at="2026-04-18T09:05:00+0100",
        exit_code=17,
        log_file="/tmp/project/logs/parent/run.log",
    )
    jobs = app_modules.persistence.load_jobs()
    child = next(job for job in jobs if job["id"] == "child")
    assert child["readiness"] == "blocked"


def test_mark_finished_reconciles_fresh_session_into_ledger(app_modules, tmp_path):
    operations = app_modules.operations
    cwd = tmp_path / "project"
    cwd.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Fresh prompt\nbody", encoding="utf-8")
    log_dir = tmp_path / "logs" / "job1"
    log_dir.mkdir(parents=True)
    job = app_modules.transitions.make_job(
        job_id="job1",
        title="Fresh prompt",
        agent="codex",
        session_mode="new",
        session_id=None,
        prompt_file=str(prompt),
        scheduled_for="2026-04-18T09:00:00+0000",
        cwd=str(cwd),
        log_dir=str(log_dir),
    )
    job["git_root"] = str(cwd)
    job["git_branch"] = "main"
    job = app_modules.transitions.on_submit(job, "42")
    app_modules.persistence.save_jobs([job])

    operations.mark_running(
        "job1", started_at="2026-04-18T09:00:00+0000", log_file=str(log_dir / "run.log")
    )
    rollout = Path(os.environ["CODEX_HOME"]) / "sessions" / "2026" / "04" / "18" / "fresh.jsonl"
    _write_jsonl(
        rollout,
        {"type": "session_meta", "payload": {"id": "fresh-1", "source": "exec", "cwd": str(cwd)}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Fresh prompt"}},
    )

    updated = operations.mark_finished(
        "job1",
        finished_at="2026-04-18T09:05:00+0000",
        exit_code=0,
        log_file=str(log_dir / "run.log"),
    )

    assert updated["session_reconciliation"]["ledger_written"] is True
    assert updated["session_reconciliation"]["matched_session_id"] == "fresh-1"


def test_mark_finished_avoids_false_certainty_for_ambiguous_sessions(app_modules, tmp_path):
    operations = app_modules.operations
    cwd = tmp_path / "project"
    cwd.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Same prompt\nbody", encoding="utf-8")
    log_dir = tmp_path / "logs" / "job1"
    log_dir.mkdir(parents=True)
    job = app_modules.transitions.make_job(
        job_id="job1",
        title="Same prompt",
        agent="codex",
        session_mode="new",
        session_id=None,
        prompt_file=str(prompt),
        scheduled_for="2026-04-18T09:00:00+0000",
        cwd=str(cwd),
        log_dir=str(log_dir),
    )
    job["git_root"] = str(cwd)
    job["git_branch"] = "main"
    job = app_modules.transitions.on_submit(job, "42")
    app_modules.persistence.save_jobs([job])

    operations.mark_running(
        "job1", started_at="2026-04-18T09:00:00+0000", log_file=str(log_dir / "run.log")
    )
    base = Path(os.environ["CODEX_HOME"]) / "sessions" / "2026" / "04" / "18"
    _write_jsonl(
        base / "one.jsonl",
        {"type": "session_meta", "payload": {"id": "cand-1", "source": "exec", "cwd": str(cwd)}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Same prompt"}},
    )
    _write_jsonl(
        base / "two.jsonl",
        {"type": "session_meta", "payload": {"id": "cand-2", "source": "exec", "cwd": str(cwd)}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Same prompt"}},
    )

    updated = operations.mark_finished(
        "job1",
        finished_at="2026-04-18T09:05:00+0000",
        exit_code=0,
        log_file=str(log_dir / "run.log"),
    )

    assert updated["session_reconciliation"]["ledger_written"] is False
    assert "ambiguous" in updated["session_reconciliation"]["warning"]


def test_mark_finished_updates_ledger_for_resumed_session_without_new_artifact(
    app_modules, tmp_path
):
    operations = app_modules.operations
    cwd = tmp_path / "project"
    cwd.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Resume prompt\nbody", encoding="utf-8")
    log_dir = tmp_path / "logs" / "job1"
    log_dir.mkdir(parents=True)
    existing = Path(os.environ["CODEX_HOME"]) / "sessions" / "2026" / "04" / "18" / "existing.jsonl"
    _write_jsonl(
        existing,
        {
            "type": "session_meta",
            "payload": {"id": "existing-1", "source": "exec", "cwd": str(cwd)},
        },
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Resume prompt"}},
    )
    job = app_modules.transitions.make_job(
        job_id="job1",
        title="Resume prompt",
        agent="codex",
        session_mode="resume",
        session_id="existing-1",
        prompt_file=str(prompt),
        scheduled_for="2026-04-18T09:00:00+0000",
        cwd=str(cwd),
        log_dir=str(log_dir),
    )
    job["git_root"] = str(cwd)
    job["git_branch"] = "main"
    job = app_modules.transitions.on_submit(job, "42")
    app_modules.persistence.save_jobs([job])

    operations.mark_running(
        "job1", started_at="2026-04-18T09:00:00+0000", log_file=str(log_dir / "run.log")
    )
    updated = operations.mark_finished(
        "job1",
        finished_at="2026-04-18T09:05:00+0000",
        exit_code=0,
        log_file=str(log_dir / "run.log"),
    )

    assert updated["session_reconciliation"]["ledger_written"] is True
    assert updated["session_reconciliation"]["matched_session_id"] == "existing-1"


def test_delete_job_removes_prompt_and_log_dir(app_modules, tmp_path, monkeypatch):
    operations = app_modules.operations
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("hello", encoding="utf-8")
    log_dir = tmp_path / "logs" / "job1"
    log_dir.mkdir(parents=True)
    (log_dir / "run.log").write_text("output", encoding="utf-8")
    _write_job(app_modules, prompt_file=str(prompt_path), log_dir=str(log_dir))

    operations.delete_job("job1")
    assert app_modules.persistence.load_jobs() == []
    assert not prompt_path.exists()
    assert not log_dir.exists()
