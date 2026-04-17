from __future__ import annotations

SUBMISSION = frozenset({"queued", "scheduled", "running", "cancelled"})
EXECUTION = frozenset({"pending", "running", "success", "failed"})
SESSION_MODE = frozenset({"new", "resume"})
READINESS = frozenset({"ready", "waiting_dependency", "blocked"})


STATUS_LABELS = {
    "queued": "Queued",
    "scheduled": "Scheduled",
    "running": "Running",
    "waiting": "Waiting",
    "blocked": "Blocked",
    "completed": "Completed",
    "failed": "Failed",
    "removed": "Removed",
    "invalid": "Invalid",
}


SCHEDULER_LABELS = {
    "queued": "Queued",
    "missing": "Missing",
    "drifted": "Drifted",
    "not_queued": "Not queued",
    "unknown": "Unknown",
}


def derive_display_state(job: dict) -> str:
    if job.get("_invalid"):
        return "invalid"
    if job["submission"] == "cancelled":
        return "removed"
    if job["submission"] == "running" or job["execution"] == "running":
        return "running"
    if job["execution"] == "failed":
        return "failed"
    if job["execution"] == "success":
        return "completed"
    if job["readiness"] == "blocked":
        return "blocked"
    if job["readiness"] == "waiting_dependency":
        return "waiting"
    if job["submission"] == "scheduled":
        return "scheduled"
    return "queued"


def display_label(job: dict) -> str:
    return STATUS_LABELS[derive_display_state(job)]


def scheduler_label(status: str) -> str:
    return SCHEDULER_LABELS[status]


def check_invariants(job: dict) -> None:
    errors: list[str] = []

    if job.get("submission") not in SUBMISSION:
        errors.append(f"Unknown submission state: {job.get('submission')!r}")
    if job.get("execution") not in EXECUTION:
        errors.append(f"Unknown execution state: {job.get('execution')!r}")
    if job.get("session_mode") not in SESSION_MODE:
        errors.append(f"Unknown session mode: {job.get('session_mode')!r}")
    if job.get("readiness") not in READINESS:
        errors.append(f"Unknown readiness state: {job.get('readiness')!r}")

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
    if not job.get("scheduled_for"):
        errors.append("Invariant 9: scheduled_for is required")
    if not job.get("title"):
        errors.append("Invariant 10: title is required")
    if not job.get("log_dir"):
        errors.append("Invariant 11: log_dir is required")

    if errors:
        raise ValueError("Job invariant violation(s):\n" + "\n".join(errors))


def can_submit(job: dict) -> bool:
    return (
        job["submission"] in {"queued", "scheduled"}
        and job["execution"] == "pending"
        and job["readiness"] == "ready"
    )


def can_reschedule(job: dict) -> bool:
    return job["submission"] != "running"


def can_unschedule(job: dict) -> bool:
    return job["submission"] != "running"


def can_change_session(job: dict) -> bool:
    return job["submission"] != "running"


def can_delete(job: dict) -> bool:
    return job["submission"] != "running"


def can_edit_prompt(job: dict) -> bool:
    return job["submission"] != "running"


def can_retry(job: dict) -> bool:
    return job["execution"] in {"failed", "success"} and job["submission"] != "running"
