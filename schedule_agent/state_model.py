from __future__ import annotations

SUBMISSION = frozenset({"queued", "scheduled", "running", "cancelled"})
EXECUTION = frozenset({"pending", "running", "success", "failed"})
SESSION_MODE = frozenset({"new", "resume"})
READINESS = frozenset({"ready", "waiting_dependency", "blocked"})


def derive_display_state(job: dict) -> str:
    if job["submission"] == "cancelled":
        return "cancelled"
    if job["submission"] == "running" or job["execution"] == "running":
        return "running"
    if job["execution"] == "failed":
        return "failed"
    if job["execution"] == "success":
        return "done"
    if job["readiness"] == "blocked":
        return "blocked"
    if job["readiness"] == "waiting_dependency":
        return "waiting"
    if job["submission"] == "scheduled":
        return "scheduled"
    return "queued"


def check_invariants(job: dict) -> None:
    """Raise ValueError if any invariant is violated."""
    errors = []

    if job.get("submission") == "scheduled" and not job.get("at_job_id"):
        errors.append("Invariant 1: submission=scheduled requires at_job_id")
    if job.get("submission") != "scheduled" and job.get("at_job_id"):
        errors.append("Invariant 2: at_job_id must be absent when submission!=scheduled")
    if job.get("session_mode") == "resume" and not job.get("session_id"):
        errors.append("Invariant 3: session_mode=resume requires session_id")
    if job.get("session_mode") == "new" and job.get("session_id"):
        errors.append("Invariant 4: session_id must be absent when session_mode=new")
    if job.get("execution") == "running" and job.get("submission") != "running":
        errors.append("Invariant 5: execution=running requires submission=running")
    if job.get("readiness") == "waiting_dependency" and not job.get("depends_on"):
        errors.append("Invariant 6: readiness=waiting_dependency requires depends_on")
    if job.get("readiness") == "blocked" and not job.get("depends_on"):
        errors.append("Invariant 7: readiness=blocked requires depends_on")
    if job.get("execution") in ("success", "failed") and not job.get("last_run_at"):
        errors.append("Invariant 8: execution=success|failed requires last_run_at")

    if errors:
        raise ValueError("Job invariant violation(s):\n" + "\n".join(errors))


def can_submit(job: dict) -> bool:
    return (
        job["submission"] == "queued"
        and job["execution"] == "pending"
        and job["readiness"] == "ready"
    )


def can_reschedule(job: dict) -> bool:
    return job["submission"] != "cancelled"


def can_cancel(job: dict) -> bool:
    return job["submission"] in ("queued", "scheduled")


def can_change_session(job: dict) -> bool:
    return True


def can_delete(job: dict) -> bool:
    return True


def can_retry(job: dict) -> bool:
    return job["execution"] == "failed"
