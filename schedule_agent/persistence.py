from __future__ import annotations

import json
import os
from pathlib import Path

from .scheduler_backend import normalize_legacy_when, query_atq
from .state_model import check_invariants
from .time_utils import normalize_legacy_timestamp, now_iso, title_from_prompt

APP_NAME = "schedule-agent"


def _state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP_NAME


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME


def _ensure_dirs() -> tuple[Path, Path, Path, Path, Path]:
    state_dir = _state_home()
    data_dir = _data_home()
    prompt_dir = data_dir / "agent_prompts"
    logs_dir = state_dir / "logs"
    queue_file = state_dir / "agent_queue.jsonl"
    state_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    prompt_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    return state_dir, data_dir, prompt_dir, logs_dir, queue_file


def _queue_file() -> Path:
    return _state_home() / "agent_queue.jsonl"


def _legacy_state_file() -> Path:
    return _state_home() / "agent_queue_state.json"


def job_log_dir(job_id: str) -> str:
    _ensure_dirs()
    return str(_state_home() / "logs" / job_id)


def _legacy_prompt_title(prompt_file: str) -> str:
    try:
        return title_from_prompt(Path(prompt_file).read_text(encoding="utf-8"))
    except Exception:
        return "(untitled job)"


_STATUS_MAP = {
    "queued": ("queued", "pending"),
    "submitted": ("scheduled", "pending"),
    "running": ("running", "running"),
    "success": ("queued", "success"),
    "failed": ("queued", "failed"),
    "cancelled": ("cancelled", "pending"),
}


def migrate_job(
    job: dict, legacy_state: dict | None = None, atq_entries: dict[str, object] | None = None
) -> dict:
    if "scheduled_for" in job and "title" in job and "log_dir" in job:
        return job

    migrated = dict(job)
    old_session = migrated.pop("session", None)
    migrated["session_mode"] = migrated.get("session_mode") or ("resume" if old_session else "new")
    migrated["session_id"] = migrated.get("session_id", old_session)

    old_status = (legacy_state or {}).get("status", "queued")
    submission, execution = _STATUS_MAP.get(old_status, ("queued", "pending"))
    migrated["submission"] = migrated.get("submission", submission)
    migrated["execution"] = migrated.get("execution", execution)

    at_job_id = migrated.get("at_job_id")
    if not at_job_id and migrated["submission"] == "scheduled":
        at_job_id = (legacy_state or {}).get("at_job_id")
    migrated["at_job_id"] = at_job_id

    migrated["readiness"] = migrated.get("readiness") or (
        "waiting_dependency" if migrated.get("depends_on") else "ready"
    )
    migrated["created_at"] = normalize_legacy_timestamp(migrated.get("created_at")) or now_iso()
    migrated["updated_at"] = (
        normalize_legacy_timestamp(
            migrated.get("updated_at") or (legacy_state or {}).get("updated_at")
        )
        or now_iso()
    )
    migrated["last_started_at"] = normalize_legacy_timestamp(migrated.get("last_started_at"))
    migrated["last_run_at"] = normalize_legacy_timestamp(
        migrated.get("last_run_at") or (legacy_state or {}).get("last_run_at")
    )
    migrated["last_exit_code"] = migrated.get("last_exit_code")

    migrated["title"] = migrated.get("title") or _legacy_prompt_title(
        migrated.get("prompt_file", "")
    )
    migrated["log_dir"] = migrated.get("log_dir") or job_log_dir(migrated["id"])
    if not migrated.get("last_log_file"):
        legacy_log = migrated.get("log")
        migrated["last_log_file"] = legacy_log if legacy_log and Path(legacy_log).exists() else None

    scheduled_for = migrated.get("scheduled_for")
    if not scheduled_for and at_job_id and atq_entries:
        entry = atq_entries.get(str(at_job_id))
        if entry:
            scheduled_for = entry.scheduled_for
    if not scheduled_for:
        scheduled_for = normalize_legacy_when(migrated.get("when"))
    if not scheduled_for:
        raise ValueError("Could not resolve legacy schedule into scheduled_for")
    migrated["scheduled_for"] = scheduled_for
    migrated.pop("when", None)

    return migrated


def load_jobs() -> list[dict]:
    queue_file = _queue_file()
    _ensure_dirs()
    if not queue_file.exists():
        return []

    raw_jobs = [
        json.loads(line)
        for line in queue_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    legacy = load_legacy_state()
    at_job_ids = [
        str((job.get("at_job_id") or legacy.get(job.get("id"), {}).get("at_job_id")))
        for job in raw_jobs
        if (
            job.get("scheduled_for") is None
            and (job.get("at_job_id") or legacy.get(job.get("id"), {}).get("at_job_id"))
        )
    ]
    atq_entries, _ = query_atq(at_job_ids or None) if at_job_ids else ({}, None)

    results = []
    for job in raw_jobs:
        try:
            migrated = migrate_job(job, legacy.get(job.get("id")), atq_entries)
            check_invariants(migrated)
            results.append(migrated)
        except Exception as exc:
            job_id = job.get("id", "<unknown>")
            results.append({"id": job_id, "_invalid": True, "_error": str(exc)})
    return results


def save_jobs(jobs: list[dict]) -> None:
    queue_file = _queue_file()
    _ensure_dirs()
    queue_file.write_text(
        "\n".join(json.dumps(job, ensure_ascii=False) for job in jobs),
        encoding="utf-8",
    )


def find_job(jobs: list[dict], job_id: str) -> tuple[int, dict] | tuple[None, None]:
    for idx, job in enumerate(jobs):
        if job["id"] == job_id:
            return idx, job
    return None, None


def update_job_in_list(jobs: list[dict], updated: dict) -> list[dict]:
    return [updated if job["id"] == updated["id"] else job for job in jobs]


def write_prompt_file(prompt_dir: Path, job_id: str, prompt: str) -> str:
    path = prompt_dir / f"{job_id}.md"
    path.write_text(prompt, encoding="utf-8")
    return str(path)


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
