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


def test_prompt_prefix_is_prepended(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cmd = build_agent_cmd(_job("claude", prompt_file="/tmp/p.md"))
    # prefix file path for claude should appear before the prompt cat
    prefix_token = "prompt-prefix-claude.md"
    assert prefix_token in cmd
    assert cmd.index(prefix_token) < cmd.index("/tmp/p.md")
