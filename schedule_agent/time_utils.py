from __future__ import annotations

import re
import subprocess
from datetime import datetime

ISO_SECONDS_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
DISPLAY_MINUTE_FORMAT = "%Y-%m-%d %H:%M"
DISPLAY_SECOND_FORMAT = "%Y-%m-%d %H:%M:%S"
AT_TIME_FORMAT = "%Y%m%d%H%M.00"


def now_iso() -> str:
    return datetime.now().astimezone().strftime(ISO_SECONDS_FORMAT)


def normalize_schedule_input(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError("Schedule cannot be empty")

    shorthand = re.fullmatch(r"(?i)(?:now\s*\+\s*)?(\d+)\s*([mhd])", text)
    if shorthand:
        amount = int(shorthand.group(1))
        unit = shorthand.group(2).lower()
        word = {"m": "minutes", "h": "hours", "d": "days"}[unit]
        return f"now + {amount} {word}"

    return text


def resolve_schedule_input(value: str) -> str:
    normalized = normalize_schedule_input(value)
    proc = subprocess.run(
        ["date", "-d", normalized, f"+{ISO_SECONDS_FORMAT}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise ValueError(proc.stderr.strip() or f"Could not parse time: {value}")
    parsed = parse_iso_datetime(proc.stdout.strip())
    # Normalize to minute precision for scheduler consistency.
    normalized_dt = parsed.replace(second=0, microsecond=0)
    return normalized_dt.strftime(ISO_SECONDS_FORMAT)


def parse_iso_datetime(value: str) -> datetime:
    candidate = value.strip()
    if re.search(r"[+-]\d{2}:\d{2}$", candidate):
        candidate = candidate[:-3] + candidate[-2:]
    return datetime.strptime(candidate, ISO_SECONDS_FORMAT)


def iso_to_display(value: str | None, with_seconds: bool = False) -> str:
    if not value:
        return "-"
    fmt = DISPLAY_SECOND_FORMAT if with_seconds else DISPLAY_MINUTE_FORMAT
    return parse_iso_datetime(value).astimezone().strftime(fmt)


def iso_to_at_time(value: str) -> str:
    return parse_iso_datetime(value).astimezone().strftime(AT_TIME_FORMAT)


def sort_key_for_iso(value: str | None) -> tuple[int, float]:
    if not value:
        return (1, float("inf"))
    return (0, parse_iso_datetime(value).timestamp())


def title_from_prompt(prompt: str) -> str:
    for line in prompt.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return "(untitled job)"
