from __future__ import annotations

import re
from datetime import datetime, timezone

from schedule_agent.cli import (
    RUN_AT_W,
    SESSION_W,
    STATUS_W,
    TITLE_MAX,
    TITLE_MIN,
    _column_header,
    _column_value,
    _summary_columns,
)

UTC = timezone.utc
_NOW = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)


def _make_job(
    state: str,
    scheduled_for: str | None = None,
    updated_at: str | None = None,
    created_at: str | None = None,
) -> dict:
    return {
        "title": "Test job",
        "display_state": state,
        "display_label": state.capitalize(),
        "scheduled_for": scheduled_for or "2026-04-28T10:00:00+00:00",
        "updated_at": updated_at or "2026-04-27T10:00:00+00:00",
        "created_at": created_at or "2026-04-25T08:00:00+00:00",
        "session_mode": "append",
        "session_id": None,
    }


# ---------------------------------------------------------------------------
# Status column: glyph + compact label
# ---------------------------------------------------------------------------


def test_status_completed():
    job = _make_job("completed")
    val = _column_value(job, "status")
    assert val == "+ OK"


def test_status_failed():
    job = _make_job("failed")
    val = _column_value(job, "status")
    assert val == "x Fail"


def test_status_scheduled():
    job = _make_job("scheduled")
    val = _column_value(job, "status")
    assert val == "o Sched"


def test_status_running():
    job = _make_job("running")
    val = _column_value(job, "status")
    assert val == "> Run"


def test_status_queued():
    job = _make_job("queued")
    val = _column_value(job, "status")
    assert val == ". Queue"


# ---------------------------------------------------------------------------
# Compact time columns — tolerance: any single token, no spaces except "now"
# ---------------------------------------------------------------------------

_COMPACT_TOKEN_RE = re.compile(
    r"^(\w+|\d{2}-\d{2}|\d{4}-\d{2}-\d{2}|\+\d+[mhd]|\d+[mhwd]|\d{2}:\d{2})$"
)


def test_run_at_compact():
    job = _make_job("scheduled", scheduled_for="2026-04-28T10:00:00+00:00")
    val = _column_value(job, "run_at")
    assert _COMPACT_TOKEN_RE.match(val), f"Expected compact token, got {val!r}"
    assert "(" not in val


def test_updated_compact():
    job = _make_job("completed", updated_at="2026-04-26T09:00:00+00:00")
    val = _column_value(job, "updated")
    assert _COMPACT_TOKEN_RE.match(val), f"Expected compact token, got {val!r}"
    assert "(" not in val


def test_created_compact():
    job = _make_job("completed", created_at="2026-01-01T00:00:00+00:00")
    val = _column_value(job, "created")
    assert _COMPACT_TOKEN_RE.match(val), f"Expected compact token, got {val!r}"
    assert "(" not in val


def test_run_at_none_yields_dash():
    job = _make_job("scheduled")
    job["scheduled_for"] = None
    val = _column_value(job, "run_at")
    assert val == "-"


# ---------------------------------------------------------------------------
# Header rename
# ---------------------------------------------------------------------------


def test_column_header_run_at():
    assert _column_header("run_at") == "Run"


def test_column_header_title():
    assert _column_header("title") == "Title"


def test_column_header_status():
    assert _column_header("status") == "Status"


# ---------------------------------------------------------------------------
# _summary_columns: shape and width budget
# ---------------------------------------------------------------------------


def _total_row_width(cols: list[tuple[str, int]]) -> int:
    # gutter "  " (2) + sum of column widths + 1 gap per column
    return 2 + sum(w for _, w in cols) + len(cols)


def test_narrow_columns_shape():
    cols = _summary_columns("narrow", 60)
    names = [name for name, _ in cols]
    assert names == ["title", "status"]


def test_medium_columns_shape():
    cols = _summary_columns("medium", 100)
    names = [name for name, _ in cols]
    assert names == ["title", "status", "run_at", "session"]


def test_wide_columns_shape():
    cols = _summary_columns("wide", 140)
    names = [name for name, _ in cols]
    assert names == ["title", "status", "run_at", "session", "updated"]


def test_xwide_columns_shape():
    cols = _summary_columns("xwide", 200)
    names = [name for name, _ in cols]
    assert names == ["title", "status", "run_at", "session", "updated", "created"]


def test_wide_terminal_gets_longer_title_than_medium():
    cols_medium = dict(_summary_columns("medium", 100))
    cols_wide = dict(_summary_columns("wide", 160))
    assert cols_wide["title"] > cols_medium["title"]


def test_narrow_terminal_title_at_min():
    cols = _summary_columns("narrow", 40)
    title_w = dict(cols)["title"]
    assert title_w == TITLE_MIN


def test_fixed_widths_respected():
    cols = dict(_summary_columns("medium", 100))
    assert cols["status"] == STATUS_W
    assert cols["run_at"] == RUN_AT_W
    assert cols["session"] == SESSION_W


def test_title_never_exceeds_max():
    for mode, width in [("narrow", 300), ("medium", 300), ("wide", 300), ("xwide", 300)]:
        cols = dict(_summary_columns(mode, width))
        assert cols["title"] <= TITLE_MAX, f"title exceeded TITLE_MAX in {mode}"


def test_row_fits_in_given_width_medium():
    total = 100
    cols = _summary_columns("medium", total)
    row_w = _total_row_width(cols)
    assert row_w <= total + 1  # trailing space may be stripped by rstrip


def test_row_fits_in_given_width_wide():
    total = 140
    cols = _summary_columns("wide", total)
    row_w = _total_row_width(cols)
    assert row_w <= total + 1
