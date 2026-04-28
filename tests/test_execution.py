from schedule_agent.execution import AGENTS, build_agent_cmd


def _job(agent, prompt_file="/tmp/p.md", session_mode="new", session_id=None, **extra):
    job = {
        "agent": agent,
        "prompt_file": prompt_file,
        "session_mode": session_mode,
        "session_id": session_id,
        "cwd": "/tmp",
        "log": "/tmp/log.txt",
    }
    job.update(extra)
    return job


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def test_codex_new_session():
    cmd = build_agent_cmd(_job("codex"))
    assert "codex" in cmd
    assert "exec" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "cat /tmp/p.md" in cmd
    assert "< /dev/null" in cmd
    assert "resume" not in cmd


def test_codex_resume_session():
    cmd = build_agent_cmd(_job("codex", session_mode="resume", session_id="sess-123"))
    assert "codex" in cmd
    assert "exec resume" in cmd
    assert "sess-123" in cmd
    assert "cat /tmp/p.md" in cmd
    assert "< /dev/null" in cmd


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------


def test_claude_new_session():
    cmd = build_agent_cmd(_job("claude"))
    assert "claude" in cmd
    assert "-p" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "cat /tmp/p.md" in cmd
    assert "< /dev/null" in cmd
    assert "--resume" not in cmd


def test_claude_resume_session():
    cmd = build_agent_cmd(_job("claude", session_mode="resume", session_id="sess-999"))
    assert "claude" in cmd
    assert "--resume" in cmd
    assert "sess-999" in cmd
    assert "-p" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "cat /tmp/p.md" in cmd
    assert "< /dev/null" in cmd


# ---------------------------------------------------------------------------
# Legacy model compatibility (job with "session" field instead of session_mode/session_id)
# ---------------------------------------------------------------------------


def test_legacy_session_field_codex_resume():
    job = {
        "agent": "codex",
        "prompt_file": "/tmp/p.md",
        "session": "legacy-sess",
        "cwd": "/tmp",
        "log": "/tmp/log.txt",
    }
    cmd = build_agent_cmd(job)
    assert "exec resume" in cmd
    assert "legacy-sess" in cmd


def test_legacy_no_session_field():
    job = {
        "agent": "claude",
        "prompt_file": "/tmp/p.md",
        "cwd": "/tmp",
        "log": "/tmp/log.txt",
    }
    cmd = build_agent_cmd(job)
    assert "--resume" not in cmd


# ---------------------------------------------------------------------------
# Path quoting
# ---------------------------------------------------------------------------


def test_prompt_path_with_spaces_is_quoted():
    cmd = build_agent_cmd(_job("claude", prompt_file="/tmp/my prompt/p.md"))
    assert "'/tmp/my prompt/p.md'" in cmd or '"/tmp/my prompt/p.md"' in cmd or "my\\ prompt" in cmd


def test_agents_config_has_required_keys():
    for name, cfg in AGENTS.items():
        assert "label" in cfg
        assert "bin" in cfg
        assert "base_args" in cfg


# ---------------------------------------------------------------------------
# Stream decoding (claude only)
# ---------------------------------------------------------------------------


def test_claude_uses_streaming_json_output():
    cmd = build_agent_cmd(_job("claude"))
    assert "--output-format stream-json" in cmd
    assert "--include-partial-messages" in cmd
    assert "--verbose" in cmd


def test_claude_pipes_through_stream_decoder():
    cmd = build_agent_cmd(_job("claude"))
    # Pipe must be after `< /dev/null` — stdin redirect needs to bind to
    # claude, not to the decoder.
    assert "< /dev/null" in cmd
    decode_idx = cmd.find("stream-decode")
    stdin_idx = cmd.find("< /dev/null")
    assert decode_idx > stdin_idx > 0
    assert "| " in cmd[stdin_idx:decode_idx]


def test_claude_resume_also_pipes_through_decoder():
    cmd = build_agent_cmd(_job("claude", session_mode="resume", session_id="sess-x"))
    assert "stream-decode" in cmd
    assert cmd.find("stream-decode") > cmd.find("< /dev/null")


def test_codex_does_not_pipe_through_stream_decoder():
    # Codex `exec` already streams plain text; piping it would just garble
    # the log. The decoder is claude-only.
    cmd = build_agent_cmd(_job("codex"))
    assert "stream-decode" not in cmd
    cmd = build_agent_cmd(_job("codex", session_mode="resume", session_id="s"))
    assert "stream-decode" not in cmd


def test_prompt_prefix_snapshot_is_prepended(tmp_path):
    # Prefix is now a per-job immutable snapshot written at submit time by
    # scheduler_backend; build_agent_cmd simply reads the path from
    # job["prefix_snapshot_file"] and cats it before the prompt.
    snapshot = tmp_path / "prefix.snapshot"
    snapshot.write_text("PREFIX TEXT\n", encoding="utf-8")
    cmd = build_agent_cmd(
        _job("claude", prompt_file="/tmp/p.md", prefix_snapshot_file=str(snapshot))
    )
    assert str(snapshot) in cmd
    assert cmd.index(str(snapshot)) < cmd.index("/tmp/p.md")


def test_prompt_prefix_absent_when_no_snapshot():
    # Legacy job records (pre-snapshot era) lack the field; build_agent_cmd
    # omits the prefix fragment entirely rather than referencing a file
    # that will never exist.
    cmd = build_agent_cmd(_job("claude", prompt_file="/tmp/p.md"))
    assert "prefix.snapshot" not in cmd
    assert "cat /tmp/p.md" in cmd
