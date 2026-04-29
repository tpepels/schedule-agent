from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_HEAD_LINES = 200
DEFAULT_TAIL_LINES = 200
DEFAULT_MAX_BYTES = 256 * 1024


@dataclass(frozen=True)
class JsonlSample:
    head_records: tuple[dict[str, Any], ...]
    tail_records: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]
    file_size: int
    truncated_head: bool
    truncated_tail: bool


def _parse_jsonl_lines(
    lines: list[str],
    path: Path,
    offset: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            warnings.append(f"{path}: skipped malformed JSONL line in {offset} window ({index})")
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
        else:
            warnings.append(f"{path}: skipped non-object JSONL line in {offset} window ({index})")
    return records, warnings


def _read_head_window(
    path: Path,
    *,
    max_lines: int,
    max_bytes: int,
) -> tuple[list[str], bool]:
    lines: list[str] = []
    bytes_read = 0
    truncated = False
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for _index, line in enumerate(handle, start=1):
            line_bytes = len(line.encode("utf-8"))
            if lines and bytes_read + line_bytes > max_bytes:
                truncated = True
                break
            lines.append(line)
            bytes_read += line_bytes
            if len(lines) >= max_lines:
                truncated = handle.readline() != ""
                break
    return lines, truncated


def _read_tail_window(
    path: Path,
    *,
    max_lines: int,
    max_bytes: int,
) -> tuple[list[str], bool]:
    with open(path, "rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        read_size = min(size, max_bytes)
        truncated = read_size < size
        handle.seek(size - read_size)
        payload = handle.read(read_size)
    text = payload.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    if truncated and lines:
        lines = lines[1:]
    return lines[-max_lines:], truncated


def read_first_json_object(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                parsed = json.loads(line)
                return parsed if isinstance(parsed, dict) else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return None


def read_jsonl_sample(
    path: Path,
    *,
    head_lines: int = DEFAULT_HEAD_LINES,
    tail_lines: int = DEFAULT_TAIL_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> JsonlSample:
    warnings: list[str] = []
    try:
        file_size = path.stat().st_size
    except OSError as exc:
        return JsonlSample(
            head_records=(),
            tail_records=(),
            warnings=(f"{path}: {exc}",),
            file_size=0,
            truncated_head=False,
            truncated_tail=False,
        )

    try:
        head_lines_raw, truncated_head = _read_head_window(
            path,
            max_lines=head_lines,
            max_bytes=max_bytes,
        )
    except OSError as exc:
        return JsonlSample(
            head_records=(),
            tail_records=(),
            warnings=(f"{path}: {exc}",),
            file_size=file_size,
            truncated_head=False,
            truncated_tail=False,
        )
    head_records, head_warnings = _parse_jsonl_lines(head_lines_raw, path, "head")
    warnings.extend(head_warnings)

    if not truncated_head:
        return JsonlSample(
            head_records=tuple(head_records),
            tail_records=(),
            warnings=tuple(warnings),
            file_size=file_size,
            truncated_head=False,
            truncated_tail=False,
        )

    try:
        tail_lines_raw, truncated_tail = _read_tail_window(
            path,
            max_lines=tail_lines,
            max_bytes=max_bytes,
        )
    except OSError as exc:
        warnings.append(f"{path}: {exc}")
        tail_records: list[dict[str, Any]] = []
        truncated_tail = False
    else:
        tail_records, tail_warnings = _parse_jsonl_lines(tail_lines_raw, path, "tail")
        warnings.extend(tail_warnings)

    return JsonlSample(
        head_records=tuple(head_records),
        tail_records=tuple(tail_records),
        warnings=tuple(warnings),
        file_size=file_size,
        truncated_head=True,
        truncated_tail=truncated_tail,
    )
