import pytest

from schedule_agent.scheduler_backend import (
    build_script,
    parse_at_job_id,
    remove_at_job,
    submit_job,
)


def _job(agent="claude", when="03:00 tomorrow", session_mode="new", session_id=None):
    return {
        "id": "job1",
        "agent": agent,
        "session_mode": session_mode,
        "session_id": session_id,
        "prompt_file": "/tmp/p.md",
        "when": when,
        "cwd": "/tmp/project",
        "log": "/tmp/project/log.txt",
    }


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# parse_at_job_id
# ---------------------------------------------------------------------------

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
def test_parse_at_job_id(stdout, expected):
    assert parse_at_job_id(stdout) == expected


# ---------------------------------------------------------------------------
# build_script
# ---------------------------------------------------------------------------

def test_build_script_contains_expected_parts():
    script = build_script(_job())
    assert "cd /tmp/project" in script
    assert "export PATH=/usr/local/bin:/usr/bin:/bin" in script
    assert "< /dev/null" in script
    assert ">> /tmp/project/log.txt 2>&1" in script


def test_build_script_quotes_cwd_with_spaces():
    job = _job()
    job["cwd"] = "/tmp/my project"
    job["log"] = "/tmp/my project/log.txt"
    script = build_script(job)
    assert "'/tmp/my project'" in script or "\\ " in script


# ---------------------------------------------------------------------------
# submit_job dry_run
# ---------------------------------------------------------------------------

def test_submit_job_dry_run_returns_preview_and_does_not_call_subprocess(monkeypatch):
    calls = []

    import schedule_agent.scheduler_backend as sb

    monkeypatch.setattr(sb.subprocess, "run", lambda *a, **k: calls.append((a, k)) or _Proc())

    at_job_id, output = submit_job(_job(), dry_run=True)

    assert at_job_id is None
    assert "Would schedule at: 03:00 tomorrow" in output
    assert "cd /tmp/project" in output
    assert calls == []


# ---------------------------------------------------------------------------
# submit_job live
# ---------------------------------------------------------------------------

def test_submit_job_calls_at_and_returns_at_job_id(monkeypatch):
    captured = {}

    import schedule_agent.scheduler_backend as sb

    def fake_run(cmd, input=None, text=None, capture_output=None):
        captured["cmd"] = cmd
        captured["input"] = input
        return _Proc(returncode=0, stdout="job 42 at Tue Apr 14 12:00:00 2026\n")

    monkeypatch.setattr(sb.subprocess, "run", fake_run)

    at_job_id, output = submit_job(_job())

    assert captured["cmd"] == ["at", "03:00 tomorrow"]
    assert at_job_id == "42"
    assert "job 42" in output


def test_submit_job_raises_on_at_failure(monkeypatch):
    import schedule_agent.scheduler_backend as sb

    monkeypatch.setattr(
        sb.subprocess, "run",
        lambda *a, **k: _Proc(returncode=1, stderr="Garbled time"),
    )

    with pytest.raises(RuntimeError, match="Garbled time"):
        submit_job(_job())


def test_submit_job_raises_with_fallback_message_when_stderr_empty(monkeypatch):
    import schedule_agent.scheduler_backend as sb

    monkeypatch.setattr(
        sb.subprocess, "run",
        lambda *a, **k: _Proc(returncode=1, stderr=""),
    )

    with pytest.raises(RuntimeError, match="Failed to schedule job"):
        submit_job(_job())


# ---------------------------------------------------------------------------
# remove_at_job
# ---------------------------------------------------------------------------

def test_remove_at_job_success(monkeypatch):
    import schedule_agent.scheduler_backend as sb

    captured = {}

    def fake_run(cmd, capture_output=None, text=None):
        captured["cmd"] = cmd
        return _Proc(returncode=0)

    monkeypatch.setattr(sb.subprocess, "run", fake_run)

    ok, err = remove_at_job("55")
    assert ok is True
    assert err == ""
    assert captured["cmd"] == ["atrm", "55"]


def test_remove_at_job_failure(monkeypatch):
    import schedule_agent.scheduler_backend as sb

    monkeypatch.setattr(
        sb.subprocess, "run",
        lambda *a, **k: _Proc(returncode=1, stderr="Cannot find jobid 55"),
    )

    ok, err = remove_at_job("55")
    assert ok is False
    assert "Cannot find jobid 55" in err
