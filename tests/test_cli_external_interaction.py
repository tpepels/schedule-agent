import importlib
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


def make_job(
    job_id="job1",
    agent="claude",
    when="03:00 tomorrow",
    cwd="/tmp/project",
    log="/tmp/project/log.txt",
    prompt_file="/tmp/project/prompt.md",
    session=None,
):
    job = {
        "id": job_id,
        "agent": agent,
        "when": when,
        "cwd": cwd,
        "log": log,
        "prompt_file": prompt_file,
    }
    if session is not None:
        job["session"] = session
    return job


class DummyProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_build_cmd_codex_new_session_contains_expected_flags_and_stdin_detach(cli_module):
    job = make_job(agent="codex", prompt_file="/tmp/prompt.md")

    cmd = cli_module.build_cmd(job)

    assert "codex" in cmd
    assert "exec" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert '$(cat /tmp/prompt.md)' in cmd
    assert "< /dev/null" in cmd
    assert "resume" not in cmd


def test_build_cmd_codex_resume_session_contains_resume(cli_module):
    job = make_job(agent="codex", prompt_file="/tmp/prompt.md", session="sess-123")

    cmd = cli_module.build_cmd(job)

    assert "codex" in cmd
    assert "exec resume" in cmd
    assert "sess-123" in cmd
    assert '$(cat /tmp/prompt.md)' in cmd
    assert "< /dev/null" in cmd


def test_build_cmd_claude_new_session_contains_expected_flags_and_stdin_detach(cli_module):
    job = make_job(agent="claude", prompt_file="/tmp/prompt.md")

    cmd = cli_module.build_cmd(job)

    assert "claude" in cmd
    assert "-p" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert '$(cat /tmp/prompt.md)' in cmd
    assert "< /dev/null" in cmd
    assert "--resume" not in cmd


def test_build_cmd_claude_resume_session_contains_resume(cli_module):
    job = make_job(agent="claude", prompt_file="/tmp/prompt.md", session="sess-999")

    cmd = cli_module.build_cmd(job)

    assert "claude" in cmd
    assert "--resume" in cmd
    assert "sess-999" in cmd
    assert "-p" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert '$(cat /tmp/prompt.md)' in cmd
    assert "< /dev/null" in cmd


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("job 12 at Tue Apr 14 12:00:00 2026", "12"),
        ("warning: commands will be executed using /bin/sh\njob 7 at Fri Apr 10 23:26:00 2026", "7"),
        ("job 999 at Mon Jan  1 00:00:00 2027\n", "999"),
        ("no job here", None),
        ("job abc at tomorrow", None),
        ("", None),
    ],
)
def test_parse_at_job_id_handles_supported_and_unsupported_outputs(cli_module, stdout, expected):
    assert cli_module.parse_at_job_id(stdout) == expected


def test_schedule_dry_run_returns_script_and_does_not_touch_state_or_subprocess(cli_module, monkeypatch):
    job = make_job()

    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("subprocess.run should not be called in dry-run mode")

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    output = cli_module.schedule(job, dry_run=True)

    assert "Would schedule at: 03:00 tomorrow" in output
    assert "cd /tmp/project" in output
    assert "export PATH=/usr/local/bin:/usr/bin:/bin" in output
    assert "< /dev/null" in output
    assert calls == []
    assert cli_module.load_state() == {}


def test_schedule_calls_at_with_expected_script_and_persists_submitted_state(cli_module, monkeypatch):
    job = make_job(agent="claude", prompt_file="/tmp/prompt.md", job_id="job-claude")

    captured = {}

    def fake_run(cmd, input=None, text=None, capture_output=None):
        captured["cmd"] = cmd
        captured["input"] = input
        captured["text"] = text
        captured["capture_output"] = capture_output
        return DummyProc(returncode=0, stdout="job 42 at Tue Apr 14 12:00:00 2026\n", stderr="")

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    out = cli_module.schedule(job)

    assert captured["cmd"] == ["at", "03:00 tomorrow"]
    assert captured["text"] is True
    assert captured["capture_output"] is True

    script = captured["input"]
    assert "cd /tmp/project" in script
    assert "export PATH=/usr/local/bin:/usr/bin:/bin" in script
    assert "claude -p --dangerously-skip-permissions" in script
    assert '$(cat /tmp/prompt.md)' in script
    assert "< /dev/null" in script
    assert ">> /tmp/project/log.txt 2>&1" in script

    assert "job 42 at" in out

    state = cli_module.load_state()
    assert state["job-claude"]["status"] == "submitted"
    assert state["job-claude"]["scheduled_for"] == "03:00 tomorrow"
    assert state["job-claude"]["at_job_id"] == "42"
    assert state["job-claude"]["agent"] == "claude"


def test_schedule_raises_on_at_failure_and_does_not_write_submitted_state(cli_module, monkeypatch):
    job = make_job(job_id="job-fail")

    def fake_run(cmd, input=None, text=None, capture_output=None):
        return DummyProc(returncode=1, stdout="", stderr="Garbled time")

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Garbled time"):
        cli_module.schedule(job)

    assert cli_module.load_state() == {}


def test_cancel_at_job_returns_false_when_no_at_job_id_is_recorded(cli_module):
    cli_module.set_state("job1", "submitted")
    assert cli_module.cancel_at_job("job1") is False

    state = cli_module.load_state()
    assert state["job1"]["status"] == "submitted"
    assert "at_job_id" not in state["job1"]


def test_cancel_at_job_success_updates_state_and_drops_at_job_id(cli_module, monkeypatch):
    cli_module.set_state("job1", "submitted", at_job_id="55")

    captured = {}

    def fake_run(cmd, capture_output=None, text=None):
        captured["cmd"] = cmd
        return DummyProc(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    assert cli_module.cancel_at_job("job1") is True
    assert captured["cmd"] == ["atrm", "55"]

    state = cli_module.load_state()
    assert state["job1"]["at_job_removed"] is True
    assert "at_job_id" not in state["job1"]
    assert "at_job_remove_attempted_at" in state["job1"]


def test_cancel_at_job_failure_records_error_and_drops_at_job_id(cli_module, monkeypatch):
    cli_module.set_state("job1", "submitted", at_job_id="77")

    def fake_run(cmd, capture_output=None, text=None):
        return DummyProc(returncode=1, stdout="", stderr="Cannot find jobid 77")

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    assert cli_module.cancel_at_job("job1") is False

    state = cli_module.load_state()
    assert state["job1"]["at_job_removed"] is False
    assert state["job1"]["at_job_remove_error"] == "Cannot find jobid 77"
    assert "at_job_id" not in state["job1"]


def test_discover_sessions_returns_most_recent_ten_sorted_by_mtime_desc(cli_module, tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    sessions_root = fake_home / ".codex" / "sessions"
    sessions_root.mkdir(parents=True)

    monkeypatch.setattr(cli_module.Path, "home", lambda: fake_home)

    created = []
    for i in range(12):
        p = sessions_root / f"session-{i}.jsonl"
        p.write_text(f"session {i}", encoding="utf-8")
        created.append(p)

    # Make later-numbered files newer.
    for i, p in enumerate(created):
        ts = 1_700_000_000 + i
        os_utime = __import__("os").utime
        os_utime(p, (ts, ts))

    discovered = cli_module.discover_sessions("codex")

    assert len(discovered) == 10
    names = [p.name for p in discovered]
    assert names[0] == "session-11.jsonl"
    assert names[-1] == "session-2.jsonl"


def test_discover_sessions_returns_empty_when_root_missing(cli_module, tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(cli_module.Path, "home", lambda: fake_home)

    assert cli_module.discover_sessions("codex") == []
    assert cli_module.discover_sessions("claude") == []


def test_create_job_writes_prompt_file_and_uses_interaction_results(cli_module, monkeypatch, tmp_path):
    monkeypatch.setattr(cli_module, "choose", lambda msg, choices, default=None: "Codex" if msg == "Agent" else choices[0])
    monkeypatch.setattr(cli_module, "choose_session", lambda agent: "sess-123")
    monkeypatch.setattr(cli_module, "read_prompt", lambda: "hello prompt")
    monkeypatch.setattr(cli_module, "resolve_time", lambda: "now + 15 minutes")
    monkeypatch.setattr(cli_module, "info", lambda msg: None)
    monkeypatch.setattr(cli_module.Path, "cwd", lambda: tmp_path)

    job = cli_module.create_job()

    assert job["agent"] == "codex"
    assert job["session"] == "sess-123"
    assert job["when"] == "now + 15 minutes"
    assert job["cwd"] == str(tmp_path)
    assert Path(job["prompt_file"]).exists()
    assert Path(job["prompt_file"]).read_text(encoding="utf-8") == "hello prompt"


def test_prepare_mutation_cancels_only_submitted_jobs(cli_module, monkeypatch):
    cli_module.set_state("submitted-job", "submitted", at_job_id="12")
    cli_module.set_state("queued-job", "queued")

    called = []

    def fake_cancel(job_id):
        called.append(job_id)
        return True

    monkeypatch.setattr(cli_module, "cancel_at_job", fake_cancel)

    assert cli_module.prepare_mutation("submitted-job") is True
    assert cli_module.prepare_mutation("queued-job") is False
    assert called == ["submitted-job"]


def test_apply_job_update_updates_queued_job_without_resubmitting(cli_module, monkeypatch):
    prompt_path = Path(cli_module.write_prompt_file("job1", "prompt"))
    job = make_job(job_id="job1", when="now + 5 minutes", prompt_file=str(prompt_path))
    cli_module.save_jobs([job])
    cli_module.set_state("job1", "queued", log=job["log"], cwd=job["cwd"], agent=job["agent"])

    scheduled = []

    def fake_schedule(updated, dry_run=False):
        scheduled.append(updated)
        return "scheduled"

    monkeypatch.setattr(cli_module, "schedule", fake_schedule)

    rc = cli_module.apply_job_update(
        "job1",
        lambda d: {**d, "when": "03:00 tomorrow"},
        success_message="updated",
        interactive=False,
    )

    assert rc == 0
    assert scheduled == []

    jobs = cli_module.load_jobs()
    assert jobs[0]["when"] == "03:00 tomorrow"

    state = cli_module.load_state()
    assert state["job1"]["status"] == "queued"
    assert state["job1"]["scheduled_for"] == "03:00 tomorrow"


def test_apply_job_update_resubmits_previously_submitted_job(cli_module, monkeypatch):
    prompt_path = Path(cli_module.write_prompt_file("job1", "prompt"))
    job = make_job(job_id="job1", when="now + 5 minutes", prompt_file=str(prompt_path))
    cli_module.save_jobs([job])
    cli_module.set_state("job1", "submitted", at_job_id="12", log=job["log"], cwd=job["cwd"], agent=job["agent"])

    cancelled = []
    scheduled = []

    def fake_cancel(job_id):
        cancelled.append(job_id)
        return True

    def fake_schedule(updated, dry_run=False):
        scheduled.append(updated.copy())
        cli_module.set_state(
            updated["id"],
            "submitted",
            at_job_id="99",
            scheduled_for=updated["when"],
            log=updated["log"],
            cwd=updated["cwd"],
            agent=updated["agent"],
        )
        return "job 99 at Tue Apr 14 12:00:00 2026"

    monkeypatch.setattr(cli_module, "cancel_at_job", fake_cancel)
    monkeypatch.setattr(cli_module, "schedule", fake_schedule)

    rc = cli_module.apply_job_update(
        "job1",
        lambda d: {**d, "when": "03:00 tomorrow"},
        success_message="updated",
        interactive=False,
    )

    assert rc == 0
    assert cancelled == ["job1"]
    assert len(scheduled) == 1
    assert scheduled[0]["when"] == "03:00 tomorrow"

    state = cli_module.load_state()
    assert state["job1"]["status"] == "submitted"
    assert state["job1"]["at_job_id"] == "99"


def test_apply_job_update_falls_back_to_queued_when_resubmit_fails(cli_module, monkeypatch, capsys):
    prompt_path = Path(cli_module.write_prompt_file("job1", "prompt"))
    job = make_job(job_id="job1", when="now + 5 minutes", prompt_file=str(prompt_path))
    cli_module.save_jobs([job])
    cli_module.set_state("job1", "submitted", at_job_id="12", log=job["log"], cwd=job["cwd"], agent=job["agent"])

    monkeypatch.setattr(cli_module, "cancel_at_job", lambda job_id: True)

    def fake_schedule(updated, dry_run=False):
        raise RuntimeError("Garbled time")

    monkeypatch.setattr(cli_module, "schedule", fake_schedule)

    rc = cli_module.apply_job_update(
        "job1",
        lambda d: {**d, "when": "03:00 tomorrow"},
        success_message="updated",
        interactive=False,
    )

    assert rc == 1

    state = cli_module.load_state()
    assert state["job1"]["status"] == "queued"
    assert state["job1"]["scheduled_for"] == "03:00 tomorrow"

    out = capsys.readouterr().out
    assert "Garbled time" in out
    assert "remains queued" in out


def test_apply_job_update_delete_removes_job_state_and_prompt_file(cli_module):
    prompt_path = Path(cli_module.write_prompt_file("job1", "prompt"))
    job = make_job(job_id="job1", prompt_file=str(prompt_path))
    cli_module.save_jobs([job])
    cli_module.set_state("job1", "queued")

    rc = cli_module.apply_job_update(
        "job1",
        lambda d: None,
        success_message="Deleted.",
        interactive=False,
    )

    assert rc == 0
    assert cli_module.load_jobs() == []
    assert cli_module.load_state() == {}
    assert not prompt_path.exists()


def test_cli_show_job_prints_details_and_returns_zero(cli_module, capsys):
    prompt_path = Path(cli_module.write_prompt_file("job1", "prompt"))
    job = make_job(job_id="job1", prompt_file=str(prompt_path))
    cli_module.save_jobs([job])

    rc = cli_module.cli_show_job("job1")

    assert rc == 0
    out = capsys.readouterr().out
    assert "id:         job1" in out
    assert f"promptfile: {prompt_path}" in out


def test_cli_show_job_returns_one_for_missing_job(cli_module, capsys):
    rc = cli_module.cli_show_job("missing")
    assert rc == 1
    assert "No such job." in capsys.readouterr().out


def test_cli_reschedule_job_updates_time(cli_module, capsys):
    prompt_path = Path(cli_module.write_prompt_file("job1", "prompt"))
    job = make_job(job_id="job1", when="now + 5 minutes", prompt_file=str(prompt_path))
    cli_module.save_jobs([job])
    cli_module.set_state("job1", "queued")

    rc = cli_module.cli_reschedule_job("job1", "03:00 tomorrow")

    assert rc == 0
    assert cli_module.load_jobs()[0]["when"] == "03:00 tomorrow"
    assert "Rescheduled job1 from now + 5 minutes to 03:00 tomorrow." in capsys.readouterr().out


def test_cli_change_session_sets_specific_session(cli_module, capsys):
    prompt_path = Path(cli_module.write_prompt_file("job1", "prompt"))
    job = make_job(job_id="job1", prompt_file=str(prompt_path))
    cli_module.save_jobs([job])
    cli_module.set_state("job1", "queued")

    rc = cli_module.cli_change_session("job1", "sess-abc")

    assert rc == 0
    assert cli_module.load_jobs()[0]["session"] == "sess-abc"
    assert "Changed session for job1 to sess-abc." in capsys.readouterr().out


def test_cli_change_session_can_clear_to_new(cli_module, capsys):
    prompt_path = Path(cli_module.write_prompt_file("job1", "prompt"))
    job = make_job(job_id="job1", prompt_file=str(prompt_path), session="old-sess")
    cli_module.save_jobs([job])
    cli_module.set_state("job1", "queued")

    rc = cli_module.cli_change_session("job1", None)

    assert rc == 0
    assert cli_module.load_jobs()[0]["session"] is None
    assert "Changed session for job1 to new." in capsys.readouterr().out


def test_remove_job_noninteractive_deletes_without_prompt(cli_module, capsys):
    prompt_path = Path(cli_module.write_prompt_file("job1", "prompt"))
    job = make_job(job_id="job1", prompt_file=str(prompt_path))
    cli_module.save_jobs([job])
    cli_module.set_state("job1", "queued")

    rc = cli_module.remove_job("job1", interactive=False)

    assert rc == 0
    assert cli_module.load_jobs() == []
    assert cli_module.load_state() == {}
    assert not prompt_path.exists()
    assert "Deleted." in capsys.readouterr().out


def test_main_list_subcommand_prints_jobs(cli_module, capsys):
    prompt_path = Path(cli_module.write_prompt_file("job1", "prompt"))
    job = make_job(job_id="job1", prompt_file=str(prompt_path))
    cli_module.save_jobs([job])

    rc = cli_module.main(["list"])

    assert rc == 0
    assert "job1" in capsys.readouterr().out


def test_main_show_subcommand_dispatches(cli_module, monkeypatch):
    called = {}

    def fake_show(job_id):
        called["job_id"] = job_id
        return 0

    monkeypatch.setattr(cli_module, "cli_show_job", fake_show)

    rc = cli_module.main(["show", "job1"])

    assert rc == 0
    assert called["job_id"] == "job1"


def test_main_delete_subcommand_dispatches(cli_module, monkeypatch):
    called = {}

    def fake_delete(job_id, interactive=False):
        called["job_id"] = job_id
        called["interactive"] = interactive
        return 0

    monkeypatch.setattr(cli_module, "remove_job", fake_delete)

    rc = cli_module.main(["delete", "job1"])

    assert rc == 0
    assert called == {"job_id": "job1", "interactive": False}


def test_main_reschedule_subcommand_dispatches(cli_module, monkeypatch):
    called = {}

    def fake_reschedule(job_id, when):
        called["job_id"] = job_id
        called["when"] = when
        return 0

    monkeypatch.setattr(cli_module, "cli_reschedule_job", fake_reschedule)

    rc = cli_module.main(["reschedule", "job1", "03:00 tomorrow"])

    assert rc == 0
    assert called == {"job_id": "job1", "when": "03:00 tomorrow"}


def test_main_session_subcommand_dispatches_with_specific_session(cli_module, monkeypatch):
    called = {}

    def fake_change(job_id, session):
        called["job_id"] = job_id
        called["session"] = session
        return 0

    monkeypatch.setattr(cli_module, "cli_change_session", fake_change)

    rc = cli_module.main(["session", "job1", "sess-1"])

    assert rc == 0
    assert called == {"job_id": "job1", "session": "sess-1"}


def test_main_session_subcommand_dispatches_with_new(cli_module, monkeypatch):
    called = {}

    def fake_change(job_id, session):
        called["job_id"] = job_id
        called["session"] = session
        return 0

    monkeypatch.setattr(cli_module, "cli_change_session", fake_change)

    rc = cli_module.main(["session", "job1", "--new"])

    assert rc == 0
    assert called == {"job_id": "job1", "session": None}


def test_main_dry_run_uses_interactive_create_path(cli_module, monkeypatch):
    monkeypatch.setattr(cli_module, "choose", lambda msg, choices, default=None: "Create job")
    called = {}

    def fake_create_and_maybe_submit(dry_run=False):
        called["dry_run"] = dry_run
        return 0

    monkeypatch.setattr(cli_module, "create_and_maybe_submit", fake_create_and_maybe_submit)

    rc = cli_module.main(["--dry-run"])

    assert rc == 0
    assert called["dry_run"] is True