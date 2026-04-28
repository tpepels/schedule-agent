from __future__ import annotations

import json

from schedule_agent.config import load_ui_prefs, save_ui_prefs


def test_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    save_ui_prefs({"filter": "active", "scope": "all"})
    result = load_ui_prefs()
    assert result == {"filter": "active", "scope": "all"}


def test_missing_file_returns_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    result = load_ui_prefs()
    assert result == {"filter": "all", "scope": "project"}


def test_corrupt_json_returns_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    prefs_dir = tmp_path / "schedule-agent"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / "ui-prefs.json").write_text("not valid json", encoding="utf-8")
    result = load_ui_prefs()
    assert result == {"filter": "all", "scope": "project"}


def test_unknown_filter_value_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    prefs_dir = tmp_path / "schedule-agent"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / "ui-prefs.json").write_text(
        json.dumps({"filter": "bogus", "scope": "all"}), encoding="utf-8"
    )
    result = load_ui_prefs()
    assert result["filter"] == "all"
    assert result["scope"] == "all"


def test_unknown_scope_value_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    prefs_dir = tmp_path / "schedule-agent"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / "ui-prefs.json").write_text(
        json.dumps({"filter": "completed", "scope": "universe"}), encoding="utf-8"
    )
    result = load_ui_prefs()
    assert result["scope"] == "project"
    assert result["filter"] == "completed"


def test_file_permissions_600(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    save_ui_prefs({"filter": "all", "scope": "project"})
    prefs_file = tmp_path / "schedule-agent" / "ui-prefs.json"
    mode = prefs_file.stat().st_mode & 0o777
    assert mode == 0o600


def test_unknown_keys_dropped(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    save_ui_prefs({"filter": "active", "scope": "all", "unknown_key": "ignored"})
    raw = json.loads((tmp_path / "schedule-agent" / "ui-prefs.json").read_text())
    assert "unknown_key" not in raw
    assert raw == {"filter": "active", "scope": "all"}


def test_all_valid_filter_values(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for f in ("all", "active", "completed"):
        save_ui_prefs({"filter": f, "scope": "project"})
        assert load_ui_prefs()["filter"] == f


def test_all_valid_scope_values(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for s in ("project", "all"):
        save_ui_prefs({"filter": "all", "scope": s})
        assert load_ui_prefs()["scope"] == s
