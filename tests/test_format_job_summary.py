from __future__ import annotations

from schedule_agent.operations import format_job_summary


def test_cwd_uses_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    job = {
        "id": "abc123",
        "title": "Test",
        "display_label": "Completed",
        "scheduler_label": "at",
        "scheduled_for": None,
        "created_at": None,
        "updated_at": None,
        "last_started_at": None,
        "last_run_at": None,
        "session_mode": "append",
        "session_id": None,
        "depends_on": None,
        "at_job_id": None,
        "log_dir": str(tmp_path / "Projects" / "foo" / "logs"),
        "last_log_file": None,
        "prompt_file": None,
        "cwd": str(tmp_path / "Projects" / "foo"),
    }
    summary = format_job_summary(job)
    assert "~/Projects/foo" in summary
    assert str(tmp_path) not in summary


def test_log_dir_uses_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    job = {
        "id": "def456",
        "title": "Log test",
        "display_label": "Completed",
        "scheduler_label": "at",
        "scheduled_for": None,
        "created_at": None,
        "updated_at": None,
        "last_started_at": None,
        "last_run_at": None,
        "session_mode": "append",
        "session_id": None,
        "depends_on": None,
        "at_job_id": None,
        "log_dir": str(tmp_path / ".local" / "share" / "schedule-agent" / "logs"),
        "last_log_file": str(tmp_path / ".local" / "share" / "schedule-agent" / "logs" / "run.log"),
        "prompt_file": str(tmp_path / ".config" / "schedule-agent" / "prompt.md"),
        "cwd": "/other/path",
    }
    summary = format_job_summary(job)
    assert "~/.local/share/schedule-agent/logs" in summary
    assert "~/.config/schedule-agent/prompt.md" in summary
    # absolute path should not appear for home-relative fields
    assert str(tmp_path / ".local") not in summary


def test_none_path_fields_show_dash(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    job = {
        "id": "ghi789",
        "title": "Dash test",
        "display_label": "Completed",
        "scheduler_label": "at",
        "scheduled_for": None,
        "created_at": None,
        "updated_at": None,
        "last_started_at": None,
        "last_run_at": None,
        "session_mode": "append",
        "session_id": None,
        "depends_on": None,
        "at_job_id": None,
        "log_dir": None,
        "last_log_file": None,
        "prompt_file": None,
        "cwd": None,
    }
    summary = format_job_summary(job)
    assert "log_dir:       -" in summary
    assert "cwd:           -" in summary
