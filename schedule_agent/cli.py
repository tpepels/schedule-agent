from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.shortcuts import message_dialog, radiolist_dialog, yes_no_dialog


APP_NAME = "schedule-agent"


def _state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP_NAME


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME


STATE_DIR = _state_home()
DATA_DIR = _data_home()
QUEUE_FILE = STATE_DIR / "agent_queue.jsonl"
STATE_FILE = STATE_DIR / "agent_queue_state.json"
PROMPT_DIR = DATA_DIR / "agent_prompts"

AGENTS = {
    "codex": {
        "label": "Codex",
        "bin": "codex",
        "base_args": ["exec", "--dangerously-bypass-approvals-and-sandbox"],
    },
    "claude": {
        "label": "Claude",
        "bin": "claude",
        "base_args": ["-p", "--dangerously-skip-permissions"],
    },
}


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


def _ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROMPT_DIR.mkdir(parents=True, exist_ok=True)


def _resolve_editor() -> list[str]:
    editor = os.environ.get("SCHEDULE_AGENT_EDITOR") or os.environ.get("EDITOR") or "nano"
    try:
        parts = shlex.split(editor)
    except ValueError:
        parts = [editor]
    return parts or ["nano"]


def load_jobs():
    _ensure_dirs()
    if not QUEUE_FILE.exists():
        return []
    return [json.loads(line) for line in QUEUE_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]


def save_jobs(jobs):
    _ensure_dirs()
    QUEUE_FILE.write_text("\n".join(json.dumps(j) for j in jobs), encoding="utf-8")


def load_state():
    _ensure_dirs()
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    _ensure_dirs()
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def set_state(job_id: str, status: str, **extra):
    state = load_state()
    entry = state.get(job_id, {})
    entry["status"] = status
    entry["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry.update(extra)
    state[job_id] = entry
    save_state(state)


def clear_state(job_id: str):
    state = load_state()
    if job_id in state:
        del state[job_id]
        save_state(state)


def discover_sessions(agent: str):
    root = Path.home() / (".codex/sessions" if agent == "codex" else ".claude/projects")
    if not root.exists():
        return []
    files = [p for p in root.rglob("*") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:10]


def choose_session(agent: str):
    sessions = discover_sessions(agent)
    if not sessions:
        return None
    labels = ["New session"] + [p.stem for p in sessions]
    selected = choose("Session", labels, default="New session")
    if selected == "New session":
        return None
    return selected


def choose_offset():
    hours = [str(i) for i in range(0, 24)]
    minutes = [str(i) for i in range(0, 60)]
    h = int(choose("Offset hours", hours, default="0"))
    m = int(choose("Offset minutes", minutes, default="5"))
    total = max(1, h * 60 + m)
    return f"now + {total} minutes"


def resolve_time():
    t = choose("When?", ["Offset", "Today", "Tomorrow"], default="Offset")
    if t == "Offset":
        return choose_offset()
    hh = choose("Hour", [f"{i:02d}" for i in range(24)])
    mm = choose("Minute", [f"{i:02d}" for i in range(60)])
    return f"{hh}:{mm}" if t == "Today" else f"{hh}:{mm} tomorrow"


def read_prompt():
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
    path = PROMPT_DIR / f"{job_id}.md"
    path.write_text(prompt, encoding="utf-8")
    return str(path)


def build_cmd(job):
    cfg = AGENTS[job["agent"]]
    prompt_path = shlex.quote(job["prompt_file"])
    base = " ".join(cfg["base_args"])
    if job.get("session"):
        if job["agent"] == "codex":
            return f"{cfg['bin']} exec resume {shlex.quote(job['session'])} \"$(cat {prompt_path})\" < /dev/null"
        return f"{cfg['bin']} --resume {shlex.quote(job['session'])} {base} \"$(cat {prompt_path})\" < /dev/null"
    return f"{cfg['bin']} {base} \"$(cat {prompt_path})\" < /dev/null"


def parse_at_job_id(stdout: str) -> str | None:
    match = re.search(r"\bjob\s+(\d+)\s+at\b", stdout)
    return match.group(1) if match else None


def cancel_at_job(job_id: str) -> bool:
    state = load_state()
    entry = state.get(job_id, {})
    at_job_id = entry.get("at_job_id")
    if not at_job_id:
        return False
    proc = subprocess.run(["atrm", str(at_job_id)], capture_output=True, text=True)
    entry["at_job_removed"] = proc.returncode == 0
    entry["at_job_remove_attempted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if proc.stderr.strip():
        entry["at_job_remove_error"] = proc.stderr.strip()
    entry.pop("at_job_id", None)
    state[job_id] = entry
    save_state(state)
    return proc.returncode == 0


def schedule(job):
    cmd = build_cmd(job)
    script = f"cd {shlex.quote(job['cwd'])}\nexport PATH=/usr/local/bin:/usr/bin:/bin\n{cmd} >> {shlex.quote(job['log'])} 2>&1\n"
    proc = subprocess.run(["at", job["when"]], input=script, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Failed to schedule job")
    at_job_id = parse_at_job_id(proc.stdout)
    set_state(
        job["id"],
        "submitted",
        scheduled_for=job["when"],
        log=job["log"],
        cwd=job["cwd"],
        agent=job["agent"],
        at_job_id=at_job_id,
        at_submitted_output=proc.stdout.strip(),
    )
    return proc.stdout.strip()


def create_job():
    agent_label = choose("Agent", [cfg["label"] for cfg in AGENTS.values()], default="Codex")
    agent = "codex" if agent_label == "Codex" else "claude"
    session = choose_session(agent)
    info("A temporary file will open in your configured editor. Save and close it to continue. Leave it empty to cancel.")
    prompt = read_prompt()
    when = resolve_time()
    job_id = f"{agent}-{ts()}"
    return {
        "id": job_id,
        "agent": agent,
        "session": session,
        "prompt_file": write_prompt_file(job_id, prompt),
        "when": when,
        "cwd": str(Path.cwd()),
        "log": str(Path.cwd() / f"log-{ts()}.txt"),
    }


def format_job_label(job, state):
    status = state.get(job["id"], {}).get("status")
    submitted = " (S)" if status == "submitted" else ""
    session = job.get("session") or "new"
    return f"{job['id']}{submitted}  [{job['when']}]  [{job['agent']}]  [session={session}]"


def list_jobs():
    jobs = load_jobs()
    if not jobs:
        info("No jobs.")
        return
    state = load_state()
    info("\n".join(format_job_label(job, state) for job in jobs))


def get_job_and_index(job_id: str):
    jobs = load_jobs()
    for idx, job in enumerate(jobs):
        if job["id"] == job_id:
            return jobs, idx, job
    return jobs, None, None


def prepare_mutation(job_id: str):
    state = load_state()
    was_submitted = state.get(job_id, {}).get("status") == "submitted"
    if was_submitted:
        cancel_at_job(job_id)
    return was_submitted


def apply_job_update(job_id: str, mutator: Callable[[dict], Optional[dict]], success_message: str | None = None):
    jobs, idx, job = get_job_and_index(job_id)
    if job is None:
        info("No such job.")
        return
    was_submitted = prepare_mutation(job_id)
    updated = mutator(dict(job))
    if updated is None:
        jobs = [j for j in jobs if j["id"] != job_id]
        save_jobs(jobs)
        clear_state(job_id)
        pf = job.get("prompt_file")
        if pf:
            Path(pf).unlink(missing_ok=True)
        if success_message:
            info(success_message)
        return
    jobs[idx] = updated
    save_jobs(jobs)
    if was_submitted:
        try:
            out = schedule(updated)
            msg = success_message or "Updated."
            if out:
                msg += f"\n\n{out}"
            info(msg)
        except RuntimeError as e:
            set_state(updated["id"], "queued", scheduled_for=updated["when"], log=updated["log"], cwd=updated["cwd"], agent=updated["agent"])
            info((success_message or "Updated.") + f"\n\n{e}\n\nThe job was updated, but re-submission failed. It remains queued.")
    else:
        set_state(updated["id"], "queued", scheduled_for=updated["when"], log=updated["log"], cwd=updated["cwd"], agent=updated["agent"])
        if success_message:
            info(success_message)


def choose_job_action_from_list():
    jobs = load_jobs()
    if not jobs:
        info("No jobs.")
        return None, None

    selected_index = {"value": 0}
    result = {"action": None, "job": None}

    def get_text():
        jobs_now = load_jobs()
        state_now = load_state()
        lines = []
        lines.append(("class:title", "Jobs\n"))
        lines.append(("class:hint", "Enter=view  R=reschedule  D=delete  C=change session  Q=quit\n\n"))
        for i, job in enumerate(jobs_now):
            label = format_job_label(job, state_now)
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


def show_job(job):
    info(
        f"id:         {job['id']}\n"
        f"agent:      {job['agent']}\n"
        f"when:       {job['when']}\n"
        f"session:    {job.get('session') or 'new'}\n"
        f"cwd:        {job['cwd']}\n"
        f"log:        {job['log']}\n"
        f"promptfile: {job['prompt_file']}"
    )


def reschedule_job(job_id: str):
    jobs, _, job = get_job_and_index(job_id)
    if job is None:
        info("No such job.")
        return
    old_when = job["when"]
    new_when = resolve_time()

    def mutate(d):
        d["when"] = new_when
        return d

    apply_job_update(job_id, mutate, success_message=f"Rescheduled {job_id} from {old_when} to {new_when}.")


def change_session(job_id: str):
    jobs, _, job = get_job_and_index(job_id)
    if job is None:
        info("No such job.")
        return
    new_session = choose_session(job["agent"])

    def mutate(d):
        d["session"] = new_session
        return d

    apply_job_update(job_id, mutate, success_message=f"Changed session for {job_id} to {new_session or 'new'}.")


def remove_job(job_id: str):
    if not confirm(f"Delete {job_id}?", default=False):
        info("Cancelled.")
        return
    apply_job_update(job_id, lambda d: None, success_message="Deleted.")


def create_and_maybe_submit():
    job = create_job()
    jobs = load_jobs()
    jobs.append(job)
    save_jobs(jobs)
    set_state(job["id"], "queued", log=job["log"], cwd=job["cwd"], agent=job["agent"])
    if confirm("Submit now?", default=True):
        try:
            out = schedule(job)
            info("Submitted." + (f"\n\n{out}" if out else ""))
        except RuntimeError as e:
            info(f"{e}\n\nThe job was saved as queued.")
    else:
        info("Saved as queued.")


def jobs_menu():
    while True:
        action, job = choose_job_action_from_list()
        if action in (None, "quit"):
            return
        if job is None:
            return
        if action == "view":
            show_job(job)
        elif action == "reschedule":
            reschedule_job(job["id"])
        elif action == "delete":
            remove_job(job["id"])
        elif action == "change_session":
            change_session(job["id"])


def main():
    try:
        action = choose("Action", ["Create job", "Jobs"], default="Create job")
        if action == "Create job":
            create_and_maybe_submit()
        else:
            jobs_menu()
    except KeyboardInterrupt:
        print("\nCancelled.")


if __name__ == "__main__":
    main()