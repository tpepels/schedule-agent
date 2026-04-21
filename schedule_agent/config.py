from __future__ import annotations

import os
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
