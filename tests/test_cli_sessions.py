import json
from datetime import datetime, timezone
from pathlib import Path

from schedule_agent.sessions.model import (
    SessionCandidate,
    SessionDiscoveryDiagnostic,
    SourceDiagnostic,
)


def _candidate(**overrides):
    defaults = dict(
        agent="codex",
        session_id="sess-1",
        resume_id="sess-1",
        title="Session title",
        cwd=Path("/tmp/project"),
        git_root=Path("/tmp/project"),
        git_branch="main",
        source_path=Path("/tmp/.codex/sessions/2026/04/28/sess-1.jsonl"),
        source_kind="ledger+rollout",
        provider_version=None,
        created_at=None,
        modified_at=datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
        last_user_message="Session title",
        message_count=1,
        archived=False,
        subagent=False,
        resumable=True,
        confidence=95,
        evidence=("ledger entry",),
        warnings=(),
    )
    defaults.update(overrides)
    return SessionCandidate(**defaults)


def test_sessions_command_prints_compact_table(app_modules, monkeypatch, capsys):
    cli = app_modules.cli
    monkeypatch.setattr(cli, "discover_all_session_candidates", lambda **kwargs: [_candidate()])

    rc = cli.main(["sessions"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Agent | OK | Conf | Modified | Session | Title | Project | Source | Warnings" in out
    assert "codex | yes | 95 | 2026-04-28 | sess-1" in out


def test_sessions_command_filters_agent(app_modules, monkeypatch):
    cli = app_modules.cli
    calls = []

    def fake_discover(agent, **kwargs):
        calls.append((agent, kwargs))
        return []

    monkeypatch.setattr(cli, "discover_session_candidates", fake_discover)

    assert cli.main(["sessions", "claude"]) == 0
    assert cli.main(["sessions", "codex"]) == 0
    assert [call[0] for call in calls] == ["claude", "codex"]


def test_sessions_command_json_output_is_valid(app_modules, monkeypatch, capsys):
    cli = app_modules.cli
    monkeypatch.setattr(cli, "discover_all_session_candidates", lambda **kwargs: [_candidate()])

    rc = cli.main(["sessions", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["session_id"] == "sess-1"
    assert payload[0]["source_kind"] == "ledger+rollout"


def test_sessions_command_passes_include_non_resumable(app_modules, monkeypatch, capsys):
    cli = app_modules.cli
    seen = {}

    def fake_discover(**kwargs):
        seen.update(kwargs)
        return [
            _candidate(
                session_id="archived-1",
                resume_id="archived-1",
                archived=True,
                resumable=False,
                source_kind="archived_jsonl",
                warnings=("archived session",),
            )
        ]

    monkeypatch.setattr(cli, "discover_all_session_candidates", fake_discover)

    rc = cli.main(["sessions", "--include-non-resumable"])

    assert rc == 0
    assert seen["include_non_resumable"] is True
    assert "archived_jsonl" in capsys.readouterr().out


def test_sessions_doctor_reports_checked_roots_and_warnings(app_modules, monkeypatch, capsys):
    cli = app_modules.cli
    monkeypatch.setattr(
        cli,
        "diagnose_session_candidates",
        lambda cwd=None: SessionDiscoveryDiagnostic(
            sources=(
                SourceDiagnostic(
                    agent="claude",
                    source_kind="projects/jsonl",
                    root=Path("/tmp/.claude/projects"),
                    checked=True,
                    available=True,
                    candidate_count=2,
                    error=None,
                ),
                SourceDiagnostic(
                    agent="all",
                    source_kind="ledger",
                    root=Path("/tmp/state/schedule-agent/session-ledger.jsonl"),
                    checked=True,
                    available=True,
                    candidate_count=1,
                    error=None,
                ),
            ),
            excluded=(_candidate(agent="claude", session_id="hidden-1", resume_id="hidden-1"),),
            warnings=("skipped malformed JSONL line",),
        ),
    )

    rc = cli.main(["sessions", "doctor"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Claude:" in out
    assert "Ledger:" in out
    assert "projects/jsonl" in out
    assert "Warnings:" in out
    assert "skipped malformed JSONL line" in out


def test_session_picker_items_preserve_new_and_paste_with_rich_labels(app_modules):
    cli = app_modules.cli

    items = cli._session_picker_items([_candidate()])

    assert items[0] == ("New session", None)
    assert items[1] == (cli.PASTE_SESSION_LABEL, cli.PASTE_SESSION)
    assert "Session title [sess-1]" in items[2][0]
    assert "ledger+rollout" in items[2][0]
