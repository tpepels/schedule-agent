import pytest

from schedule_agent import time_utils


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_resolve_schedule_input_normalizes_to_minute_precision(monkeypatch):
    def fake_run(cmd, capture_output=None, text=None):
        assert cmd[:3] == ["date", "-d", "now + 10 minutes"]
        return _Proc(stdout="2026-04-18T09:10:37+0000\n")

    monkeypatch.setattr(time_utils.subprocess, "run", fake_run)

    assert time_utils.resolve_schedule_input("10m") == "2026-04-18T09:10:00+0000"


def test_resolve_schedule_input_surfaces_date_errors(monkeypatch):
    monkeypatch.setattr(
        time_utils.subprocess,
        "run",
        lambda *args, **kwargs: _Proc(returncode=1, stderr="date: invalid date"),
    )

    with pytest.raises(ValueError, match="date: invalid date"):
        time_utils.resolve_schedule_input("tomorrowish")


def test_time_parsing_helpers_handle_offsets_and_sort_fallbacks():
    with_colon = time_utils.parse_iso_datetime("2026-04-18T09:00:00+01:00")
    without_colon = time_utils.parse_iso_datetime("2026-04-18T09:00:00+0100")

    assert with_colon == without_colon
    assert time_utils.iso_to_display(None) == "-"
    assert time_utils.sort_key_for_iso(None) == (1, float("inf"))
    assert time_utils.sort_key_for_iso("not-an-iso")[0] == 1


def test_normalize_legacy_timestamp_and_title_from_prompt():
    assert time_utils.normalize_legacy_timestamp(None) is None
    assert (
        time_utils.normalize_legacy_timestamp("2026-04-18T09:00:00+0000")
        == "2026-04-18T09:00:00+0000"
    )
    assert (
        time_utils.normalize_legacy_timestamp("2026-04-18 09:00:00")
        == "2026-04-18T09:00:00+0000"
    )
    with pytest.raises(ValueError):
        time_utils.normalize_legacy_timestamp("18/04/2026 09:00")

    assert time_utils.title_from_prompt("\n  \nFirst line\nSecond line") == "First line"
    assert time_utils.title_from_prompt("\n \n") == "(untitled job)"
