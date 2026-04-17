from __future__ import annotations

import json
import os
from pathlib import Path

from .legacy import compat as legacy_compat
from .scheduler_backend import query_atq
from .state_model import check_invariants
from .time_utils import now_iso

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
    """Deprecated compatibility wrapper. See `schedule_agent.legacy.compat`."""

    return legacy_compat.legacy_state_file(_state_home())


def job_log_dir(job_id: str) -> str:
    _ensure_dirs()
    return str(_state_home() / "logs" / job_id)


def migrate_job(
    job: dict, legacy_state: dict | None = None, atq_entries: dict[str, object] | None = None
) -> dict:
    """Deprecated compatibility wrapper. See `schedule_agent.legacy.compat`."""

    return legacy_compat.migrate_job(
        job,
        legacy_state,
        atq_entries,
        job_log_dir=job_log_dir,
        now_iso=now_iso,
    )


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

    legacy = legacy_compat.load_legacy_state(_ensure_dirs, _state_home())
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
            migrated = legacy_compat.migrate_job(
                job,
                legacy.get(job.get("id")),
                atq_entries,
                job_log_dir=job_log_dir,
                now_iso=now_iso,
            )
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
    """Deprecated compatibility wrapper. See `schedule_agent.legacy.compat`."""

    return legacy_compat.load_legacy_state(_ensure_dirs, _state_home())


def save_legacy_state(state: dict) -> None:
    """Deprecated compatibility wrapper. See `schedule_agent.legacy.compat`."""

    legacy_compat.save_legacy_state(_ensure_dirs, _state_home(), state)
