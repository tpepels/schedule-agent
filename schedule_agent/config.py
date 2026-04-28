from __future__ import annotations

import json
import os
import stat
from pathlib import Path

APP_NAME = "schedule-agent"

DEFAULT_PROMPT_PREFIX = """\
You are executing a scheduled task autonomously. Work from the task prompt
below. When you finish, summarise what was done in the final message.
"""


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME


def config_home() -> Path:
    path = _config_home()
    path.mkdir(parents=True, exist_ok=True)
    return path


def prompt_prefix_path(agent: str) -> Path:
    if agent not in {"claude", "codex"}:
        raise ValueError(f"Unknown agent: {agent}")
    return config_home() / f"prompt-prefix-{agent}.md"


def ensure_prompt_prefix(agent: str) -> Path:
    path = prompt_prefix_path(agent)
    if not path.exists():
        path.write_text(DEFAULT_PROMPT_PREFIX, encoding="utf-8")
    return path


def load_prompt_prefix(agent: str) -> str:
    return ensure_prompt_prefix(agent).read_text(encoding="utf-8")


_VALID_FILTERS = {"all", "active", "completed"}
_VALID_SCOPES = {"project", "all"}
_UI_PREFS_DEFAULTS: dict = {"filter": "all", "scope": "project"}
_UI_PREFS_FILE = "ui-prefs.json"


def _ui_prefs_path() -> Path:
    return _config_home() / _UI_PREFS_FILE


def load_ui_prefs() -> dict:
    try:
        raw = json.loads(_ui_prefs_path().read_text(encoding="utf-8"))
    except Exception:
        return dict(_UI_PREFS_DEFAULTS)
    result = dict(_UI_PREFS_DEFAULTS)
    if raw.get("filter") in _VALID_FILTERS:
        result["filter"] = raw["filter"]
    if raw.get("scope") in _VALID_SCOPES:
        result["scope"] = raw["scope"]
    return result


def save_ui_prefs(prefs: dict) -> None:
    path = _ui_prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    payload = {}
    if prefs.get("filter") in _VALID_FILTERS:
        payload["filter"] = prefs["filter"]
    if prefs.get("scope") in _VALID_SCOPES:
        payload["scope"] = prefs["scope"]
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)
