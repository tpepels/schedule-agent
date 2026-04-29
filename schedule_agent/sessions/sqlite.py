from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


def connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def list_tables(path: Path) -> tuple[list[str], str | None]:
    try:
        with closing(connect_readonly(path)) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
    except sqlite3.Error as exc:
        return [], str(exc)
    return [str(row[0]) for row in rows], None


def table_columns(path: Path, table: str) -> tuple[list[str], str | None]:
    try:
        with closing(connect_readonly(path)) as conn:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error as exc:
        return [], str(exc)
    return [str(row[1]) for row in rows], None


def decode_json_cell(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
