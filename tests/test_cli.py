import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def cli_module(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    import schedule_agent.cli as cli
    cli = importlib.reload(cli)
    cli._ensure_dirs()
    return cli


# ---------------------------------------------------------------------------
# Path / dir helpers
# ---------------------------------------------------------------------------

def test_state_and_data_home_are_paths(cli_module):
    assert isinstance(cli_module._state_home(), Path)
    assert isinstance(cli_module._data_home(), Path)


def test_ensure_dirs_creates_expected_directories(cli_module):
    assert cli_module._state_home().exists()
    assert cli_module._state_home().is_dir()
    assert cli_module._data_home().exists()
    assert cli_module._data_home().is_dir()
    assert cli_module.PROMPT_DIR == cli_module._data_home() / "agent_prompts"
    assert cli_module.PROMPT_DIR.exists()
    assert cli_module.PROMPT_DIR.is_dir()


# ---------------------------------------------------------------------------
# load_jobs / save_jobs
# ---------------------------------------------------------------------------

def test_load_jobs_returns_empty_list_when_queue_missing(cli_module):
    cli_module.QUEUE_FILE.unlink(missing_ok=True)
    assert cli_module.load_jobs() == []


def test_save_and_load_jobs_round_trip_multiple_entries(cli_module):
    from schedule_agent.transitions import make_job
    jobs = [
        make_job("job1", "codex", "new", None, "/tmp/p1.md", "now + 5 minutes", "/tmp/project1", "/tmp/project1/log.txt"),
        make_job("job2", "claude", "resume", "abc123", "/tmp/p2.md", "03:00 tomorrow", "/tmp/project2", "/tmp/project2/log.txt"),
    ]
    cli_module.save_jobs(jobs)
    loaded = cli_module.load_jobs()
    assert len(loaded) == 2
    assert loaded[0]["id"] == "job1"
    assert loaded[1]["id"] == "job2"
    assert loaded[1]["session_mode"] == "resume"
    assert loaded[1]["session_id"] == "abc123"


def test_save_jobs_overwrites_previous_contents(cli_module):
    from schedule_agent.transitions import make_job
    first = [make_job("job1", "codex", "new", None, "/tmp/p1.md", "now", "/tmp", "/tmp/log1.txt")]
    second = [make_job("job2", "claude", "new", None, "/tmp/p2.md", "tomorrow", "/tmp", "/tmp/log2.txt")]
    cli_module.save_jobs(first)
    cli_module.save_jobs(second)
    loaded = cli_module.load_jobs()
    assert len(loaded) == 1
    assert loaded[0]["id"] == "job2"


# ---------------------------------------------------------------------------
# Legacy state helpers (still functional for backward compat)
# ---------------------------------------------------------------------------

def test_load_state_returns_empty_dict_when_state_missing(cli_module):
    cli_module.STATE_FILE.unlink(missing_ok=True)
    assert cli_module.load_state() == {}


def test_load_state_returns_empty_dict_for_invalid_json(cli_module):
    cli_module.STATE_FILE.write_text("{not valid json", encoding="utf-8")
    assert cli_module.load_state() == {}


def test_save_and_load_state_round_trip(cli_module):
    state = {"job1": {"status": "queued"}, "job2": {"status": "submitted", "at_job_id": "42"}}
    cli_module.save_state(state)
    assert cli_module.load_state() == state


# ---------------------------------------------------------------------------
# set_state / clear_state — update both legacy file and job record
# ---------------------------------------------------------------------------

def test_set_state_updates_legacy_file(cli_module):
    from schedule_agent.transitions import make_job
    job = make_job("job1", "claude", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt")
    cli_module.save_jobs([job])

    cli_module.set_state("job1", "queued", foo="bar")
    legacy = cli_module.load_state()
    assert legacy["job1"]["status"] == "queued"
    assert legacy["job1"]["foo"] == "bar"
    assert "updated_at" in legacy["job1"]


def test_set_state_updates_job_submission_field(cli_module):
    from schedule_agent.transitions import make_job, on_submit
    job = make_job("job1", "claude", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt")
    # Pre-submit so job has at_job_id and submission=scheduled
    job = on_submit(job, "42")
    cli_module.save_jobs([job])

    cli_module.set_state("job1", "submitted", at_job_id="99")
    loaded = cli_module.load_jobs()
    j = next(x for x in loaded if x["id"] == "job1")
    assert j["submission"] == "scheduled"
    assert j["at_job_id"] == "99"


def test_clear_state_removes_only_target_job(cli_module):
    cli_module.set_state("job1", "queued")
    cli_module.set_state("job2", "queued")
    cli_module.clear_state("job1")
    state = cli_module.load_state()
    assert "job1" not in state
    assert "job2" in state


# ---------------------------------------------------------------------------
# write_prompt_file
# ---------------------------------------------------------------------------

def test_write_prompt_file_writes_under_prompt_dir(cli_module):
    path = Path(cli_module.write_prompt_file("job1", "Test prompt"))
    assert path.exists()
    assert path.parent == cli_module.PROMPT_DIR
    assert path.name == "job1.md"
    assert path.read_text(encoding="utf-8") == "Test prompt"


# ---------------------------------------------------------------------------
# build_cmd (compat shim for build_agent_cmd)
# ---------------------------------------------------------------------------

def test_build_cmd_codex_new_session(cli_module):
    job = {"agent": "codex", "prompt_file": "/tmp/p.md", "session_mode": "new", "session_id": None, "cwd": "/tmp", "log": "/tmp/log.txt"}
    cmd = cli_module.build_cmd(job)
    assert "codex" in cmd
    assert "exec" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert '$(cat /tmp/p.md)' in cmd
    assert "< /dev/null" in cmd


def test_build_cmd_codex_resume_session(cli_module):
    job = {"agent": "codex", "prompt_file": "/tmp/p.md", "session_mode": "resume", "session_id": "sess-123", "cwd": "/tmp", "log": "/tmp/log.txt"}
    cmd = cli_module.build_cmd(job)
    assert "exec resume" in cmd
    assert "sess-123" in cmd


def test_build_cmd_claude_new_session(cli_module):
    job = {"agent": "claude", "prompt_file": "/tmp/p.md", "session_mode": "new", "session_id": None, "cwd": "/tmp", "log": "/tmp/log.txt"}
    cmd = cli_module.build_cmd(job)
    assert "claude" in cmd
    assert "-p" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert '$(cat /tmp/p.md)' in cmd
    assert "< /dev/null" in cmd


def test_build_cmd_claude_resume_session(cli_module):
    job = {"agent": "claude", "prompt_file": "/tmp/p.md", "session_mode": "resume", "session_id": "sess-999", "cwd": "/tmp", "log": "/tmp/log.txt"}
    cmd = cli_module.build_cmd(job)
    assert "--resume" in cmd
    assert "sess-999" in cmd


# ---------------------------------------------------------------------------
# parse_at_job_id
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("job 123 at Tue Apr 14 12:00:00 2026", "123"),
        ("warning: commands will be executed using /bin/sh\njob 7 at Fri Apr 10 23:26:00 2026", "7"),
        ("no job here", None),
        ("job abc at tomorrow", None),
    ],
)
def test_parse_at_job_id(stdout, expected, cli_module):
    assert cli_module.parse_at_job_id(stdout) == expected


# ---------------------------------------------------------------------------
# format_job_label — uses derive_display_state
# ---------------------------------------------------------------------------

def test_format_job_label_shows_scheduled_tag(cli_module):
    from schedule_agent.transitions import make_job, on_submit
    job = make_job("job1", "claude", "resume", "sess-1", "/tmp/p.md", "03:00 tomorrow", "/tmp", "/tmp/log.txt")
    job_sched = on_submit(job, "99")

    queued_label = cli_module.format_job_label(job)
    sched_label = cli_module.format_job_label(job_sched)

    # New format shows full display state word, not single-letter abbreviation
    assert "scheduled" in sched_label
    assert "(S)" not in sched_label
    assert "(S)" not in queued_label
    assert "claude" in sched_label
    assert "resume" in sched_label


def test_format_job_label_session_new(cli_module):
    from schedule_agent.transitions import make_job
    job = make_job("job1", "codex", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt")
    label = cli_module.format_job_label(job)
    assert "new" in label


# ---------------------------------------------------------------------------
# _format_job_row
# ---------------------------------------------------------------------------

def test_format_job_row_normal(cli_module):
    from schedule_agent.transitions import make_job, on_submit
    job = on_submit(
        make_job("job1", "claude", "resume", "sess-1", "/tmp/p.md", "03:00 tomorrow", "/tmp", "/tmp/log.txt"),
        "99",
    )
    row = cli_module._format_job_row(job, id_width=4)
    # All four columns present
    assert "job1" in row
    assert "scheduled" in row
    assert "claude" in row
    assert "resume" in row
    # No invalid marker for valid job
    assert "[!]" not in row


def test_format_job_row_session_new(cli_module):
    from schedule_agent.transitions import make_job
    job = make_job("job2", "codex", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt")
    row = cli_module._format_job_row(job, id_width=4)
    assert "new" in row
    assert "codex" in row


def test_format_job_row_session_resume(cli_module):
    from schedule_agent.transitions import make_job
    job = make_job("job1", "claude", "resume", "sess-1", "/tmp/p.md", "now", "/tmp", "/tmp/log.txt")
    row = cli_module._format_job_row(job, id_width=4)
    assert "resume" in row


def test_format_job_row_with_depends_on(cli_module):
    from schedule_agent.transitions import make_job
    job = make_job("job2", "codex", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt",
                   depends_on="job1")
    row = cli_module._format_job_row(job, id_width=4)
    assert "depends: job1" in row


def test_format_job_row_depends_on_truncated(cli_module):
    from schedule_agent.transitions import make_job
    long_dep = "a" * 50
    job = make_job("job2", "codex", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt",
                   depends_on=long_dep)
    row = cli_module._format_job_row(job, id_width=4)
    # dep_id truncated to 40 chars with ellipsis
    assert "depends: " + "a" * 40 + "…" in row
    assert long_dep not in row


def test_format_job_row_missing_prompt_file(cli_module):
    from schedule_agent.transitions import make_job
    job = make_job("job1", "claude", "new", None, "/nonexistent/path/p.md", "now", "/tmp", "/tmp/log.txt")
    row = cli_module._format_job_row(job, id_width=4)
    assert "[prompt missing]" in row


def test_format_job_row_existing_prompt_file(cli_module, tmp_path):
    from schedule_agent.transitions import make_job
    pf = tmp_path / "p.md"
    pf.write_text("hello")
    job = make_job("job1", "claude", "new", None, str(pf), "now", "/tmp", "/tmp/log.txt")
    row = cli_module._format_job_row(job, id_width=4)
    assert "[prompt missing]" not in row


def test_format_job_row_invalid_job(cli_module):
    invalid_job = {"id": "broken-job", "_invalid": True}
    row = cli_module._format_job_row(invalid_job, id_width=4)
    assert "broken-job" in row
    assert "[!]" in row
    # Should not have normal columns
    assert "scheduled" not in row


def test_format_job_row_id_width_applied(cli_module):
    from schedule_agent.transitions import make_job, on_submit
    job = on_submit(
        make_job("j1", "claude", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt"),
        "1",
    )
    row = cli_module._format_job_row(job, id_width=20)
    # id column should be padded to width 20
    assert row.startswith("j1" + " " * 18)


# ---------------------------------------------------------------------------
# list_jobs_noninteractive
# ---------------------------------------------------------------------------

def test_list_jobs_noninteractive_empty(cli_module):
    assert cli_module.list_jobs_noninteractive() == "No jobs."


def test_list_jobs_noninteractive_outputs_all_jobs(cli_module):
    from schedule_agent.transitions import make_job, on_submit
    jobs = [
        make_job("job1", "codex", "new", None, "/tmp/p1.md", "now + 5 minutes", "/tmp", "/tmp/log1.txt"),
        on_submit(make_job("job2", "claude", "new", None, "/tmp/p2.md", "03:00 tomorrow", "/tmp", "/tmp/log2.txt"), "55"),
    ]
    cli_module.save_jobs(jobs)
    output = cli_module.list_jobs_noninteractive()
    assert "job1" in output
    assert "job2" in output
    assert "codex" in output
    assert "claude" in output


def test_list_jobs_noninteractive_id_width_matches_longest(cli_module):
    from schedule_agent.transitions import make_job
    jobs = [
        make_job("short", "codex", "new", None, "/tmp/p1.md", "now", "/tmp", "/tmp/log1.txt"),
        make_job("a-much-longer-job-id", "claude", "new", None, "/tmp/p2.md", "now", "/tmp", "/tmp/log2.txt"),
    ]
    cli_module.save_jobs(jobs)
    output = cli_module.list_jobs_noninteractive()
    lines = output.splitlines()
    assert len(lines) == 2
    # Both lines' id columns should be padded to longest id length (20)
    longest = len("a-much-longer-job-id")
    for line in lines:
        # The id portion at the start should be at least longest chars before a space
        assert line[:longest].rstrip() in ("short", "a-much-longer-job-id")


# ---------------------------------------------------------------------------
# get_job_and_index
# ---------------------------------------------------------------------------

def test_get_job_and_index_found(cli_module):
    from schedule_agent.transitions import make_job
    job = make_job("job1", "codex", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt")
    cli_module.save_jobs([job])
    loaded_jobs, idx, found = cli_module.get_job_and_index("job1")
    assert idx == 0
    assert found["id"] == "job1"


def test_get_job_and_index_missing(cli_module):
    from schedule_agent.transitions import make_job
    job = make_job("job1", "codex", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt")
    cli_module.save_jobs([job])
    loaded_jobs, idx, found = cli_module.get_job_and_index("missing")
    assert idx is None
    assert found is None


# ---------------------------------------------------------------------------
# apply_job_update
# ---------------------------------------------------------------------------

def test_apply_job_update_handles_missing_job(cli_module, capsys):
    ret = cli_module.apply_job_update("nope", lambda d: d, interactive=False)
    assert "No such job" in capsys.readouterr().out
    assert ret == 1


def test_apply_job_update_removes_job_and_prompt_file(tmp_path, cli_module, capsys):
    from schedule_agent.transitions import make_job
    pf = tmp_path / "prompt.md"
    pf.write_text("prompt")
    job = make_job("job1", "codex", "new", None, str(pf), "now", "/tmp", "/tmp/log.txt")
    cli_module.save_jobs([job])
    ret = cli_module.apply_job_update("job1", lambda d: None, interactive=False)
    assert ret == 0
    assert not pf.exists()
    assert cli_module.load_jobs() == []


def test_apply_job_update_handles_schedule_failure(monkeypatch, cli_module, capsys):
    from schedule_agent.transitions import make_job, on_submit
    job = on_submit(make_job("job1", "codex", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt"), "12")
    cli_module.save_jobs([job])
    monkeypatch.setattr(cli_module, "schedule", lambda job, dry_run=False: (_ for _ in ()).throw(RuntimeError("fail")))
    monkeypatch.setattr(cli_module, "cancel_at_job", lambda jid: True)
    ret = cli_module.apply_job_update("job1", lambda d: d, interactive=False)
    assert ret == 1
    # Job should remain queued after resubmit failure
    loaded = cli_module.load_jobs()
    j = next(x for x in loaded if x["id"] == "job1")
    assert j["submission"] == "queued"


# ---------------------------------------------------------------------------
# remove_job
# ---------------------------------------------------------------------------

def test_remove_job_cancel(monkeypatch, cli_module):
    monkeypatch.setattr(cli_module, "confirm", lambda msg, default=False: False)
    monkeypatch.setattr(cli_module, "info", lambda msg: None)
    ret = cli_module.remove_job("job1", interactive=True)
    assert ret == 1


def test_remove_job_noninteractive_removes(cli_module):
    from schedule_agent.transitions import make_job
    job = make_job("job1", "codex", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt")
    cli_module.save_jobs([job])
    ret = cli_module.remove_job("job1", interactive=False)
    assert ret == 0
    assert cli_module.load_jobs() == []


# ---------------------------------------------------------------------------
# cancel_at_job
# ---------------------------------------------------------------------------

def test_cancel_at_job_handles_missing(cli_module):
    from schedule_agent.transitions import make_job
    job = make_job("job1", "codex", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt")
    cli_module.save_jobs([job])
    assert cli_module.cancel_at_job("job1") is False


def test_cancel_at_job_with_at_job_id_calls_atrm(monkeypatch, cli_module):
    from schedule_agent.transitions import make_job, on_submit
    job = on_submit(make_job("job1", "codex", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt"), "42")
    cli_module.save_jobs([job])

    called = []
    monkeypatch.setattr(
        "schedule_agent.scheduler_backend.subprocess.run",
        lambda cmd, **kw: called.append(cmd) or type("P", (), {"returncode": 0, "stderr": ""})()
    )

    result = cli_module.cancel_at_job("job1")
    assert result is True
    assert any("42" in str(c) for c in called)


def test_cancel_at_job_failure_records_error(monkeypatch, cli_module):
    from schedule_agent.transitions import make_job, on_submit
    job = on_submit(make_job("job1", "codex", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt"), "77")
    cli_module.save_jobs([job])

    monkeypatch.setattr(
        "schedule_agent.scheduler_backend.subprocess.run",
        lambda *a, **k: type("P", (), {"returncode": 1, "stderr": "err", "stdout": ""})()
    )

    assert cli_module.cancel_at_job("job1") is False
    state = cli_module.load_state()
    assert state["job1"]["at_job_remove_error"] == "err"
    assert "at_job_id" not in state["job1"]


# ---------------------------------------------------------------------------
# create_and_maybe_submit
# ---------------------------------------------------------------------------

def test_create_and_maybe_submit_dry_run(monkeypatch, cli_module):
    from schedule_agent.transitions import make_job
    fake_job = make_job("job1", "codex", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt")
    monkeypatch.setattr(cli_module, "create_job", lambda: fake_job)
    monkeypatch.setattr(cli_module, "schedule", lambda job, dry_run=False: "DRYRUN" if dry_run else "RUN")
    monkeypatch.setattr(cli_module, "info", lambda msg: None)
    ret = cli_module.create_and_maybe_submit(dry_run=True)
    assert ret == 0


def test_create_and_maybe_submit_decline_submit(monkeypatch, cli_module):
    from schedule_agent.transitions import make_job
    fake_job = make_job("job1", "codex", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt")
    monkeypatch.setattr(cli_module, "create_job", lambda: fake_job)
    monkeypatch.setattr(cli_module, "confirm", lambda msg, default=True: False)
    monkeypatch.setattr(cli_module, "info", lambda msg: None)
    ret = cli_module.create_and_maybe_submit(dry_run=False)
    assert ret == 0


def test_create_and_maybe_submit_schedule_failure(monkeypatch, cli_module):
    from schedule_agent.transitions import make_job
    fake_job = make_job("job1", "codex", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt")
    monkeypatch.setattr(cli_module, "create_job", lambda: fake_job)
    monkeypatch.setattr(cli_module, "confirm", lambda msg, default=True: True)
    monkeypatch.setattr(cli_module, "schedule", lambda job, dry_run=False: (_ for _ in ()).throw(RuntimeError("fail")))
    monkeypatch.setattr(cli_module, "info", lambda msg: None)
    ret = cli_module.create_and_maybe_submit(dry_run=False)
    assert ret == 1


# ---------------------------------------------------------------------------
# read_prompt
# ---------------------------------------------------------------------------

def test_read_prompt_empty(monkeypatch, cli_module, tmp_path):
    def fake_editor():
        return ["true"]
    monkeypatch.setattr(cli_module, "_resolve_editor", fake_editor)

    def fake_run(cmd, check):
        Path(cmd[-1]).write_text("")
    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        cli_module.tempfile,
        "NamedTemporaryFile",
        lambda **kwargs: type("F", (), {
            "__enter__": lambda s: type("O", (), {"name": str(tmp_path / "p.md")})(),
            "__exit__": lambda s, a, b, c: None,
        })()
    )
    with pytest.raises(KeyboardInterrupt):
        cli_module.read_prompt()


# ---------------------------------------------------------------------------
# Automated transitions via CLI (mark-running, mark-done, mark-failed, retry)
# ---------------------------------------------------------------------------

def test_cli_mark_running(cli_module):
    from schedule_agent.transitions import make_job, on_submit
    job = on_submit(make_job("job1", "codex", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt"), "1")
    cli_module.save_jobs([job])
    ret = cli_module.cli_mark_running("job1")
    assert ret == 0
    loaded = cli_module.load_jobs()
    j = next(x for x in loaded if x["id"] == "job1")
    assert j["submission"] == "running"
    assert j["execution"] == "running"


def test_cli_mark_done(cli_module):
    from schedule_agent.transitions import make_job, on_start, on_submit
    job = on_start(on_submit(make_job("job1", "codex", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt"), "1"))
    cli_module.save_jobs([job])
    ret = cli_module.cli_mark_done("job1")
    assert ret == 0
    loaded = cli_module.load_jobs()
    j = next(x for x in loaded if x["id"] == "job1")
    assert j["execution"] == "success"


def test_cli_mark_failed(cli_module):
    from schedule_agent.transitions import make_job, on_start, on_submit
    job = on_start(on_submit(make_job("job1", "codex", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt"), "1"))
    cli_module.save_jobs([job])
    ret = cli_module.cli_mark_failed("job1")
    assert ret == 0
    loaded = cli_module.load_jobs()
    j = next(x for x in loaded if x["id"] == "job1")
    assert j["execution"] == "failed"


def test_cli_retry_job(cli_module):
    from schedule_agent.transitions import make_job, on_failure, on_start, on_submit
    job = on_failure(on_start(on_submit(
        make_job("job1", "codex", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt"), "1"
    )))
    cli_module.save_jobs([job])
    ret = cli_module.cli_retry_job("job1")
    assert ret == 0
    loaded = cli_module.load_jobs()
    j = next(x for x in loaded if x["id"] == "job1")
    assert j["execution"] == "pending"
    assert j["submission"] == "queued"


# ---------------------------------------------------------------------------
# Dependency transitions via CLI
# ---------------------------------------------------------------------------

def test_cli_notify_dependency_success(cli_module):
    from schedule_agent.transitions import make_job
    job = make_job("child", "codex", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt", depends_on="parent")
    cli_module.save_jobs([job])
    ret = cli_module.cli_notify_dependency("child", "success")
    assert ret == 0
    loaded = cli_module.load_jobs()
    j = next(x for x in loaded if x["id"] == "child")
    assert j["readiness"] == "ready"


def test_cli_notify_dependency_failure(cli_module):
    from schedule_agent.transitions import make_job
    job = make_job("child", "codex", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt", depends_on="parent")
    cli_module.save_jobs([job])
    ret = cli_module.cli_notify_dependency("child", "failed")
    assert ret == 0
    loaded = cli_module.load_jobs()
    j = next(x for x in loaded if x["id"] == "child")
    assert j["readiness"] == "blocked"


# ---------------------------------------------------------------------------
# show_job_text
# ---------------------------------------------------------------------------

def test_show_job_text_contains_all_new_fields(cli_module, tmp_path):
    from schedule_agent.transitions import make_job
    prompt = tmp_path / "p.md"
    prompt.write_text("hello")
    job = make_job("job1", "claude", "new", None, str(prompt), "03:00 tomorrow", "/tmp", "/tmp/log.txt")
    text = cli_module.show_job_text(job)
    assert "id:" in text
    assert "display:" in text
    assert "submission:" in text
    assert "execution:" in text
    assert "readiness:" in text
    assert "session:" in text
    assert "agent:" in text
    assert "when:" in text
    assert "at_job_id:" in text
    assert "depends_on:" in text
    assert "session_id:" in text
    assert "cwd:" in text
    assert "log:" in text
    assert "prompt_file:" in text
    assert "prompt_exists:" in text
    assert "created_at:" in text
    assert "updated_at:" in text
    assert "last_run_at:" in text


def test_show_job_text_at_job_id_shows_dash_when_none(cli_module, tmp_path):
    from schedule_agent.transitions import make_job
    prompt = tmp_path / "p.md"
    prompt.write_text("hello")
    job = make_job("job1", "claude", "new", None, str(prompt), "now", "/tmp", "/tmp/log.txt")
    assert job["at_job_id"] is None
    text = cli_module.show_job_text(job)
    assert "at_job_id:     -" in text


def test_show_job_text_depends_on_shows_dash_when_absent(cli_module, tmp_path):
    from schedule_agent.transitions import make_job
    prompt = tmp_path / "p.md"
    prompt.write_text("hello")
    job = make_job("job1", "claude", "new", None, str(prompt), "now", "/tmp", "/tmp/log.txt")
    assert "depends_on" not in job
    text = cli_module.show_job_text(job)
    assert "depends_on:    -" in text


def test_show_job_text_session_id_shows_dash_for_new_mode(cli_module, tmp_path):
    from schedule_agent.transitions import make_job
    prompt = tmp_path / "p.md"
    prompt.write_text("hello")
    job = make_job("job1", "claude", "new", None, str(prompt), "now", "/tmp", "/tmp/log.txt")
    assert job["session_mode"] == "new"
    text = cli_module.show_job_text(job)
    assert "session_id:    -" in text


def test_show_job_text_session_id_truncated_to_40_chars(cli_module, tmp_path):
    from schedule_agent.transitions import make_job
    prompt = tmp_path / "p.md"
    prompt.write_text("hello")
    long_id = "a" * 60
    job = make_job("job1", "claude", "resume", long_id, str(prompt), "now", "/tmp", "/tmp/log.txt")
    text = cli_module.show_job_text(job)
    assert "a" * 40 in text
    assert "a" * 41 not in text


def test_show_job_text_prompt_exists_yes_when_file_exists(cli_module, tmp_path):
    from schedule_agent.transitions import make_job
    prompt = tmp_path / "p.md"
    prompt.write_text("hello")
    job = make_job("job1", "claude", "new", None, str(prompt), "now", "/tmp", "/tmp/log.txt")
    text = cli_module.show_job_text(job)
    assert "prompt_exists: yes" in text


def test_show_job_text_prompt_exists_no_when_file_missing(cli_module, tmp_path):
    from schedule_agent.transitions import make_job
    prompt = tmp_path / "missing.md"
    job = make_job("job1", "claude", "new", None, str(prompt), "now", "/tmp", "/tmp/log.txt")
    text = cli_module.show_job_text(job)
    assert "prompt_exists: no (file not found)" in text


def test_show_job_text_display_line_and_indented_dimensions(cli_module, tmp_path):
    from schedule_agent.transitions import make_job
    from schedule_agent.state_model import derive_display_state
    prompt = tmp_path / "p.md"
    prompt.write_text("hello")
    job = make_job("job1", "claude", "new", None, str(prompt), "now", "/tmp", "/tmp/log.txt")
    expected_display = derive_display_state(job)
    text = cli_module.show_job_text(job)
    assert f"display:       {expected_display}" in text
    assert "  submission:" in text
    assert "  execution:" in text
    assert "  readiness:" in text
    assert "  session:" in text


def test_show_job_text_depends_on_shows_condition_when_set(cli_module, tmp_path):
    from schedule_agent.transitions import make_job
    prompt = tmp_path / "p.md"
    prompt.write_text("hello")
    job = make_job("job1", "claude", "new", None, str(prompt), "now", "/tmp", "/tmp/log.txt",
                   depends_on="job0", dependency_condition="success")
    text = cli_module.show_job_text(job)
    assert "job0 (condition: success)" in text


def test_show_job_text_last_run_at_shows_dash_when_none(cli_module, tmp_path):
    from schedule_agent.transitions import make_job
    prompt = tmp_path / "p.md"
    prompt.write_text("hello")
    job = make_job("job1", "claude", "new", None, str(prompt), "now", "/tmp", "/tmp/log.txt")
    assert job["last_run_at"] is None
    text = cli_module.show_job_text(job)
    assert "last_run_at:   -" in text
