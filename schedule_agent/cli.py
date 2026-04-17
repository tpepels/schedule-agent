from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .execution import AGENTS, build_agent_cmd
from .operations import (
    OperationError,
    change_session,
    create_job,
    delete_job,
    edit_prompt_contents,
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
    job_log_dir,
    load_jobs,
    load_legacy_state,
    save_jobs,
    save_legacy_state,
    update_job_in_list,
    write_prompt_file as _write_prompt_file,
)
from .scheduler_backend import build_script, parse_at_job_id, query_atq, remove_at_job, submit_job
from .time_utils import iso_to_display, now_iso, title_from_prompt
from .transitions import on_cancel


APP_NAME = "schedule-agent"


def _state_home_fn() -> Path:
    return _state_home()


def _data_home_fn() -> Path:
    return _data_home()


def _make_paths():
    state_dir, data_dir, prompt_dir, _, queue_file = _ensure_dirs()
    return state_dir, data_dir, queue_file, state_dir / "agent_queue_state.json", prompt_dir


STATE_DIR, DATA_DIR, QUEUE_FILE, STATE_FILE, PROMPT_DIR = _make_paths()


def build_cmd(job: dict) -> str:
    return build_agent_cmd(job)


def _require_prompt_toolkit():
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import HSplit, VSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.shortcuts import input_dialog, message_dialog, radiolist_dialog, yes_no_dialog
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
        "FormattedTextControl": FormattedTextControl,
        "input_dialog": input_dialog,
        "message_dialog": message_dialog,
        "radiolist_dialog": radiolist_dialog,
        "yes_no_dialog": yes_no_dialog,
        "Style": Style,
    }


def ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def choose(msg: str, choices: Sequence[str], default: str | None = None) -> str:
    toolkit = _require_prompt_toolkit()
    values = [(choice, choice) for choice in choices]
    result = toolkit["radiolist_dialog"](
        title=APP_NAME,
        text=msg,
        values=values,
        default=default if default in choices else (choices[0] if choices else None),
    ).run()
    if result is None:
        raise KeyboardInterrupt
    return result


def prompt_text(msg: str, default: str = "") -> str:
    toolkit = _require_prompt_toolkit()
    result = toolkit["input_dialog"](title=APP_NAME, text=msg, default=default).run()
    if result is None:
        raise KeyboardInterrupt
    return result.strip()


def confirm(msg: str, default: bool = True) -> bool:
    toolkit = _require_prompt_toolkit()
    result = toolkit["yes_no_dialog"](title=APP_NAME, text=msg).run()
    if result is None:
        raise KeyboardInterrupt
    return bool(result)


def info(msg: str) -> None:
    toolkit = _require_prompt_toolkit()
    toolkit["message_dialog"](title=APP_NAME, text=msg).run()


def _resolve_editor() -> list[str]:
    editor = os.environ.get("SCHEDULE_AGENT_EDITOR") or os.environ.get("EDITOR") or "nano"
    try:
        parts = shlex.split(editor)
    except ValueError:
        parts = [editor]
    return parts or ["nano"]


def edit_file(path: Path) -> None:
    subprocess.run([*_resolve_editor(), str(path)], check=False)


def load_state() -> dict:
    return load_legacy_state()


def save_state(state: dict) -> None:
    save_legacy_state(state)


def set_state(job_id: str, status: str, **extra) -> None:
    state = load_legacy_state()
    entry = state.get(job_id, {})
    entry["status"] = status
    entry["updated_at"] = now_iso()
    entry.update(extra)
    state[job_id] = entry
    save_legacy_state(state)


def clear_state(job_id: str) -> None:
    state = load_legacy_state()
    if job_id in state:
        del state[job_id]
        save_legacy_state(state)


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


def _discover_codex_sessions(limit: int) -> list[SessionInfo]:
    root = Path.home() / ".codex" / "sessions"
    if not root.exists():
        return []
    files = [path for path in root.rglob("*.jsonl") if path.is_file()]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [
        SessionInfo(
            id=path.stem,
            path=path,
            agent="codex",
            project=None,
            title=extract_session_title(str(path), "codex"),
            modified_at=path.stat().st_mtime,
        )
        for path in files[:limit]
    ]


def _discover_claude_sessions(cwd: Path | None, limit: int) -> list[SessionInfo]:
    root = Path.home() / ".claude" / "projects"
    if not root.exists():
        return []

    def top_level_jsonl(project_dir: Path) -> list[tuple[Path, str]]:
        return [(path, project_dir.name) for path in project_dir.glob("*.jsonl") if path.is_file()]

    preferred_name = cwd.as_posix().replace("/", "-") if cwd is not None else None
    preferred: list[tuple[Path, str]] = []
    other: list[tuple[Path, str]] = []

    if preferred_name:
        preferred_dir = root / preferred_name
        if preferred_dir.exists():
            preferred = top_level_jsonl(preferred_dir)
            preferred.sort(key=lambda pair: pair[0].stat().st_mtime, reverse=True)

    for project_dir in root.iterdir():
        if not project_dir.is_dir():
            continue
        if preferred_name and project_dir.name == preferred_name:
            continue
        other.extend(top_level_jsonl(project_dir))

    other.sort(key=lambda pair: pair[0].stat().st_mtime, reverse=True)
    combined = (preferred + other)[:limit]
    return [
        SessionInfo(
            id=path.stem,
            path=path,
            agent="claude",
            project=project_name,
            title=extract_session_title(str(path), "claude"),
            modified_at=path.stat().st_mtime,
        )
        for path, project_name in combined
    ]


def discover_sessions(agent: str, cwd: Path | None = None, limit: int = 10) -> list[SessionInfo]:
    if agent == "codex":
        return _discover_codex_sessions(limit)
    return _discover_claude_sessions(cwd, limit)


def choose_session(agent: str, cwd: Path | None = None) -> str | None:
    sessions = discover_sessions(agent, cwd=cwd)
    if not sessions:
        return None

    labels = ["New session"] + [f"{(session.title or '[no title]')} [{session.id[:8]}]" for session in sessions]
    selected = choose("Session", labels, default="New session")
    if selected == "New session":
        return None
    for session, label in zip(sessions, labels[1:]):
        if label == selected:
            return session.id
    return None


def read_prompt() -> str:
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as handle:
        path = Path(handle.name)
    try:
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


def cancel_at_job(job_id: str) -> bool:
    jobs = load_jobs()
    _, job = find_job(jobs, job_id)
    if job is None or not job.get("at_job_id"):
        return False
    ok, err = remove_at_job(job["at_job_id"])
    legacy = load_state()
    entry = legacy.get(job_id, {})
    entry["at_job_removed"] = ok
    entry["at_job_remove_attempted_at"] = now_iso()
    if err:
        entry["at_job_remove_error"] = err
    entry.pop("at_job_id", None)
    legacy[job_id] = entry
    save_state(legacy)
    if ok:
        updated = dict(job)
        updated["at_job_id"] = None
        jobs = update_job_in_list(jobs, updated)
        save_jobs(jobs)
    return ok


def schedule(job: dict, dry_run: bool = False) -> str:
    _, output = submit_job(job, dry_run=dry_run)
    return output


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


def create_job_interactive() -> int:
    agent_label = choose("Agent", [cfg["label"] for cfg in AGENTS.values()], default="Codex")
    agent = "codex" if agent_label == "Codex" else "claude"
    session_id = choose_session(agent, cwd=Path.cwd())
    session_mode = "resume" if session_id else "new"
    schedule_spec = prompt_text("Run at", default="now + 5 minutes")
    prompt = read_prompt()
    try:
        job = create_job(
            agent=agent,
            session_mode=session_mode,
            session_id=session_id,
            prompt_text=prompt,
            schedule_spec=schedule_spec,
            cwd=str(Path.cwd()),
            submit=True,
        )
    except OperationError as exc:
        info(str(exc))
        return 1
    info(f"Scheduled {job['id']} for {iso_to_display(job['scheduled_for'], with_seconds=True)}")
    return 0


def _tui_select_session(job: dict) -> str | None:
    return choose_session(job["agent"], cwd=Path(job["cwd"]))


def jobs_menu() -> int:
    toolkit = _require_prompt_toolkit()
    Application = toolkit["Application"]
    KeyBindings = toolkit["KeyBindings"]
    Layout = toolkit["Layout"]
    HSplit = toolkit["HSplit"]
    VSplit = toolkit["VSplit"]
    Window = toolkit["Window"]
    FormattedTextControl = toolkit["FormattedTextControl"]
    Style = toolkit["Style"]

    state = {"selected": 0, "filter": "all", "message": "", "quit": False}

    def current_jobs() -> list[dict]:
        jobs, atq_error = list_job_views(state["filter"])
        if atq_error:
            state["message"] = f"atq warning: {atq_error}"
        return jobs

    def selected_job() -> dict | None:
        jobs = current_jobs()
        if not jobs:
            return None
        state["selected"] %= len(jobs)
        return jobs[state["selected"]]

    def header_text():
        timezone = datetime.now().astimezone().tzname() or "local"
        return [("class:header", f" Scheduled Jobs   Filter: {state['filter'].title()}   TZ: {timezone} ")]

    def table_text():
        jobs = current_jobs()
        lines: list[tuple[str, str]] = [("class:table-header", "Title | Status | Scheduler | Run At | Updated | Created | Session | Dependency\n")]
        if not jobs:
            lines.append(("class:muted", "No jobs.\n"))
            return lines

        for index, job in enumerate(jobs):
            style = ""
            if index == state["selected"]:
                style = "class:selected"
            elif job["display_state"] == "completed":
                style = "class:muted"
            lines.append((style, _format_list_row(job) + "\n"))
        return lines

    def detail_text():
        job = selected_job()
        if job is None:
            return [("class:muted", "No job selected.\n")]
        return [("", format_job_summary(job) + "\n")]

    def footer_text():
        help_text = " n new  e edit prompt  t reschedule  c session  u unschedule  s submit/repair  r retry  d delete  f filter  g refresh  q quit "
        message = state["message"]
        if message:
            return [("class:footer", help_text + "\n"), ("class:message", message)]
        return [("class:footer", help_text)]

    kb = KeyBindings()

    @kb.add("q")
    def _quit(event):
        state["quit"] = True
        event.app.exit()

    @kb.add("j")
    @kb.add("down")
    def _down(event):
        jobs = current_jobs()
        if jobs:
            state["selected"] = (state["selected"] + 1) % len(jobs)

    @kb.add("k")
    @kb.add("up")
    def _up(event):
        jobs = current_jobs()
        if jobs:
            state["selected"] = (state["selected"] - 1) % len(jobs)

    @kb.add("f")
    def _filter(event):
        filters = ["all", "active", "completed"]
        state["filter"] = filters[(filters.index(state["filter"]) + 1) % len(filters)]

    @kb.add("g")
    def _refresh(event):
        state["message"] = "Refreshed scheduler state."

    def run_action(action):
        try:
            action()
            state["message"] = "Updated."
        except KeyboardInterrupt:
            state["message"] = "Cancelled."
        except OperationError as exc:
            state["message"] = str(exc)

    @kb.add("n")
    def _new(event):
        def action():
            create_job_interactive()
        run_action(action)

    @kb.add("e")
    def _edit(event):
        job = selected_job()
        if not job:
            return

        def action():
            path = Path(job["prompt_file"])
            edit_file(path)
            refresh_prompt(job["id"])
            state["message"] = f"Prompt updated for {job['id']}."
        run_action(action)

    @kb.add("t")
    def _reschedule(event):
        job = selected_job()
        if not job:
            return

        def action():
            schedule_spec = prompt_text("New run time", default="now + 5 minutes")
            updated = reschedule_job(job["id"], schedule_spec)
            state["message"] = f"Rescheduled for {iso_to_display(updated['scheduled_for'], with_seconds=True)}."
        run_action(action)

    @kb.add("c")
    def _session(event):
        job = selected_job()
        if not job:
            return

        def action():
            session_id = _tui_select_session(job)
            updated = change_session(job["id"], session_id)
            label = updated["session_id"][:12] if updated["session_id"] else "new"
            state["message"] = f"Session set to {label}."
        run_action(action)

    @kb.add("u")
    def _unschedule(event):
        job = selected_job()
        if not job:
            return

        def action():
            unschedule_job(job["id"])
            state["message"] = f"Removed {job['id']} from queue."
        run_action(action)

    @kb.add("s")
    def _submit(event):
        job = selected_job()
        if not job:
            return

        def action():
            updated = submit_or_repair_job(job["id"])
            state["message"] = f"Queued in at as {updated['at_job_id']}."
        run_action(action)

    @kb.add("r")
    def _retry(event):
        job = selected_job()
        if not job:
            return

        def action():
            schedule_spec = prompt_text("Retry run time", default="now + 5 minutes")
            updated = retry_job(job["id"], schedule_spec)
            state["message"] = f"Retry scheduled for {iso_to_display(updated['scheduled_for'], with_seconds=True)}."
        run_action(action)

    @kb.add("d")
    def _delete(event):
        job = selected_job()
        if not job:
            return

        def action():
            if confirm(f"Delete {job['id']} permanently?", default=False):
                delete_job(job["id"])
                state["message"] = f"Deleted {job['id']}."
        run_action(action)

    root = HSplit(
        [
            Window(height=1, content=FormattedTextControl(header_text)),
            VSplit(
                [
                    Window(content=FormattedTextControl(table_text), wrap_lines=False),
                    Window(width=1, char="|"),
                    Window(content=FormattedTextControl(detail_text), wrap_lines=True),
                ]
            ),
            Window(height=2, content=FormattedTextControl(footer_text)),
        ]
    )

    style = Style.from_dict(
        {
            "header": "reverse bold",
            "table-header": "bold",
            "selected": "reverse",
            "muted": "fg:#666666",
            "footer": "reverse",
            "message": "fg:#ffaf00",
        }
    )

    app = Application(layout=Layout(root), key_bindings=kb, full_screen=True, style=style)
    app.run()
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=APP_NAME, description="Schedule Codex and Claude CLI jobs.")
    sub = parser.add_subparsers(dest="command")

    list_p = sub.add_parser("list", help="List jobs.")
    list_p.add_argument("--filter", choices=["all", "active", "completed"], default="all")

    show_p = sub.add_parser("show", help="Show one job.")
    show_p.add_argument("job_id")

    del_p = sub.add_parser("delete", help="Delete one job permanently.")
    del_p.add_argument("job_id")

    unschedule_p = sub.add_parser("unschedule", help="Remove a job from the at queue without deleting it.")
    unschedule_p.add_argument("job_id")

    cancel_p = sub.add_parser("cancel", help=argparse.SUPPRESS)
    cancel_p.add_argument("job_id")

    edit_p = sub.add_parser("edit-prompt", help="Edit a job prompt and re-sync the scheduler if needed.")
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

    return parser


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
            return cli_cancel_job(args.job_id)
        if args.command == "edit-prompt":
            return cli_edit_prompt(args.job_id)
        if args.command == "reschedule":
            return cli_reschedule_job(args.job_id, args.when)
        if args.command in ("set-session", "session"):
            if args.new:
                return cli_change_session(args.job_id, None)
            if args.session is None:
                parser.error(f"{args.command} requires either a session id or --new")
            return cli_change_session(args.job_id, args.session)
        if args.command == "retry":
            return cli_retry_job(args.job_id, args.when)
        if args.command == "submit":
            return cli_submit_job(args.job_id)
        if args.command == "mark":
            if args.mark_state == "running":
                return cli_mark_running(args.job_id, started_at=args.started_at, log_file=args.log_file)
            if args.mark_state == "done":
                return cli_mark_done(args.job_id, finished_at=args.finished_at, exit_code=args.exit_code, log_file=args.log_file)
            if args.mark_state == "failed":
                return cli_mark_failed(args.job_id, finished_at=args.finished_at, exit_code=args.exit_code, log_file=args.log_file)
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
