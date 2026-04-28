from __future__ import annotations

from datetime import datetime, timezone

from schedule_agent.cli import compact_status_label, format_compact_time
from schedule_agent.display import display_path

UTC = timezone.utc


# ---------------------------------------------------------------------------
# display_path
# ---------------------------------------------------------------------------


def test_display_path_none():
    assert display_path(None) == ""


def test_display_path_empty_string():
    assert display_path("") == ""


def test_display_path_exact_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert display_path(str(tmp_path)) == "~"


def test_display_path_under_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    child = str(tmp_path / "projects" / "foo")
    assert display_path(child) == "~/projects/foo"


def test_display_path_outside_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert display_path("/etc/hosts") == "/etc/hosts"


def test_display_path_path_object(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    child = tmp_path / "a" / "b"
    assert display_path(child) == "~/a/b"


def test_display_path_unrelated_prefix(monkeypatch, tmp_path):
    # /home/tomfoo should not match HOME=/home/tom
    monkeypatch.setenv("HOME", str(tmp_path))
    # Construct a path that shares the same string prefix but is a sibling dir
    sibling = tmp_path.parent / (tmp_path.name + "extra")
    assert display_path(str(sibling)) == str(sibling)


# ---------------------------------------------------------------------------
# format_compact_time
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
_NOW_ISO = "2026-04-27T12:00:00+00:00"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def test_fmt_none():
    assert format_compact_time(None, _NOW) == ""


def test_fmt_invalid():
    assert format_compact_time("not-a-date", _NOW) == ""


def test_fmt_just_now_past():
    t = datetime(2026, 4, 27, 11, 59, 45, tzinfo=UTC)
    assert format_compact_time(_iso(t), _NOW) == "now"


def test_fmt_just_now_future():
    t = datetime(2026, 4, 27, 12, 0, 20, tzinfo=UTC)
    assert format_compact_time(_iso(t), _NOW) == "now"


def test_fmt_past_minutes():
    t = datetime(2026, 4, 27, 11, 55, 0, tzinfo=UTC)  # 5 minutes ago
    assert format_compact_time(_iso(t), _NOW) == "5m"


def test_fmt_past_hours():
    t = datetime(2026, 4, 27, 10, 0, 0, tzinfo=UTC)  # 2h ago
    assert format_compact_time(_iso(t), _NOW) == "2h"


def test_fmt_past_days():
    t = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)  # 3d ago
    assert format_compact_time(_iso(t), _NOW) == "3d"


def test_fmt_past_weeks():
    t = datetime(2026, 4, 13, 12, 0, 0, tzinfo=UTC)  # 14d = 2w ago
    assert format_compact_time(_iso(t), _NOW) == "2w"


def test_fmt_future_minutes():
    t = datetime(2026, 4, 27, 12, 10, 0, tzinfo=UTC)  # 10m ahead
    assert format_compact_time(_iso(t), _NOW) == "+10m"


def test_fmt_future_hours():
    t = datetime(2026, 4, 27, 15, 0, 0, tzinfo=UTC)  # 3h ahead
    assert format_compact_time(_iso(t), _NOW) == "+3h"


def test_fmt_future_days():
    t = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)  # 2d ahead
    assert format_compact_time(_iso(t), _NOW) == "+2d"


def test_fmt_same_day_relative_shorter():
    # now=12:00, target=12:25 (+25m) vs clock "12:25" — same length → prefer relative
    t = datetime(2026, 4, 27, 12, 25, 0, tzinfo=UTC)
    result = format_compact_time(_iso(t), _NOW)
    assert result == "+25m"


def test_fmt_same_day_past_shows_relative():
    # Past on same day: 45m ago → "45m", not the clock time
    now = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
    t = datetime(2026, 4, 27, 11, 15, 0, tzinfo=UTC)  # 45m ago
    result = format_compact_time(_iso(t), now)
    assert result == "45m"


def test_fmt_outside_28d_same_year():
    t = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)  # ~57d ago, same year
    assert format_compact_time(_iso(t), _NOW) == "03-01"


def test_fmt_outside_28d_different_year():
    t = datetime(2025, 4, 25, 12, 0, 0, tzinfo=UTC)  # different year
    assert format_compact_time(_iso(t), _NOW) == "2025-04-25"


# ---------------------------------------------------------------------------
# compact_status_label
# ---------------------------------------------------------------------------


def test_label_completed():
    assert compact_status_label("completed") == "OK"


def test_label_failed():
    assert compact_status_label("failed") == "Fail"


def test_label_running():
    assert compact_status_label("running") == "Run"


def test_label_queued():
    assert compact_status_label("queued") == "Queue"


def test_label_scheduled():
    assert compact_status_label("scheduled") == "Sched"


def test_label_removed():
    assert compact_status_label("removed") == "Cncl"


def test_label_waiting():
    assert compact_status_label("waiting") == "Wait"


def test_label_blocked():
    assert compact_status_label("blocked") == "Block"


def test_label_invalid():
    assert compact_status_label("invalid") == "Inv"


def test_label_unknown_string():
    assert compact_status_label("something_else") == "?"


def test_label_none():
    assert compact_status_label(None) == "?"
