from __future__ import annotations

import os
import subprocess

import pytest

from schedule_agent import environment
from schedule_agent.environment import (
    AgentProbe,
    capture_path,
    probe_agent,
)


class _Proc:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_capture_path_drops_empty_entries(tmp_path, monkeypatch):
    d = tmp_path / "bin"
    d.mkdir()
    raw = f"{d}{os.pathsep}{os.pathsep}{d}"
    monkeypatch.setattr(environment, "PATH_FLOOR", [])
    result = capture_path(raw)
    assert result == [str(d)]


def test_capture_path_drops_relative_entries(tmp_path, monkeypatch):
    d = tmp_path / "abs"
    d.mkdir()
    raw = os.pathsep.join(["./bin", "bin", "../foo", str(d)])
    monkeypatch.setattr(environment, "PATH_FLOOR", [])
    result = capture_path(raw)
    assert result == [str(d)]


def test_capture_path_drops_nonexistent_dirs(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    missing = tmp_path / "missing"
    raw = os.pathsep.join([str(missing), str(real)])
    monkeypatch.setattr(environment, "PATH_FLOOR", [])
    result = capture_path(raw)
    assert result == [str(real)]


def test_capture_path_dedup_preserves_order(tmp_path, monkeypatch):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    raw = os.pathsep.join([str(a), str(b), str(a), str(b)])
    monkeypatch.setattr(environment, "PATH_FLOOR", [])
    result = capture_path(raw)
    assert result == [str(a), str(b)]


def test_capture_path_appends_floor_without_duplicates(tmp_path, monkeypatch):
    floor1 = tmp_path / "floor1"
    floor2 = tmp_path / "floor2"
    floor1.mkdir()
    floor2.mkdir()
    monkeypatch.setattr(environment, "PATH_FLOOR", [str(floor1), str(floor2)])
    raw = str(floor1)
    result = capture_path(raw)
    assert result == [str(floor1), str(floor2)]


def test_capture_path_floor_skips_missing_dirs(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(environment, "PATH_FLOOR", [str(missing), str(real)])
    result = capture_path("")
    assert result == [str(real)]


def test_capture_path_unset(monkeypatch):
    monkeypatch.setattr(environment, "PATH_FLOOR", [])
    assert capture_path("") == []


def test_capture_path_raw_overrides_environment(tmp_path, monkeypatch):
    env_dir = tmp_path / "env"
    raw_dir = tmp_path / "raw"
    env_dir.mkdir()
    raw_dir.mkdir()
    monkeypatch.setenv("PATH", str(env_dir))
    monkeypatch.setattr(environment, "PATH_FLOOR", [])
    result = capture_path(str(raw_dir))
    assert result == [str(raw_dir)]
    env_result = capture_path()
    assert env_result == [str(env_dir)]


# ---------------------------------------------------------------------------
# probe_agent
# ---------------------------------------------------------------------------


def test_probe_agent_binary_missing(monkeypatch):
    monkeypatch.setattr(environment.shutil, "which", lambda _: None)
    probe = probe_agent("claude")
    assert probe == AgentProbe(
        agent="claude",
        resolved_path=None,
        version=None,
        version_known_good=False,
        help_ok=False,
        error="binary not found",
    )


def test_probe_agent_version_raises_help_still_runs(monkeypatch):
    monkeypatch.setattr(environment.shutil, "which", lambda _: "/fake/claude")

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "--version" in cmd:
            raise subprocess.TimeoutExpired(cmd, 5)
        return _Proc(
            stdout="--resume --dangerously-skip-permissions",
            returncode=0,
        )

    monkeypatch.setattr(environment.subprocess, "run", fake_run)
    probe = probe_agent("claude")
    assert probe.version is None
    assert probe.version_known_good is False
    assert probe.help_ok is True
    assert any("--help" in c for c in calls)


def test_probe_agent_version_unparseable(monkeypatch):
    monkeypatch.setattr(environment.shutil, "which", lambda _: "/fake/claude")

    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return _Proc(stdout="some garbage", returncode=0)
        return _Proc(
            stdout="--resume --dangerously-skip-permissions",
            returncode=0,
        )

    monkeypatch.setattr(environment.subprocess, "run", fake_run)
    probe = probe_agent("claude")
    assert probe.version is None
    assert probe.version_known_good is False


def test_probe_agent_known_good_version(monkeypatch):
    monkeypatch.setattr(environment.shutil, "which", lambda _: "/fake/claude")

    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return _Proc(stdout="2.1.112 (Claude Code)", returncode=0)
        return _Proc(
            stdout="--resume --dangerously-skip-permissions",
            returncode=0,
        )

    monkeypatch.setattr(environment.subprocess, "run", fake_run)
    probe = probe_agent("claude")
    assert probe.version == "2.1.112"
    assert probe.version_known_good is True
    assert probe.help_ok is True
    assert probe.error is None


def test_probe_agent_unknown_version_warn(monkeypatch):
    monkeypatch.setattr(environment.shutil, "which", lambda _: "/fake/claude")

    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            # Below the 2.1.112 minimum → flagged as not known-good.
            return _Proc(stdout="1.0.0 (ancient)", returncode=0)
        return _Proc(
            stdout="--resume --dangerously-skip-permissions",
            returncode=0,
        )

    monkeypatch.setattr(environment.subprocess, "run", fake_run)
    probe = probe_agent("claude")
    assert probe.version == "1.0.0"
    assert probe.version_known_good is False


def test_probe_agent_future_version_ok(monkeypatch):
    monkeypatch.setattr(environment.shutil, "which", lambda _: "/fake/claude")

    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return _Proc(stdout="9.9.9 (future)", returncode=0)
        return _Proc(
            stdout="--resume --dangerously-skip-permissions",
            returncode=0,
        )

    monkeypatch.setattr(environment.subprocess, "run", fake_run)
    probe = probe_agent("claude")
    assert probe.version == "9.9.9"
    assert probe.version_known_good is True


def test_probe_agent_help_missing_substring(monkeypatch):
    monkeypatch.setattr(environment.shutil, "which", lambda _: "/fake/claude")

    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return _Proc(stdout="2.1.112", returncode=0)
        return _Proc(stdout="--resume only", returncode=0)

    monkeypatch.setattr(environment.subprocess, "run", fake_run)
    probe = probe_agent("claude")
    assert probe.help_ok is False


def test_probe_agent_help_nonzero_returncode(monkeypatch):
    monkeypatch.setattr(environment.shutil, "which", lambda _: "/fake/claude")

    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return _Proc(stdout="2.1.112", returncode=0)
        return _Proc(
            stdout="--resume --dangerously-skip-permissions",
            returncode=1,
        )

    monkeypatch.setattr(environment.subprocess, "run", fake_run)
    probe = probe_agent("claude")
    assert probe.help_ok is False


def test_probe_agent_help_raises(monkeypatch):
    monkeypatch.setattr(environment.shutil, "which", lambda _: "/fake/claude")

    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return _Proc(stdout="2.1.112", returncode=0)
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(environment.subprocess, "run", fake_run)
    probe = probe_agent("claude")
    assert probe.help_ok is False
    assert probe.error is not None


def test_probe_agent_happy_path_codex(monkeypatch):
    monkeypatch.setattr(environment.shutil, "which", lambda _: "/fake/codex")

    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return _Proc(stdout="codex 0.120.0\n", returncode=0)
        return _Proc(
            stdout="exec --dangerously-bypass-approvals-and-sandbox",
            returncode=0,
        )

    monkeypatch.setattr(environment.subprocess, "run", fake_run)
    probe = probe_agent("codex")
    assert probe.resolved_path == "/fake/codex"
    assert probe.version == "0.120.0"
    assert probe.version_known_good is True
    assert probe.help_ok is True
    assert probe.error is None


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    yield
