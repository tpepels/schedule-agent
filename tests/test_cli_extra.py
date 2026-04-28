import importlib
import json
import os
from pathlib import Path

import pytest


def _write_jsonl(path: Path, *entries: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(entry) for entry in entries),
        encoding="utf-8",
    )


def _job(app_modules, **overrides):
    job = app_modules.transitions.make_job(
        job_id="job1",
        title="Do the thing",
        agent="claude",
        session_mode="new",
        session_id=None,
        prompt_file="/tmp/prompt.md",
        scheduled_for="2026-04-18T09:00:00+0000",
        cwd="/tmp/project",
        log_dir="/tmp/project/logs/job1",
    )
    job.update(overrides)
    return job


def test_extract_session_title_reads_supported_claude_and_codex_formats(tmp_path):
    from schedule_agent.sessions.providers.common import extract_session_title

    claude_ai = tmp_path / "claude-ai.jsonl"
    claude_user = tmp_path / "claude-user.jsonl"
    codex_event = tmp_path / "codex-event.jsonl"
    codex_response = tmp_path / "codex-response.jsonl"

    _write_jsonl(
        claude_ai,
        {"type": "ai-title", "aiTitle": "Claude title"},
        {"type": "user", "message": {"role": "user", "content": "ignored"}},
    )
    _write_jsonl(
        claude_user,
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "ignored"}},
        {"type": "user", "message": {"role": "user", "content": "First line\nSecond line"}},
    )
    _write_jsonl(
        codex_event,
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Codex event\nMore"}},
    )
    _write_jsonl(
        codex_response,
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Codex response\nMore"}],
            },
        },
    )

    assert extract_session_title(claude_ai, "claude") == "Claude title"
    assert extract_session_title(claude_user, "claude") == "First line"
    assert extract_session_title(codex_event, "codex") == "Codex event"
    assert extract_session_title(codex_response, "codex") == "Codex response"
    assert extract_session_title(tmp_path / "missing.jsonl", "codex") is None


def test_choose_session_prefers_current_claude_project_and_hides_unrelated_by_default(
    app_modules, monkeypatch, tmp_path
):
    cli = app_modules.cli
    monkeypatch.setenv("HOME", str(tmp_path))
    cwd = Path("/work/current/project")
    preferred_dir = tmp_path / ".claude" / "projects" / cwd.as_posix().replace("/", "-")
    other_dir = tmp_path / ".claude" / "projects" / "other-project"

    preferred = preferred_dir / "preferred.jsonl"
    other = other_dir / "other.jsonl"
    nested = preferred_dir / "nested" / "ignored.jsonl"

    _write_jsonl(
        preferred,
        {"type": "user", "message": {"role": "user", "content": "Preferred session"}},
    )
    _write_jsonl(
        other,
        {"type": "user", "message": {"role": "user", "content": "Other session"}},
    )
    _write_jsonl(
        nested,
        {"type": "user", "message": {"role": "user", "content": "Should be ignored"}},
    )
    os.utime(preferred, (10, 10))
    os.utime(other, (20, 20))

    captured = {}

    def fake_choose(message, choices, default=None):
        captured["message"] = message
        captured["choices"] = choices
        return choices[1]

    monkeypatch.setattr(cli, "choose", fake_choose)

    selected = cli.choose_session("claude", cwd=cwd)

    assert captured["message"] == "Session"
    assert captured["choices"][0] == "New session"
    assert "Preferred session" in captured["choices"][1]
    assert captured["choices"][2] == cli.PASTE_SESSION_LABEL
    assert selected == "preferred"


def test_public_session_discovery_api_prefers_current_claude_project(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    import schedule_agent.sessions.discovery as discovery

    discovery = importlib.reload(discovery)
    cwd = Path("/work/current/project")
    preferred_dir = tmp_path / ".claude" / "projects" / cwd.as_posix().replace("/", "-")
    other_dir = tmp_path / ".claude" / "projects" / "other-project"

    preferred = preferred_dir / "preferred.jsonl"
    other = other_dir / "other.jsonl"

    _write_jsonl(
        preferred,
        {"type": "user", "message": {"role": "user", "content": "Preferred session"}},
    )
    _write_jsonl(
        other,
        {"type": "user", "message": {"role": "user", "content": "Other session"}},
    )

    sessions = discovery.discover_sessions("claude", cwd=cwd, limit=10, all_projects=True)

    assert sessions[0].title == "Preferred session"
    assert any(session.title == "Other session" for session in sessions)


def test_public_session_discovery_api_hides_codex_subagent_rollouts(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HOME", str(tmp_path))

    import schedule_agent.sessions.discovery as discovery

    discovery = importlib.reload(discovery)
    root = tmp_path / ".codex" / "sessions"
    top = root / "2026" / "04" / "18" / "rollout-top.jsonl"
    sub = root / "2026" / "04" / "18" / "rollout-sub.jsonl"

    _write_jsonl(
        top,
        {"type": "session_meta", "payload": {"id": "top", "source": "exec"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Top"}},
    )
    _write_jsonl(
        sub,
        {
            "type": "session_meta",
            "payload": {
                "id": "sub",
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": "top"}}},
            },
        },
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Sub"}},
    )

    sessions = discovery.discover_sessions("codex", cwd=tmp_path, limit=10, all_projects=True)
    ids = [session.session_id for session in sessions]

    assert "top" in ids
    assert "sub" not in ids


def test_read_prompt_uses_editor_output_and_cleans_up_tempfile(app_modules, monkeypatch):
    cli = app_modules.cli
    seen = {}

    def fake_edit(path):
        seen["path"] = Path(path)
        seen["path"].write_text("  Prompt title\n\nbody  ", encoding="utf-8")

    monkeypatch.setattr(cli, "edit_file", fake_edit)

    assert cli.read_prompt() == "Prompt title\n\nbody"
    assert seen["path"].exists() is False


def test_read_prompt_raises_when_editor_leaves_blank_file(app_modules, monkeypatch):
    cli = app_modules.cli
    seen = {}

    def fake_edit(path):
        seen["path"] = Path(path)
        seen["path"].write_text("   \n", encoding="utf-8")

    monkeypatch.setattr(cli, "edit_file", fake_edit)

    with pytest.raises(KeyboardInterrupt):
        cli.read_prompt()

    assert seen["path"].exists() is False


def test_cancel_at_job_updates_legacy_state_and_queue_membership(app_modules, monkeypatch):
    cli = app_modules.cli
    job = app_modules.transitions.on_submit(_job(app_modules), "42")
    app_modules.persistence.save_jobs([job])
    cli.save_state({"job1": {"at_job_id": "42"}})
    monkeypatch.setattr(cli.legacy_cli_state, "remove_at_job", lambda at_job_id: (True, ""))

    assert cli.cancel_at_job("job1") is True

    updated = app_modules.persistence.load_jobs()[0]
    state = cli.load_state()["job1"]
    assert updated["at_job_id"] is None
    assert state["at_job_removed"] is True
    assert "at_job_id" not in state
    assert state["at_job_remove_attempted_at"]


def test_cancel_at_job_records_remove_errors_without_mutating_queue(app_modules, monkeypatch):
    cli = app_modules.cli
    job = app_modules.transitions.on_submit(_job(app_modules), "42")
    app_modules.persistence.save_jobs([job])
    cli.save_state({"job1": {"at_job_id": "42"}})
    monkeypatch.setattr(
        cli.legacy_cli_state,
        "remove_at_job",
        lambda at_job_id: (False, "atrm failed"),
    )

    assert cli.cancel_at_job("job1") is False

    updated = app_modules.persistence.load_jobs()[0]
    state = cli.load_state()["job1"]
    assert updated["at_job_id"] == "42"
    assert state["at_job_remove_error"] == "atrm failed"


def test_list_jobs_noninteractive_appends_atq_warning(app_modules, monkeypatch):
    cli = app_modules.cli
    monkeypatch.setattr(
        cli,
        "list_job_views",
        lambda filter_name="all": (
            [
                {
                    "title": "Do the thing",
                    "display_label": "Queued",
                    "scheduler_label": "Unknown",
                    "scheduled_for": "2026-04-18T09:00:00+0000",
                    "updated_at": "2026-04-17T08:00:00+0000",
                    "created_at": "2026-04-17T07:00:00+0000",
                    "session_mode": "new",
                    "session_id": None,
                    "depends_on": "-",
                }
            ],
            "atq unavailable",
        ),
    )

    output = cli.list_jobs_noninteractive()

    assert (
        "Title | Status | Scheduler | Run At | Updated | Created | Session | Dependency" in output
    )
    assert output.endswith("atq warning: atq unavailable")


def test_cli_cancel_job_marks_unscheduled_job_removed(app_modules, capsys):
    cli = app_modules.cli
    app_modules.persistence.save_jobs([_job(app_modules)])

    rc = cli.cli_cancel_job("job1")

    assert rc == 0
    assert app_modules.persistence.load_jobs()[0]["submission"] == "cancelled"
    assert "job1: marked removed" in capsys.readouterr().out


def test_main_handles_set_session_variants(app_modules, monkeypatch, capsys):
    cli = app_modules.cli
    calls = []
    monkeypatch.setattr(
        cli,
        "cli_change_session",
        lambda job_id, session: calls.append((job_id, session)) or 0,
    )

    assert cli.main(["set-session", "job1", "--new"]) == 0
    assert cli.main(["session", "job1", "sess-123"]) == 0
    assert calls == [("job1", None), ("job1", "sess-123")]
    assert "warning: `session` is deprecated" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        cli.main(["set-session", "job1"])

    assert "requires either a session id or --new" in capsys.readouterr().err


def test_main_warns_for_cancel_alias(app_modules, monkeypatch, capsys):
    cli = app_modules.cli
    monkeypatch.setattr(cli, "cli_cancel_job", lambda job_id: 0)

    assert cli.main(["cancel", "job1"]) == 0
    assert "warning: `cancel` is deprecated" in capsys.readouterr().err
