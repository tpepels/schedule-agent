from __future__ import annotations

from .state_model import check_invariants
from .time_utils import now_iso


def make_job(
    job_id: str,
    title: str,
    agent: str,
    session_mode: str,
    session_id: str | None,
    prompt_file: str,
    scheduled_for: str,
    cwd: str,
    log_dir: str,
    depends_on: str | None = None,
    dependency_condition: str | None = None,
    last_log_file: str | None = None,
) -> dict:
    job: dict = {
        "id": job_id,
        "title": title,
        "agent": agent,
        "session_mode": session_mode,
        "session_id": session_id,
        "prompt_file": prompt_file,
        "scheduled_for": scheduled_for,
        "cwd": cwd,
        "log_dir": log_dir,
        "last_log_file": last_log_file,
        "submission": "queued",
        "execution": "pending",
        "readiness": "waiting_dependency" if depends_on else "ready",
        "at_job_id": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "last_started_at": None,
        "last_run_at": None,
        "last_exit_code": None,
    }
    if depends_on:
        job["depends_on"] = depends_on
        job["dependency_condition"] = dependency_condition or "success"
    check_invariants(job)
    return job


def on_submit(job: dict, at_job_id: str, scheduled_for: str | None = None) -> dict:
    j = dict(job)
    j["submission"] = "scheduled"
    j["at_job_id"] = at_job_id
    if scheduled_for:
        j["scheduled_for"] = scheduled_for
    j["updated_at"] = now_iso()
    check_invariants(j)
    return j


def on_start(job: dict, started_at: str, log_file: str) -> dict:
    j = dict(job)
    j["submission"] = "running"
    j["execution"] = "running"
    j["at_job_id"] = None
    j["last_started_at"] = started_at
    j["last_log_file"] = log_file
    j["updated_at"] = now_iso()
    check_invariants(j)
    return j


def on_success(job: dict, finished_at: str, exit_code: int, log_file: str | None = None) -> dict:
    j = dict(job)
    j["submission"] = "queued"
    j["execution"] = "success"
    j["at_job_id"] = None
    j["last_run_at"] = finished_at
    j["last_exit_code"] = exit_code
    if log_file:
        j["last_log_file"] = log_file
    j["updated_at"] = now_iso()
    check_invariants(j)
    return j


def on_failure(job: dict, finished_at: str, exit_code: int, log_file: str | None = None) -> dict:
    j = dict(job)
    j["submission"] = "queued"
    j["execution"] = "failed"
    j["at_job_id"] = None
    j["last_run_at"] = finished_at
    j["last_exit_code"] = exit_code
    if log_file:
        j["last_log_file"] = log_file
    j["updated_at"] = now_iso()
    check_invariants(j)
    return j


def on_cancel(job: dict) -> dict:
    j = dict(job)
    j["submission"] = "cancelled"
    j["at_job_id"] = None
    j["updated_at"] = now_iso()
    check_invariants(j)
    return j


def on_unschedule(job: dict) -> dict:
    j = dict(job)
    j["submission"] = "queued"
    j["at_job_id"] = None
    j["updated_at"] = now_iso()
    check_invariants(j)
    return j


def on_reschedule(job: dict, scheduled_for: str) -> dict:
    j = dict(job)
    if j["execution"] in ("success", "failed"):
        j["execution"] = "pending"
        j["submission"] = "queued"
        j["at_job_id"] = None
        j["last_run_at"] = None
        j["last_exit_code"] = None
    j["scheduled_for"] = scheduled_for
    j["updated_at"] = now_iso()
    check_invariants(j)
    return j


def on_change_session(job: dict, session_mode: str, session_id: str | None) -> dict:
    j = dict(job)
    j["session_mode"] = session_mode
    j["session_id"] = session_id
    j["updated_at"] = now_iso()
    check_invariants(j)
    return j


def on_update_prompt(job: dict, title: str) -> dict:
    j = dict(job)
    j["title"] = title
    j["updated_at"] = now_iso()
    check_invariants(j)
    return j


def on_dependency_success(job: dict) -> dict:
    j = dict(job)
    j["readiness"] = "ready"
    j["updated_at"] = now_iso()
    check_invariants(j)
    return j


def on_dependency_failure(job: dict) -> dict:
    j = dict(job)
    j["readiness"] = "blocked"
    j["updated_at"] = now_iso()
    check_invariants(j)
    return j


def on_resubmit_failed(job: dict) -> dict:
    j = dict(job)
    j["submission"] = "queued"
    j["at_job_id"] = None
    j["updated_at"] = now_iso()
    check_invariants(j)
    return j


def on_retry(job: dict, scheduled_for: str) -> dict:
    j = dict(job)
    j["execution"] = "pending"
    j["submission"] = "queued"
    j["readiness"] = "ready"
    j["at_job_id"] = None
    j["scheduled_for"] = scheduled_for
    j["last_run_at"] = None
    j["last_exit_code"] = None
    j["updated_at"] = now_iso()
    check_invariants(j)
    return j
