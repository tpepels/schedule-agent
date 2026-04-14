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


def _make_new_job(
    job_id="job1",
    agent="claude",
    when="03:00 tomorrow",
    cwd="/tmp/project",
    log="/tmp/project/log.txt",
    prompt_file="/tmp/project/prompt.md",
    session_mode="new",
    session_id=None,
):
    from schedule_agent.transitions import make_job
    return make_job(
        job_id=job_id,
        agent=agent,
        session_mode=session_mode,
        session_id=session_id,
        prompt_file=prompt_file,
        when=when,
        cwd=cwd,
        log=log,
    )


class DummyProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# build_cmd (via build_agent_cmd)
# ---------------------------------------------------------------------------

def test_build_cmd_codex_new_session_contains_expected_flags_and_stdin_detach(cli_module):
    job = _make_new_job(agent="codex", prompt_file="/tmp/prompt.md")
    cmd = cli_module.build_cmd(job)
    assert "codex" in cmd
    assert "exec" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert '$(cat /tmp/prompt.md)' in cmd
    assert "< /dev/null" in cmd
    assert "resume" not in cmd


def test_build_cmd_codex_resume_session_contains_resume(cli_module):
    job = _make_new_job(agent="codex", prompt_file="/tmp/prompt.md", session_mode="resume", session_id="sess-123")
    cmd = cli_module.build_cmd(job)
    assert "exec resume" in cmd
    assert "sess-123" in cmd


def test_build_cmd_claude_new_session_contains_expected_flags_and_stdin_detach(cli_module):
    job = _make_new_job(agent="claude", prompt_file="/tmp/prompt.md")
    cmd = cli_module.build_cmd(job)
    assert "claude" in cmd
    assert "-p" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert '$(cat /tmp/prompt.md)' in cmd
    assert "< /dev/null" in cmd
    assert "--resume" not in cmd


def test_build_cmd_claude_resume_session_contains_resume(cli_module):
    job = _make_new_job(agent="claude", prompt_file="/tmp/prompt.md", session_mode="resume", session_id="sess-999")
    cmd = cli_module.build_cmd(job)
    assert "--resume" in cmd
    assert "sess-999" in cmd
    assert "-p" in cmd
    assert "--dangerously-skip-permissions" in cmd


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
def test_parse_at_job_id_handles_supported_and_unsupported_outputs(cli_module, stdout, expected):
    assert cli_module.parse_at_job_id(stdout) == expected


# ---------------------------------------------------------------------------
# schedule — dry run
# ---------------------------------------------------------------------------

def test_schedule_dry_run_returns_script_and_does_not_touch_subprocess(cli_module, monkeypatch):
    job = _make_new_job()
    calls = []

    monkeypatch.setattr(
        "schedule_agent.scheduler_backend.subprocess.run",
        lambda *a, **k: calls.append((a, k)) or (_ for _ in ()).throw(AssertionError("subprocess.run called in dry-run")),
    )

    output = cli_module.schedule(job, dry_run=True)

    assert "Would schedule at: 03:00 tomorrow" in output
    assert "cd /tmp/project" in output
    assert "export PATH=/usr/local/bin:/usr/bin:/bin" in output
    assert "< /dev/null" in output
    assert calls == []
    # Dry run must not mutate the job list
    assert cli_module.load_jobs() == []


# ---------------------------------------------------------------------------
# schedule — live
# ---------------------------------------------------------------------------

def test_schedule_calls_at_and_persists_scheduled_state(cli_module, monkeypatch):
    job = _make_new_job(agent="claude", prompt_file="/tmp/prompt.md", job_id="job-claude")
    cli_module.save_jobs([job])

    captured = {}

    def fake_run(cmd, input=None, text=None, capture_output=None):
        captured["cmd"] = cmd
        captured["input"] = input
        return DummyProc(returncode=0, stdout="job 42 at Tue Apr 14 12:00:00 2026\n")

    monkeypatch.setattr("schedule_agent.scheduler_backend.subprocess.run", fake_run)

    out = cli_module.schedule(job)

    assert captured["cmd"] == ["at", "03:00 tomorrow"]
    script = captured["input"]
    assert "cd /tmp/project" in script
    assert "export PATH=/usr/local/bin:/usr/bin:/bin" in script
    assert "claude -p --dangerously-skip-permissions" in script
    assert ">> /tmp/project/log.txt 2>&1" in script
    assert "job 42 at" in out

    # Job record should be updated to scheduled
    loaded = cli_module.load_jobs()
    j = next(x for x in loaded if x["id"] == "job-claude")
    assert j["submission"] == "scheduled"
    assert j["at_job_id"] == "42"

    # Legacy state file also updated
    state = cli_module.load_state()
    assert state["job-claude"]["status"] == "submitted"
    assert state["job-claude"]["at_job_id"] == "42"


def test_schedule_raises_on_at_failure_and_does_not_write_scheduled_state(cli_module, monkeypatch):
    job = _make_new_job(job_id="job-fail")
    cli_module.save_jobs([job])

    monkeypatch.setattr(
        "schedule_agent.scheduler_backend.subprocess.run",
        lambda *a, **k: DummyProc(returncode=1, stderr="Garbled time"),
    )

    with pytest.raises(RuntimeError, match="Garbled time"):
        cli_module.schedule(job)

    loaded = cli_module.load_jobs()
    j = next(x for x in loaded if x["id"] == "job-fail")
    assert j["submission"] == "queued"  # unchanged


# ---------------------------------------------------------------------------
# cancel_at_job
# ---------------------------------------------------------------------------

def test_cancel_at_job_returns_false_when_no_at_job_id_is_recorded(cli_module):
    job = _make_new_job(job_id="job1")
    cli_module.save_jobs([job])
    assert cli_module.cancel_at_job("job1") is False


def test_cancel_at_job_success_updates_state_and_drops_at_job_id(cli_module, monkeypatch):
    from schedule_agent.transitions import on_submit
    job = on_submit(_make_new_job(job_id="job1"), "55")
    cli_module.save_jobs([job])

    captured = {}

    def fake_run(cmd, capture_output=None, text=None):
        captured["cmd"] = cmd
        return DummyProc(returncode=0)

    monkeypatch.setattr("schedule_agent.scheduler_backend.subprocess.run", fake_run)

    assert cli_module.cancel_at_job("job1") is True
    assert captured["cmd"] == ["atrm", "55"]

    state = cli_module.load_state()
    assert state["job1"]["at_job_removed"] is True
    assert "at_job_id" not in state["job1"]

    loaded = cli_module.load_jobs()
    j = next(x for x in loaded if x["id"] == "job1")
    assert j["at_job_id"] is None


def test_cancel_at_job_failure_records_error_and_drops_at_job_id(cli_module, monkeypatch):
    from schedule_agent.transitions import on_submit
    job = on_submit(_make_new_job(job_id="job1"), "77")
    cli_module.save_jobs([job])

    monkeypatch.setattr(
        "schedule_agent.scheduler_backend.subprocess.run",
        lambda *a, **k: DummyProc(returncode=1, stderr="Cannot find jobid 77"),
    )

    assert cli_module.cancel_at_job("job1") is False

    state = cli_module.load_state()
    assert state["job1"]["at_job_removed"] is False
    assert state["job1"]["at_job_remove_error"] == "Cannot find jobid 77"
    assert "at_job_id" not in state["job1"]


# ---------------------------------------------------------------------------
# discover_sessions
# ---------------------------------------------------------------------------

def test_discover_sessions_codex_recurses_date_subdirs_and_returns_ten_newest(cli_module, tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    monkeypatch.setattr(cli_module.Path, "home", lambda: fake_home)

    # Create sessions in date-based subdirectory structure
    created = []
    for i in range(12):
        date_dir = fake_home / ".codex" / "sessions" / "2026" / "01" / f"{(i % 28) + 1:02d}"
        date_dir.mkdir(parents=True, exist_ok=True)
        p = date_dir / f"session-{i}.jsonl"
        p.write_text(f"session {i}", encoding="utf-8")
        created.append(p)

    for i, p in enumerate(created):
        ts = 1_700_000_000 + i
        __import__("os").utime(p, (ts, ts))

    discovered = cli_module.discover_sessions("codex")
    assert len(discovered) == 10
    assert all(hasattr(s, "id") and hasattr(s, "path") for s in discovered)
    assert discovered[0].path.name == "session-11.jsonl"
    assert discovered[-1].path.name == "session-2.jsonl"
    assert all(s.agent == "codex" for s in discovered)


def test_discover_sessions_codex_only_includes_jsonl_files(cli_module, tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    monkeypatch.setattr(cli_module.Path, "home", lambda: fake_home)

    session_dir = fake_home / ".codex" / "sessions" / "2026" / "01" / "01"
    session_dir.mkdir(parents=True)
    (session_dir / "session-a.jsonl").write_text("{}", encoding="utf-8")
    (session_dir / "metadata.json").write_text("{}", encoding="utf-8")
    (session_dir / "notes.txt").write_text("notes", encoding="utf-8")

    discovered = cli_module.discover_sessions("codex")
    assert len(discovered) == 1
    assert discovered[0].path.name == "session-a.jsonl"


def test_discover_sessions_returns_empty_when_root_missing(cli_module, tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(cli_module.Path, "home", lambda: fake_home)
    assert cli_module.discover_sessions("codex") == []
    assert cli_module.discover_sessions("claude") == []


def test_discover_sessions_claude_prefers_current_project(cli_module, tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    monkeypatch.setattr(cli_module.Path, "home", lambda: fake_home)

    cwd = Path("/home/tom/Projects/myproject")
    project_dir_name = cwd.as_posix().replace("/", "-")

    # Create sessions for the current project
    current_proj = fake_home / ".claude" / "projects" / project_dir_name
    current_proj.mkdir(parents=True)
    for i in range(3):
        p = current_proj / f"session-current-{i}.jsonl"
        p.write_text("{}", encoding="utf-8")
        __import__("os").utime(p, (1_700_000_000 - i, 1_700_000_000 - i))

    # Create sessions for another project with newer mtimes
    other_proj = fake_home / ".claude" / "projects" / "-home-tom-Projects-other"
    other_proj.mkdir(parents=True)
    for i in range(3):
        p = other_proj / f"session-other-{i}.jsonl"
        p.write_text("{}", encoding="utf-8")
        __import__("os").utime(p, (1_800_000_000 + i, 1_800_000_000 + i))

    discovered = cli_module.discover_sessions("claude", cwd=cwd)
    # Current project sessions must come first despite older mtimes
    ids = [s.id for s in discovered]
    current_ids = [s for s in ids if "current" in s]
    other_ids = [s for s in ids if "other" in s]
    assert len(current_ids) == 3
    assert ids.index(current_ids[0]) < ids.index(other_ids[0])


def test_discover_sessions_claude_excludes_subagent_files(cli_module, tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    monkeypatch.setattr(cli_module.Path, "home", lambda: fake_home)

    cwd = Path("/home/tom/Projects/myproject")
    project_dir_name = cwd.as_posix().replace("/", "-")
    project_dir = fake_home / ".claude" / "projects" / project_dir_name
    project_dir.mkdir(parents=True)

    # Top-level session
    (project_dir / "abc123.jsonl").write_text("{}", encoding="utf-8")
    # Subagent file that should be excluded
    subagent_dir = project_dir / "abc123" / "subagents"
    subagent_dir.mkdir(parents=True)
    (subagent_dir / "agent-xyz.jsonl").write_text("{}", encoding="utf-8")

    discovered = cli_module.discover_sessions("claude", cwd=cwd)
    assert len(discovered) == 1
    assert discovered[0].id == "abc123"


# ---------------------------------------------------------------------------
# extract_session_title
# ---------------------------------------------------------------------------

def test_extract_session_title_claude_returns_ai_title(cli_module, tmp_path):
    f = tmp_path / "session.jsonl"
    f.write_text(
        '{"type": "user", "content": "hello"}\n'
        '{"type": "ai-title", "aiTitle": "My Session Title"}\n',
        encoding="utf-8",
    )
    assert cli_module.extract_session_title(str(f), "claude") == "My Session Title"


def test_extract_session_title_claude_returns_none_when_no_ai_title(cli_module, tmp_path):
    f = tmp_path / "session.jsonl"
    f.write_text('{"type": "user", "content": "hello"}\n', encoding="utf-8")
    assert cli_module.extract_session_title(str(f), "claude") is None


def test_extract_session_title_claude_ignores_user_content(cli_module, tmp_path):
    f = tmp_path / "session.jsonl"
    # Has user content but no ai-title
    f.write_text(
        '{"type": "user", "content": "please do not use this as a title"}\n',
        encoding="utf-8",
    )
    assert cli_module.extract_session_title(str(f), "claude") is None


def test_extract_session_title_codex_returns_first_user_message(cli_module, tmp_path):
    import json as _json
    f = tmp_path / "session.jsonl"
    lines = [
        _json.dumps({"type": "session_meta", "payload": {"type": "init"}}),
        _json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "Build me a feature\nWith more details"}}),
    ]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert cli_module.extract_session_title(str(f), "codex") == "Build me a feature"


def test_extract_session_title_codex_returns_none_when_no_user_message(cli_module, tmp_path):
    import json as _json
    f = tmp_path / "session.jsonl"
    lines = [
        _json.dumps({"type": "session_meta", "payload": {"type": "init"}}),
        _json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "Hi"}}),
    ]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert cli_module.extract_session_title(str(f), "codex") is None


def test_extract_session_title_returns_none_for_missing_file(cli_module):
    assert cli_module.extract_session_title("/nonexistent/path.jsonl", "claude") is None
    assert cli_module.extract_session_title("/nonexistent/path.jsonl", "codex") is None


def test_extract_session_title_returns_none_for_unknown_agent(cli_module, tmp_path):
    f = tmp_path / "session.jsonl"
    f.write_text('{"type": "ai-title", "aiTitle": "Title"}\n', encoding="utf-8")
    assert cli_module.extract_session_title(str(f), "unknown") is None


# ---------------------------------------------------------------------------
# choose_session
# ---------------------------------------------------------------------------

def test_choose_session_shows_no_title_label_when_title_missing(cli_module, tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    monkeypatch.setattr(cli_module.Path, "home", lambda: fake_home)

    session_dir = fake_home / ".codex" / "sessions" / "2026" / "01" / "01"
    session_dir.mkdir(parents=True)
    p = session_dir / "abcdef12-1234-1234-1234-abcdef123456.jsonl"
    p.write_text("{}", encoding="utf-8")  # no title content

    labels_seen = []

    def fake_choose(msg, choices, default=None):
        if msg == "Session":
            labels_seen.extend(choices)
        return "New session"

    monkeypatch.setattr(cli_module, "choose", fake_choose)
    cli_module.choose_session("codex")

    assert any("[no title]" in label for label in labels_seen)
    assert any("abcdef12" in label for label in labels_seen)


def test_choose_session_shows_title_when_present(cli_module, tmp_path, monkeypatch):
    import json as _json
    fake_home = tmp_path / "home"
    monkeypatch.setattr(cli_module.Path, "home", lambda: fake_home)

    session_dir = fake_home / ".codex" / "sessions" / "2026" / "01" / "01"
    session_dir.mkdir(parents=True)
    p = session_dir / "abcdef12-1234-1234-1234-abcdef123456.jsonl"
    p.write_text(
        _json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "Fix the login bug"}}) + "\n",
        encoding="utf-8",
    )

    labels_seen = []

    def fake_choose(msg, choices, default=None):
        if msg == "Session":
            labels_seen.extend(choices)
        return "New session"

    monkeypatch.setattr(cli_module, "choose", fake_choose)
    cli_module.choose_session("codex")

    assert any("Fix the login bug" in label for label in labels_seen)


# ---------------------------------------------------------------------------
# external JSON format validation
# ---------------------------------------------------------------------------

def test_real_codex_session_format_matches_expected_layout(cli_module):
    import json as _json
    codex_root = Path.home() / ".codex" / "sessions"
    if not codex_root.exists():
        import pytest
        pytest.skip("No codex sessions directory found")

    files = list(codex_root.rglob("*.jsonl"))
    if not files:
        import pytest
        pytest.skip("No codex session files found")

    # Check at most 5 recent files
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[:5]:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = _json.loads(line)
                assert "type" in obj, f"Missing 'type' key in {path}"
                # Verify event_msg user_message structure
                if obj.get("type") == "event_msg":
                    assert "payload" in obj, f"event_msg missing 'payload' in {path}"
                    payload = obj["payload"]
                    assert "type" in payload, f"event_msg payload missing 'type' in {path}"
                    if payload.get("type") == "user_message":
                        assert "message" in payload, f"user_message payload missing 'message' in {path}"
                break  # Only check first valid line per file


def test_real_claude_session_format_matches_expected_layout(cli_module):
    import json as _json
    claude_root = Path.home() / ".claude" / "projects"
    if not claude_root.exists():
        import pytest
        pytest.skip("No claude projects directory found")

    files = []
    for proj in claude_root.iterdir():
        if proj.is_dir():
            files.extend(proj.glob("*.jsonl"))

    if not files:
        import pytest
        pytest.skip("No claude session files found")

    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[:5]:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = _json.loads(line)
                assert "type" in obj, f"Missing 'type' key in {path}"
                if obj.get("type") == "ai-title":
                    assert "aiTitle" in obj, f"ai-title record missing 'aiTitle' in {path}"
                break  # Only check first valid line per file


# ---------------------------------------------------------------------------
# create_job
# ---------------------------------------------------------------------------

def test_create_job_writes_prompt_file_and_uses_interaction_results(cli_module, monkeypatch, tmp_path):
    monkeypatch.setattr(cli_module, "choose", lambda msg, choices, default=None: "Codex" if msg == "Agent" else choices[0])
    monkeypatch.setattr(cli_module, "choose_session", lambda agent, cwd=None: "sess-123")
    monkeypatch.setattr(cli_module, "read_prompt", lambda: "hello prompt")
    monkeypatch.setattr(cli_module, "resolve_time", lambda: "now + 15 minutes")
    monkeypatch.setattr(cli_module, "info", lambda msg: None)
    monkeypatch.setattr(cli_module.Path, "cwd", lambda: tmp_path)

    job = cli_module.create_job()

    assert job["agent"] == "codex"
    assert job["session_mode"] == "resume"
    assert job["session_id"] == "sess-123"
    assert job["when"] == "now + 15 minutes"
    assert job["cwd"] == str(tmp_path)
    assert Path(job["prompt_file"]).exists()
    assert Path(job["prompt_file"]).read_text(encoding="utf-8").startswith("hello prompt")


# ---------------------------------------------------------------------------
# prepare_mutation
# ---------------------------------------------------------------------------

def test_prepare_mutation_cancels_only_scheduled_jobs(cli_module, monkeypatch):
    from schedule_agent.transitions import make_job, on_submit
    scheduled = on_submit(_make_new_job(job_id="scheduled-job"), "12")
    queued = _make_new_job(job_id="queued-job")
    cli_module.save_jobs([scheduled, queued])

    called = []
    monkeypatch.setattr(cli_module, "cancel_at_job", lambda jid: called.append(jid) or True)

    assert cli_module.prepare_mutation("scheduled-job") is True
    assert cli_module.prepare_mutation("queued-job") is False
    assert called == ["scheduled-job"]


# ---------------------------------------------------------------------------
# apply_job_update — queued path
# ---------------------------------------------------------------------------

def test_apply_job_update_updates_queued_job_without_resubmitting(cli_module, monkeypatch):
    prompt_path = Path(cli_module.write_prompt_file("job1", "prompt"))
    job = _make_new_job(job_id="job1", when="now + 5 minutes", prompt_file=str(prompt_path))
    cli_module.save_jobs([job])

    scheduled = []
    monkeypatch.setattr(cli_module, "schedule", lambda j, dry_run=False: scheduled.append(j) or "scheduled")

    from schedule_agent.transitions import on_reschedule
    rc = cli_module.apply_job_update(
        "job1",
        lambda d: on_reschedule(d, "03:00 tomorrow"),
        success_message="updated",
        interactive=False,
    )

    assert rc == 0
    assert scheduled == []
    loaded = cli_module.load_jobs()
    assert loaded[0]["when"] == "03:00 tomorrow"
    assert loaded[0]["submission"] == "queued"


# ---------------------------------------------------------------------------
# apply_job_update — scheduled path (resubmit)
# ---------------------------------------------------------------------------

def test_apply_job_update_resubmits_previously_scheduled_job(cli_module, monkeypatch):
    from schedule_agent.transitions import make_job, on_submit
    prompt_path = Path(cli_module.write_prompt_file("job1", "prompt"))
    job = on_submit(_make_new_job(job_id="job1", when="now + 5 minutes", prompt_file=str(prompt_path)), "12")
    cli_module.save_jobs([job])

    cancelled = []
    scheduled = []

    monkeypatch.setattr(cli_module, "cancel_at_job", lambda jid: cancelled.append(jid) or True)

    def fake_schedule(updated, dry_run=False):
        scheduled.append(updated.copy())
        from schedule_agent.transitions import on_submit as _on_submit
        updated_sched = _on_submit(updated, "99")
        jobs = cli_module.load_jobs()
        from schedule_agent.persistence import find_job, update_job_in_list
        idx, _ = find_job(jobs, updated["id"])
        jobs[idx] = updated_sched
        cli_module.save_jobs(jobs)
        return "job 99 at Tue Apr 14 12:00:00 2026"

    monkeypatch.setattr(cli_module, "schedule", fake_schedule)

    from schedule_agent.transitions import on_reschedule
    rc = cli_module.apply_job_update(
        "job1",
        lambda d: on_reschedule(d, "03:00 tomorrow"),
        success_message="updated",
        interactive=False,
    )

    assert rc == 0
    assert cancelled == ["job1"]
    assert len(scheduled) == 1
    assert scheduled[0]["when"] == "03:00 tomorrow"

    loaded = cli_module.load_jobs()
    j = next(x for x in loaded if x["id"] == "job1")
    assert j["submission"] == "scheduled"
    assert j["at_job_id"] == "99"


# ---------------------------------------------------------------------------
# apply_job_update — resubmit fails
# ---------------------------------------------------------------------------

def test_apply_job_update_falls_back_to_queued_when_resubmit_fails(cli_module, monkeypatch, capsys):
    from schedule_agent.transitions import on_submit
    prompt_path = Path(cli_module.write_prompt_file("job1", "prompt"))
    job = on_submit(_make_new_job(job_id="job1", when="now + 5 minutes", prompt_file=str(prompt_path)), "12")
    cli_module.save_jobs([job])

    monkeypatch.setattr(cli_module, "cancel_at_job", lambda jid: True)
    monkeypatch.setattr(cli_module, "schedule", lambda j, dry_run=False: (_ for _ in ()).throw(RuntimeError("Garbled time")))

    from schedule_agent.transitions import on_reschedule
    rc = cli_module.apply_job_update(
        "job1",
        lambda d: on_reschedule(d, "03:00 tomorrow"),
        success_message="updated",
        interactive=False,
    )

    assert rc == 1
    loaded = cli_module.load_jobs()
    j = next(x for x in loaded if x["id"] == "job1")
    assert j["submission"] == "queued"

    out = capsys.readouterr().out
    assert "Garbled time" in out
    assert "remains queued" in out


# ---------------------------------------------------------------------------
# apply_job_update — delete
# ---------------------------------------------------------------------------

def test_apply_job_update_delete_removes_job_state_and_prompt_file(cli_module):
    prompt_path = Path(cli_module.write_prompt_file("job1", "prompt"))
    job = _make_new_job(job_id="job1", prompt_file=str(prompt_path))
    cli_module.save_jobs([job])
    cli_module.set_state("job1", "queued")

    rc = cli_module.apply_job_update("job1", lambda d: None, success_message="Deleted.", interactive=False)

    assert rc == 0
    assert cli_module.load_jobs() == []
    assert cli_module.load_state() == {}
    assert not prompt_path.exists()


# ---------------------------------------------------------------------------
# CLI show
# ---------------------------------------------------------------------------

def test_cli_show_job_prints_details_and_returns_zero(cli_module, capsys):
    prompt_path = Path(cli_module.write_prompt_file("job1", "prompt"))
    job = _make_new_job(job_id="job1", prompt_file=str(prompt_path))
    cli_module.save_jobs([job])

    rc = cli_module.cli_show_job("job1")
    assert rc == 0
    out = capsys.readouterr().out
    assert "id:            job1" in out
    assert f"prompt_file:   {prompt_path}" in out


def test_cli_show_job_returns_one_for_missing_job(cli_module, capsys):
    rc = cli_module.cli_show_job("missing")
    assert rc == 1
    assert "No such job." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# CLI reschedule
# ---------------------------------------------------------------------------

def test_cli_reschedule_job_updates_time(cli_module, capsys):
    prompt_path = Path(cli_module.write_prompt_file("job1", "prompt"))
    job = _make_new_job(job_id="job1", when="now + 5 minutes", prompt_file=str(prompt_path))
    cli_module.save_jobs([job])

    rc = cli_module.cli_reschedule_job("job1", "03:00 tomorrow")
    assert rc == 0
    assert cli_module.load_jobs()[0]["when"] == "03:00 tomorrow"
    assert "Rescheduled job1 from now + 5 minutes to 03:00 tomorrow." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# CLI change session
# ---------------------------------------------------------------------------

def test_cli_change_session_sets_specific_session(cli_module, capsys):
    prompt_path = Path(cli_module.write_prompt_file("job1", "prompt"))
    job = _make_new_job(job_id="job1", prompt_file=str(prompt_path))
    cli_module.save_jobs([job])

    rc = cli_module.cli_change_session("job1", "sess-abc")
    assert rc == 0
    loaded = cli_module.load_jobs()[0]
    assert loaded["session_id"] == "sess-abc"
    assert loaded["session_mode"] == "resume"
    assert "Changed session for job1 to sess-abc." in capsys.readouterr().out


def test_cli_change_session_can_clear_to_new(cli_module, capsys):
    prompt_path = Path(cli_module.write_prompt_file("job1", "prompt"))
    job = _make_new_job(job_id="job1", prompt_file=str(prompt_path), session_mode="resume", session_id="old-sess")
    cli_module.save_jobs([job])

    rc = cli_module.cli_change_session("job1", None)
    assert rc == 0
    loaded = cli_module.load_jobs()[0]
    assert loaded["session_id"] is None
    assert loaded["session_mode"] == "new"
    assert "Changed session for job1 to new." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# remove_job non-interactive
# ---------------------------------------------------------------------------

def test_remove_job_noninteractive_deletes_without_prompt(cli_module, capsys):
    prompt_path = Path(cli_module.write_prompt_file("job1", "prompt"))
    job = _make_new_job(job_id="job1", prompt_file=str(prompt_path))
    cli_module.save_jobs([job])

    rc = cli_module.remove_job("job1", interactive=False)
    assert rc == 0
    assert cli_module.load_jobs() == []
    assert not prompt_path.exists()
    assert "Deleted." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main subcommands
# ---------------------------------------------------------------------------

def test_main_list_subcommand_prints_jobs(cli_module, capsys):
    prompt_path = Path(cli_module.write_prompt_file("job1", "prompt"))
    job = _make_new_job(job_id="job1", prompt_file=str(prompt_path))
    cli_module.save_jobs([job])
    rc = cli_module.main(["list"])
    assert rc == 0
    assert "job1" in capsys.readouterr().out


def test_main_show_subcommand_dispatches(cli_module, monkeypatch):
    called = {}
    monkeypatch.setattr(cli_module, "cli_show_job", lambda jid: called.update({"job_id": jid}) or 0)
    rc = cli_module.main(["show", "job1"])
    assert rc == 0
    assert called["job_id"] == "job1"


def test_main_delete_subcommand_dispatches(cli_module, monkeypatch):
    called = {}
    monkeypatch.setattr(cli_module, "remove_job", lambda jid, interactive=False: called.update({"job_id": jid, "interactive": interactive}) or 0)
    rc = cli_module.main(["delete", "job1"])
    assert rc == 0
    assert called == {"job_id": "job1", "interactive": False}


def test_main_reschedule_subcommand_dispatches(cli_module, monkeypatch):
    called = {}
    monkeypatch.setattr(cli_module, "cli_reschedule_job", lambda jid, when: called.update({"job_id": jid, "when": when}) or 0)
    rc = cli_module.main(["reschedule", "job1", "03:00 tomorrow"])
    assert rc == 0
    assert called == {"job_id": "job1", "when": "03:00 tomorrow"}


def test_main_session_subcommand_dispatches_with_specific_session(cli_module, monkeypatch):
    called = {}
    monkeypatch.setattr(cli_module, "cli_change_session", lambda jid, session: called.update({"job_id": jid, "session": session}) or 0)
    rc = cli_module.main(["session", "job1", "sess-1"])
    assert rc == 0
    assert called == {"job_id": "job1", "session": "sess-1"}


def test_main_session_subcommand_dispatches_with_new(cli_module, monkeypatch):
    called = {}
    monkeypatch.setattr(cli_module, "cli_change_session", lambda jid, session: called.update({"job_id": jid, "session": session}) or 0)
    rc = cli_module.main(["session", "job1", "--new"])
    assert rc == 0
    assert called == {"job_id": "job1", "session": None}


def test_main_retry_subcommand_dispatches(cli_module, monkeypatch):
    called = {}
    monkeypatch.setattr(cli_module, "cli_retry_job", lambda jid: called.update({"job_id": jid}) or 0)
    rc = cli_module.main(["retry", "job1"])
    assert rc == 0
    assert called["job_id"] == "job1"


def test_main_mark_running_dispatches(cli_module, monkeypatch):
    called = {}
    monkeypatch.setattr(cli_module, "cli_mark_running", lambda jid: called.update({"job_id": jid}) or 0)
    rc = cli_module.main(["mark", "running", "job1"])
    assert rc == 0
    assert called["job_id"] == "job1"


def test_main_mark_done_dispatches(cli_module, monkeypatch):
    called = {}
    monkeypatch.setattr(cli_module, "cli_mark_done", lambda jid: called.update({"job_id": jid}) or 0)
    rc = cli_module.main(["mark", "done", "job1"])
    assert rc == 0
    assert called["job_id"] == "job1"


def test_main_mark_failed_dispatches(cli_module, monkeypatch):
    called = {}
    monkeypatch.setattr(cli_module, "cli_mark_failed", lambda jid: called.update({"job_id": jid}) or 0)
    rc = cli_module.main(["mark", "failed", "job1"])
    assert rc == 0
    assert called["job_id"] == "job1"


def test_main_dry_run_uses_interactive_create_path(cli_module, monkeypatch):
    monkeypatch.setattr(cli_module, "choose", lambda msg, choices, default=None: "Create job")
    called = {}
    monkeypatch.setattr(cli_module, "create_and_maybe_submit", lambda dry_run=False: called.update({"dry_run": dry_run}) or 0)
    rc = cli_module.main(["--dry-run"])
    assert rc == 0
    assert called["dry_run"] is True
