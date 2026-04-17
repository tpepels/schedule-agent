from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..time_utils import (
    ISO_SECONDS_FORMAT,
    normalize_schedule_input,
    parse_iso_datetime,
    resolve_schedule_input,
    title_from_prompt,
)

LEGACY_STATE_FILENAME = "agent_queue_state.json"
_LEGACY_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

_STATUS_MAP = {
    "queued": ("queued", "pending"),
    "submitted": ("scheduled", "pending"),
    "running": ("running", "running"),
    "success": ("queued", "success"),
    "failed": ("queued", "failed"),
    "cancelled": ("cancelled", "pending"),
}


def legacy_state_file(state_home: Path) -> Path:
    """Return the deprecated queue state file path.

    Deprecated compatibility surface. See `schedule_agent.legacy`.
    """

    return state_home / LEGACY_STATE_FILENAME


def load_legacy_state(ensure_dirs: Callable[[], object], state_home: Path) -> dict:
    """Load deprecated queue-side state from disk.

    Deprecated compatibility surface. See `schedule_agent.legacy`.
    """

    ensure_dirs()
    path = legacy_state_file(state_home)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_legacy_state(ensure_dirs: Callable[[], object], state_home: Path, state: dict) -> None:
    """Persist deprecated queue-side state to disk.

    Deprecated compatibility surface. See `schedule_agent.legacy`.
    """

    ensure_dirs()
    legacy_state_file(state_home).write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def resolve_session_id(job: dict) -> str | None:
    """Read the canonical or deprecated session identifier from a job."""

    return job.get("session_id") or job.get("session")


def normalize_legacy_timestamp(value: str | None) -> str | None:
    """Convert a deprecated non-ISO timestamp to ISO format."""

    if not value:
        return None
    try:
        parse_iso_datetime(value)
        return value
    except ValueError:
        pass
    dt = datetime.strptime(value.strip(), _LEGACY_DATETIME_FORMAT)
    return dt.astimezone().strftime(ISO_SECONDS_FORMAT)


def normalize_legacy_when(value: str | None) -> str | None:
    """Resolve the deprecated `when` field into the canonical schedule format."""

    if not value:
        return None
    try:
        return resolve_schedule_input(normalize_schedule_input(value))
    except ValueError:
        return None


def legacy_prompt_title(prompt_file: str) -> str:
    """Read a title from a deprecated prompt-file-only job record."""

    try:
        return title_from_prompt(Path(prompt_file).read_text(encoding="utf-8"))
    except Exception:
        return "(untitled job)"


def migrate_job(
    job: dict,
    legacy_state: dict | None = None,
    atq_entries: dict[str, object] | None = None,
    *,
    job_log_dir: Callable[[str], str],
    now_iso: Callable[[], str],
) -> dict:
    """Upgrade a deprecated job record to the canonical schema.

    Deprecated compatibility surface. See `schedule_agent.legacy`.
    """

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

    migrated["title"] = migrated.get("title") or legacy_prompt_title(
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
