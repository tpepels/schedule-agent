from __future__ import annotations

from datetime import datetime

from .state_model import check_invariants


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def make_job(
    job_id: str,
    agent: str,
    session_mode: str,
    session_id: str | None,
    prompt_file: str,
    when: str,
    cwd: str,
    log: str,
    depends_on: str | None = None,
    dependency_condition: str | None = None,
) -> dict:
    """Create a new job with correct initial state."""
    job: dict = {
        "id": job_id,
        "agent": agent,
        "session_mode": session_mode,
        "session_id": session_id,
        "prompt_file": prompt_file,
        "when": when,
        "cwd": cwd,
        "log": log,
        "submission": "queued",
        "execution": "pending",
        "readiness": "waiting_dependency" if depends_on else "ready",
        "at_job_id": None,
        "created_at": _now(),
        "updated_at": _now(),
        "last_run_at": None,
    }
    if depends_on:
        job["depends_on"] = depends_on
        job["dependency_condition"] = dependency_condition or "success"
    check_invariants(job)
    return job


def on_submit(job: dict, at_job_id: str) -> dict:
    """Transition: queued -> scheduled after at job is created."""
    j = dict(job)
    j["submission"] = "scheduled"
    j["at_job_id"] = at_job_id
    j["updated_at"] = _now()
    check_invariants(j)
    return j


def on_start(job: dict) -> dict:
    """Transition: scheduled -> running when execution begins."""
    j = dict(job)
    j["submission"] = "running"
    j["execution"] = "running"
    j["at_job_id"] = None
    j["updated_at"] = _now()
    check_invariants(j)
    return j


def on_success(job: dict) -> dict:
    """Transition: running -> queued/success after successful completion."""
    j = dict(job)
    j["submission"] = "queued"
    j["execution"] = "success"
    j["at_job_id"] = None
    j["last_run_at"] = _now()
    j["updated_at"] = _now()
    check_invariants(j)
    return j


def on_failure(job: dict) -> dict:
    """Transition: running -> queued/failed after failed completion."""
    j = dict(job)
    j["submission"] = "queued"
    j["execution"] = "failed"
    j["at_job_id"] = None
    j["last_run_at"] = _now()
    j["updated_at"] = _now()
    check_invariants(j)
    return j


def on_cancel(job: dict) -> dict:
    """Transition: queued|scheduled -> cancelled. Caller must atrm first."""
    j = dict(job)
    j["submission"] = "cancelled"
    j["at_job_id"] = None
    j["updated_at"] = _now()
    check_invariants(j)
    return j


def on_reschedule(job: dict, new_when: str) -> dict:
    """Transition: update scheduled time.

    For done/failed jobs resets execution to pending so the job can run again.
    Caller must atrm+resubmit if job was scheduled.
    """
    j = dict(job)
    if j["execution"] in ("success", "failed"):
        j["execution"] = "pending"
        j["submission"] = "queued"
        j["at_job_id"] = None
        j["last_run_at"] = None
    j["when"] = new_when
    j["updated_at"] = _now()
    check_invariants(j)
    return j


def on_change_session(job: dict, session_mode: str, session_id: str | None) -> dict:
    """Transition: update session_mode and session_id. Caller must atrm+resubmit if scheduled."""
    j = dict(job)
    j["session_mode"] = session_mode
    j["session_id"] = session_id
    j["updated_at"] = _now()
    check_invariants(j)
    return j


def on_dependency_success(job: dict) -> dict:
    """Transition: waiting_dependency -> ready when parent job succeeds."""
    j = dict(job)
    j["readiness"] = "ready"
    j["updated_at"] = _now()
    check_invariants(j)
    return j


def on_dependency_failure(job: dict) -> dict:
    """Transition: waiting_dependency -> blocked when parent job fails."""
    j = dict(job)
    j["readiness"] = "blocked"
    j["updated_at"] = _now()
    check_invariants(j)
    return j


def on_resubmit_failed(job: dict) -> dict:
    """Transition: resubmit failed — reset to queued so the job can be retried.

    Called when apply_job_update successfully updated a job but the at-resubmit
    failed. The job stays updated (e.g. new when/session) but returns to queued
    submission state.
    """
    j = dict(job)
    j["submission"] = "queued"
    j["at_job_id"] = None
    j["updated_at"] = _now()
    check_invariants(j)
    return j


def on_retry(job: dict) -> dict:
    """Transition: reset failed job so it can be submitted again."""
    j = dict(job)
    j["execution"] = "pending"
    j["submission"] = "queued"
    j["readiness"] = "ready"
    j["at_job_id"] = None
    j["updated_at"] = _now()
    check_invariants(j)
    return j
