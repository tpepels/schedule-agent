from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil as _shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from .config import ensure_prompt_prefix
from .execution import AGENTS, build_agent_cmd
from .legacy import cli_state as legacy_cli_state
from .legacy.compat import legacy_state_file
from .operations import (
    OperationError,
    change_session,
    create_job,
    delete_job,
    format_job_summary,
    get_job_view,
    list_job_views,
    refresh_prompt,
    reschedule_job,
    retry_job,
    submit_or_repair_job,
    unschedule_job,
)
from .persistence import (
    _data_home,
    _ensure_dirs,
    _state_home,
    find_job,
    load_jobs,
    save_jobs,
)
from .persistence import (
    write_prompt_file as _write_prompt_file,
)
from .scheduler_backend import submit_job
from .time_utils import iso_to_display, parse_iso_datetime
from .transitions import on_cancel

APP_NAME = "schedule-agent"

load_state = legacy_cli_state.load_state
save_state = legacy_cli_state.save_state
set_state = legacy_cli_state.set_state
clear_state = legacy_cli_state.clear_state
cancel_at_job = legacy_cli_state.cancel_at_job


def _state_home_fn() -> Path:
    return _state_home()


def _data_home_fn() -> Path:
    return _data_home()


def _make_paths():
    state_dir, data_dir, prompt_dir, _, queue_file = _ensure_dirs()
    return state_dir, data_dir, queue_file, legacy_state_file(state_dir), prompt_dir


STATE_DIR, DATA_DIR, QUEUE_FILE, STATE_FILE, PROMPT_DIR = _make_paths()


def build_cmd(job: dict) -> str:
    return build_agent_cmd(job)


# --- TUI toolkit import ---------------------------------------------------
# The jobs screen is the only caller. It deliberately does NOT import
# prompt_toolkit's modal dialog helpers (input_dialog, yes_no_dialog,
# radiolist_dialog, message_dialog) — those launch nested mini-apps that
# conflict with the running full-screen Application. The jobs screen owns
# every interaction (confirm / input / picker) via inline overlays.
def _require_prompt_toolkit():
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, VSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension
        from prompt_toolkit.styles import Style
    except ModuleNotFoundError as exc:
        raise OperationError(
            "prompt_toolkit is required for the interactive UI. Install project dependencies first."
        ) from exc

    return {
        "Application": Application,
        "KeyBindings": KeyBindings,
        "Layout": Layout,
        "HSplit": HSplit,
        "VSplit": VSplit,
        "Window": Window,
        "ConditionalContainer": ConditionalContainer,
        "FormattedTextControl": FormattedTextControl,
        "Dimension": Dimension,
        "Style": Style,
    }


def ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# --- Non-TUI helpers ------------------------------------------------------
# `choose` is intentionally kept for non-TUI call sites (and for unit tests
# that monkeypatch it). It must NEVER be called from inside jobs_menu — the
# jobs screen uses a TUI-native picker overlay instead.
def choose(msg: str, choices: Sequence[str], default: str | None = None) -> str:
    try:
        from prompt_toolkit.shortcuts import radiolist_dialog
    except ModuleNotFoundError as exc:
        raise OperationError(
            "prompt_toolkit is required for the interactive UI. Install project dependencies first."
        ) from exc
    values = [(choice, choice) for choice in choices]
    result = radiolist_dialog(
        title=APP_NAME,
        text=msg,
        values=values,
        default=default if default in choices else (choices[0] if choices else None),
    ).run()
    if result is None:
        raise KeyboardInterrupt
    return result


def _resolve_editor() -> list[str]:
    editor = os.environ.get("SCHEDULE_AGENT_EDITOR") or os.environ.get("EDITOR") or "nano"
    try:
        parts = shlex.split(editor)
    except ValueError:
        parts = [editor]
    return parts or ["nano"]


def edit_file(path: Path) -> None:
    subprocess.run([*_resolve_editor(), str(path)], check=False)


@dataclass
class SessionInfo:
    id: str
    path: Path
    agent: str
    project: str | None
    title: str | None
    modified_at: float


def extract_session_title(path: str, agent: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except Exception:
        return None

    def iter_json():
        for line in lines:
            try:
                yield json.loads(line)
            except Exception:
                continue

    agent = agent.lower().strip()

    if agent == "claude":
        for obj in iter_json():
            if obj.get("type") == "ai-title":
                title = obj.get("aiTitle")
                if isinstance(title, str) and title.strip():
                    return title.strip()
        for obj in iter_json():
            if obj.get("type") != "user" or obj.get("isMeta"):
                continue
            message = obj.get("message", {})
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip().splitlines()[0]
        return None

    if agent == "codex":
        for obj in iter_json():
            if obj.get("type") == "event_msg":
                payload = obj.get("payload", {})
                if payload.get("type") == "user_message":
                    message = payload.get("message")
                    if isinstance(message, str) and message.strip():
                        return message.strip().splitlines()[0]
        for obj in iter_json():
            if obj.get("type") == "response_item":
                payload = obj.get("payload", {})
                if payload.get("type") == "message" and payload.get("role") == "user":
                    for item in payload.get("content", []):
                        if item.get("type") == "input_text":
                            text = item.get("text")
                            if isinstance(text, str) and text.strip():
                                return text.strip().splitlines()[0]
        return None

    return None


def _codex_session_is_subagent(path: Path) -> bool:
    """True when the first `session_meta` row marks this file as a subagent.

    Codex writes `payload.source = {"subagent": {...}}` for sessions spawned
    via its subagent mechanism; top-level sessions use a string ('exec',
    'cli', 'vscode'). These subagent rollouts cannot be resumed meaningfully,
    so they are hidden from the picker.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            first = handle.readline()
    except OSError:
        return False
    try:
        record = json.loads(first)
    except (ValueError, TypeError):
        return False
    payload = record.get("payload") or {}
    source = payload.get("source")
    return isinstance(source, dict) and "subagent" in source


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _discover_codex_sessions(limit: int) -> list[SessionInfo]:
    root = Path.home() / ".codex" / "sessions"
    if not root.exists():
        return []
    try:
        files = [path for path in root.rglob("*.jsonl") if path.is_file()]
    except OSError:
        return []
    files = [p for p in files if not _codex_session_is_subagent(p)]
    files.sort(key=_safe_mtime, reverse=True)
    return [
        SessionInfo(
            id=path.stem,
            path=path,
            agent="codex",
            project=None,
            title=extract_session_title(str(path), "codex"),
            modified_at=_safe_mtime(path),
        )
        for path in files[:limit]
    ]


def _discover_claude_sessions(cwd: Path | None, limit: int) -> list[SessionInfo]:
    root = Path.home() / ".claude" / "projects"
    if not root.exists():
        return []

    def top_level_jsonl(project_dir: Path) -> list[tuple[Path, str]]:
        try:
            return [
                (path, project_dir.name) for path in project_dir.glob("*.jsonl") if path.is_file()
            ]
        except OSError:
            return []

    preferred_name = cwd.as_posix().replace("/", "-") if cwd is not None else None
    preferred: list[tuple[Path, str]] = []
    other: list[tuple[Path, str]] = []

    if preferred_name:
        preferred_dir = root / preferred_name
        if preferred_dir.exists():
            preferred = top_level_jsonl(preferred_dir)
            preferred.sort(key=lambda pair: _safe_mtime(pair[0]), reverse=True)

    try:
        project_dirs = list(root.iterdir())
    except OSError:
        project_dirs = []
    for project_dir in project_dirs:
        if not project_dir.is_dir():
            continue
        if preferred_name and project_dir.name == preferred_name:
            continue
        other.extend(top_level_jsonl(project_dir))

    other.sort(key=lambda pair: _safe_mtime(pair[0]), reverse=True)
    combined = (preferred + other)[:limit]
    return [
        SessionInfo(
            id=path.stem,
            path=path,
            agent="claude",
            project=project_name,
            title=extract_session_title(str(path), "claude"),
            modified_at=_safe_mtime(path),
        )
        for path, project_name in combined
    ]


def discover_sessions(agent: str, cwd: Path | None = None, limit: int = 10) -> list[SessionInfo]:
    if agent == "codex":
        return _discover_codex_sessions(limit)
    return _discover_claude_sessions(cwd, limit)


PASTE_SESSION_LABEL = "Paste session ID..."


def _prompt_paste_session_id() -> str | None:
    try:
        from prompt_toolkit.shortcuts import input_dialog
    except ModuleNotFoundError as exc:
        raise OperationError(
            "prompt_toolkit is required for the interactive UI. Install project dependencies first."
        ) from exc
    result = input_dialog(
        title=APP_NAME,
        text="Paste session ID (leave empty to cancel):",
    ).run()
    if result is None:
        raise KeyboardInterrupt
    result = result.strip()
    return result or None


def choose_session(agent: str, cwd: Path | None = None) -> str | None:
    sessions = discover_sessions(agent, cwd=cwd)
    labels = (
        ["New session"]
        + [f"{(session.title or '[no title]')} [{session.id[:8]}]" for session in sessions]
        + [PASTE_SESSION_LABEL]
    )

    selected = choose("Session", labels, default="New session")
    if selected == "New session":
        return None
    if selected == PASTE_SESSION_LABEL:
        return _prompt_paste_session_id()
    for session, label in zip(sessions, labels[1:-1]):
        if label == selected:
            return session.id
    return None


def read_prompt(initial: str = "") -> str:
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as handle:
        path = Path(handle.name)
    try:
        if initial:
            path.write_text(initial, encoding="utf-8")
        edit_file(path)
        text = path.read_text(encoding="utf-8").strip()
    finally:
        path.unlink(missing_ok=True)
    if not text:
        raise KeyboardInterrupt
    return text


def write_prompt_file(job_id: str, prompt: str) -> str:
    _ensure_dirs()
    return _write_prompt_file(PROMPT_DIR, job_id, prompt)


def schedule(job: dict, dry_run: bool = False) -> str:
    _, output = submit_job(job, dry_run=dry_run)
    return output


# --- Non-TUI list / show --------------------------------------------------
def _format_list_row(job: dict) -> str:
    session = (job.get("session_id") or job.get("session_mode", "-"))[:12]
    dependency = job.get("depends_on", "-")
    return " | ".join(
        [
            job.get("title", "(invalid)")[:32],
            job.get("display_label", "Invalid"),
            job.get("scheduler_label", "Unknown"),
            iso_to_display(job.get("scheduled_for")),
            iso_to_display(job.get("updated_at")),
            iso_to_display(job.get("created_at")),
            session,
            dependency[:16],
        ]
    )


def list_jobs_noninteractive(filter_name: str = "all") -> str:
    jobs, atq_error = list_job_views(filter_name)
    if not jobs:
        return "No jobs."
    header = "Title | Status | Scheduler | Run At | Updated | Created | Session | Dependency"
    body = "\n".join(_format_list_row(job) for job in jobs)
    if atq_error:
        return f"{header}\n{body}\n\natq warning: {atq_error}"
    return f"{header}\n{body}"


def show_job_text(job: dict) -> str:
    return format_job_summary(job)


def cli_show_job(job_id: str) -> int:
    job = get_job_view(job_id)
    if job is None:
        print("No such job.")
        return 1
    print(show_job_text(job))
    return 0


def _handle_operation(fn, *args):
    try:
        return fn(*args)
    except OperationError as exc:
        raise OperationError(str(exc))


def cli_reschedule_job(job_id: str, new_when: str) -> int:
    try:
        job = _handle_operation(reschedule_job, job_id, new_when)
    except OperationError as exc:
        print(f"error: {exc}")
        return 1
    print(f"{job_id}: rescheduled")
    print(f"  run_at: {iso_to_display(job['scheduled_for'], with_seconds=True)}")
    return 0


def cli_change_session(job_id: str, session: str | None) -> int:
    try:
        job = _handle_operation(change_session, job_id, session)
    except OperationError as exc:
        print(f"error: {exc}")
        return 1
    target = session[:12] if session else "new"
    print(f"{job_id}: session updated")
    print(f"  session: {job['session_mode']}:{target}")
    return 0


def cli_edit_prompt(job_id: str) -> int:
    job = get_job_view(job_id)
    if job is None:
        print("No such job.")
        return 1
    path = Path(job["prompt_file"])
    if not path.exists():
        print(f"error: prompt file not found: {path}")
        return 1
    edit_file(path)
    try:
        updated = refresh_prompt(job_id)
    except OperationError as exc:
        print(f"error: {exc}")
        return 1
    print(f"{job_id}: prompt updated")
    print(f"  title: {updated['title']}")
    return 0


def cli_retry_job(job_id: str, schedule_spec: str) -> int:
    try:
        job = retry_job(job_id, schedule_spec)
    except OperationError as exc:
        print(f"error: {exc}")
        return 1
    print(f"{job_id}: retried")
    print(f"  run_at: {iso_to_display(job['scheduled_for'], with_seconds=True)}")
    return 0


def cli_unschedule_job(job_id: str) -> int:
    try:
        unschedule_job(job_id)
    except OperationError as exc:
        print(f"error: {exc}")
        return 1
    print(f"{job_id}: removed from queue")
    return 0


def cli_delete_job(job_id: str) -> int:
    try:
        delete_job(job_id)
    except OperationError as exc:
        print(f"error: {exc}")
        return 1
    print(f"{job_id}: deleted")
    return 0


def cli_submit_job(job_id: str) -> int:
    try:
        job = submit_or_repair_job(job_id)
    except OperationError as exc:
        print(f"error: {exc}")
        return 1
    print(f"{job_id}: submitted")
    print(f"  at_job_id: {job['at_job_id']}")
    print(f"  run_at:    {iso_to_display(job['scheduled_for'], with_seconds=True)}")
    return 0


def cli_mark_running(job_id: str, started_at: str, log_file: str) -> int:
    from .operations import mark_running

    try:
        mark_running(job_id, started_at=started_at, log_file=log_file)
    except OperationError as exc:
        print(f"error: {exc}")
        return 1
    return 0


def cli_mark_done(job_id: str, finished_at: str, exit_code: int, log_file: str | None) -> int:
    from .operations import mark_finished

    try:
        mark_finished(job_id, finished_at=finished_at, exit_code=exit_code, log_file=log_file)
    except OperationError as exc:
        print(f"error: {exc}")
        return 1
    return 0


def cli_mark_failed(job_id: str, finished_at: str, exit_code: int, log_file: str | None) -> int:
    from .operations import mark_finished

    try:
        mark_finished(job_id, finished_at=finished_at, exit_code=exit_code, log_file=log_file)
    except OperationError as exc:
        print(f"error: {exc}")
        return 1
    return 0


def cli_cancel_job(job_id: str) -> int:
    job = get_job_view(job_id)
    if job is None:
        print(f"error: job {job_id} not found")
        return 1
    if job.get("submission") == "running":
        print(f"error: job {job_id} cannot be removed from queue while running")
        return 1
    if job.get("submission") == "scheduled":
        return cli_unschedule_job(job_id)
    jobs = load_jobs()
    idx, stored = find_job(jobs, job_id)
    if idx is None or stored is None:
        print(f"error: job {job_id} not found")
        return 1
    jobs[idx] = on_cancel(stored)
    save_jobs(jobs)
    print(f"{job_id}: marked removed")
    return 0


# --- JobsScreenState ------------------------------------------------------
@dataclass
class OverlayState:
    """Active overlay inside the jobs screen.

    kind:
        None       - no overlay, normal list keybindings apply
        "confirm"  - y/N prompt in footer
        "input"    - single-line text input in footer
        "picker"   - scrollable selection list
        "message"  - transient one-line informational prompt
                     (press any key to dismiss)

    The staged new-job flow is a sequence of picker/input overlays whose
    callbacks close over the in-progress form dict; there is no separate
    "new_job" overlay kind.
    """

    kind: str | None = None
    prompt: str = ""
    # confirm
    on_confirm: Callable[[bool], str | None] | None = None
    default: bool = False
    # input
    buffer: str = ""
    default_value: str = ""
    on_submit: Callable[[str], str | None] | None = None
    # picker
    items: list[Any] = field(default_factory=list)  # list[(label, value)]
    picker_index: int = 0
    on_pick: Callable[[Any], str | None] | None = None


@dataclass
class JobsScreenState:
    selected: int = 0
    filter: str = "all"
    scope: str = "project"  # "project" (cwd-only) or "all"
    cwd: str = field(default_factory=lambda: str(Path.cwd()))
    message: str = ""
    quit: bool = False
    cached_jobs: list[dict] = field(default_factory=list)
    atq_error: str | None = None
    show_detail: bool = True  # used in narrow mode
    show_help: bool = False
    search_query: str = ""
    overlay: OverlayState = field(default_factory=OverlayState)

    def refresh_jobs(self) -> None:
        jobs, atq_error = list_job_views(self.filter)
        if self.scope == "project":
            jobs = [j for j in jobs if (j.get("cwd") or "") == self.cwd]
        if self.search_query:
            needle = self.search_query.lower()
            jobs = [j for j in jobs if needle in (j.get("title") or "").lower()]
        self.cached_jobs = jobs
        self.atq_error = atq_error
        if not jobs:
            self.selected = 0
        else:
            self.selected %= len(jobs)

    def current_job(self) -> dict | None:
        if not self.cached_jobs:
            return None
        self.selected %= len(self.cached_jobs)
        return self.cached_jobs[self.selected]


# --- SummaryRowRenderer ---------------------------------------------------
# Width-aware compact summary rows. No pipe-delimited fake tables.
#
# Column widths by mode:
#   narrow  (< 80 cols) : title + status              (list-only)
#   medium (80-119)     : title + status + run_at + session
#   wide   (>= 120)     : title + status + run_at + session + updated
#
# Column widths are fixed except title, which flexes between
# TITLE_MIN .. TITLE_MAX given the remaining budget.

TITLE_MIN = 18
TITLE_IDEAL = 28
TITLE_MAX = 80
STATUS_W = 10
RUN_AT_W = 28
SESSION_W = 12
UPDATED_W = 16
CREATED_W = 16


def _format_delta_short(seconds: float) -> str:
    """Compact human delta: '5s', '42m', '3h 12m', '2d 4h'."""
    total = int(abs(seconds))
    if total < 60:
        return f"{total}s"
    minutes, _ = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, rem_min = divmod(minutes, 60)
    if hours < 24:
        if rem_min:
            return f"{hours}h {rem_min}m"
        return f"{hours}h"
    days, rem_hrs = divmod(hours, 24)
    if rem_hrs:
        return f"{days}d {rem_hrs}h"
    return f"{days}d"


def _relative_time(iso: str | None, now: datetime | None = None) -> str:
    """Return '(in 5m)' / '(4h ago)' / '(now)' for an ISO timestamp."""
    if not iso:
        return ""
    try:
        target = parse_iso_datetime(iso).astimezone()
    except Exception:
        return ""
    current = (now or datetime.now()).astimezone()
    delta = (target - current).total_seconds()
    if -30 <= delta <= 30:
        return "(now)"
    if delta > 0:
        return f"(in {_format_delta_short(delta)})"
    return f"({_format_delta_short(delta)} ago)"


def _elapsed_since(iso: str | None, now: datetime | None = None) -> str | None:
    """Return 'running for Xm Ys' given an ISO start timestamp, or None."""
    if not iso:
        return None
    try:
        started = parse_iso_datetime(iso).astimezone()
    except Exception:
        return None
    current = (now or datetime.now()).astimezone()
    delta = int((current - started).total_seconds())
    if delta < 0:
        delta = 0
    if delta < 60:
        return f"running for {delta}s"
    minutes, seconds = divmod(delta, 60)
    if minutes < 60:
        return f"running for {minutes}m {seconds}s"
    hours, rem_min = divmod(minutes, 60)
    return f"running for {hours}h {rem_min}m"


def _truncate(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(value) <= width:
        return value
    if width <= 1:
        return value[:width]
    return value[: width - 1] + "\u2026"


def _pad(value: str, width: int) -> str:
    truncated = _truncate(value, width)
    return truncated + " " * (width - len(truncated))


def _layout_mode(total_width: int) -> str:
    if total_width < 80:
        return "narrow"
    if total_width < 120:
        return "medium"
    if total_width < 160:
        return "wide"
    return "xwide"


def _summary_columns(mode: str, total_width: int) -> list[tuple[str, int]]:
    """Return ordered (column_name, width) tuples for the given layout mode.

    On wide/xwide terminals, the detail pane consumes roughly half the
    width (capped at DETAIL_MAX_W elsewhere), so the list budget is based
    on `total_width` rather than the full terminal width — callers are
    expected to pass the already-apportioned width when a detail pane is
    visible.
    """
    # Reserve 1 column for left gutter (selection marker), and gaps (1 each).
    # Build right-hand fixed columns first, then give the rest to title.
    if mode == "narrow":
        # title + status
        title_width = max(TITLE_MIN, min(TITLE_MAX, total_width - STATUS_W - 2))
        return [("title", title_width), ("status", STATUS_W)]
    if mode == "medium":
        # title + status + run_at + session
        fixed = STATUS_W + RUN_AT_W + SESSION_W + 3  # 3 single-space gaps
        title_width = max(TITLE_MIN, min(TITLE_MAX, total_width - fixed - 1))
        return [
            ("title", title_width),
            ("status", STATUS_W),
            ("run_at", RUN_AT_W),
            ("session", SESSION_W),
        ]
    if mode == "wide":
        fixed = STATUS_W + RUN_AT_W + SESSION_W + UPDATED_W + 4
        title_width = max(TITLE_MIN, min(TITLE_MAX, total_width - fixed - 1))
        return [
            ("title", title_width),
            ("status", STATUS_W),
            ("run_at", RUN_AT_W),
            ("session", SESSION_W),
            ("updated", UPDATED_W),
        ]
    # xwide
    fixed = STATUS_W + RUN_AT_W + SESSION_W + UPDATED_W + CREATED_W + 5
    title_width = max(TITLE_MIN, min(TITLE_MAX, total_width - fixed - 1))
    return [
        ("title", title_width),
        ("status", STATUS_W),
        ("run_at", RUN_AT_W),
        ("session", SESSION_W),
        ("updated", UPDATED_W),
        ("created", CREATED_W),
    ]


def _column_value(job: dict, column: str) -> str:
    if column == "title":
        return job.get("title") or "(invalid)"
    if column == "status":
        return job.get("display_label") or "Invalid"
    if column == "run_at":
        base = iso_to_display(job.get("scheduled_for")) or "-"
        rel = _relative_time(job.get("scheduled_for"))
        return f"{base} {rel}".rstrip() if rel else base
    if column == "session":
        sid = job.get("session_id")
        if sid:
            return sid[:SESSION_W]
        return job.get("session_mode") or "-"
    if column == "updated":
        return iso_to_display(job.get("updated_at")) or "-"
    if column == "created":
        return iso_to_display(job.get("created_at")) or "-"
    return ""


def _column_header(column: str) -> str:
    return {
        "title": "Title",
        "status": "Status",
        "run_at": "Run At",
        "session": "Session",
        "updated": "Updated",
        "created": "Created",
    }.get(column, column.title())


def render_summary_header(columns: list[tuple[str, int]]) -> str:
    parts = ["  "]  # gutter matches row prefix
    for column, width in columns:
        parts.append(_pad(_column_header(column), width))
        parts.append(" ")
    return "".join(parts).rstrip()


def render_summary_row(job: dict, columns: list[tuple[str, int]], selected: bool) -> str:
    marker = "> " if selected else "  "
    parts = [marker]
    for column, width in columns:
        parts.append(_pad(_column_value(job, column), width))
        parts.append(" ")
    return "".join(parts).rstrip()


# --- DetailRenderer -------------------------------------------------------
def render_detail(job: dict | None) -> str:
    if job is None:
        return "No job selected."
    return format_job_summary(job)


# --- Overlay input / editor ----------------------------------------------
_PRINTABLE_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    " _-+.,:;/@=()[]{}<>!?#$%&*'\"\\|^~`"
)


def _input_char_accept(ch: str) -> bool:
    if not ch:
        return False
    if len(ch) != 1:
        return False
    return ch in _PRINTABLE_CHARS


# --- dispatch_action ------------------------------------------------------
# Action architecture:
#   * Every action is a plain synchronous function.
#   * It returns a user-facing string on success, or raises OperationError
#     / KeyboardInterrupt on failure / cancellation.
#   * The dispatcher stores the result string as the screen message.
#   * No asyncio, no coroutines, no nested .run() dialogs.
def _dispatch_action(
    state: JobsScreenState,
    action: Callable[[], str],
    *,
    refresh: bool = True,
) -> None:
    try:
        message = action()
    except KeyboardInterrupt:
        state.message = "Cancelled."
    except OperationError as exc:
        state.message = f"error: {exc}"
    except Exception as exc:  # noqa: BLE001
        state.message = f"error: {exc}"
    else:
        if message:
            state.message = message
    finally:
        if refresh:
            state.refresh_jobs()


# --- Schedule picker helpers ---------------------------------------------
# The TUI scheduler is fully constrained: the user picks HH then MM (same
# shape as a clock input) and we treat the result as an offset from now,
# producing a string resolve_schedule_input accepts. Using an offset avoids
# ambiguity around "specific time" drifting into the past by a few minutes.
def _resolve_offset_pick(hours: int, minutes: int) -> str:
    """Return a schedule spec for `now + HH:MM` as total minutes."""
    total = hours * 60 + minutes
    if total <= 0:
        total = 1
    return f"now + {total} minutes"


HELP_TEXT = """\
Statuses
  Queued     Created, not yet submitted to at(1)
  Scheduled  Submitted to at(1), waiting to run
  Running    Currently executing
  Waiting    Waiting on a dependency job
  Blocked    A blocking condition prevents run
  Completed  Finished with exit code 0
  Failed     Finished with non-zero exit code
  Removed    Cancelled / removed from queue
  Invalid    On-disk metadata is broken

Actions
  N new          Create a job (agent -> session -> schedule -> prompt)
  Y duplicate    Duplicate selected job (reuse agent/session, edit prompt)
  E edit         Edit the selected job's prompt in $EDITOR
  L log          Tail (running) or page (completed) the job's log file
  P prefix       Edit the prompt prefix for Claude or Codex ($EDITOR)
  T reschedule   Change when the selected job runs
  C session      Change the selected job's session id
  U unschedule   Remove from at(1) but keep metadata (confirmed)
  S submit       Submit or repair the selected job
  R retry        Reschedule a completed/failed job
  D delete       Permanently delete the selected job
  f filter       Cycle: all / active / completed
  F scope        Toggle project (cwd) / all projects
  G refresh      Reload from disk now (auto every 30s)
  V detail       Toggle detail pane (narrow mode)
  / search       Filter by title substring (Esc clears)
  Home/End       Jump to first/last job
  PgUp/PgDn      Page up/down
  ? help         Toggle this help view
  Q quit         Exit the jobs screen
"""


# --- jobs_menu ------------------------------------------------------------
def jobs_menu() -> int:
    toolkit = _require_prompt_toolkit()
    Application = toolkit["Application"]
    KeyBindings = toolkit["KeyBindings"]
    Layout = toolkit["Layout"]
    HSplit = toolkit["HSplit"]
    VSplit = toolkit["VSplit"]
    Window = toolkit["Window"]
    ConditionalContainer = toolkit["ConditionalContainer"]
    FormattedTextControl = toolkit["FormattedTextControl"]
    Dimension = toolkit["Dimension"]
    Style = toolkit["Style"]

    from prompt_toolkit.application import run_in_terminal
    from prompt_toolkit.filters import Condition

    state = JobsScreenState()
    state.refresh_jobs()

    def _terminal_width() -> int:
        try:
            return _shutil.get_terminal_size(fallback=(80, 24)).columns
        except Exception:
            return 80

    def _layout_mode_now() -> str:
        return _layout_mode(_terminal_width())

    # ---------- overlay openers ----------
    def open_confirm(
        prompt: str,
        on_confirm: Callable[[bool], str | None],
        default: bool = False,
    ) -> None:
        state.overlay = OverlayState(
            kind="confirm",
            prompt=prompt,
            on_confirm=on_confirm,
            default=default,
        )

    def open_input(
        prompt: str,
        default: str,
        on_submit: Callable[[str], str | None],
    ) -> None:
        state.overlay = OverlayState(
            kind="input",
            prompt=prompt,
            buffer=default,
            default_value=default,
            on_submit=on_submit,
        )

    def open_picker(
        prompt: str,
        items: list[tuple[str, Any]],
        on_pick: Callable[[Any], str | None],
    ) -> None:
        state.overlay = OverlayState(
            kind="picker",
            prompt=prompt,
            items=items,
            picker_index=0,
            on_pick=on_pick,
        )

    # ---------- action implementations ----------
    def action_reschedule(job: dict, spec: str) -> str:
        updated = reschedule_job(job["id"], spec)
        run_at = iso_to_display(updated["scheduled_for"], with_seconds=True)
        return f"Rescheduled {job['id']} for {run_at}."

    def action_retry(job: dict, spec: str) -> str:
        updated = retry_job(job["id"], spec)
        run_at = iso_to_display(updated["scheduled_for"], with_seconds=True)
        return f"Retry scheduled {job['id']} for {run_at}."

    def action_change_session(job: dict, session_id: str | None) -> str:
        updated = change_session(job["id"], session_id)
        label = (
            updated["session_id"][:12] if updated.get("session_id") else updated.get("session_mode")
        )
        return f"Session for {job['id']} set to {label}."

    def action_unschedule(job: dict) -> str:
        unschedule_job(job["id"])
        return f"Removed {job['id']} from queue."

    def action_submit(job: dict) -> str:
        updated = submit_or_repair_job(job["id"])
        return f"Submitted {job['id']} (at job {updated.get('at_job_id')})."

    def action_delete(job: dict) -> str:
        delete_job(job["id"])
        return f"Deleted {job['id']}."

    def action_force_delete(job: dict) -> str:
        # Used for entries whose metadata is broken enough that the normal
        # delete path cannot run. Bypasses scheduler interaction.
        jobs = load_jobs()
        idx, _stored = find_job(jobs, job["id"])
        if idx is None:
            raise OperationError(f"No such job: {job['id']}")
        del jobs[idx]
        save_jobs(jobs)
        return f"Force-deleted {job['id']}."

    def action_view_log(job: dict) -> str | None:
        log_file = job.get("last_log_file")
        if not log_file:
            raise OperationError("No log file recorded for this job yet.")
        path = Path(log_file)
        if not path.exists():
            raise OperationError(f"Log file missing: {path}")
        is_running = job.get("display_state") == "running"
        if is_running:
            cmd = ["tail", "-f", str(path)]
        else:
            pager = os.environ.get("PAGER") or "less"
            try:
                cmd = [*shlex.split(pager), str(path)]
            except ValueError:
                cmd = [pager, str(path)]

        def _suspend_and_view() -> None:
            try:
                subprocess.run(cmd, check=False)
            except FileNotFoundError as exc:
                state.message = f"error: {exc}"
                state.refresh_jobs()
                return
            except Exception as exc:  # noqa: BLE001
                state.message = f"error: {exc}"
                state.refresh_jobs()
                return
            state.message = (
                f"Stopped tailing log for {job['id']}."
                if is_running
                else f"Closed log for {job['id']}."
            )
            state.refresh_jobs()

        run_in_terminal(_suspend_and_view)
        return None

    def action_edit_prefix(agent: str) -> str | None:
        path = ensure_prompt_prefix(agent)

        def _suspend_and_edit() -> None:
            try:
                edit_file(path)
                state.message = f"Updated {agent} prompt prefix."
            except Exception as exc:  # noqa: BLE001
                state.message = f"error: {exc}"
            state.refresh_jobs()

        run_in_terminal(_suspend_and_edit)
        return None

    def start_prefix_edit_flow() -> None:
        items = [(cfg["label"], key) for key, cfg in AGENTS.items()]
        open_picker(
            "Edit prompt prefix: pick agent",
            items,
            on_pick=lambda agent: action_edit_prefix(agent),
        )

    def action_edit_prompt(job: dict) -> str | None:
        path = Path(job["prompt_file"])
        if not path.exists():
            raise OperationError(f"Prompt file not found: {path}")

        # `run_in_terminal` schedules `func` to run once the TUI is
        # suspended and returns immediately with a Future. The post-edit
        # refresh therefore has to happen INSIDE the callback, not after,
        # or it would race with the editor.
        def _suspend_and_edit() -> None:
            try:
                edit_file(path)
                refresh_prompt(job["id"])
                state.message = f"Prompt updated for {job['id']}."
            except OperationError as exc:
                state.message = f"error: {exc}"
            except Exception as exc:  # noqa: BLE001
                state.message = f"error: {exc}"
            state.refresh_jobs()

        run_in_terminal(_suspend_and_edit)
        return None

    def action_create_job(form: dict) -> str:
        agent = form["agent"]
        session_id = form.get("session_id")
        session_mode = "resume" if session_id else "new"
        spec = form["schedule_spec"]
        prompt = form["prompt"]
        cwd = form.get("cwd") or str(Path.cwd())
        submit = form.get("submit", True)
        job = create_job(
            agent=agent,
            session_mode=session_mode,
            session_id=session_id,
            prompt_text=prompt,
            schedule_spec=spec,
            cwd=cwd,
            submit=submit,
        )
        if submit:
            run_at = iso_to_display(job["scheduled_for"], with_seconds=True)
            return f"Scheduled {job['id']} for {run_at}."
        return f"Created {job['id']} (not submitted)."

    # ---------- schedule picker (generic) ----------
    # Two-stage constrained flow: pick HH, then pick MM (in 5-min steps).
    # The (hh, mm) pair is interpreted as an offset from now and converted
    # to a spec string that resolve_schedule_input accepts. `on_spec(spec)`
    # is called once the user has finished picking. Callers: new-job,
    # reschedule, retry.
    def schedule_picker_start(prompt_prefix: str, on_spec: Callable[[str], str | None]) -> None:
        hour_items = [(f"{h:02d}", h) for h in range(24)]
        open_picker(
            f"{prompt_prefix}: hours from now",
            hour_items,
            on_pick=lambda hour: _schedule_pick_minute(prompt_prefix, hour, on_spec),
        )

    def _schedule_pick_minute(
        prompt_prefix: str,
        hour: int,
        on_spec: Callable[[str], str | None],
    ) -> str | None:
        items = [(f"{m:02d}", m) for m in range(0, 60, 5)]
        open_picker(
            f"{prompt_prefix}: minutes from now",
            items,
            on_pick=lambda minute: on_spec(_resolve_offset_pick(hour, minute)),
        )
        return None

    # ---------- staged new-job flow ----------
    # The flow is a sequence of picker/input/editor steps. The in-progress
    # form dict is held in closures passed to each step's callback; it is
    # not stashed on the overlay, because overlay handlers clear the
    # overlay BEFORE invoking on_pick/on_submit, which would otherwise
    # lose the form between steps.
    def start_new_job_flow() -> None:
        form: dict = {
            "agent": None,
            "session_id": None,
            "schedule_spec": "now + 5 minutes",
            "prompt": "",
            "cwd": str(Path.cwd()),
        }
        _nj_pick_agent(form)

    def _nj_pick_agent(form: dict) -> None:
        items = [(cfg["label"], key) for key, cfg in AGENTS.items()]
        open_picker(
            "New job: pick agent",
            items,
            on_pick=lambda value: _nj_picked_agent(form, value),
        )

    def _nj_picked_agent(form: dict, agent: str) -> str | None:
        form["agent"] = agent
        _nj_pick_session(form)
        return None

    def _nj_pick_session(form: dict) -> None:
        sessions = discover_sessions(form["agent"], cwd=Path(form["cwd"]))
        items: list[tuple[str, Any]] = [("New session", None)]
        for session in sessions:
            title = session.title or "[no title]"
            label = f"{title} [{session.id[:8]}]"
            items.append((label, session.id))
        items.append((PASTE_SESSION_LABEL, PASTE_SESSION_LABEL))

        def on_pick(value: Any) -> str | None:
            if value == PASTE_SESSION_LABEL:
                open_input(
                    "Paste session ID",
                    "",
                    lambda pasted: _nj_picked_session(form, pasted.strip() or None),
                )
                return None
            return _nj_picked_session(form, value)

        open_picker("New job: pick session", items, on_pick=on_pick)

    def _nj_picked_session(form: dict, session_id: str | None) -> str | None:
        form["session_id"] = session_id
        _nj_pick_schedule(form)
        return None

    def _nj_pick_schedule(form: dict) -> None:
        schedule_picker_start(
            "New job: schedule",
            on_spec=lambda spec: _nj_picked_schedule(form, spec),
        )

    def _nj_picked_schedule(form: dict, spec: str) -> str | None:
        form["schedule_spec"] = spec
        _nj_capture_prompt(form)
        return None

    def _nj_capture_prompt(form: dict) -> None:
        # `run_in_terminal` is async — the callback must contain everything
        # that happens after the editor exits. When the editor closes, we
        # arm the submit-confirm overlay so it appears as soon as the TUI
        # redraws.
        initial = form.get("prompt", "") or ""

        def _suspend_and_capture() -> None:
            try:
                text = read_prompt(initial=initial)
            except KeyboardInterrupt:
                state.message = "New job cancelled."
                state.refresh_jobs()
                return
            except Exception as exc:  # noqa: BLE001
                state.message = f"error: {exc}"
                state.refresh_jobs()
                return
            form["prompt"] = text or ""
            state.overlay = OverlayState(
                kind="confirm",
                prompt="Submit to at queue now?",
                default=True,
                on_confirm=lambda submit: _nj_confirmed_submit(form, submit),
            )

        run_in_terminal(_suspend_and_capture)

    def _nj_confirmed_submit(form: dict, submit: bool) -> str | None:
        form["submit"] = submit
        return action_create_job(form)

    def start_duplicate_job_flow(job: dict) -> None:
        """Duplicate selected job: reuse agent/session, pre-fill prompt, pick schedule."""
        try:
            existing_prompt = Path(job["prompt_file"]).read_text(encoding="utf-8")
        except Exception:
            existing_prompt = ""
        form: dict = {
            "agent": job.get("agent"),
            "session_id": job.get("session_id"),
            "schedule_spec": "now + 5 minutes",
            "prompt": existing_prompt,
            "cwd": job.get("cwd") or str(Path.cwd()),
        }
        _nj_pick_schedule(form)

    # ---------- rendering ----------
    _COUNT_ORDER = [
        ("Q", "queued"),
        ("S", "scheduled"),
        ("R", "running"),
        ("W", "waiting"),
        ("B", "blocked"),
        ("C", "completed"),
        ("F", "failed"),
        ("X", "removed"),
        ("!", "invalid"),
    ]

    def _status_counts_text() -> str:
        counts: dict[str, int] = {}
        for job in state.cached_jobs:
            key = job.get("display_state") or "invalid"
            counts[key] = counts.get(key, 0) + 1
        parts = [
            f"{letter}:{counts.get(state_key, 0)}"
            for letter, state_key in _COUNT_ORDER
            if counts.get(state_key, 0) > 0
        ]
        return " ".join(parts) if parts else "empty"

    def header_fragments():
        timezone = datetime.now().astimezone().tzname() or "local"
        count = len(state.cached_jobs)
        counts_text = _status_counts_text()
        scope_label = "Project" if state.scope == "project" else "All"
        text = (
            f" Jobs   Scope: {scope_label}   Filter: {state.filter.title()}   "
            f"TZ: {timezone}   {count} item{'s' if count != 1 else ''}   "
            f"[{counts_text}]"
        )
        if state.search_query:
            text += f"   Search: {state.search_query} ({count})"
        text += " "
        return [("class:header", text)]

    def _visible_row_budget() -> int:
        try:
            term_lines = _shutil.get_terminal_size(fallback=(80, 24)).lines
        except Exception:
            term_lines = 24
        # header(1) + footer(2) + table-header(1)
        reserved = 4
        if _layout_mode_now() == "narrow" and (state.show_detail or state.show_help):
            # detail stacked under summary → roughly halve remaining space
            return max(3, (term_lines - reserved - 1) // 2)
        return max(3, term_lines - reserved)

    def summary_fragments():
        mode = _layout_mode_now()
        width = _terminal_width()
        columns = _summary_columns(mode, width)

        fragments: list[tuple[str, str]] = []
        fragments.append(("class:table-header", render_summary_header(columns) + "\n"))

        if not state.cached_jobs:
            if state.search_query:
                msg = f"No jobs match '{state.search_query}'. Press / to edit, Esc to clear.\n"
            else:
                msg = "No jobs. Press N to create one.\n"
            fragments.append(("class:muted", msg))
            return fragments

        n = len(state.cached_jobs)
        budget = _visible_row_budget()
        if n <= budget:
            start, end = 0, n
        else:
            start = max(0, state.selected - budget // 2)
            start = min(start, n - budget)
            end = start + budget

        if start > 0:
            fragments.append(("class:muted", f"  ... {start} above\n"))

        for index in range(start, end):
            job = state.cached_jobs[index]
            selected = index == state.selected
            # Selected row uses the reverse-video selected style uniformly so
            # the highlight stays legible; everything else is either normal
            # or muted (completed), with the status column getting its own
            # per-state colour for quick visual scanning.
            row_style = ""
            if selected:
                row_style = "class:selected"
            elif job.get("display_state") == "completed":
                row_style = "class:muted"
            fragments.append((row_style, "> " if selected else "  "))
            for column, col_width in columns:
                padded = _pad(_column_value(job, column), col_width)
                if column == "status" and not selected:
                    status_style = f"class:status-{job.get('display_state', 'invalid')}"
                    fragments.append((status_style, padded))
                else:
                    fragments.append((row_style, padded))
                fragments.append((row_style, " "))
            fragments.append((row_style, "\n"))

        if end < n:
            fragments.append(("class:muted", f"  ... {n - end} below\n"))
        return fragments

    def detail_fragments():
        if state.show_help:
            return [("", HELP_TEXT)]
        job = state.current_job()
        fragments: list[tuple[str, str]] = []
        if job is not None and job.get("display_state") == "running":
            elapsed = _elapsed_since(job.get("last_started_at"))
            if elapsed:
                fragments.append(("class:status-running", f"{elapsed}\n\n"))
        fragments.append(("", render_detail(job) + "\n"))
        return fragments

    def _maybe_dismiss_help() -> None:
        if state.show_help:
            state.show_help = False

    _HELP_HINT_PAIRS: list[tuple[str, str]] = [
        ("N", "ew"),
        ("Y", " dup"),
        ("E", "dit"),
        ("L", "og"),
        ("P", "refix"),
        ("T", " time"),
        ("C", " session"),
        ("U", "nschedule"),
        ("S", "ubmit"),
        ("R", "etry"),
        ("D", "elete"),
        ("f", "ilter"),
        ("F", " scope"),
        ("/", " search"),
        ("G", " refresh"),
        ("V", " detail"),
        ("?", " help"),
        ("Q", "uit"),
    ]

    def _help_hint_fragments() -> list[tuple[str, str]]:
        fragments: list[tuple[str, str]] = [("class:footer", " ")]
        for key, rest in _HELP_HINT_PAIRS:
            fragments.append(("class:key", key))
            fragments.append(("class:footer", f"{rest}  "))
        fragments.append(("class:footer", "\n"))
        return fragments

    def footer_fragments():
        fragments: list[tuple[str, str]] = _help_hint_fragments()
        overlay = state.overlay
        if overlay.kind == "confirm":
            hint = "[Y/n]" if overlay.default else "[y/N]"
            fragments.append(("class:overlay", f" {overlay.prompt} {hint} "))
            return fragments
        if overlay.kind == "input":
            fragments.append(("class:overlay", f" {overlay.prompt}: {overlay.buffer}_ "))
            return fragments
        if overlay.kind == "message":
            fragments.append(("class:overlay", f" {overlay.prompt} (press any key) "))
            return fragments
        if state.message:
            fragments.append(("class:message", f" {state.message} "))
        if state.atq_error:
            fragments.append(("class:message", f" atq: {state.atq_error} "))
        return fragments

    def picker_fragments():
        overlay = state.overlay
        if overlay.kind != "picker":
            return [("", "")]
        fragments: list[tuple[str, str]] = [("class:overlay-title", f" {overlay.prompt} \n")]
        if not overlay.items:
            fragments.append(("class:muted", " (no choices) \n"))
            return fragments
        for idx, (label, _value) in enumerate(overlay.items):
            marker = "> " if idx == overlay.picker_index else "  "
            style = "class:selected" if idx == overlay.picker_index else ""
            fragments.append((style, f"{marker}{label}\n"))
        fragments.append(
            (
                "class:muted",
                " (Up/Down to move, Enter to pick, Esc to cancel) \n",
            )
        )
        return fragments

    # ---------- overlay input handlers ----------
    def overlay_confirm_key(ch: str) -> None:
        overlay = state.overlay
        if overlay.kind != "confirm" or overlay.on_confirm is None:
            return
        answered: bool | None = None
        if ch in ("y", "Y"):
            answered = True
        elif ch in ("n", "N", "escape"):
            answered = False

        if answered is None:
            return

        on_confirm = overlay.on_confirm
        state.overlay = OverlayState()

        def _run() -> str:
            result = on_confirm(answered)
            return result or ""

        _dispatch_action(state, _run)

    def overlay_input_submit() -> None:
        overlay = state.overlay
        if overlay.kind != "input" or overlay.on_submit is None:
            return
        value = overlay.buffer
        on_submit = overlay.on_submit
        state.overlay = OverlayState()

        def _run() -> str:
            result = on_submit(value)
            return result or ""

        _dispatch_action(state, _run)

    def overlay_picker_submit() -> None:
        overlay = state.overlay
        if overlay.kind != "picker" or overlay.on_pick is None:
            return
        if not overlay.items:
            state.overlay = OverlayState()
            return
        idx = overlay.picker_index % len(overlay.items)
        _label, value = overlay.items[idx]
        on_pick = overlay.on_pick
        state.overlay = OverlayState()

        def _run() -> str:
            result = on_pick(value)
            return result or ""

        _dispatch_action(state, _run)

    # ---------- keybindings ----------
    kb = KeyBindings()

    no_overlay = Condition(lambda: state.overlay.kind is None)
    confirm_overlay = Condition(lambda: state.overlay.kind == "confirm")
    input_overlay = Condition(lambda: state.overlay.kind == "input")
    picker_overlay = Condition(lambda: state.overlay.kind == "picker")

    # ----- overlay: confirm -----
    @kb.add("y", filter=confirm_overlay)
    @kb.add("Y", filter=confirm_overlay)
    def _confirm_yes(event):
        overlay_confirm_key("y")

    @kb.add("n", filter=confirm_overlay)
    @kb.add("N", filter=confirm_overlay)
    def _confirm_no(event):
        overlay_confirm_key("n")

    @kb.add("escape", filter=confirm_overlay)
    def _confirm_esc(event):
        overlay_confirm_key("escape")

    @kb.add("enter", filter=confirm_overlay)
    def _confirm_enter(event):
        overlay = state.overlay
        if overlay.kind != "confirm" or overlay.on_confirm is None:
            return
        overlay_confirm_key("y" if overlay.default else "n")

    # ----- overlay: input -----
    @kb.add("enter", filter=input_overlay)
    def _input_enter(event):
        overlay_input_submit()

    @kb.add("escape", filter=input_overlay)
    def _input_esc(event):
        state.overlay = OverlayState()
        state.message = "Cancelled."

    @kb.add("backspace", filter=input_overlay)
    def _input_backspace(event):
        overlay = state.overlay
        if overlay.buffer:
            overlay.buffer = overlay.buffer[:-1]

    @kb.add("c-u", filter=input_overlay)
    def _input_clear(event):
        state.overlay.buffer = ""

    @kb.add("<any>", filter=input_overlay)
    def _input_any(event):
        ch = event.data or ""
        if _input_char_accept(ch):
            state.overlay.buffer += ch

    # ----- overlay: picker -----
    @kb.add("up", filter=picker_overlay)
    @kb.add("k", filter=picker_overlay)
    def _picker_up(event):
        overlay = state.overlay
        if overlay.items:
            overlay.picker_index = (overlay.picker_index - 1) % len(overlay.items)

    @kb.add("down", filter=picker_overlay)
    @kb.add("j", filter=picker_overlay)
    def _picker_down(event):
        overlay = state.overlay
        items = overlay.items
        if items:
            overlay.picker_index = (overlay.picker_index + 1) % len(items)

    @kb.add("enter", filter=picker_overlay)
    def _picker_enter(event):
        overlay_picker_submit()

    @kb.add("escape", filter=picker_overlay)
    def _picker_esc(event):
        state.overlay = OverlayState()
        state.message = "Cancelled."

    # ----- normal keybindings (no overlay) -----
    # Convention: every no_overlay handler starts by calling
    # _maybe_dismiss_help() so pressing any top-level key closes the help
    # view and still runs the action. `?` itself toggles, bypassing that.
    @kb.add("?", filter=no_overlay)
    def _toggle_help(event):
        state.show_help = not state.show_help

    @kb.add("q", filter=no_overlay)
    def _quit(event):
        _maybe_dismiss_help()
        state.quit = True
        event.app.exit()

    @kb.add("j", filter=no_overlay)
    @kb.add("down", filter=no_overlay)
    def _down(event):
        _maybe_dismiss_help()
        if state.cached_jobs:
            state.selected = (state.selected + 1) % len(state.cached_jobs)

    @kb.add("k", filter=no_overlay)
    @kb.add("up", filter=no_overlay)
    def _up(event):
        _maybe_dismiss_help()
        if state.cached_jobs:
            state.selected = (state.selected - 1) % len(state.cached_jobs)

    @kb.add("f", filter=no_overlay)
    def _cycle_filter(event):
        _maybe_dismiss_help()
        filters = ["all", "active", "completed"]
        state.filter = filters[(filters.index(state.filter) + 1) % len(filters)]
        state.selected = 0
        state.refresh_jobs()
        state.message = f"Filter: {state.filter}."

    @kb.add("F", filter=no_overlay)
    def _cycle_scope(event):
        _maybe_dismiss_help()
        state.scope = "all" if state.scope == "project" else "project"
        state.selected = 0
        state.refresh_jobs()
        label = "project (cwd)" if state.scope == "project" else "all projects"
        state.message = f"Scope: {label}."

    @kb.add("g", filter=no_overlay)
    def _refresh(event):
        _maybe_dismiss_help()
        state.refresh_jobs()
        state.message = "Refreshed."

    @kb.add("v", filter=no_overlay)
    def _toggle_detail(event):
        _maybe_dismiss_help()
        state.show_detail = not state.show_detail
        state.message = "Detail shown." if state.show_detail else "Detail hidden."

    @kb.add("enter", filter=no_overlay)
    def _enter_toggle_detail(event):
        _maybe_dismiss_help()
        # In narrow mode, Enter toggles detail; in wider modes it is a
        # no-op (detail already visible beside the list).
        if _layout_mode_now() == "narrow":
            state.show_detail = not state.show_detail

    @kb.add("n", filter=no_overlay)
    def _new(event):
        _maybe_dismiss_help()
        start_new_job_flow()

    @kb.add("e", filter=no_overlay)
    def _edit(event):
        _maybe_dismiss_help()
        job = state.current_job()
        if not job:
            state.message = "No job selected."
            return
        _dispatch_action(state, lambda: action_edit_prompt(job))

    @kb.add("p", filter=no_overlay)
    def _prefix(event):
        _maybe_dismiss_help()
        start_prefix_edit_flow()

    @kb.add("l", filter=no_overlay)
    def _log(event):
        _maybe_dismiss_help()
        job = state.current_job()
        if not job:
            state.message = "No job selected."
            return
        _dispatch_action(state, lambda: action_view_log(job))

    @kb.add("t", filter=no_overlay)
    def _reschedule(event):
        _maybe_dismiss_help()
        job = state.current_job()
        if not job:
            state.message = "No job selected."
            return
        schedule_picker_start(
            f"Reschedule {job['id']}",
            on_spec=lambda spec: action_reschedule(job, spec),
        )

    @kb.add("c", filter=no_overlay)
    def _session(event):
        _maybe_dismiss_help()
        job = state.current_job()
        if not job:
            state.message = "No job selected."
            return
        sessions = discover_sessions(job["agent"], cwd=Path(job["cwd"]))
        items: list[tuple[str, Any]] = [("New session", None)]
        for session in sessions:
            title = session.title or "[no title]"
            label = f"{title} [{session.id[:8]}]"
            items.append((label, session.id))
        items.append((PASTE_SESSION_LABEL, PASTE_SESSION_LABEL))

        def on_pick(value: Any) -> str | None:
            if value == PASTE_SESSION_LABEL:
                open_input(
                    "Paste session ID",
                    "",
                    lambda pasted: action_change_session(job, pasted.strip() or None),
                )
                return None
            return action_change_session(job, value)

        open_picker(f"Session for {job['id']}", items, on_pick=on_pick)

    @kb.add("u", filter=no_overlay)
    def _unschedule(event):
        _maybe_dismiss_help()
        job = state.current_job()
        if not job:
            state.message = "No job selected."
            return

        def on_confirm(answered: bool) -> str | None:
            if not answered:
                return "Unschedule cancelled."
            return action_unschedule(job)

        open_confirm(f"Remove {job['id']} from queue?", on_confirm)

    @kb.add("s", filter=no_overlay)
    def _submit(event):
        _maybe_dismiss_help()
        job = state.current_job()
        if not job:
            state.message = "No job selected."
            return
        _dispatch_action(state, lambda: action_submit(job))

    @kb.add("r", filter=no_overlay)
    def _retry(event):
        _maybe_dismiss_help()
        job = state.current_job()
        if not job:
            state.message = "No job selected."
            return
        schedule_picker_start(
            f"Retry {job['id']}",
            on_spec=lambda spec: action_retry(job, spec),
        )

    @kb.add("d", filter=no_overlay)
    def _delete(event):
        _maybe_dismiss_help()
        job = state.current_job()
        if not job:
            state.message = "No job selected."
            return

        def on_confirm(answered: bool) -> str | None:
            if not answered:
                return "Delete cancelled."
            # Jobs whose on-disk metadata is broken can't go through the
            # normal delete path (scheduler calls would fail). Force-delete
            # bypasses the scheduler; it is scoped to `_invalid` entries
            # only, so that real errors (e.g. "cannot delete while
            # running") surface to the user instead of being papered over.
            if job.get("_invalid"):
                return action_force_delete(job)
            return action_delete(job)

        open_confirm(f"Delete {job['id']} permanently?", on_confirm)

    def _page_size() -> int:
        try:
            term_lines = _shutil.get_terminal_size(fallback=(80, 24)).lines
        except Exception:
            term_lines = 24
        return max(1, term_lines - 6)

    @kb.add("home", filter=no_overlay)
    def _home(event):
        _maybe_dismiss_help()
        if state.cached_jobs:
            state.selected = 0

    @kb.add("end", filter=no_overlay)
    def _end(event):
        _maybe_dismiss_help()
        if state.cached_jobs:
            state.selected = len(state.cached_jobs) - 1

    @kb.add("pageup", filter=no_overlay)
    def _pageup(event):
        _maybe_dismiss_help()
        if state.cached_jobs:
            state.selected = max(0, state.selected - _page_size())

    @kb.add("pagedown", filter=no_overlay)
    def _pagedown(event):
        _maybe_dismiss_help()
        if state.cached_jobs:
            state.selected = min(len(state.cached_jobs) - 1, state.selected + _page_size())

    @kb.add("/", filter=no_overlay)
    def _search(event):
        _maybe_dismiss_help()

        def on_submit(value: str) -> str | None:
            state.search_query = value.strip()
            state.refresh_jobs()
            if state.search_query:
                return f"Search: {state.search_query}"
            return "Search cleared."

        open_input("Search (empty to clear)", state.search_query, on_submit)

    @kb.add("escape", filter=no_overlay)
    def _clear_search(event):
        if state.search_query:
            state.search_query = ""
            state.refresh_jobs()
            state.message = "Search cleared."

    @kb.add("y", filter=no_overlay)
    def _duplicate(event):
        _maybe_dismiss_help()
        job = state.current_job()
        if not job:
            state.message = "No job selected."
            return
        start_duplicate_job_flow(job)

    @kb.add("c-c")
    def _ctrl_c(event):
        state.quit = True
        event.app.exit()

    # ---------- layout ----------
    # Main body: list on the left, detail on the right — but the detail pane
    # is only shown when width is large enough and the user hasn't toggled
    # it off (narrow mode).
    show_detail_pane = Condition(
        lambda: _layout_mode_now() != "narrow" and (state.show_detail or state.show_help)
    )
    show_detail_overlay = Condition(
        lambda: _layout_mode_now() == "narrow" and (state.show_detail or state.show_help)
    )
    show_picker_overlay = Condition(lambda: state.overlay.kind == "picker")

    summary_window = Window(
        content=FormattedTextControl(summary_fragments),
        wrap_lines=False,
        dont_extend_width=False,
    )
    detail_window = Window(
        content=FormattedTextControl(detail_fragments),
        wrap_lines=True,
        width=Dimension(preferred=60, max=80, min=30),
    )
    picker_window = Window(
        content=FormattedTextControl(picker_fragments),
        wrap_lines=False,
        height=Dimension(min=3, max=16),
    )

    body = HSplit(
        [
            VSplit(
                [
                    summary_window,
                    ConditionalContainer(
                        Window(width=1, char="\u2502"),
                        filter=show_detail_pane,
                    ),
                    ConditionalContainer(detail_window, filter=show_detail_pane),
                ]
            ),
            ConditionalContainer(
                HSplit(
                    [Window(height=1, char="\u2500"), detail_window],
                ),
                filter=show_detail_overlay,
            ),
            ConditionalContainer(picker_window, filter=show_picker_overlay),
        ]
    )

    root = HSplit(
        [
            Window(height=1, content=FormattedTextControl(header_fragments)),
            body,
            Window(height=2, content=FormattedTextControl(footer_fragments)),
        ]
    )

    style = Style.from_dict(
        {
            "header": "reverse bold",
            "table-header": "bold",
            "selected": "reverse",
            "muted": "fg:#888888",
            "footer": "reverse",
            "key": "reverse bold underline",
            "message": "fg:#ffaf00",
            "overlay": "fg:#ffffff bg:#005f87 bold",
            "overlay-title": "bold",
            "status-queued": "fg:#ffd75f",
            "status-scheduled": "fg:#5fd7ff",
            "status-running": "fg:#5fff5f bold",
            "status-waiting": "fg:#ffaf00",
            "status-blocked": "fg:#ff5f5f",
            "status-completed": "fg:#87d787",
            "status-failed": "fg:#ff5f5f bold",
            "status-removed": "fg:#888888",
            "status-invalid": "fg:#ff5fff",
        }
    )

    app = Application(
        layout=Layout(root),
        key_bindings=kb,
        full_screen=True,
        style=style,
        mouse_support=False,
    )

    async def _auto_refresh() -> None:
        # Silent 30s tick. Skips while an overlay is open so the user's
        # in-progress picker/confirm/input doesn't visibly reset.
        try:
            while True:
                await asyncio.sleep(30)
                if state.quit:
                    return
                if state.overlay.kind is None:
                    state.refresh_jobs()
                    app.invalidate()
        except asyncio.CancelledError:
            return

    def _start_background_tasks() -> None:
        app.create_background_task(_auto_refresh())

    app.run(pre_run=_start_background_tasks)
    return 0


# --- argparse + entrypoint ------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Schedule Codex and Claude CLI jobs.",
    )
    sub = parser.add_subparsers(dest="command")

    list_p = sub.add_parser("list", help="List jobs.")
    list_p.add_argument("--filter", choices=["all", "active", "completed"], default="all")

    show_p = sub.add_parser("show", help="Show one job.")
    show_p.add_argument("job_id")

    del_p = sub.add_parser("delete", help="Delete one job permanently.")
    del_p.add_argument("job_id")

    unschedule_p = sub.add_parser(
        "unschedule",
        help="Remove a job from the at queue without deleting it.",
    )
    unschedule_p.add_argument("job_id")

    cancel_p = sub.add_parser("cancel", help=argparse.SUPPRESS)
    cancel_p.add_argument("job_id")

    edit_p = sub.add_parser(
        "edit-prompt",
        help="Edit a job prompt and re-sync the scheduler if needed.",
    )
    edit_p.add_argument("job_id")

    res_p = sub.add_parser("reschedule", help="Reschedule one job.")
    res_p.add_argument("job_id")
    res_p.add_argument("when")

    set_ses_p = sub.add_parser("set-session", help="Change session for a job.")
    set_ses_p.add_argument("job_id")
    set_ses_p.add_argument("session", nargs="?")
    set_ses_p.add_argument("--new", action="store_true")

    ses_alias_p = sub.add_parser("session", help=argparse.SUPPRESS)
    ses_alias_p.add_argument("job_id")
    ses_alias_p.add_argument("session", nargs="?")
    ses_alias_p.add_argument("--new", action="store_true")

    retry_p = sub.add_parser("retry", help="Retry a completed/failed job at a new time.")
    retry_p.add_argument("job_id")
    retry_p.add_argument("when")

    sub_p = sub.add_parser("submit", help="Submit or repair a queued/scheduled job.")
    sub_p.add_argument("job_id")

    mark_p = sub.add_parser("mark", help="Update job execution state (for scheduled wrapper use).")
    mark_sub = mark_p.add_subparsers(dest="mark_state")
    run_p = mark_sub.add_parser("running")
    run_p.add_argument("job_id")
    run_p.add_argument("--started-at", required=True)
    run_p.add_argument("--log-file", required=True)

    done_p = mark_sub.add_parser("done")
    done_p.add_argument("job_id")
    done_p.add_argument("--finished-at", required=True)
    done_p.add_argument("--exit-code", required=True, type=int)
    done_p.add_argument("--log-file")

    failed_p = mark_sub.add_parser("failed")
    failed_p.add_argument("job_id")
    failed_p.add_argument("--finished-at", required=True)
    failed_p.add_argument("--exit-code", required=True, type=int)
    failed_p.add_argument("--log-file")

    doctor_p = sub.add_parser(
        "doctor",
        help="Run environment preflight checks and print a report.",
    )
    doctor_p.add_argument(
        "--roundtrip",
        action="store_true",
        help="Also submit and cancel a real `at` job to verify atd.",
    )
    doctor_p.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of the human table.",
    )
    verbosity = doctor_p.add_mutually_exclusive_group()
    verbosity.add_argument("--verbose", action="store_true", help="Include PASS/SKIP details.")
    verbosity.add_argument("--quiet", action="store_true", help="Only show WARN/FAIL rows.")

    return parser


def _doctor_filter(results: list, quiet: bool) -> list:
    if quiet:
        return [r for r in results if r.severity in ("FAIL", "WARN")]
    return list(results)


def cli_doctor(
    roundtrip: bool = False,
    as_json: bool = False,
    verbose: bool = False,
    quiet: bool = False,
) -> int:
    from . import preflight

    report = preflight.run_checks(include_roundtrip=roundtrip)

    if as_json:
        payload = {
            "critical_ok": report.critical_ok(),
            "results": [
                {
                    "name": r.name,
                    "label": r.label,
                    "severity": r.severity,
                    "message": r.message,
                    "detail": r.detail,
                }
                for r in report.results
            ],
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0 if report.critical_ok() else 1

    rows = _doctor_filter(report.results, quiet=quiet)
    label_width = max((len(r.label) for r in rows), default=0)
    sev_width = max((len(r.severity) for r in rows), default=4)
    for r in rows:
        print(f"{r.severity:<{sev_width}}  {r.label:<{label_width}}  {r.message}")
        if verbose and r.detail:
            for key, value in r.detail.items():
                print(f"{'':<{sev_width}}  {'':<{label_width}}    {key}: {value}")

    if report.critical_failures():
        print()
        print(f"{len(report.critical_failures())} critical failure(s).")
        return 1
    if report.warnings() and not quiet:
        print()
        print(f"{len(report.warnings())} warning(s).")
    return 0


def _print_deprecated_cli_surface(command: str, replacement: str) -> None:
    print(
        f"warning: `{command}` is deprecated and will be removed; use `{replacement}` instead.",
        file=sys.stderr,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "list":
            print(list_jobs_noninteractive(args.filter))
            return 0
        if args.command == "show":
            return cli_show_job(args.job_id)
        if args.command == "delete":
            return cli_delete_job(args.job_id)
        if args.command == "unschedule":
            return cli_unschedule_job(args.job_id)
        if args.command == "cancel":
            _print_deprecated_cli_surface("cancel", "unschedule")
            return cli_cancel_job(args.job_id)
        if args.command == "edit-prompt":
            return cli_edit_prompt(args.job_id)
        if args.command == "reschedule":
            return cli_reschedule_job(args.job_id, args.when)
        if args.command in ("set-session", "session"):
            if args.command == "session":
                _print_deprecated_cli_surface("session", "set-session")
            if args.new:
                return cli_change_session(args.job_id, None)
            if args.session is None:
                parser.error(f"{args.command} requires either a session id or --new")
            return cli_change_session(args.job_id, args.session)
        if args.command == "retry":
            return cli_retry_job(args.job_id, args.when)
        if args.command == "submit":
            return cli_submit_job(args.job_id)
        if args.command == "doctor":
            return cli_doctor(
                roundtrip=args.roundtrip,
                as_json=args.json,
                verbose=args.verbose,
                quiet=args.quiet,
            )
        if args.command == "mark":
            if args.mark_state == "running":
                return cli_mark_running(
                    args.job_id,
                    started_at=args.started_at,
                    log_file=args.log_file,
                )
            if args.mark_state == "done":
                return cli_mark_done(
                    args.job_id,
                    finished_at=args.finished_at,
                    exit_code=args.exit_code,
                    log_file=args.log_file,
                )
            if args.mark_state == "failed":
                return cli_mark_failed(
                    args.job_id,
                    finished_at=args.finished_at,
                    exit_code=args.exit_code,
                    log_file=args.log_file,
                )
            parser.error("mark requires a state: running, done, or failed")

        return jobs_menu()
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except OperationError as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
