from __future__ import annotations

import json

from schedule_agent import preflight


def _result(name: str, severity: str, message: str = "", detail: dict | None = None):
    return preflight.CheckResult(
        name=name,
        label=name.replace("_", " "),
        severity=severity,
        message=message,
        detail=detail or {},
    )


def _install_report(monkeypatch, app_modules, results):
    report = preflight.PreflightReport(results=results)
    monkeypatch.setattr(
        app_modules.cli.preflight if hasattr(app_modules.cli, "preflight") else preflight,
        "run_checks",
        lambda include_roundtrip=False: report,
    )
    return report


def test_doctor_pass_prints_table_returns_zero(app_modules, monkeypatch, capsys):
    _install_report(monkeypatch, app_modules, [_result("at_binary", "PASS", "/usr/bin/at")])
    rc = app_modules.cli.cli_doctor()
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out and "/usr/bin/at" in out


def test_doctor_fail_returns_nonzero(app_modules, monkeypatch, capsys):
    _install_report(
        monkeypatch,
        app_modules,
        [_result("atd_active", "FAIL", "atd not active")],
    )
    rc = app_modules.cli.cli_doctor()
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out
    assert "1 critical failure(s)" in out


def test_doctor_quiet_hides_pass_rows(app_modules, monkeypatch, capsys):
    _install_report(
        monkeypatch,
        app_modules,
        [
            _result("at_binary", "PASS", "/usr/bin/at"),
            _result("agent_codex", "WARN", "codex 9.9.9 untested"),
        ],
    )
    rc = app_modules.cli.cli_doctor(quiet=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" not in out
    assert "WARN" in out


def test_doctor_json_output(app_modules, monkeypatch, capsys):
    _install_report(
        monkeypatch,
        app_modules,
        [_result("at_binary", "PASS", "/usr/bin/at", detail={"resolved": "/usr/bin/at"})],
    )
    rc = app_modules.cli.cli_doctor(as_json=True)
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0
    assert payload["critical_ok"] is True
    assert payload["results"][0]["severity"] == "PASS"


def test_doctor_verbose_prints_detail(app_modules, monkeypatch, capsys):
    _install_report(
        monkeypatch,
        app_modules,
        [_result("at_binary", "PASS", "ok", detail={"resolved": "/usr/bin/at"})],
    )
    rc = app_modules.cli.cli_doctor(verbose=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "resolved: /usr/bin/at" in out
