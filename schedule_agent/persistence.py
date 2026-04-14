from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from .state_model import check_invariants


APP_NAME = "schedule-agent"


def _state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP_NAME


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME


def _ensure_dirs() -> tuple[Path, Path, Path, Path]:
    state_dir = _state_home()
    data_dir = _data_home()
    prompt_dir = data_dir / "agent_prompts"
    state_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    prompt_dir.mkdir(parents=True, exist_ok=True)
    return state_dir, data_dir, prompt_dir, state_dir / "agent_queue.jsonl"


def _queue_file() -> Path:
    return _state_home() / "agent_queue.jsonl"


def _legacy_state_file() -> Path:
    return _state_home() / "agent_queue_state.json"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------

_STATUS_MAP = {
    "queued": ("queued", "pending"),
    "submitted": ("scheduled", "pending"),
    "running": ("running", "running"),
    "success": ("queued", "success"),
    "failed": ("queued", "failed"),
}


def migrate_job(job: dict, legacy_state: dict | None = None) -> dict:
    """Convert a job from the old single-status model to the new multi-dimensional model.

    If the job already has a ``submission`` field it is returned unchanged.
    ``legacy_state`` is the per-job entry from ``agent_queue_state.json``.
    """
    if "submission" in job:
        return job

    migrated = dict(job)

    # session -> session_mode / session_id
    old_session = migrated.pop("session", None)
    migrated["session_mode"] = "resume" if old_session else "new"
    migrated["session_id"] = old_session

    # Map old status
    old_status = (legacy_state or {}).get("status", "queued")
    submission, execution = _STATUS_MAP.get(old_status, ("queued", "pending"))
    migrated["submission"] = submission
    migrated["execution"] = execution

    # at_job_id — only kept when scheduled
    if submission == "scheduled" and legacy_state and legacy_state.get("at_job_id"):
        migrated["at_job_id"] = legacy_state["at_job_id"]
    else:
        migrated["at_job_id"] = None

    # readiness
    if migrated.get("depends_on"):
        migrated["readiness"] = "waiting_dependency"
    else:
        migrated["readiness"] = "ready"

    # timestamps
    migrated.setdefault("created_at", _now())
    migrated["updated_at"] = (legacy_state or {}).get("updated_at") or _now()
    # last_run_at is set when execution completed
    if execution in ("success", "failed"):
        migrated["last_run_at"] = migrated.get("last_run_at") or (legacy_state or {}).get("last_run_at") or _now()
    else:
        migrated.setdefault("last_run_at", None)

    return migrated


# ---------------------------------------------------------------------------
# Core persistence
# ---------------------------------------------------------------------------

def load_jobs() -> list[dict]:
    """Load all jobs from the queue file, migrating legacy entries on the fly."""
    queue_file = _queue_file()
    _ensure_dirs()
    if not queue_file.exists():
        return []

    raw = [
        json.loads(line)
        for line in queue_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    # Load legacy state file if it exists so we can merge it during migration
    legacy: dict = {}
    legacy_file = _legacy_state_file()
    if legacy_file.exists():
        try:
            legacy = json.loads(legacy_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    results = []
    for j in raw:
        try:
            migrated = migrate_job(j, legacy.get(j.get("id")))
            check_invariants(migrated)
            results.append(migrated)
        except Exception as e:
            job_id = j.get("id", "<unknown>")
            results.append({"id": job_id, "_invalid": True, "_error": str(e)})
    return results


def save_jobs(jobs: list[dict]) -> None:
    """Persist jobs to the queue file."""
    queue_file = _queue_file()
    _ensure_dirs()
    queue_file.write_text(
        "\n".join(json.dumps(j) for j in jobs),
        encoding="utf-8",
    )


def find_job(jobs: list[dict], job_id: str) -> tuple[int, dict] | tuple[None, None]:
    """Return (index, job) or (None, None) if not found."""
    for idx, job in enumerate(jobs):
        if job["id"] == job_id:
            return idx, job
    return None, None


def update_job_in_list(jobs: list[dict], updated: dict) -> list[dict]:
    """Return a new list with the matching job replaced."""
    return [updated if j["id"] == updated["id"] else j for j in jobs]


def write_prompt_file(prompt_dir: Path, job_id: str, prompt: str) -> str:
    path = prompt_dir / f"{job_id}.md"
    path.write_text(prompt, encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# Legacy state file helpers (kept for backward compat and tests)
# ---------------------------------------------------------------------------

def load_legacy_state() -> dict:
    _ensure_dirs()
    legacy_file = _legacy_state_file()
    if not legacy_file.exists():
        return {}
    try:
        return json.loads(legacy_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_legacy_state(state: dict) -> None:
    _ensure_dirs()
    _legacy_state_file().write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
