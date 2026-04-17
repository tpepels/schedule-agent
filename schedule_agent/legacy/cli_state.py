from __future__ import annotations

from ..persistence import (
    _ensure_dirs,
    _state_home,
    find_job,
    load_jobs,
    save_jobs,
    update_job_in_list,
)
from ..scheduler_backend import remove_at_job
from ..time_utils import now_iso
from ..transitions import on_unschedule
from .compat import load_legacy_state as _load_legacy_state
from .compat import save_legacy_state as _save_legacy_state


def load_state() -> dict:
    """Load deprecated queue-side state. See `schedule_agent.legacy`."""

    return _load_legacy_state(_ensure_dirs, _state_home())


def save_state(state: dict) -> None:
    """Save deprecated queue-side state. See `schedule_agent.legacy`."""

    _save_legacy_state(_ensure_dirs, _state_home(), state)


def set_state(job_id: str, status: str, **extra) -> None:
    """Update deprecated queue-side state. See `schedule_agent.legacy`."""

    state = load_state()
    entry = state.get(job_id, {})
    entry["status"] = status
    entry["updated_at"] = now_iso()
    entry.update(extra)
    state[job_id] = entry
    save_state(state)


def clear_state(job_id: str) -> None:
    """Clear deprecated queue-side state. See `schedule_agent.legacy`."""

    state = load_state()
    if job_id in state:
        del state[job_id]
        save_state(state)


def cancel_at_job(job_id: str) -> bool:
    """Remove a deprecated at-job reference while keeping the record valid.

    Deprecated compatibility surface. See `schedule_agent.legacy`.
    """

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
        updated = on_unschedule(job)
        jobs = update_job_in_list(jobs, updated)
        save_jobs(jobs)
    return ok
