from __future__ import annotations

import pytest

from schedule_agent import environment, preflight


def _probe(**overrides):
    defaults = dict(
        agent="claude",
        resolved_path="/fake/claude",
        version="2.1.112",
        version_known_good=True,
        help_ok=True,
        error=None,
    )
    defaults.update(overrides)
    return environment.AgentProbe(**defaults)


def _report(*results: preflight.CheckResult) -> preflight.PreflightReport:
    return preflight.PreflightReport(results=list(results))


def _pass(name: str = "at_binary") -> preflight.CheckResult:
    return preflight.CheckResult(name=name, label=name, severity="PASS", message="ok")


def _fail(name: str = "agent_claude") -> preflight.CheckResult:
    return preflight.CheckResult(
        name=name, label="claude CLI", severity="FAIL", message="binary not found"
    )


def _warn(msg: str = "codex 9.9.9 is untested") -> preflight.CheckResult:
    return preflight.CheckResult(
        name="agent_codex", label="codex CLI", severity="WARN", message=msg
    )


def _create(app_modules, monkeypatch, **overrides):
    ops = app_modules.operations
    monkeypatch.setattr(ops, "resolve_schedule_spec", lambda spec: "2026-04-18T11:30:00+0000")
    monkeypatch.setattr(ops, "now_iso", lambda: "2026-04-17T10:00:00+0000")
    kwargs = dict(
        agent="claude",
        session_mode="new",
        session_id=None,
        prompt_text="hello",
        schedule_spec="tomorrow 11:30",
        cwd="/tmp/project",
        submit=False,
    )
    kwargs.update(overrides)
    return ops.create_job(**kwargs)


def test_create_job_blocks_on_critical_preflight_failure(app_modules, monkeypatch):
    ops = app_modules.operations
    monkeypatch.setattr(
        ops, "_submit_preflight", lambda agent: (_report(_fail()), _probe(resolved_path=None))
    )
    with pytest.raises(ops.OperationError, match="Preflight failed"):
        _create(app_modules, monkeypatch)
    assert app_modules.persistence.load_jobs() == []


def test_create_job_passes_with_warn_and_records_warnings(app_modules, monkeypatch):
    ops = app_modules.operations
    report = _report(_pass(), _warn("codex 9.9.9 is untested"))
    monkeypatch.setattr(ops, "_submit_preflight", lambda agent: (report, _probe()))

    job = _create(app_modules, monkeypatch)
    prov = job["provenance"]
    assert prov["preflight"]["critical_ok"] is True
    assert prov["preflight"]["warnings"] == ["codex 9.9.9 is untested"]


def test_create_job_populates_provenance_fields(app_modules, monkeypatch):
    ops = app_modules.operations
    monkeypatch.setenv("PATH", "/home/u/.local/bin:/usr/bin")
    monkeypatch.setattr(
        environment, "capture_path", lambda raw=None: ["/home/u/.local/bin", "/usr/bin"]
    )
    monkeypatch.setattr(
        ops, "_submit_preflight", lambda agent: (_report(_pass()), _probe())
    )
    job = _create(app_modules, monkeypatch)
    prov = job["provenance"]
    assert prov["agent_path"] == "/fake/claude"
    assert prov["agent_version"] == "2.1.112"
    assert prov["path_snapshot_raw"] == "/home/u/.local/bin:/usr/bin"
    assert prov["path_snapshot_cleaned"] == ["/home/u/.local/bin", "/usr/bin"]
    assert prov["submitted_at"] == "2026-04-17T10:00:00+0000"
    assert prov["preflight"] == {"critical_ok": True, "warnings": []}
