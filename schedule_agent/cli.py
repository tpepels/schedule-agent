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
from typing import Callable, Optional, Sequence

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.shortcuts import message_dialog, radiolist_dialog, yes_no_dialog

from .execution import AGENTS, build_agent_cmd
from .persistence import (
    _data_home,
    _ensure_dirs,
    _state_home,
    find_job,
    load_jobs,
    save_jobs,
    update_job_in_list,
    write_prompt_file as _write_prompt_file,
)
from .scheduler_backend import parse_at_job_id, remove_at_job, submit_job
from .state_model import (
    can_cancel,
    can_change_session,
    can_delete,
    can_reschedule,
    can_retry,
    can_submit,
    derive_display_state,
)
from .transitions import (
    make_job as _make_job,
    on_cancel,
    on_change_session,
    on_dependency_failure,
    on_dependency_success,
    on_failure,
    on_reschedule,
    on_retry,
    on_start,
    on_submit,
    on_success,
)


APP_NAME = "schedule-agent"


# ---------------------------------------------------------------------------
# Path helpers — thin wrappers kept for import compat and tests
# ---------------------------------------------------------------------------

def _state_home_fn() -> Path:  # noqa: D401
    return _state_home()


def _data_home_fn() -> Path:  # noqa: D401
    return _data_home()


# Re-expose paths the tests reference on the module
def _make_paths():
    state_dir = _state_home()
    data_dir = _data_home()
    return state_dir, data_dir, state_dir / "agent_queue.jsonl", state_dir / "agent_queue_state.json", data_dir / "agent_prompts"


STATE_DIR = _state_home()
DATA_DIR = _data_home()
QUEUE_FILE = STATE_DIR / "agent_queue.jsonl"
STATE_FILE = STATE_DIR / "agent_queue_state.json"
PROMPT_DIR = DATA_DIR / "agent_prompts"


# ---------------------------------------------------------------------------
# Compatibility shims: build_cmd is now build_agent_cmd in execution.py
# ---------------------------------------------------------------------------

def build_cmd(job: dict) -> str:
    return build_agent_cmd(job)


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def choose(msg: str, choices: Sequence[str], default: str | None = None) -> str:
    values = [(c, c) for c in choices]
    result = radiolist_dialog(
        title=APP_NAME,
        text=msg,
        values=values,
        default=default if default in choices else (choices[0] if choices else None),
    ).run()
    if result is None:
        raise KeyboardInterrupt
    return result


def confirm(msg: str, default: bool = True) -> bool:
    result = yes_no_dialog(title=APP_NAME, text=msg).run()
    if result is None:
        raise KeyboardInterrupt
    return bool(result)


def info(msg: str) -> None:
    message_dialog(title=APP_NAME, text=msg).run()


def _resolve_editor() -> list[str]:
    editor = os.environ.get("SCHEDULE_AGENT_EDITOR") or os.environ.get("EDITOR") or "nano"
    try:
        parts = shlex.split(editor)
    except ValueError:
        parts = [editor]
    return parts or ["nano"]


# ---------------------------------------------------------------------------
# Persistence wrappers (kept for test compatibility)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """Load the legacy state file."""
    from .persistence import load_legacy_state
    return load_legacy_state()


def save_state(state: dict) -> None:
    """Save the legacy state file."""
    from .persistence import save_legacy_state
    save_legacy_state(state)


def set_state(job_id: str, status: str, **extra) -> None:
    """Update the legacy state file AND the job record's state fields.

    Maps the old ``status`` value to new submission/execution fields in the
    job record so both storage layers stay consistent during migration.
    """
    from .persistence import _STATUS_MAP, load_legacy_state, save_legacy_state

    # Update legacy state file
    state = load_legacy_state()
    entry = state.get(job_id, {})
    entry["status"] = status
    entry["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry.update(extra)
    state[job_id] = entry
    save_legacy_state(state)

    # Update the job record if it exists
    jobs = load_jobs()
    idx, job = find_job(jobs, job_id)
    if job is None:
        return

    submission, execution = _STATUS_MAP.get(status, ("queued", "pending"))
    updated = dict(job)
    updated["submission"] = submission
    updated["execution"] = execution
    updated["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if "at_job_id" in extra:
        updated["at_job_id"] = extra["at_job_id"]
    elif submission != "scheduled":
        updated["at_job_id"] = None
    for k in ("scheduled_for", "log", "cwd", "agent"):
        if k in extra:
            updated[k] = extra[k]
    jobs[idx] = updated
    save_jobs(jobs)


def clear_state(job_id: str) -> None:
    """Remove a job from the legacy state file."""
    from .persistence import load_legacy_state, save_legacy_state
    state = load_legacy_state()
    if job_id in state:
        del state[job_id]
        save_legacy_state(state)


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------

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
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
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
                if isinstance(title, str):
                    title = title.strip()
                    if title:
                        return title
        return None

    if agent == "codex":
        for obj in iter_json():
            if obj.get("type") == "event_msg":
                payload = obj.get("payload", {})
                if payload.get("type") == "user_message":
                    message = payload.get("message")
                    if isinstance(message, str):
                        message = message.strip()
                        if message:
                            return message.splitlines()[0]
        return None

    return None


def _discover_codex_sessions(limit: int) -> list[SessionInfo]:
    root = Path.home() / ".codex" / "sessions"
    if not root.exists():
        return []
    files = [p for p in root.rglob("*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    files = files[:limit]
    result = []
    for p in files:
        result.append(SessionInfo(
            id=p.stem,
            path=p,
            agent="codex",
            project=None,
            title=extract_session_title(str(p), "codex"),
            modified_at=p.stat().st_mtime,
        ))
    return result


def _discover_claude_sessions(cwd: Path | None, limit: int) -> list[SessionInfo]:
    root = Path.home() / ".claude" / "projects"
    if not root.exists():
        return []

    def get_project_sessions(project_dir: Path) -> list[tuple[Path, str]]:
        return [
            (p, project_dir.name)
            for p in project_dir.glob("*.jsonl")
            if p.is_file()
        ]

    preferred_project_name: str | None = None
    preferred: list[tuple[Path, str]] = []
    other: list[tuple[Path, str]] = []

    if cwd is not None:
        preferred_project_name = cwd.as_posix().replace("/", "-")
        preferred_dir = root / preferred_project_name
        if preferred_dir.exists():
            preferred = get_project_sessions(preferred_dir)
            preferred.sort(key=lambda t: t[0].stat().st_mtime, reverse=True)

    for project_dir in root.iterdir():
        if not project_dir.is_dir():
            continue
        if preferred_project_name is not None and project_dir.name == preferred_project_name:
            continue
        other.extend(get_project_sessions(project_dir))

    other.sort(key=lambda t: t[0].stat().st_mtime, reverse=True)

    combined = (preferred + other)[:limit]

    result = []
    for p, project_name in combined:
        result.append(SessionInfo(
            id=p.stem,
            path=p,
            agent="claude",
            project=project_name,
            title=extract_session_title(str(p), "claude"),
            modified_at=p.stat().st_mtime,
        ))
    return result


def discover_sessions(agent: str, cwd: Path | None = None, limit: int = 10) -> list[SessionInfo]:
    if agent == "codex":
        return _discover_codex_sessions(limit)
    return _discover_claude_sessions(cwd, limit)


def choose_session(agent: str, cwd: Path | None = None) -> str | None:
    sessions = discover_sessions(agent, cwd=cwd)
    if not sessions:
        return None

    labels = ["New session"] + [
        f"{(s.title or '[no title]')} [{s.id[:8]}]"
        for s in sessions
    ]

    selected = choose("Session", labels, default="New session")
    if selected == "New session":
        return None

    for session, label in zip(sessions, labels[1:]):
        if label == selected:
            return session.id

    return None


def choose_offset() -> str:
    hours = [str(i) for i in range(0, 24)]
    minutes = [str(i) for i in range(0, 60)]
    h = int(choose("Offset hours", hours, default="0"))
    m = int(choose("Offset minutes", minutes, default="5"))
    total = max(1, h * 60 + m)
    return f"now + {total} minutes"


def resolve_time() -> str:
    t = choose("When?", ["Offset", "Today", "Tomorrow"], default="Offset")
    if t == "Offset":
        return choose_offset()
    hh = choose("Hour", [f"{i:02d}" for i in range(24)])
    mm = choose("Minute", [f"{i:02d}" for i in range(60)])
    return f"{hh}:{mm}" if t == "Today" else f"{hh}:{mm} tomorrow"


def read_prompt() -> str:
    editor_cmd = _resolve_editor()
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
        path = Path(f.name)
    try:
        subprocess.run([*editor_cmd, str(path)], check=False)
        txt = path.read_text(encoding="utf-8").strip()
    finally:
        path.unlink(missing_ok=True)
    if not txt:
        raise KeyboardInterrupt
    return txt + "\n\nDo not ask me any questions.\nMake grounded assumptions where needed and continue without waiting for input."


def write_prompt_file(job_id: str, prompt: str) -> str:
    _ensure_dirs()
    prompt_dir = _data_home() / "agent_prompts"
    return _write_prompt_file(prompt_dir, job_id, prompt)


# ---------------------------------------------------------------------------
# Scheduler integration
# ---------------------------------------------------------------------------

def cancel_at_job(job_id: str) -> bool:
    """Cancel the at job for job_id and update state.

    Returns True if atrm succeeded (or job was not scheduled).
    """
    jobs = load_jobs()
    _, job = find_job(jobs, job_id)
    at_id = (job or {}).get("at_job_id")

    # Also check legacy state file
    if not at_id:
        legacy = load_state()
        at_id = legacy.get(job_id, {}).get("at_job_id")

    if not at_id:
        return False

    success, err = remove_at_job(at_id)

    # Update legacy state file for compat
    legacy = load_state()
    entry = legacy.get(job_id, {})
    entry["at_job_removed"] = success
    entry["at_job_remove_attempted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if err:
        entry["at_job_remove_error"] = err
    entry.pop("at_job_id", None)
    legacy[job_id] = entry
    save_state(legacy)

    # Update job record
    if job is not None:
        updated = dict(job)
        updated["at_job_id"] = None
        updated["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_jobs(update_job_in_list(jobs, updated))

    return success


def schedule(job: dict, dry_run: bool = False) -> str:
    """Submit job to `at` and persist state. Returns at output string.

    On dry_run returns a preview string without touching at or state.
    Raises RuntimeError if at fails.
    """
    at_job_id, output = submit_job(job, dry_run=dry_run)

    if dry_run:
        return output

    # Apply on_submit transition to the job record
    updated_job = on_submit(job, at_job_id)
    jobs = load_jobs()
    idx, existing = find_job(jobs, job["id"])
    if idx is not None:
        jobs[idx] = updated_job
    else:
        jobs.append(updated_job)
    save_jobs(jobs)

    # Update legacy state file for compat
    set_state(
        job["id"],
        "submitted",
        scheduled_for=job["when"],
        log=job["log"],
        cwd=job["cwd"],
        agent=job["agent"],
        at_job_id=at_job_id,
        at_submitted_output=output,
    )
    return output


# ---------------------------------------------------------------------------
# Job creation
# ---------------------------------------------------------------------------

def create_job() -> dict:
    agent_label = choose("Agent", [cfg["label"] for cfg in AGENTS.values()], default="Codex")
    agent = "codex" if agent_label == "Codex" else "claude"
    session_id = choose_session(agent, cwd=Path.cwd())
    session_mode = "resume" if session_id else "new"
    info("A temporary file will open in your configured editor. Save and close it to continue. Leave it empty to cancel.")
    prompt = read_prompt()
    when = resolve_time()
    job_id = f"{agent}-{ts()}"
    prompt_file = write_prompt_file(job_id, prompt)
    return _make_job(
        job_id=job_id,
        agent=agent,
        session_mode=session_mode,
        session_id=session_id,
        prompt_file=prompt_file,
        when=when,
        cwd=str(Path.cwd()),
        log=str(Path.cwd() / f"log-{ts()}.txt"),
    )


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def format_job_label(job: dict, state: dict | None = None) -> str:
    """Format a one-line label for the job list UI (interactive TUI).

    ``state`` is the legacy state dict (keyed by job_id); it is used only
    when the job itself has not yet been migrated to the new model.

    Returns a simple readable format: ``job1 (scheduled)  [claude] [resume]``
    """
    display = derive_display_state(job)
    display_tag = f" ({display})" if display != "queued" else ""
    session_mode = job.get("session_mode") or ("resume" if job.get("session_id") else "new")
    return f"{job['id']}{display_tag}  [{job['agent']}] [{session_mode}]"


def _format_job_row(job: dict, id_width: int) -> str:
    """Format a single job as a columnar row for non-interactive list output.

    Columns: <id>  <display_state>  <agent>  <session>  [depends: <dep>]  [prompt missing]

    Invalid sentinel jobs (``_invalid=True``) are shown as ``<id>  [!]`` only.
    """
    STATE_W = 10   # longest display state is "cancelled" (9 chars)
    AGENT_W = 8    # longest agent name is "claude" (6 chars)
    SESSION_W = 7  # longest session value is "resume" (6 chars)

    job_id = job.get("id", "")
    if job.get("_invalid"):
        return f"{job_id}  [!]"

    display = derive_display_state(job)
    agent = job.get("agent", "")
    session_mode = job.get("session_mode", "new")

    row = (
        f"{job_id.ljust(id_width)} "
        f"{display.ljust(STATE_W)} "
        f"{agent.ljust(AGENT_W)} "
        f"{session_mode.ljust(SESSION_W)}"
    )

    dep = job.get("depends_on")
    if dep:
        if len(dep) > 40:
            dep = dep[:40] + "…"
        row += f" depends: {dep}"

    prompt_file = job.get("prompt_file", "")
    if prompt_file and not Path(prompt_file).exists():
        row += " [prompt missing]"

    return row


def list_jobs_noninteractive() -> str:
    jobs = load_jobs()
    if not jobs:
        return "No jobs."
    id_width = max(len(j.get("id", "")) for j in jobs)
    return "\n".join(_format_job_row(job, id_width) for job in jobs)


def list_jobs() -> None:
    info(list_jobs_noninteractive())


# ---------------------------------------------------------------------------
# Job query helpers
# ---------------------------------------------------------------------------

def get_job_and_index(job_id: str) -> tuple[list, int | None, dict | None]:
    jobs = load_jobs()
    idx, job = find_job(jobs, job_id)
    return jobs, idx, job


def prepare_mutation(job_id: str) -> bool:
    """Cancel the at job if the job is currently scheduled. Returns True if it was scheduled."""
    jobs = load_jobs()
    _, job = find_job(jobs, job_id)
    if job is None:
        return False
    if job.get("submission") == "scheduled":
        cancel_at_job(job_id)
        return True
    return False


# ---------------------------------------------------------------------------
# Generic update
# ---------------------------------------------------------------------------

def apply_job_update(
    job_id: str,
    mutator: Callable[[dict], Optional[dict]],
    success_message: str | None = None,
    interactive: bool = True,
) -> int:
    jobs, idx, job = get_job_and_index(job_id)
    if job is None:
        if interactive:
            info("No such job.")
        else:
            print("No such job.")
        return 1

    was_scheduled = job.get("submission") == "scheduled"
    if was_scheduled:
        cancel_at_job(job_id)
        # Reload after cancel so job record is fresh
        jobs, idx, job = get_job_and_index(job_id)

    updated = mutator(dict(job))
    if updated is None:
        # Delete
        jobs = [j for j in jobs if j["id"] != job_id]
        save_jobs(jobs)
        clear_state(job_id)
        pf = job.get("prompt_file")
        if pf:
            Path(pf).unlink(missing_ok=True)
        if success_message:
            if interactive:
                info(success_message)
            else:
                print(success_message)
        return 0

    jobs[idx] = updated
    save_jobs(jobs)

    if was_scheduled:
        try:
            out = schedule(updated)
            msg = success_message or "Updated."
            if out:
                msg += f"\n\n{out}"
            if interactive:
                info(msg)
            else:
                print(msg)
            return 0
        except RuntimeError as e:
            # Resubmit failed — leave as queued
            updated_queued = dict(updated)
            updated_queued["submission"] = "queued"
            updated_queued["at_job_id"] = None
            updated_queued["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _, reload_idx, _ = get_job_and_index(job_id)
            if reload_idx is not None:
                fresh_jobs = load_jobs()
                fresh_jobs[reload_idx] = updated_queued
                save_jobs(fresh_jobs)
            set_state(updated["id"], "queued", scheduled_for=updated["when"], log=updated["log"], cwd=updated["cwd"], agent=updated["agent"])
            msg = (success_message or "Updated.") + f"\n\n{e}\n\nThe job was updated, but re-submission failed. It remains queued."
            if interactive:
                info(msg)
            else:
                print(msg)
            return 1
    else:
        # Keep as queued, update scheduled_for in legacy state
        set_state(updated["id"], "queued", scheduled_for=updated["when"], log=updated["log"], cwd=updated["cwd"], agent=updated["agent"])
        if success_message:
            if interactive:
                info(success_message)
            else:
                print(success_message)
        return 0


# ---------------------------------------------------------------------------
# Interactive job menu
# ---------------------------------------------------------------------------

def choose_job_action_from_list() -> tuple[str | None, dict | None]:
    jobs = load_jobs()
    if not jobs:
        info("No jobs.")
        return None, None

    selected_index = {"value": 0}
    result: dict = {"action": None, "job": None}

    def get_text():
        jobs_now = load_jobs()
        lines = []
        lines.append(("class:title", "Jobs\n"))
        lines.append(("class:hint", "Enter=view  R=reschedule  D=delete  C=change session  Q=quit\n\n"))
        for i, job in enumerate(jobs_now):
            label = format_job_label(job)
            prefix = "❯ " if i == selected_index["value"] else "  "
            style = "class:selected" if i == selected_index["value"] else ""
            lines.append((style, prefix + label + "\n"))
        return lines

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _up(event):
        count = len(load_jobs())
        if count:
            selected_index["value"] = (selected_index["value"] - 1) % count

    @kb.add("down")
    @kb.add("j")
    def _down(event):
        count = len(load_jobs())
        if count:
            selected_index["value"] = (selected_index["value"] + 1) % count

    def _pick(action_name, event):
        jobs_now = load_jobs()
        if not jobs_now:
            result["action"] = "quit"
        else:
            result["action"] = action_name
            result["job"] = jobs_now[selected_index["value"]]
        event.app.exit()

    @kb.add("enter")
    def _enter(event):
        _pick("view", event)

    @kb.add("r")
    @kb.add("R")
    def _reschedule(event):
        _pick("reschedule", event)

    @kb.add("d")
    @kb.add("D")
    def _delete(event):
        _pick("delete", event)

    @kb.add("c")
    @kb.add("C")
    def _change_session(event):
        _pick("change_session", event)

    @kb.add("q")
    @kb.add("Q")
    @kb.add("escape")
    def _quit(event):
        result["action"] = "quit"
        event.app.exit()

    root = HSplit([Window(content=FormattedTextControl(get_text), always_hide_cursor=True)])
    app = Application(layout=Layout(root), key_bindings=kb, full_screen=False)
    app.run()
    return result["action"], result["job"]


def show_job_text(job: dict) -> str:
    display = derive_display_state(job)

    # at_job_id
    at_job_id_val = job.get("at_job_id") or "-"

    # depends_on
    if job.get("depends_on"):
        cond = job.get("dependency_condition", "success")
        depends_on_val = f"{job['depends_on']} (condition: {cond})"
    else:
        depends_on_val = "-"

    # session_id
    if job.get("session_mode") == "new":
        session_id_val = "-"
    else:
        raw = job.get("session_id") or "-"
        session_id_val = raw[:40]

    # prompt_exists
    if Path(job["prompt_file"]).exists():
        prompt_exists_val = "yes"
    else:
        prompt_exists_val = "no (file not found)"

    # timestamps
    def _ts(v: str | None) -> str:
        return v if v is not None else "-"

    w = 15  # column label width (key + colon, left-aligned, values start at col 16)
    lbl = lambda key: f"{key}:".ljust(w)
    indented_label_w = w - 2  # account for 2-space indent prefix

    lines = [
        f"{lbl('id')}{job['id']}",
        f"{lbl('display')}{display}",
        f"  {'submission:'.ljust(indented_label_w)}{job['submission']}",
        f"  {'execution:'.ljust(indented_label_w)}{job['execution']}",
        f"  {'readiness:'.ljust(indented_label_w)}{job['readiness']}",
        f"  {'session:'.ljust(indented_label_w)}{job['session_mode']}",
        f"{lbl('agent')}{job['agent']}",
        f"{lbl('when')}{job['when']}",
        f"{lbl('at_job_id')}{at_job_id_val}",
        f"{lbl('depends_on')}{depends_on_val}",
        f"{lbl('session_id')}{session_id_val}",
        f"{lbl('cwd')}{job['cwd']}",
        f"{lbl('log')}{job['log']}",
        f"{lbl('prompt_file')}{job['prompt_file']}",
        f"{lbl('prompt_exists')}{prompt_exists_val}",
        f"{lbl('created_at')}{_ts(job.get('created_at'))}",
        f"{lbl('updated_at')}{_ts(job.get('updated_at'))}",
        f"{lbl('last_run_at')}{_ts(job.get('last_run_at'))}",
    ]
    return "\n".join(lines)


def show_job(job: dict) -> None:
    info(show_job_text(job))


def cli_show_job(job_id: str) -> int:
    _, _, job = get_job_and_index(job_id)
    if job is None:
        print("No such job.")
        return 1
    print(show_job_text(job))
    return 0


# ---------------------------------------------------------------------------
# Non-interactive job actions
# ---------------------------------------------------------------------------

def reschedule_job(job_id: str, interactive: bool = True) -> int:
    _, _, job = get_job_and_index(job_id)
    if job is None:
        if interactive:
            info("No such job.")
        else:
            print("No such job.")
        return 1
    old_when = job["when"]
    new_when = resolve_time() if interactive else None
    if new_when is None:
        print("New time required.")
        return 1

    def mutate(d: dict) -> dict:
        return on_reschedule(d, new_when)

    return apply_job_update(job_id, mutate, success_message=f"Rescheduled {job_id} from {old_when} to {new_when}.", interactive=interactive)


def cli_reschedule_job(job_id: str, new_when: str) -> int:
    _, _, job = get_job_and_index(job_id)
    if job is None:
        print("No such job.")
        return 1
    old_when = job["when"]
    old_execution = job["execution"]
    was_scheduled = job.get("submission") == "scheduled"

    print(f"{job_id}: rescheduled")
    print(f"  when:     {old_when} \u2192 {new_when}")

    def mutate(d: dict) -> dict:
        return on_reschedule(d, new_when)

    rc = apply_job_update(job_id, mutate, success_message=None, interactive=False)
    if rc == 0:
        if was_scheduled:
            print("  note: at job removed and resubmitted")
        if old_execution in ("success", "failed"):
            print("  note: execution state reset to pending")
    elif was_scheduled:
        print("  note: at job removed")
        print("  warning: resubmit failed: job left as queued")
    return rc


def change_session(job_id: str, interactive: bool = True) -> int:
    _, _, job = get_job_and_index(job_id)
    if job is None:
        if interactive:
            info("No such job.")
        else:
            print("No such job.")
        return 1
    if not interactive:
        print("New session required.")
        return 1
    job_cwd = Path(job["cwd"]) if job.get("cwd") else None
    new_session_id = choose_session(job["agent"], cwd=job_cwd)
    new_mode = "resume" if new_session_id else "new"

    def mutate(d: dict) -> dict:
        return on_change_session(d, new_mode, new_session_id)

    return apply_job_update(job_id, mutate, success_message=f"Changed session for {job_id} to {new_session_id or 'new'}.", interactive=interactive)


def cli_change_session(job_id: str, session: str | None) -> int:
    _, _, job = get_job_and_index(job_id)
    if job is None:
        print("No such job.")
        return 1
    old_mode = job.get("session_mode", "new")
    old_session_id = job.get("session_id")
    was_scheduled = job.get("submission") == "scheduled"
    was_running = job.get("submission") == "running"
    session_mode = "resume" if session else "new"

    def mutate(d: dict) -> dict:
        return on_change_session(d, session_mode, session)

    rc = apply_job_update(job_id, mutate, success_message=None, interactive=False)
    if rc == 0:
        old_label = old_mode if not old_session_id else f"{old_mode}:{old_session_id[:8]}"
        new_label = session_mode if not session else f"{session_mode}:{session[:8]}"
        print(f"{job_id}: session updated")
        print(f"  session:  {old_label} \u2192 {new_label}")
        if was_scheduled:
            print("  note: at job removed and resubmitted")
        if was_running:
            print("  warning: job is currently running; session change takes effect on next run")
    return rc


def remove_job(job_id: str, interactive: bool = True) -> int:
    if interactive:
        if not confirm(f"Delete {job_id}?", default=False):
            info("Cancelled.")
            return 1
        return apply_job_update(job_id, lambda d: None, success_message="Deleted.", interactive=interactive)
    # Non-interactive: capture pre-deletion state for structured output
    _, _, job = get_job_and_index(job_id)
    if job is None:
        print("No such job.")
        return 1
    was_scheduled = job.get("submission") == "scheduled"
    # Find dependent jobs before deletion
    all_jobs = load_jobs()
    dependent_ids = [j["id"] for j in all_jobs if j.get("depends_on") == job_id]
    rc = apply_job_update(job_id, lambda d: None, success_message=None, interactive=False)
    if rc == 0:
        print(f"{job_id}: deleted")
        if was_scheduled:
            print("  note: at job removed")
        if dependent_ids:
            ids_str = ", ".join(dependent_ids)
            print(f"  warning: dependent jobs [{ids_str}] remain in their current state")
    return rc


# ---------------------------------------------------------------------------
# Automated transitions (called by the at wrapper script via CLI)
# ---------------------------------------------------------------------------

def cli_mark_running(job_id: str) -> int:
    jobs, idx, job = get_job_and_index(job_id)
    if job is None:
        print(f"No such job: {job_id}")
        return 1
    updated = on_start(job)
    jobs[idx] = updated
    save_jobs(jobs)
    return 0


def cli_mark_done(job_id: str) -> int:
    jobs, idx, job = get_job_and_index(job_id)
    if job is None:
        print(f"No such job: {job_id}")
        return 1
    updated = on_success(job)
    jobs[idx] = updated
    save_jobs(jobs)
    return 0


def cli_mark_failed(job_id: str) -> int:
    jobs, idx, job = get_job_and_index(job_id)
    if job is None:
        print(f"No such job: {job_id}")
        return 1
    updated = on_failure(job)
    jobs[idx] = updated
    save_jobs(jobs)
    return 0


def cli_retry_job(job_id: str) -> int:
    jobs, idx, job = get_job_and_index(job_id)
    if job is None:
        print(f"No such job: {job_id}")
        return 1
    if not can_retry(job):
        print(f"error: job {job_id} cannot be retried\n  current execution state: {job['execution']} (retry requires: failed)")
        return 1
    old_execution = job["execution"]
    old_readiness = job["readiness"]
    updated = on_retry(job)
    jobs[idx] = updated
    save_jobs(jobs)
    print(f"{job_id}: reset for retry")
    print(f"  execution: {old_execution} \u2192 pending")
    print(f"  readiness: {old_readiness} \u2192 ready")
    if job.get("depends_on"):
        print(f"  note: dependency {job['depends_on']} has not resolved; readiness forced to ready")
    return 0


def cli_cancel_job(job_id: str) -> int:
    jobs, idx, job = get_job_and_index(job_id)
    if job is None:
        print(f"error: job {job_id} not found")
        return 1
    if job["submission"] == "cancelled":
        print(f"error: job {job_id} cannot be cancelled: already cancelled")
        return 1
    if job["submission"] == "running":
        print(f"error: job {job_id} cannot be cancelled: job is currently running")
        return 1

    old_submission = job["submission"]
    at_id = job.get("at_job_id")
    at_removed = False

    if old_submission == "scheduled":
        at_removed = cancel_at_job(job_id)
        # Reload after cancel
        jobs, idx, job = get_job_and_index(job_id)

    updated = on_cancel(job)
    jobs[idx] = updated
    save_jobs(jobs)

    print(f"{job_id}: cancelled")
    print(f"  submission: {old_submission} \u2192 cancelled")
    if old_submission == "scheduled":
        if at_removed:
            print(f"  note: at job removed")
        else:
            print(f"  warning: atrm failed for at job {at_id}")
    return 0


def cli_notify_dependency(job_id: str, parent_result: str) -> int:
    """Apply dependency transition based on parent outcome (success|failed)."""
    jobs, idx, job = get_job_and_index(job_id)
    if job is None:
        print(f"No such job: {job_id}")
        return 1
    if parent_result == "success":
        updated = on_dependency_success(job)
    elif parent_result == "failed":
        updated = on_dependency_failure(job)
    else:
        print(f"Unknown parent_result: {parent_result}")
        return 1
    jobs[idx] = updated
    save_jobs(jobs)
    return 0


# ---------------------------------------------------------------------------
# Manual submit
# ---------------------------------------------------------------------------

def cli_submit_job(job_id: str) -> int:
    jobs, idx, job = get_job_and_index(job_id)
    if job is None:
        print(f"error: job {job_id} not found")
        return 1
    if not can_submit(job):
        display = derive_display_state(job)
        print(f"error: job {job_id} is not in a submittable state")
        print(f"  current: {display} (submission={job['submission']}, execution={job['execution']}, readiness={job['readiness']})")
        print(f"  required: submission=queued, execution=pending, readiness=ready")
        return 1
    if not Path(job["prompt_file"]).exists():
        print(f"error: prompt file not found: {job['prompt_file']}")
        print(f"  job {job_id} cannot be submitted")
        return 1
    try:
        output = schedule(job)
        at_id = parse_at_job_id(output) or "?"
        print(f"{job_id}: submitted")
        print(f"  submission: queued \u2192 scheduled")
        print(f"  at_job_id: {at_id}  (for: {job['when']})")
        return 0
    except RuntimeError as e:
        print(f"error: failed to submit job {job_id}: {e}")
        return 1


# ---------------------------------------------------------------------------
# Interactive create+submit flow
# ---------------------------------------------------------------------------

def create_and_maybe_submit(dry_run: bool = False) -> int:
    job = create_job()
    jobs = load_jobs()
    jobs.append(job)
    save_jobs(jobs)
    # Mirror into legacy state as queued
    set_state(job["id"], "queued", log=job["log"], cwd=job["cwd"], agent=job["agent"])
    if dry_run:
        info(schedule(job, dry_run=True))
        return 0
    if confirm("Submit now?", default=True):
        try:
            out = schedule(job)
            info("Submitted." + (f"\n\n{out}" if out else ""))
            return 0
        except RuntimeError as e:
            info(f"{e}\n\nThe job was saved as queued.")
            return 1
    else:
        info("Saved as queued.")
        return 0


def jobs_menu() -> int:
    while True:
        action, job = choose_job_action_from_list()
        if action in (None, "quit"):
            return 0
        if job is None:
            return 0
        if action == "view":
            show_job(job)
        elif action == "reschedule":
            reschedule_job(job["id"])
        elif action == "delete":
            remove_job(job["id"])
        elif action == "change_session":
            change_session(job["id"])


# ---------------------------------------------------------------------------
# Arg parser
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=APP_NAME, description="Schedule Codex and Claude CLI jobs.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be scheduled during interactive create.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List jobs.")
    show_p = sub.add_parser("show", help="Show one job.")
    show_p.add_argument("job_id")

    del_p = sub.add_parser("delete", help="Delete one job.")
    del_p.add_argument("job_id")

    res_p = sub.add_parser("reschedule", help="Reschedule one job.")
    res_p.add_argument("job_id")
    res_p.add_argument("when", help='New time, e.g. "03:00 tomorrow" or "now + 90 minutes".')

    set_ses_p = sub.add_parser("set-session", help="Change session for a job.")
    set_ses_p.add_argument("job_id")
    set_ses_p.add_argument("session", nargs="?", help="Session id, or omit with --new to clear.")
    set_ses_p.add_argument("--new", action="store_true", help='Clear the session and use a new session.')

    ses_alias_p = sub.add_parser("session", help=argparse.SUPPRESS)
    ses_alias_p.add_argument("job_id")
    ses_alias_p.add_argument("session", nargs="?")
    ses_alias_p.add_argument("--new", action="store_true")

    retry_p = sub.add_parser("retry", help="Reset a failed job so it can be submitted again.")
    retry_p.add_argument("job_id")

    cancel_p = sub.add_parser("cancel", help="Cancel a job without deleting it.")
    cancel_p.add_argument("job_id")

    # Automated transition commands (called by the at wrapper script)
    mark_p = sub.add_parser("mark", help="Update job execution state (for at wrapper use).")
    mark_sub = mark_p.add_subparsers(dest="mark_state")
    for state in ("running", "done", "failed"):
        mp = mark_sub.add_parser(state)
        mp.add_argument("job_id")

    dep_p = sub.add_parser("notify-dependency", help="Notify a child job of parent outcome.")
    dep_p.add_argument("job_id")
    dep_p.add_argument("result", choices=["success", "failed"])

    sub_p = sub.add_parser("submit", help="Submit a queued job to at.")
    sub_p.add_argument("job_id")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "list":
            print(list_jobs_noninteractive())
            return 0
        if args.command == "show":
            return cli_show_job(args.job_id)
        if args.command == "delete":
            return remove_job(args.job_id, interactive=False)
        if args.command == "reschedule":
            return cli_reschedule_job(args.job_id, args.when)
        if args.command in ("set-session", "session"):
            if args.new:
                return cli_change_session(args.job_id, None)
            if args.session is None:
                parser.error(f"{args.command} requires either a session id or --new")
            return cli_change_session(args.job_id, args.session)
        if args.command == "retry":
            return cli_retry_job(args.job_id)
        if args.command == "cancel":
            return cli_cancel_job(args.job_id)
        if args.command == "mark":
            if args.mark_state == "running":
                return cli_mark_running(args.job_id)
            if args.mark_state == "done":
                return cli_mark_done(args.job_id)
            if args.mark_state == "failed":
                return cli_mark_failed(args.job_id)
            parser.error("mark requires a state: running, done, or failed")
        if args.command == "notify-dependency":
            return cli_notify_dependency(args.job_id, args.result)
        if args.command == "submit":
            return cli_submit_job(args.job_id)

        action = choose("Action", ["Create job", "Jobs"], default="Create job")
        if action == "Create job":
            return create_and_maybe_submit(dry_run=args.dry_run)
        return jobs_menu()
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
