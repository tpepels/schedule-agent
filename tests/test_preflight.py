from __future__ import annotations

import subprocess
from types import SimpleNamespace

from schedule_agent import preflight
from schedule_agent.environment import AgentProbe


def _make_probe(
    *,
    agent: str = "claude",
    resolved_path: str | None = "/usr/bin/claude",
    version: str | None = "2.1.112",
    version_known_good: bool = True,
    help_ok: bool = True,
    error: str | None = None,
) -> AgentProbe:
    return AgentProbe(
        agent=agent,
        resolved_path=resolved_path,
        version=version,
        version_known_good=version_known_good,
        help_ok=help_ok,
        error=error,
    )


# check_at_binary -----------------------------------------------------------


def test_check_at_binary_all_present(monkeypatch):
    mapping = {"at": "/usr/bin/at", "atrm": "/usr/bin/atrm", "atq": "/usr/bin/atq"}
    monkeypatch.setattr(preflight.shutil, "which", lambda n: mapping.get(n))
    result = preflight.check_at_binary()
    assert result.severity == "PASS"
    assert "/usr/bin/at" in result.message


def test_check_at_binary_one_missing(monkeypatch):
    mapping = {"at": "/usr/bin/at", "atrm": None, "atq": "/usr/bin/atq"}
    monkeypatch.setattr(preflight.shutil, "which", lambda n: mapping.get(n))
    result = preflight.check_at_binary()
    assert result.severity == "FAIL"
    assert "atrm" in result.message


def test_check_at_binary_all_missing(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda n: None)
    result = preflight.check_at_binary()
    assert result.severity == "FAIL"
    assert "at" in result.message and "atrm" in result.message and "atq" in result.message


# check_atd_active ----------------------------------------------------------


def test_check_atd_active_systemctl_missing(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda n: None)
    result = preflight.check_atd_active()
    assert result.severity == "SKIP"
    assert "systemctl" in result.message


def test_check_atd_active_pass(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda n: "/usr/bin/systemctl")

    def fake_run(cmd, timeout=5):
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)
    result = preflight.check_atd_active()
    assert result.severity == "PASS"


def test_check_atd_active_fail(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda n: "/usr/bin/systemctl")

    def fake_run(cmd, timeout=5):
        return SimpleNamespace(returncode=3)

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)
    result = preflight.check_atd_active()
    assert result.severity == "FAIL"
    assert "atd" in result.message


def test_check_atd_active_exception(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda n: "/usr/bin/systemctl")

    def fake_run(cmd, timeout=5):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)
    result = preflight.check_atd_active()
    assert result.severity == "FAIL"


# check_xdg_dirs ------------------------------------------------------------


def test_check_xdg_dirs_pass(monkeypatch, tmp_path):
    state = tmp_path / "state"
    data = tmp_path / "data"
    prompt = tmp_path / "prompt"
    logs = tmp_path / "logs"
    queue = tmp_path / "queue"
    for p in (state, data, prompt, logs):
        p.mkdir()
    monkeypatch.setattr(preflight, "_ensure_dirs", lambda: (state, data, prompt, logs, queue))
    result = preflight.check_xdg_dirs()
    assert result.severity == "PASS"


def test_check_xdg_dirs_one_readonly(monkeypatch, tmp_path):
    state = tmp_path / "state"
    data = tmp_path / "data"
    prompt = tmp_path / "prompt"
    logs = tmp_path / "logs"
    queue = tmp_path / "queue"
    for p in (state, data, prompt, logs):
        p.mkdir()
    prompt.chmod(0o500)
    monkeypatch.setattr(preflight, "_ensure_dirs", lambda: (state, data, prompt, logs, queue))
    try:
        result = preflight.check_xdg_dirs()
        assert result.severity == "FAIL"
        assert "prompt" in result.message
    finally:
        prompt.chmod(0o700)


def test_check_xdg_dirs_ensure_raises(monkeypatch):
    def boom():
        raise RuntimeError("disk full")

    monkeypatch.setattr(preflight, "_ensure_dirs", boom)
    result = preflight.check_xdg_dirs()
    assert result.severity == "FAIL"
    assert "disk full" in result.message


# check_agent ---------------------------------------------------------------


def test_check_agent_binary_missing():
    probe = _make_probe(resolved_path=None, version=None, version_known_good=False, help_ok=False)
    result = preflight.check_agent("claude", probe=probe)
    assert result.severity == "FAIL"
    assert "not found" in result.message


def test_check_agent_help_fail():
    probe = _make_probe(help_ok=False, error="boom")
    result = preflight.check_agent("claude", probe=probe)
    assert result.severity == "FAIL"
    assert "boom" in result.message


def test_check_agent_version_none():
    probe = _make_probe(version=None, version_known_good=False)
    result = preflight.check_agent("claude", probe=probe)
    assert result.severity == "WARN"
    assert "parse version" in result.message


def test_check_agent_version_unknown():
    probe = _make_probe(version="9.9.9", version_known_good=False)
    result = preflight.check_agent("claude", probe=probe)
    assert result.severity == "WARN"
    assert "9.9.9" in result.message
    assert "known-good" in result.message


def test_check_agent_happy_path():
    probe = _make_probe()
    result = preflight.check_agent("claude", probe=probe)
    assert result.severity == "PASS"
    assert "2.1.112" in result.message
    assert result.detail["resolved_path"] == "/usr/bin/claude"


def test_check_agent_calls_probe_when_none(monkeypatch):
    called = {}

    def fake_probe(agent):
        called["agent"] = agent
        return _make_probe()

    monkeypatch.setattr(preflight, "probe_agent", fake_probe)
    result = preflight.check_agent("codex")
    assert called["agent"] == "codex"
    assert result.severity == "PASS"


# check_session_dir ---------------------------------------------------------


def test_check_session_dir_skip_on_fail(monkeypatch):
    result = preflight.check_session_dir("claude", "FAIL")
    assert result.severity == "SKIP"
    assert "agent probe" in result.message


def test_check_session_dir_missing(monkeypatch, tmp_path):
    missing = tmp_path / "nope"
    monkeypatch.setitem(preflight._SESSION_DIRS, "claude", missing)
    result = preflight.check_session_dir("claude", "PASS")
    assert result.severity == "SKIP"
    assert "does not exist" in result.message


def test_check_session_dir_empty(monkeypatch, tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    monkeypatch.setitem(preflight._SESSION_DIRS, "claude", d)
    result = preflight.check_session_dir("claude", "PASS")
    assert result.severity == "WARN"
    assert "no session" in result.message


def test_check_session_dir_with_jsonl(monkeypatch, tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    (d / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (d / "b.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setitem(preflight._SESSION_DIRS, "claude", d)
    result = preflight.check_session_dir("claude", "PASS")
    assert result.severity == "PASS"
    assert "2" in result.message


# check_at_roundtrip --------------------------------------------------------


class _FakeProc:
    def __init__(self, returncode=0, stderr="", stdout=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def test_check_at_roundtrip_success(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "at":
            return _FakeProc(returncode=0, stderr="job 42 at Mon Apr 20 00:00:00 2026\n")
        if cmd[0] == "atrm":
            return _FakeProc(returncode=0)
        raise AssertionError(cmd)

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)
    result = preflight.check_at_roundtrip()
    assert result.severity == "PASS"
    assert "42" in result.message
    assert any(c[0] == "atrm" and "42" in c for c in calls)


def test_check_at_roundtrip_submit_fails(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "at":
            return _FakeProc(returncode=1, stderr="at: no atd running\n")
        if cmd[0] == "atrm":
            return _FakeProc(returncode=0)
        raise AssertionError(cmd)

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)
    result = preflight.check_at_roundtrip()
    assert result.severity == "FAIL"
    # atrm should NOT be called because we never got a job id
    assert all(c[0] != "atrm" for c in calls)


def test_check_at_roundtrip_parse_fails(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "at":
            return _FakeProc(returncode=0, stderr="unexpected output\n")
        if cmd[0] == "atrm":
            return _FakeProc(returncode=0)
        raise AssertionError(cmd)

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)
    result = preflight.check_at_roundtrip()
    assert result.severity == "FAIL"
    assert "parse" in result.message.lower()


def test_check_at_roundtrip_exception(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("at")

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)
    result = preflight.check_at_roundtrip()
    assert result.severity == "FAIL"


# run_checks ----------------------------------------------------------------


def _cr(name, severity="PASS"):
    return preflight.CheckResult(name=name, label=name, severity=severity, message=name)


def test_run_checks_order_and_no_roundtrip(monkeypatch):
    monkeypatch.setattr(preflight, "check_at_binary", lambda: _cr("at_binary"))
    monkeypatch.setattr(preflight, "check_atd_active", lambda: _cr("atd_active"))
    monkeypatch.setattr(preflight, "check_xdg_dirs", lambda: _cr("xdg_dirs"))

    agent_results = {
        "claude": _cr("agent_claude", "PASS"),
        "codex": _cr("agent_codex", "FAIL"),
    }
    monkeypatch.setattr(preflight, "check_agent", lambda a: agent_results[a])

    session_calls = []

    def fake_session(agent, sev):
        session_calls.append((agent, sev))
        return _cr(f"session_dir_{agent}")

    monkeypatch.setattr(preflight, "check_session_dir", fake_session)
    monkeypatch.setattr(preflight, "check_at_roundtrip", lambda: _cr("at_roundtrip"))

    report = preflight.run_checks()
    names = [r.name for r in report.results]
    assert names == [
        "at_binary",
        "atd_active",
        "xdg_dirs",
        "agent_claude",
        "agent_codex",
        "session_dir_claude",
        "session_dir_codex",
    ]
    # claude agent PASS → session dir called with PASS; codex FAIL → called with FAIL
    assert session_calls == [("claude", "PASS"), ("codex", "FAIL")]


def test_run_checks_include_roundtrip(monkeypatch):
    monkeypatch.setattr(preflight, "check_at_binary", lambda: _cr("at_binary"))
    monkeypatch.setattr(preflight, "check_atd_active", lambda: _cr("atd_active"))
    monkeypatch.setattr(preflight, "check_xdg_dirs", lambda: _cr("xdg_dirs"))
    monkeypatch.setattr(preflight, "check_agent", lambda a: _cr(f"agent_{a}"))
    monkeypatch.setattr(preflight, "check_session_dir", lambda a, s: _cr(f"session_dir_{a}"))
    monkeypatch.setattr(preflight, "check_at_roundtrip", lambda: _cr("at_roundtrip"))

    report = preflight.run_checks(include_roundtrip=True)
    assert report.results[-1].name == "at_roundtrip"
    assert len(report.results) == 8


def test_preflight_report_methods():
    results = [
        _cr("a", "PASS"),
        _cr("b", "WARN"),
        _cr("c", "FAIL"),
        _cr("d", "SKIP"),
        _cr("e", "FAIL"),
    ]
    report = preflight.PreflightReport(results=results)
    assert [r.name for r in report.critical_failures()] == ["c", "e"]
    assert [r.name for r in report.warnings()] == ["b"]
    assert [r.name for r in report.all()] == ["a", "b", "c", "d", "e"]
    assert report.critical_ok() is False

    clean = preflight.PreflightReport(results=[_cr("a", "PASS"), _cr("b", "WARN")])
    assert clean.critical_ok() is True
