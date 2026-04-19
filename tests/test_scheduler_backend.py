import pytest


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _job():
    return {
        "id": "job1",
        "agent": "claude",
        "prompt_file": "/tmp/prompt.md",
        "session_mode": "new",
        "session_id": None,
        "cwd": "/tmp/project",
        "scheduled_for": "2026-04-18T09:00:00+0000",
        "log_dir": "/tmp/project/logs/job1",
    }


def test_resolve_schedule_spec_normalizes_shorthand(app_modules, monkeypatch):
    backend = app_modules.scheduler_backend

    def fake_run(cmd, capture_output=None, text=None, **kwargs):
        assert cmd[:3] == ["date", "-d", "now + 10 minutes"]
        return _Proc(stdout="2026-04-18T09:10:00+0100\n")

    monkeypatch.setattr(backend.subprocess, "run", fake_run)
    assert backend.resolve_schedule_spec("10m") == "2026-04-18T09:10:00+0100"


def test_build_script_uses_wrapper_and_stream_redirection(app_modules):
    script = app_modules.scheduler_backend.build_script(_job())
    assert 'exec >>"$log_file" 2>&1' in script
    assert "schedule-agent mark running job1" in script
    assert "schedule-agent mark done job1" in script
    assert "schedule-agent mark failed job1" in script
    assert "scheduled_for=2026-04-18T09:00:00+0000" in script


def test_submit_job_uses_at_dash_t(app_modules, monkeypatch):
    backend = app_modules.scheduler_backend
    captured = {}

    def fake_run(cmd, input=None, text=None, capture_output=None, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = input
        return _Proc(stderr="job 42 at Fri Apr 18 09:00:00 2026\n")

    monkeypatch.setattr(backend.subprocess, "run", fake_run)
    at_job_id, output = backend.submit_job(_job())
    assert captured["cmd"] == ["at", "-t", "202604180900.00"]
    assert at_job_id == "42"
    assert "job 42" in output


def test_submit_job_raises_when_at_output_has_no_job_id(app_modules, monkeypatch):
    backend = app_modules.scheduler_backend
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *args, **kwargs: _Proc(stderr="warning: scheduled, but no id reported\n"),
    )

    with pytest.raises(RuntimeError, match="Could not determine at job id"):
        backend.submit_job(_job())


def test_parse_atq_line_and_query_atq(app_modules, monkeypatch):
    backend = app_modules.scheduler_backend

    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *args, **kwargs: _Proc(stdout="42 2026-04-18T09:00:00+0100 a tom\n"),
    )

    entries, error = backend.query_atq()
    assert error is None
    assert entries["42"].scheduled_for == "2026-04-18T09:00:00+0100"
    assert entries["42"].queue == "a"


def test_query_atq_ignores_malformed_rows_and_scopes_requested_ids(app_modules, monkeypatch):
    backend = app_modules.scheduler_backend
    captured = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _Proc(
            stdout=("garbage row\n42 2026-04-18T09:00:00+0100 a tom\n99 not-a-timestamp a tom\n")
        )

    monkeypatch.setattr(backend.subprocess, "run", fake_run)

    entries, error = backend.query_atq(["42", "99"])
    assert error is None
    assert captured["cmd"] == ["atq", "-o", backend.ATQ_TIME_FORMAT, "42", "99"]
    assert list(entries) == ["42"]


def test_query_atq_reports_error(app_modules, monkeypatch):
    backend = app_modules.scheduler_backend
    monkeypatch.setattr(
        backend.subprocess,
        "run",
        lambda *args, **kwargs: _Proc(returncode=1, stderr="atq unavailable"),
    )
    entries, error = backend.query_atq()
    assert entries == {}
    assert error == "atq unavailable"


def test_submit_job_dry_run_shows_wrapper_preview(app_modules):
    at_job_id, output = app_modules.scheduler_backend.submit_job(_job(), dry_run=True)
    assert at_job_id is None
    assert "Would schedule at 2026-04-18T09:00:00+0000 via `at -t 202604180900.00`" in output
    assert "schedule-agent mark running job1" in output


def test_build_script_legacy_job_uses_default_path(app_modules):
    script = app_modules.scheduler_backend.build_script(_job())
    assert "export PATH=/usr/local/bin:/usr/bin:/bin" in script


def test_build_script_uses_provenance_path_and_bin(app_modules):
    job = _job()
    job["provenance"] = {
        "agent_path": "/opt/claude/bin/claude",
        "path_snapshot_cleaned": ["/home/u/.local/bin", "/usr/bin", "/bin"],
    }
    script = app_modules.scheduler_backend.build_script(job)
    assert "export PATH=/home/u/.local/bin:/usr/bin:/bin" in script
    assert "/opt/claude/bin/claude" in script


def test_run_at_injects_c_locale(app_modules, monkeypatch):
    backend = app_modules.scheduler_backend
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return _Proc(stderr="job 7 at Mon Jan 1 00:00:00 2030\n")

    monkeypatch.setattr(backend.subprocess, "run", fake_run)
    backend.submit_job(_job())
    assert captured["env"]["LC_ALL"] == "C"
    assert captured["env"]["LANG"] == "C"


def test_run_at_preserves_existing_env(app_modules, monkeypatch):
    backend = app_modules.scheduler_backend
    monkeypatch.setenv("SCHEDULE_AGENT_CUSTOM", "keep-me")

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return _Proc(stderr="job 7 at Mon Jan 1 00:00:00 2030\n")

    monkeypatch.setattr(backend.subprocess, "run", fake_run)
    backend.submit_job(_job())
    assert captured["env"]["SCHEDULE_AGENT_CUSTOM"] == "keep-me"
    assert captured["env"]["LC_ALL"] == "C"
