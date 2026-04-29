import importlib
import json
import os
import sqlite3
import subprocess
import sys
import types
from pathlib import Path


def _write_jsonl(path: Path, *entries: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(entry) for entry in entries),
        encoding="utf-8",
    )


def _write_sqlite(
    path: Path, statements: list[str], rows: list[tuple[str, tuple[object, ...]]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        for statement in statements:
            conn.execute(statement)
        for statement, params in rows:
            conn.execute(statement, params)
        conn.commit()
    finally:
        conn.close()


def _git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True, text=True)


def test_claude_diagnostics_report_checked_roots_and_malformed_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    import schedule_agent.sessions.discovery as discovery

    discovery = importlib.reload(discovery)
    cwd = Path("/work/current/project")
    project_dir = tmp_path / ".claude" / "projects" / cwd.as_posix().replace("/", "-")
    transcript = project_dir / "session.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "\n".join(
            [
                '{"type":"ai-title","aiTitle":"Claude title"}',
                '{"type":"user","message":{"role":"user","content":"Prompt"}}',
                '{"type":"broken"',
            ]
        ),
        encoding="utf-8",
    )

    diagnostic = discovery.diagnose_sessions("claude", cwd=cwd)

    assert any(
        source.source_kind == "projects/jsonl" and source.available for source in diagnostic.sources
    )
    assert any("malformed JSONL line" in warning for warning in diagnostic.warnings)


def test_codex_discovery_uses_bounded_jsonl_windows(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    import schedule_agent.sessions.discovery as discovery

    discovery = importlib.reload(discovery)
    rollout = tmp_path / ".codex" / "sessions" / "2026" / "04" / "28" / "session.jsonl"
    entries = [{"type": "session_meta", "payload": {"id": "sess-1", "source": "exec"}}]
    entries.extend({"type": "noop", "payload": {"i": index}} for index in range(1, 251))
    entries.append(
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Middle title should stay unseen"},
        }
    )
    entries.extend({"type": "noop", "payload": {"i": index}} for index in range(252, 501))
    _write_jsonl(rollout, *entries)

    sessions = discovery.discover_sessions("codex", cwd=tmp_path, all_projects=True)

    assert sessions
    assert sessions[0].session_id == "sess-1"
    assert sessions[0].title != "Middle title should stay unseen"


def test_claude_config_dir_is_preferred_when_present(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))

    import schedule_agent.sessions.discovery as discovery

    discovery = importlib.reload(discovery)
    cwd = Path("/work/current/project")
    project_key = cwd.as_posix().replace("/", "-")
    config_session = tmp_path / "claude-config" / "projects" / project_key / "config.jsonl"
    home_session = tmp_path / ".claude" / "projects" / project_key / "home.jsonl"

    _write_jsonl(
        config_session,
        {"type": "user", "message": {"role": "user", "content": "Config session"}},
    )
    _write_jsonl(
        home_session,
        {"type": "user", "message": {"role": "user", "content": "Home session"}},
    )
    os.utime(config_session, (100, 100))
    os.utime(home_session, (100, 100))

    sessions = discovery.discover_sessions("claude", cwd=cwd, limit=10, all_projects=True)

    assert sessions[0].source_path == config_session


def test_claude_sdk_sessions_are_used_when_available(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    import schedule_agent.sessions.discovery as discovery

    fake_sdk = types.ModuleType("claude_code_sdk")
    fake_sdk.list_sessions = lambda cwd=None: [  # noqa: ARG005
        {
            "id": "sdk-1",
            "custom_title": "SDK title",
            "cwd": "/work/current/project",
            "resumable": True,
        }
    ]
    monkeypatch.setitem(sys.modules, "claude_code_sdk", fake_sdk)
    discovery = importlib.reload(discovery)

    sessions = discovery.discover_sessions(
        "claude",
        cwd=Path("/work/current/project"),
        limit=10,
        all_projects=True,
    )

    assert sessions[0].session_id == "sdk-1"
    assert sessions[0].title == "SDK title"
    assert sessions[0].source_kind.startswith("sdk")


def test_claude_transcript_is_resumable_and_cwd_mismatch_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    import schedule_agent.sessions.discovery as discovery

    discovery = importlib.reload(discovery)
    current_cwd = Path("/work/current/project")
    other_cwd = Path("/work/other/project")
    transcript = (
        tmp_path
        / ".claude"
        / "projects"
        / other_cwd.as_posix().replace("/", "-")
        / "session-123.jsonl"
    )
    _write_jsonl(
        transcript,
        {"type": "user", "message": {"role": "user", "content": "Claude prompt"}},
    )

    sessions = discovery.discover_sessions(
        "claude",
        cwd=current_cwd,
        limit=10,
        include_non_resumable=True,
        all_projects=True,
    )

    assert sessions[0].resume_id == "session-123"
    assert sessions[0].resumable is True
    assert "cwd mismatch" in sessions[0].warnings


def test_codex_sqlite_only_candidate_uses_codex_sqlite_home_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(tmp_path / "sqlite-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "other-codex-home"))

    import schedule_agent.sessions.discovery as discovery

    discovery = importlib.reload(discovery)
    sqlite_path = tmp_path / "sqlite-home" / "state-v1.sqlite"
    _write_sqlite(
        sqlite_path,
        [
            (
                "CREATE TABLE sessions "
                "(id TEXT, cwd TEXT, title TEXT, updated_at TEXT, source TEXT, archived INTEGER)"
            ),
        ],
        [
            (
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "sqlite-1",
                    str(tmp_path),
                    "SQLite title",
                    "2026-04-28T12:00:00+0000",
                    "exec",
                    0,
                ),
            )
        ],
    )

    sessions = discovery.discover_sessions("codex", cwd=tmp_path, limit=10, all_projects=True)

    assert sessions[0].session_id == "sqlite-1"
    assert sessions[0].title == "SQLite title"
    assert "sqlite" in sessions[0].source_kind


def test_codex_session_index_only_candidate_uses_codex_home_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    import schedule_agent.sessions.discovery as discovery

    discovery = importlib.reload(discovery)
    index_path = tmp_path / "codex-home" / "session_index.jsonl"
    _write_jsonl(
        index_path,
        {
            "session_id": "index-1",
            "cwd": str(tmp_path),
            "title": "Index title",
            "modified_at": "2026-04-28T12:00:00+0000",
            "source": "exec",
        },
    )

    sessions = discovery.discover_sessions("codex", cwd=tmp_path, limit=10, all_projects=True)

    assert sessions[0].session_id == "index-1"
    assert sessions[0].title == "Index title"
    assert "session_index" in sessions[0].source_kind


def test_codex_rollout_titles_and_archive_subagent_visibility(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    import schedule_agent.sessions.discovery as discovery

    discovery = importlib.reload(discovery)
    root = tmp_path / ".codex"
    event_rollout = root / "sessions" / "2026" / "04" / "28" / "event.jsonl"
    response_rollout = root / "sessions" / "2026" / "04" / "28" / "response.jsonl"
    archived_rollout = root / "archived_sessions" / "2026" / "04" / "28" / "archived.jsonl"
    subagent_rollout = root / "sessions" / "2026" / "04" / "28" / "subagent.jsonl"

    _write_jsonl(
        event_rollout,
        {
            "type": "session_meta",
            "payload": {"id": "event-1", "source": "exec", "cwd": str(tmp_path)},
        },
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Event title"}},
    )
    _write_jsonl(
        response_rollout,
        {
            "type": "session_meta",
            "payload": {"id": "response-1", "source": "exec", "cwd": str(tmp_path)},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Response title"}],
            },
        },
    )
    _write_jsonl(
        archived_rollout,
        {
            "type": "session_meta",
            "payload": {"id": "archived-1", "source": "exec", "cwd": str(tmp_path)},
        },
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Archived title"}},
    )
    _write_jsonl(
        subagent_rollout,
        {
            "type": "session_meta",
            "payload": {
                "id": "subagent-1",
                "cwd": str(tmp_path),
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": "event-1"}}},
            },
        },
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Subagent title"}},
    )

    visible = discovery.discover_sessions("codex", cwd=tmp_path, limit=10, all_projects=True)
    hidden = discovery.discover_sessions(
        "codex",
        cwd=tmp_path,
        limit=10,
        include_non_resumable=True,
        all_projects=True,
    )

    visible_ids = {session.session_id for session in visible}
    hidden_ids = {session.session_id for session in hidden}

    assert visible_ids == {"event-1", "response-1"}
    assert {session.title for session in visible} == {"Event title", "Response title"}
    assert "archived-1" in hidden_ids
    assert "subagent-1" in hidden_ids


def test_codex_merges_sqlite_index_and_rollout_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(tmp_path / "sqlite-home"))

    import schedule_agent.sessions.discovery as discovery

    discovery = importlib.reload(discovery)
    sqlite_path = tmp_path / "sqlite-home" / "state-v1.sqlite"
    _write_sqlite(
        sqlite_path,
        [
            (
                "CREATE TABLE sessions "
                "(id TEXT, cwd TEXT, title TEXT, updated_at TEXT, source TEXT, archived INTEGER)"
            ),
        ],
        [
            (
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "merged-1",
                    str(tmp_path),
                    "SQLite title",
                    "2026-04-28T12:00:00+0000",
                    "exec",
                    0,
                ),
            )
        ],
    )
    _write_jsonl(
        tmp_path / "codex-home" / "session_index.jsonl",
        {
            "session_id": "merged-1",
            "cwd": str(tmp_path),
            "title": "Index title",
            "modified_at": "2026-04-28T12:05:00+0000",
            "source": "exec",
        },
    )
    _write_jsonl(
        tmp_path / "codex-home" / "sessions" / "2026" / "04" / "28" / "merged.jsonl",
        {
            "type": "session_meta",
            "payload": {"id": "merged-1", "source": "exec", "cwd": str(tmp_path)},
        },
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Rollout title"}},
    )

    sessions = discovery.discover_sessions("codex", cwd=tmp_path, limit=10, all_projects=True)

    assert sessions[0].session_id == "merged-1"
    assert sessions[0].title == "SQLite title"
    assert sessions[0].source_kind == "sqlite+session_index+rollout"


def test_codex_malformed_jsonl_and_unknown_sqlite_schema_do_not_crash(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(tmp_path / "sqlite-home"))

    import schedule_agent.sessions.discovery as discovery

    discovery = importlib.reload(discovery)
    bad_rollout = tmp_path / "codex-home" / "sessions" / "2026" / "04" / "28" / "bad.jsonl"
    bad_rollout.parent.mkdir(parents=True, exist_ok=True)
    bad_rollout.write_text(
        '{"type":"session_meta","payload":{"id":"bad-1","source":"exec"}}\n{"bad"', encoding="utf-8"
    )
    _write_sqlite(
        tmp_path / "sqlite-home" / "state-v1.sqlite",
        ["CREATE TABLE unrelated (value TEXT)"],
        [],
    )

    diagnostic = discovery.diagnose_sessions("codex", cwd=tmp_path)

    assert any("malformed JSONL line" in warning for warning in diagnostic.warnings)
    assert any("unknown schema" in warning for warning in diagnostic.warnings)


def test_ranking_prefers_current_cwd_then_same_git_root_then_unrelated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    import schedule_agent.sessions.discovery as discovery

    discovery = importlib.reload(discovery)
    repo = tmp_path / "repo"
    _git_init(repo)
    current_cwd = repo / "apps" / "current"
    sibling_cwd = repo / "apps" / "sibling"
    unrelated_cwd = tmp_path / "other-project"
    current_cwd.mkdir(parents=True, exist_ok=True)
    sibling_cwd.mkdir(parents=True, exist_ok=True)
    unrelated_cwd.mkdir(parents=True, exist_ok=True)
    _write_jsonl(
        tmp_path / "codex-home" / "session_index.jsonl",
        {
            "session_id": "unrelated-1",
            "cwd": str(unrelated_cwd),
            "title": "Unrelated",
            "modified_at": "2026-04-28T12:30:00+0000",
            "source": "exec",
        },
        {
            "session_id": "sibling-1",
            "cwd": str(sibling_cwd),
            "title": "Sibling",
            "modified_at": "2026-04-28T12:20:00+0000",
            "source": "exec",
        },
        {
            "session_id": "current-1",
            "cwd": str(current_cwd),
            "title": "Current",
            "modified_at": "2026-04-28T12:10:00+0000",
            "source": "exec",
        },
    )

    sessions = discovery.discover_sessions(
        "codex",
        cwd=current_cwd,
        limit=10,
        include_non_resumable=True,
        all_projects=True,
    )

    assert [session.session_id for session in sessions[:3]] == [
        "current-1",
        "sibling-1",
        "unrelated-1",
    ]


def test_ranking_prefers_resumable_and_higher_confidence_over_newer(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(tmp_path / "sqlite-home"))

    import schedule_agent.sessions.discovery as discovery

    discovery = importlib.reload(discovery)
    _write_sqlite(
        tmp_path / "sqlite-home" / "state-v1.sqlite",
        [
            (
                "CREATE TABLE sessions "
                "(id TEXT, cwd TEXT, title TEXT, updated_at TEXT, source TEXT, archived INTEGER)"
            ),
        ],
        [
            (
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "high-confidence",
                    str(tmp_path),
                    "SQLite title",
                    "2026-04-28T12:00:00+0000",
                    "exec",
                    0,
                ),
            )
        ],
    )
    _write_jsonl(
        tmp_path / "codex-home" / "sessions" / "2026" / "04" / "28" / "subagent.jsonl",
        {
            "type": "session_meta",
            "payload": {
                "id": "subagent-1",
                "cwd": str(tmp_path),
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": "high-confidence"}}},
            },
        },
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Subagent title"}},
    )
    newer_rollout = tmp_path / "codex-home" / "sessions" / "2026" / "04" / "28" / "newer.jsonl"
    _write_jsonl(
        newer_rollout,
        {
            "type": "session_meta",
            "payload": {"id": "newer-1", "source": "exec", "cwd": str(tmp_path)},
        },
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Newer rollout"}},
    )
    os.utime(newer_rollout, (200, 200))

    sessions = discovery.discover_sessions(
        "codex",
        cwd=tmp_path,
        limit=10,
        include_non_resumable=True,
        all_projects=True,
    )

    assert sessions[0].session_id == "high-confidence"
    assert sessions[-1].session_id == "subagent-1"


def test_dedup_merges_evidence_and_warnings(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(tmp_path / "sqlite-home"))

    import schedule_agent.sessions.discovery as discovery

    discovery = importlib.reload(discovery)
    _write_sqlite(
        tmp_path / "sqlite-home" / "state-v1.sqlite",
        [
            (
                "CREATE TABLE sessions "
                "(id TEXT, cwd TEXT, title TEXT, updated_at TEXT, source TEXT, archived INTEGER)"
            ),
        ],
        [
            (
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "merged-2",
                    str(tmp_path),
                    "SQLite title",
                    "2026-04-28T12:00:00+0000",
                    "exec",
                    0,
                ),
            )
        ],
    )
    _write_jsonl(
        tmp_path / "codex-home" / "session_index.jsonl",
        {
            "session_id": "merged-2",
            "cwd": str(tmp_path / "other"),
            "title": "Index title",
            "modified_at": "2026-04-28T12:05:00+0000",
            "source": "exec",
        },
    )

    sessions = discovery.discover_sessions(
        "codex",
        cwd=tmp_path,
        limit=10,
        include_non_resumable=True,
        all_projects=True,
    )

    assert sessions[0].source_kind == "sqlite+session_index"
    assert "provider title" in sessions[0].evidence
    assert "cwd mismatch" in sessions[0].warnings


def test_ledger_path_respects_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))

    import schedule_agent.sessions.ledger as ledger

    ledger = importlib.reload(ledger)
    ledger.append_ledger_entry(
        {
            "agent": "codex",
            "schedule_job_id": "job-1",
            "cwd": str(tmp_path),
            "git_root": str(tmp_path),
            "git_branch": "main",
            "discovered_session_id": "ledger-1",
            "discovered_resume_id": "ledger-1",
            "started_at": "2026-04-28T12:00:00+0000",
            "finished_at": "2026-04-28T12:05:00+0000",
            "confidence": 95,
            "evidence": ["ledger test"],
        }
    )

    assert ledger.session_ledger_path() == (
        tmp_path / "state-home" / "schedule-agent" / "session-ledger.jsonl"
    )
    assert ledger.session_ledger_path().exists()


def test_ledger_rows_produce_candidates_and_skip_invalid_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))

    import schedule_agent.sessions.discovery as discovery
    import schedule_agent.sessions.ledger as ledger

    discovery = importlib.reload(discovery)
    ledger = importlib.reload(ledger)
    path = ledger.session_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "agent": "codex",
                        "schedule_job_id": "job-1",
                        "cwd": str(tmp_path),
                        "git_root": str(tmp_path),
                        "git_branch": "main",
                        "discovered_session_id": "ledger-1",
                        "discovered_resume_id": "ledger-1",
                        "started_at": "2026-04-28T12:00:00+0000",
                        "finished_at": "2026-04-28T12:05:00+0000",
                        "confidence": 95,
                        "evidence": ["ledger test"],
                    }
                ),
                '{"broken"',
            ]
        ),
        encoding="utf-8",
    )

    sessions = discovery.discover_sessions(
        "codex",
        cwd=tmp_path,
        limit=10,
        include_non_resumable=True,
        all_projects=True,
    )
    diagnostic = discovery.diagnose_sessions("codex", cwd=tmp_path)

    assert sessions[0].source_kind == "ledger"
    assert sessions[0].session_id == "ledger-1"
    assert any("skipped 1 invalid row" in warning for warning in diagnostic.warnings)


def test_ledger_candidates_outrank_provider_only_candidates(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    import schedule_agent.sessions.discovery as discovery
    import schedule_agent.sessions.ledger as ledger

    discovery = importlib.reload(discovery)
    ledger = importlib.reload(ledger)
    ledger.append_ledger_entry(
        {
            "agent": "codex",
            "schedule_job_id": "job-1",
            "cwd": str(tmp_path),
            "git_root": str(tmp_path),
            "git_branch": "main",
            "discovered_session_id": "ledger-1",
            "discovered_resume_id": "ledger-1",
            "started_at": "2026-04-28T12:00:00+0000",
            "finished_at": "2026-04-28T12:05:00+0000",
            "confidence": 95,
            "evidence": ["ledger test"],
            "discovered_title": "Ledger title",
        }
    )
    _write_jsonl(
        tmp_path / "codex-home" / "session_index.jsonl",
        {
            "session_id": "provider-1",
            "cwd": str(tmp_path),
            "title": "Provider title",
            "modified_at": "2026-04-28T12:30:00+0000",
            "source": "exec",
        },
    )

    sessions = discovery.discover_sessions("codex", cwd=tmp_path, limit=10, all_projects=True)

    assert [session.session_id for session in sessions[:2]] == ["ledger-1", "provider-1"]
