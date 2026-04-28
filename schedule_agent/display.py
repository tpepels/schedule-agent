from __future__ import annotations

from pathlib import Path


def display_path(path: str | Path | None) -> str:
    if path is None:
        return ""
    s = str(path)
    if not s:
        return ""
    home = str(Path.home())
    if s == home:
        return "~"
    if s.startswith(home + "/"):
        return "~/" + s[len(home) + 1 :]
    return s
