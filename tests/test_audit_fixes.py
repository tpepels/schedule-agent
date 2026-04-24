"""Tests covering release-readiness fixes from reports/audits/combined.md."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Architecture: atomic queue write
# ---------------------------------------------------------------------------


def test_save_jobs_is_atomic_via_rename(app_modules, monkeypatch):
    # A crash mid-write must not leave a torn queue.jsonl. save_jobs writes
    # to a .tmp sibling and os.replaces it into place. We assert the rename
    # happens and the tmp path never co-exists with a partial target.
    persistence = app_modules.persistence
    persistence._ensure_dirs()

    original_replace = os.replace
    calls: list[tuple[str, str]] = []

    def tracking_replace(src, dst):
        calls.append((str(src), str(dst)))
        return original_replace(src, dst)

    monkeypatch.setattr(persistence.os, "replace", tracking_replace)
    persistence.save_jobs([{"id": "a", "title": "t"}])
    assert len(calls) == 1
    src, dst = calls[0]
    assert src.endswith(".jsonl.tmp")
    assert dst.endswith(".jsonl")
    assert not Path(src).exists()
    assert Path(dst).exists()
    assert json.loads(Path(dst).read_text()) == {"id": "a", "title": "t"}


def test_save_jobs_failure_does_not_corrupt_existing_queue(app_modules, monkeypatch):
    persistence = app_modules.persistence
    persistence._ensure_dirs()
    persistence.save_jobs([{"id": "old", "title": "keep me"}])
    queue_file = persistence._queue_file()
    original = queue_file.read_text()

    def boom(src, dst):
        raise OSError("disk full simulating mid-write crash")

    monkeypatch.setattr(persistence.os, "replace", boom)
    with pytest.raises(OSError):
        persistence.save_jobs([{"id": "new", "title": "would-be replacement"}])
    # Original untouched because the atomic rename failed.
    assert queue_file.read_text() == original


# ---------------------------------------------------------------------------
# Security: sensitive dir permissions
# ---------------------------------------------------------------------------


def test_ensure_dirs_chmods_sensitive_dirs_to_0700(app_modules):
    persistence = app_modules.persistence
    state_dir, _data_dir, prompt_dir, logs_dir, _queue_file = persistence._ensure_dirs()
    for path in (state_dir, prompt_dir, logs_dir):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        # at least owner-only; allow 0o700 exactly
        assert mode == 0o700, f"{path} has mode {oct(mode)}, expected 0o700"


# ---------------------------------------------------------------------------
# Security: prompt prefix snapshot
# ---------------------------------------------------------------------------


def test_build_script_snapshots_prompt_prefix(app_modules, monkeypatch):
    scheduler_backend = app_modules.scheduler_backend
    from schedule_agent import config as _config

    monkeypatch.setattr(_config, "load_prompt_prefix", lambda agent: f"SNAPSHOT-FOR-{agent}\n")
    monkeypatch.setattr(
        scheduler_backend, "load_prompt_prefix", lambda agent: f"SNAPSHOT-FOR-{agent}\n"
    )

    log_dir = app_modules.persistence.job_log_dir("snapjob")
    job = {
        "id": "snapjob",
        "agent": "claude",
        "prompt_file": "/tmp/p.md",
        "session_mode": "new",
        "session_id": None,
        "cwd": "/tmp",
        "log_dir": log_dir,
        "scheduled_for": "2026-04-23T09:00:00+0000",
    }
    script = scheduler_backend.build_script(job)
    snapshot_path = Path(log_dir) / "prefix.snapshot"
    assert snapshot_path.exists()
    assert snapshot_path.read_text() == "SNAPSHOT-FOR-claude\n"
    mode = stat.S_IMODE(os.stat(snapshot_path).st_mode)
    assert mode == 0o600
    assert str(snapshot_path) in script


# ---------------------------------------------------------------------------
# Security: atrm skips call when id not in atq
# ---------------------------------------------------------------------------


def test_remove_at_job_skips_atrm_when_id_absent(app_modules, monkeypatch):
    scheduler_backend = app_modules.scheduler_backend
    monkeypatch.setattr(scheduler_backend, "query_atq_entry", lambda at_id: (None, None))

    called = {"ran": False}

    def spy_run_at(cmd, **kwargs):
        called["ran"] = True
        raise AssertionError("atrm must not be called when job is absent from atq")

    monkeypatch.setattr(scheduler_backend, "_run_at", spy_run_at)
    ok, err = scheduler_backend.remove_at_job("999")
    assert ok is True
    assert err == ""
    assert called["ran"] is False


# ---------------------------------------------------------------------------
# Feature: --version prints package version and exits 0
# ---------------------------------------------------------------------------


def test_version_flag_prints_and_exits(app_modules, capsys):
    cli = app_modules.cli
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "schedule-agent" in out


# ---------------------------------------------------------------------------
# Feature: edit-prefix subcommand
# ---------------------------------------------------------------------------


def test_edit_prefix_subcommand_opens_editor_on_prefix_file(app_modules, monkeypatch, tmp_path):
    cli = app_modules.cli
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    edited: dict[str, Path] = {}

    def fake_edit(path):
        edited["path"] = Path(path)
        Path(path).write_text("edited\n", encoding="utf-8")

    monkeypatch.setattr(cli, "edit_file", fake_edit)
    rc = cli.main(["edit-prefix", "claude"])
    assert rc == 0
    assert edited["path"].name == "prompt-prefix-claude.md"
    assert edited["path"].read_text() == "edited\n"


# ---------------------------------------------------------------------------
# Feature: retry defaults to 'now + 1 minute' when no schedule spec given
# ---------------------------------------------------------------------------


def test_retry_defaults_to_one_minute(app_modules, monkeypatch):
    cli = app_modules.cli
    captured = {}

    def fake_retry(job_id, spec):
        captured["job_id"] = job_id
        captured["spec"] = spec
        return 0

    monkeypatch.setattr(cli, "cli_retry_job", fake_retry)
    cli.main(["retry", "job1"])
    assert captured == {"job_id": "job1", "spec": "now + 1 minute"}


# ---------------------------------------------------------------------------
# Feature: --dry-run plumbed into submit
# ---------------------------------------------------------------------------


def test_submit_dry_run_prints_preview_and_does_not_persist(app_modules, monkeypatch, capsys):
    cli = app_modules.cli
    operations = app_modules.operations
    monkeypatch.setattr(
        operations,
        "submit_or_repair_job",
        lambda job_id, dry_run=False: (
            {
                "id": job_id,
                "_dry_run_preview": "PREVIEW-BODY",
            }
            if dry_run
            else (_ for _ in ()).throw(AssertionError("dry_run must be True in this test"))
        ),
    )
    # Mirror into cli module-level binding
    monkeypatch.setattr(
        cli,
        "submit_or_repair_job",
        operations.submit_or_repair_job,
    )
    rc = cli.main(["--dry-run", "submit", "job1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "PREVIEW-BODY" in out


# ---------------------------------------------------------------------------
# UI: empty-state hint points to the actual keybinding
# ---------------------------------------------------------------------------


def test_empty_state_hint_matches_add_binding(app_modules):
    # The empty-state copy in summary_fragments is a string literal inside
    # jobs_menu's closure. We inspect the source instead of instantiating
    # the full prompt_toolkit app, since the audit blocker is specifically
    # "the hinted key must match the live binding."
    import inspect

    source = inspect.getsource(app_modules.cli.jobs_menu)
    assert "Press A to add one" in source
    assert "Press N to create one" not in source
    # And the `a` binding still exists and calls start_new_job_flow
    assert '@kb.add("a"' in source


# ---------------------------------------------------------------------------
# UI: status glyphs are added to status column
# ---------------------------------------------------------------------------


def test_status_column_value_prepends_glyph(app_modules):
    cli = app_modules.cli
    for state, glyph in cli.STATUS_GLYPHS.items():
        job = {"display_state": state, "display_label": state.title()}
        value = cli._column_value(job, "status")
        assert value.startswith(glyph + " ")


# ---------------------------------------------------------------------------
# Architecture: import has no filesystem side effects
# ---------------------------------------------------------------------------


def test_importing_cli_does_not_create_xdg_dirs(tmp_path, monkeypatch):
    import importlib
    import sys

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    # Drop cached modules so the import re-executes module-level code.
    for name in list(sys.modules):
        if name.startswith("schedule_agent"):
            del sys.modules[name]
    importlib.import_module("schedule_agent.cli")
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "data").exists()


# ---------------------------------------------------------------------------
# TUI pure helpers: schedule resolver funcs and column layout
# ---------------------------------------------------------------------------


def test_resolve_offset_pick_produces_minutes_spec(app_modules):
    cli = app_modules.cli
    assert cli._resolve_offset_pick(1, 30) == "now + 90 minutes"
    # Zero rounds up to 1 minute (avoids "now" ambiguity in at(1)).
    assert cli._resolve_offset_pick(0, 0) == "now + 1 minutes"


def test_resolve_clock_pick_rolls_over_to_tomorrow_when_past(app_modules):
    cli = app_modules.cli
    from datetime import datetime

    # Pick a clock time that's definitely in the past relative to "now":
    # 00:01 yesterday. The helper must produce a timestamp STRICTLY in the
    # future, so if 00:01 today has passed it should produce 00:01 tomorrow.
    spec = cli._resolve_clock_pick(0, 1)
    # spec format is "YYYY-MM-DD HH:MM"
    target = datetime.strptime(spec, "%Y-%m-%d %H:%M")
    assert target > datetime.now()


def test_layout_mode_transitions(app_modules):
    cli = app_modules.cli
    assert cli._layout_mode(70) == "narrow"
    assert cli._layout_mode(80) == "medium"
    assert cli._layout_mode(120) == "wide"
    assert cli._layout_mode(200) == "xwide"


def test_summary_columns_widen_title_with_budget(app_modules):
    cli = app_modules.cli
    narrow = dict(cli._summary_columns("narrow", 80))
    medium = dict(cli._summary_columns("medium", 100))
    assert narrow["title"] >= cli.TITLE_MIN
    assert medium["title"] >= cli.TITLE_MIN
    # Wider terminals give title more room than narrow does.
    wide = dict(cli._summary_columns("xwide", 200))
    assert wide["title"] >= medium["title"]


# ---------------------------------------------------------------------------
# Feature: post-job hook receives env and is fire-and-forget
# ---------------------------------------------------------------------------


def test_post_hook_fires_with_job_env(app_modules, monkeypatch):
    operations = app_modules.operations
    monkeypatch.setenv("SCHEDULE_AGENT_POST_HOOK", "/bin/echo hookran")

    captured: dict[str, dict] = {}

    class FakePopen:
        def __init__(self, argv, env=None, **kwargs):
            captured["argv"] = argv
            captured["env"] = env

    monkeypatch.setattr(operations.subprocess, "Popen", FakePopen)
    operations._fire_post_hook(
        {
            "id": "abc",
            "title": "t",
            "last_exit_code": 0,
            "last_log_file": "/tmp/x.log",
        },
        result="success",
    )
    assert captured["argv"] == ["/bin/echo", "hookran"]
    env = captured["env"]
    assert env["JOB_ID"] == "abc"
    assert env["JOB_RESULT"] == "success"
    assert env["JOB_LOG_FILE"] == "/tmp/x.log"


def test_input_char_accept_allows_paste_burst(app_modules):
    cli = app_modules.cli
    # Multi-char printable burst (a paste of a UUID-shaped session id).
    assert cli._input_char_accept("a1b2c3d4-5678-90ab-cdef-1234567890ab")
    # Single char works.
    assert cli._input_char_accept("x")
    # Empty is rejected.
    assert not cli._input_char_accept("")
    # Any control byte in the run causes rejection.
    assert not cli._input_char_accept("abc\n")


def test_sanitize_paste_strips_control_bytes(app_modules):
    cli = app_modules.cli
    pasted = "session-id-abc\n\t\x1bdef"
    assert cli._sanitize_paste(pasted) == "session-id-abcdef"


def test_post_hook_is_noop_when_env_unset(app_modules, monkeypatch):
    operations = app_modules.operations
    monkeypatch.delenv("SCHEDULE_AGENT_POST_HOOK", raising=False)

    def fail_popen(*args, **kwargs):
        raise AssertionError("Popen must not be called when hook is unset")

    monkeypatch.setattr(operations.subprocess, "Popen", fail_popen)
    operations._fire_post_hook({"id": "abc"}, result="success")
