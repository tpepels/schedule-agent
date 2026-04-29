from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "schedule-agent"


def _unique_paths(paths: list[Path]) -> tuple[Path, ...]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.expanduser()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return tuple(unique)


def state_home() -> Path:
    raw = os.environ.get("XDG_STATE_HOME")
    if raw:
        return Path(raw).expanduser() / APP_NAME
    return Path.home() / ".local" / "state" / APP_NAME


def session_ledger_path() -> Path:
    return state_home() / "session-ledger.jsonl"


def claude_config_dir() -> Path | None:
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    if not raw:
        return None
    return Path(raw).expanduser()


def claude_project_roots(cwd: Path | None = None) -> tuple[Path, ...]:
    roots: list[Path] = []
    config_dir = claude_config_dir()
    if config_dir is not None:
        roots.append(config_dir / "projects")
    roots.append(Path.home() / ".claude" / "projects")
    if cwd is not None:
        roots.append(cwd / ".claude")
    return _unique_paths(roots)


def codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".codex"


def codex_sqlite_home() -> Path:
    raw = os.environ.get("CODEX_SQLITE_HOME")
    if raw:
        return Path(raw).expanduser()
    return codex_home()


def codex_sqlite_roots() -> tuple[Path, ...]:
    return _unique_paths([codex_sqlite_home(), codex_home()])


def codex_session_roots(cwd: Path | None = None) -> tuple[Path, ...]:
    roots = [codex_home() / "sessions", Path.home() / ".codex" / "sessions"]
    if cwd is not None:
        roots.append(cwd / ".codex")
    return _unique_paths(roots)


def codex_archived_session_roots() -> tuple[Path, ...]:
    return _unique_paths(
        [codex_home() / "archived_sessions", Path.home() / ".codex" / "archived_sessions"]
    )


def codex_session_index_paths() -> tuple[Path, ...]:
    return _unique_paths(
        [codex_home() / "session_index.jsonl", Path.home() / ".codex" / "session_index.jsonl"]
    )


def safe_glob(root: Path, pattern: str) -> tuple[list[Path], str | None]:
    try:
        return [path for path in root.glob(pattern) if path.is_file()], None
    except OSError as exc:
        return [], str(exc)


def safe_rglob(root: Path, pattern: str) -> tuple[list[Path], str | None]:
    try:
        return [path for path in root.rglob(pattern) if path.is_file()], None
    except OSError as exc:
        return [], str(exc)


def safe_iterdir(root: Path) -> tuple[list[Path], str | None]:
    try:
        return list(root.iterdir()), None
    except OSError as exc:
        return [], str(exc)
