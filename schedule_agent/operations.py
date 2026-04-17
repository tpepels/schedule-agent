from __future__ import annotations

import fcntl
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .persistence import (
    _data_home,
    _ensure_dirs,
    _state_home,
    find_job,
    job_log_dir,
    load_jobs,
    save_jobs,
    update_job_in_list,
    write_prompt_file,
)
from .scheduler_backend import query_atq, query_atq_entry, remove_at_job, resolve_schedule_spec, submit_job
from .state_model import (
    can_change_session,
    can_delete,
    can_edit_prompt,
    can_reschedule,
    can_retry,
    can_submit,
    can_unschedule,
    check_invariants,
    derive_display_state,
    display_label,
    scheduler_label,
)
from .time_utils import iso_to_display, now_iso, sort_key_for_iso, title_from_prompt
from .transitions import (
    make_job,
    on_change_session,
    on_dependency_failure,
    on_dependency_success,
    on_failure,
    on_reschedule,
    on_resubmit_failed,
    on_retry,
    on_start,
    on_submit,
    on_success,
    on_unschedule,
    on_update_prompt,
)


class OperationError(RuntimeError):
    pass


@dataclass
class MutationResult:
    job: dict | None = None
    message: str | None = None


def _lock_path() -> Path:
    return _state_home() / "queue.lock"


@contextmanager
def _locked_queue():
    _ensure_dirs()
    with open(_lock_path(), "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _job_id(agent: str) -> str:
    return f"{agent}-{now_iso().replace(':', '').replace('-', '')}"


def _prompt_dir() -> Path:
    return _data_home() / "agent_prompts"


def _job_with_scheduler(job: dict, atq_entries: dict[str, object] | None = None, atq_error: str | None = None) -> dict:
    if job.get("_invalid"):
        view = dict(job)
        view["display_state"] = "invalid"
        view["display_label"] = display_label(job)
        view["scheduler_status"] = "unknown"
        view["scheduler_label"] = scheduler_label("unknown")
        view["scheduler_run_at"] = None
        view["drift_reason"] = None
        return view

    view = dict(job)
    view["display_state"] = derive_display_state(job)
    view["display_label"] = display_label(job)

    if job["submission"] != "scheduled":
        view["scheduler_status"] = "not_queued"
        view["scheduler_label"] = scheduler_label("not_queued")
        view["scheduler_run_at"] = None
        view["drift_reason"] = None
        return view

    if atq_error:
        view["scheduler_status"] = "unknown"
        view["scheduler_label"] = scheduler_label("unknown")
        view["scheduler_run_at"] = None
        view["drift_reason"] = atq_error
        return view

    entry = (atq_entries or {}).get(str(job.get("at_job_id")))
    if entry is None:
        view["scheduler_status"] = "missing"
        view["scheduler_label"] = scheduler_label("missing")
        view["scheduler_run_at"] = None
        view["drift_reason"] = "Job is marked scheduled locally but absent from atq."
        return view

    view["scheduler_run_at"] = entry.scheduled_for
    if entry.scheduled_for != job["scheduled_for"]:
        view["scheduler_status"] = "drifted"
        view["scheduler_label"] = scheduler_label("drifted")
        view["drift_reason"] = f"Internal time {job['scheduled_for']} differs from atq time {entry.scheduled_for}."
        return view

    view["scheduler_status"] = "queued"
    view["scheduler_label"] = scheduler_label("queued")
    view["drift_reason"] = None
    return view


def _terminal(display_state: str) -> bool:
    return display_state in {"completed", "failed", "removed"}


def _sort_jobs(jobs: list[dict]) -> list[dict]:
    def key(job: dict):
        if job.get("_invalid"):
            return (2, float("inf"), 0.0)
        display_state = derive_display_state(job)
        if _terminal(display_state):
            return (1, 0.0, -sort_key_for_iso(job.get("updated_at"))[1])
        return (0, sort_key_for_iso(job.get("scheduled_for"))[1], 0.0)

    return sorted(jobs, key=key)


def list_job_views(filter_name: str = "all") -> tuple[list[dict], str | None]:
    jobs = load_jobs()
    atq_entries, atq_error = query_atq()
    views = [_job_with_scheduler(job, atq_entries, atq_error) for job in jobs]

    if filter_name == "active":
        views = [job for job in views if not _terminal(job["display_state"]) and job["display_state"] != "invalid"]
    elif filter_name == "completed":
        views = [job for job in views if _terminal(job["display_state"])]

    return _sort_jobs(views), atq_error


def get_job_view(job_id: str) -> dict | None:
    jobs = load_jobs()
    _, job = find_job(jobs, job_id)
    if job is None:
        return None
    atq_entries, atq_error = query_atq()
    return _job_with_scheduler(job, atq_entries, atq_error)


def _load_locked_job(job_id: str) -> tuple[list[dict], int, dict]:
    jobs = load_jobs()
    idx, job = find_job(jobs, job_id)
    if idx is None or job is None:
        raise OperationError(f"No such job: {job_id}")
    if job.get("_invalid"):
        raise OperationError(f"Job {job_id} is invalid: {job['_error']}")
    return jobs, idx, job


def _ensure_mutable(job: dict, allowed: bool, action: str) -> None:
    if not allowed:
        raise OperationError(f"Job {job['id']} cannot {action} while running")


def _remove_scheduler_membership(job: dict) -> dict:
    if job["submission"] != "scheduled" or not job.get("at_job_id"):
        return job

    ok, err = remove_at_job(job["at_job_id"])
    if not ok:
        entry, query_err = query_atq_entry(job["at_job_id"])
        if query_err or entry is not None:
            message = err or query_err or f"Could not remove at job {job['at_job_id']}"
            raise OperationError(message)
    return on_unschedule(job)


def _resubmit(job: dict) -> tuple[dict, str]:
    at_job_id, output = submit_job(job)
    entry, _ = query_atq_entry(at_job_id)
    scheduled_for = entry.scheduled_for if entry else job["scheduled_for"]
    return on_submit(job, at_job_id, scheduled_for=scheduled_for), output


def _apply_scheduler_mutation(
    job_id: str,
    action_name: str,
    guard: Callable[[dict], bool],
    mutator: Callable[[dict], dict | None],
    resubmit_if_previously_scheduled: bool = True,
) -> MutationResult:
    with _locked_queue():
        jobs, idx, job = _load_locked_job(job_id)
        _ensure_mutable(job, guard(job), action_name)

        was_scheduled = job["submission"] == "scheduled"
        working = _remove_scheduler_membership(job) if was_scheduled else job
        updated = mutator(working)

        if updated is None:
            prompt_file = job.get("prompt_file")
            if prompt_file:
                Path(prompt_file).unlink(missing_ok=True)
            shutil.rmtree(job.get("log_dir", ""), ignore_errors=True)
            del jobs[idx]
            save_jobs(jobs)
            return MutationResult(message=f"{job_id}: deleted")

        jobs[idx] = updated
        save_jobs(jobs)

        if was_scheduled and resubmit_if_previously_scheduled:
            try:
                submitted, output = _resubmit(updated)
                jobs[idx] = submitted
                save_jobs(jobs)
                return MutationResult(job=submitted, message=output)
            except Exception as exc:
                queued = on_resubmit_failed(updated)
                jobs[idx] = queued
                save_jobs(jobs)
                raise OperationError(str(exc))

        return MutationResult(job=updated)


def create_job(
    agent: str,
    session_mode: str,
    session_id: str | None,
    prompt_text: str,
    schedule_spec: str,
    cwd: str,
    submit: bool = True,
) -> dict:
    with _locked_queue():
        job_id = _job_id(agent)
        scheduled_for = resolve_schedule_spec(schedule_spec)
        prompt_file = write_prompt_file(_prompt_dir(), job_id, prompt_text)
        job = make_job(
            job_id=job_id,
            title=title_from_prompt(prompt_text),
            agent=agent,
            session_mode=session_mode,
            session_id=session_id,
            prompt_file=prompt_file,
            scheduled_for=scheduled_for,
            cwd=cwd,
            log_dir=job_log_dir(job_id),
        )
        jobs = load_jobs()
        jobs.append(job)
        save_jobs(jobs)
        if submit:
            try:
                submitted, _ = _resubmit(job)
                jobs = update_job_in_list(jobs, submitted)
                save_jobs(jobs)
                return submitted
            except Exception as exc:
                raise OperationError(str(exc))
        return job


def refresh_prompt(job_id: str) -> dict:
    def mutate(job: dict) -> dict:
        prompt_path = Path(job["prompt_file"])
        if not prompt_path.exists():
            raise OperationError(f"Prompt file not found: {prompt_path}")
        title = title_from_prompt(prompt_path.read_text(encoding="utf-8"))
        return on_update_prompt(job, title)

    return _apply_scheduler_mutation(job_id, "edit prompt", can_edit_prompt, mutate).job


def edit_prompt_contents(job_id: str, prompt_text: str) -> dict:
    def mutate(job: dict) -> dict:
        Path(job["prompt_file"]).write_text(prompt_text, encoding="utf-8")
        return on_update_prompt(job, title_from_prompt(prompt_text))

    return _apply_scheduler_mutation(job_id, "edit prompt", can_edit_prompt, mutate).job


def reschedule_job(job_id: str, schedule_spec: str) -> dict:
    resolved = resolve_schedule_spec(schedule_spec)
    return _apply_scheduler_mutation(
        job_id,
        "reschedule",
        can_reschedule,
        lambda job: on_reschedule(job, resolved),
    ).job


def change_session(job_id: str, session: str | None) -> dict:
    mode = "resume" if session else "new"
    return _apply_scheduler_mutation(
        job_id,
        "change session",
        can_change_session,
        lambda job: on_change_session(job, mode, session),
    ).job


def unschedule_job(job_id: str) -> dict:
    return _apply_scheduler_mutation(
        job_id,
        "remove from queue",
        can_unschedule,
        lambda job: on_unschedule(job),
        resubmit_if_previously_scheduled=False,
    ).job


def delete_job(job_id: str) -> None:
    _apply_scheduler_mutation(
        job_id,
        "delete",
        can_delete,
        lambda job: None,
        resubmit_if_previously_scheduled=False,
    )


def submit_or_repair_job(job_id: str) -> dict:
    with _locked_queue():
        jobs, idx, job = _load_locked_job(job_id)
        if job["submission"] == "running":
            raise OperationError(f"Job {job_id} is currently running")

        working = _remove_scheduler_membership(job) if job["submission"] == "scheduled" else job
        if not can_submit(working):
            raise OperationError(
                f"Job {job_id} is not submittable "
                f"(submission={working['submission']}, execution={working['execution']}, readiness={working['readiness']})"
            )
        submitted, _ = _resubmit(working)
        jobs[idx] = submitted
        save_jobs(jobs)
        return submitted


def retry_job(job_id: str, schedule_spec: str) -> dict:
    resolved = resolve_schedule_spec(schedule_spec)
    return _retry_and_submit(job_id, resolved)


def _retry_and_submit(job_id: str, resolved: str) -> dict:
    with _locked_queue():
        jobs, idx, job = _load_locked_job(job_id)
        _ensure_mutable(job, can_retry(job), "retry")
        updated = on_retry(job, resolved)
        submitted, _ = _resubmit(updated)
        jobs[idx] = submitted
        save_jobs(jobs)
        return submitted


def mark_running(job_id: str, started_at: str, log_file: str) -> dict:
    with _locked_queue():
        jobs, idx, job = _load_locked_job(job_id)
        updated = on_start(job, started_at=started_at, log_file=log_file)
        jobs[idx] = updated
        save_jobs(jobs)
        return updated


def _update_dependents(jobs: list[dict], parent_id: str, parent_result: str) -> list[dict]:
    updated_jobs = list(jobs)
    for index, candidate in enumerate(updated_jobs):
        if candidate.get("_invalid") or candidate.get("depends_on") != parent_id:
            continue
        if parent_result == "success":
            updated_jobs[index] = on_dependency_success(candidate)
        else:
            updated_jobs[index] = on_dependency_failure(candidate)
    return updated_jobs


def mark_finished(job_id: str, finished_at: str, exit_code: int, log_file: str | None = None) -> dict:
    with _locked_queue():
        jobs, idx, job = _load_locked_job(job_id)
        if exit_code == 0:
            updated = on_success(job, finished_at=finished_at, exit_code=exit_code, log_file=log_file)
            result = "success"
        else:
            updated = on_failure(job, finished_at=finished_at, exit_code=exit_code, log_file=log_file)
            result = "failed"
        jobs[idx] = updated
        jobs = _update_dependents(jobs, parent_id=job_id, parent_result=result)
        save_jobs(jobs)
        return updated


def format_job_summary(job: dict) -> str:
    lines = [
        f"id:            {job['id']}",
        f"title:         {job.get('title', '-')}",
        f"status:         {job.get('display_label', display_label(job))}",
        f"scheduler:     {job.get('scheduler_label', '-')}",
        f"run_at:        {iso_to_display(job.get('scheduled_for'), with_seconds=True)}",
        f"created_at:    {iso_to_display(job.get('created_at'), with_seconds=True)}",
        f"updated_at:    {iso_to_display(job.get('updated_at'), with_seconds=True)}",
        f"last_started:  {iso_to_display(job.get('last_started_at'), with_seconds=True)}",
        f"last_run_at:   {iso_to_display(job.get('last_run_at'), with_seconds=True)}",
        f"session:       {job.get('session_mode', '-')}{':' + job['session_id'][:12] if job.get('session_id') else ''}",
        f"dependency:    {job.get('depends_on', '-')}",
        f"at_job_id:     {job.get('at_job_id') or '-'}",
        f"log_dir:       {job.get('log_dir', '-')}",
        f"last_log_file: {job.get('last_log_file') or '-'}",
        f"prompt_file:   {job.get('prompt_file', '-')}",
        f"cwd:           {job.get('cwd', '-')}",
    ]
    if job.get("scheduler_run_at"):
        lines.insert(4, f"atq_run_at:     {iso_to_display(job.get('scheduler_run_at'), with_seconds=True)}")
    if job.get("drift_reason"):
        lines.append(f"drift_reason:   {job['drift_reason']}")
    if job.get("_invalid"):
        lines.append(f"error:          {job['_error']}")
    return "\n".join(lines)


def list_rows(filter_name: str = "all") -> tuple[list[str], str | None]:
    jobs, atq_error = list_job_views(filter_name)
    rows = []
    for job in jobs:
        rows.append(
            " | ".join(
                [
                    job.get("title", "(invalid)"),
                    job.get("display_label", "Invalid"),
                    job.get("scheduler_label", "Unknown"),
                    iso_to_display(job.get("scheduled_for")),
                    iso_to_display(job.get("updated_at")),
                    iso_to_display(job.get("created_at")),
                    (job.get("session_id") or job.get("session_mode", "-"))[:12],
                    job.get("depends_on", "-"),
                ]
            )
        )
    return rows, atq_error
