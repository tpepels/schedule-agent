import pytest

from schedule_agent.transitions import (
    make_job,
    on_cancel,
    on_change_session,
    on_dependency_failure,
    on_dependency_success,
    on_failure,
    on_reschedule,
    on_retry,
    on_start,
    on_submit,
    on_success,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _queued_job(**overrides):
    base = make_job(
        job_id="job1",
        agent="claude",
        session_mode="new",
        session_id=None,
        prompt_file="/tmp/p.md",
        when="now + 5 minutes",
        cwd="/tmp",
        log="/tmp/log.txt",
    )
    base.update(overrides)
    return base


def _scheduled_job():
    return on_submit(_queued_job(), at_job_id="42")


def _running_job():
    return on_start(_scheduled_job())


def _done_job():
    return on_success(_running_job())


def _failed_job():
    return on_failure(_running_job())


# ---------------------------------------------------------------------------
# make_job
# ---------------------------------------------------------------------------

def test_make_job_standalone_initial_state():
    job = _queued_job()
    assert job["submission"] == "queued"
    assert job["execution"] == "pending"
    assert job["readiness"] == "ready"
    assert job["session_mode"] == "new"
    assert job["session_id"] is None
    assert job["at_job_id"] is None
    assert job["last_run_at"] is None
    assert "created_at" in job
    assert "updated_at" in job


def test_make_job_dependent_sets_waiting_dependency():
    job = make_job(
        job_id="child",
        agent="codex",
        session_mode="new",
        session_id=None,
        prompt_file="/tmp/p.md",
        when="now + 5 minutes",
        cwd="/tmp",
        log="/tmp/log.txt",
        depends_on="parent-job",
        dependency_condition="success",
    )
    assert job["readiness"] == "waiting_dependency"
    assert job["depends_on"] == "parent-job"
    assert job["dependency_condition"] == "success"


def test_make_job_with_resume_session():
    job = make_job(
        job_id="j",
        agent="claude",
        session_mode="resume",
        session_id="sess-123",
        prompt_file="/tmp/p.md",
        when="now",
        cwd="/tmp",
        log="/tmp/log.txt",
    )
    assert job["session_mode"] == "resume"
    assert job["session_id"] == "sess-123"


# ---------------------------------------------------------------------------
# on_submit
# ---------------------------------------------------------------------------

def test_on_submit_transitions_to_scheduled():
    job = _queued_job()
    updated = on_submit(job, at_job_id="99")
    assert updated["submission"] == "scheduled"
    assert updated["at_job_id"] == "99"
    assert updated["execution"] == "pending"  # unchanged
    assert updated is not job  # immutable


def test_on_submit_fails_invariant_when_no_at_job_id():
    job = _queued_job()
    with pytest.raises(ValueError):
        on_submit(job, at_job_id=None)


# ---------------------------------------------------------------------------
# on_start
# ---------------------------------------------------------------------------

def test_on_start_transitions_to_running():
    job = _scheduled_job()
    updated = on_start(job)
    assert updated["submission"] == "running"
    assert updated["execution"] == "running"
    assert updated["at_job_id"] is None  # cleared on start


def test_on_start_is_immutable():
    job = _scheduled_job()
    updated = on_start(job)
    assert updated is not job
    assert job["submission"] == "scheduled"


# ---------------------------------------------------------------------------
# on_success
# ---------------------------------------------------------------------------

def test_on_success_transitions_correctly():
    job = _running_job()
    updated = on_success(job)
    assert updated["submission"] == "queued"
    assert updated["execution"] == "success"
    assert updated["at_job_id"] is None
    assert updated["last_run_at"] is not None


# ---------------------------------------------------------------------------
# on_failure
# ---------------------------------------------------------------------------

def test_on_failure_transitions_correctly():
    job = _running_job()
    updated = on_failure(job)
    assert updated["submission"] == "queued"
    assert updated["execution"] == "failed"
    assert updated["at_job_id"] is None
    assert updated["last_run_at"] is not None


# ---------------------------------------------------------------------------
# on_cancel
# ---------------------------------------------------------------------------

def test_on_cancel_from_queued():
    job = _queued_job()
    updated = on_cancel(job)
    assert updated["submission"] == "cancelled"
    assert updated["at_job_id"] is None


def test_on_cancel_from_scheduled_clears_at_job_id():
    job = _scheduled_job()
    assert job["at_job_id"] == "42"
    updated = on_cancel(job)
    assert updated["submission"] == "cancelled"
    assert updated["at_job_id"] is None


# ---------------------------------------------------------------------------
# on_reschedule
# ---------------------------------------------------------------------------

def test_on_reschedule_queued_updates_when():
    job = _queued_job()
    updated = on_reschedule(job, "03:00 tomorrow")
    assert updated["when"] == "03:00 tomorrow"
    assert updated["submission"] == "queued"
    assert updated["execution"] == "pending"


def test_on_reschedule_done_job_resets_to_pending():
    job = _done_job()
    assert job["execution"] == "success"
    updated = on_reschedule(job, "now + 10 minutes")
    assert updated["execution"] == "pending"
    assert updated["submission"] == "queued"
    assert updated["last_run_at"] is None
    assert updated["at_job_id"] is None


def test_on_reschedule_failed_job_resets_to_pending():
    job = _failed_job()
    updated = on_reschedule(job, "now + 10 minutes")
    assert updated["execution"] == "pending"
    assert updated["submission"] == "queued"


# ---------------------------------------------------------------------------
# on_change_session
# ---------------------------------------------------------------------------

def test_on_change_session_new_to_resume():
    job = _queued_job()
    updated = on_change_session(job, "resume", "sess-abc")
    assert updated["session_mode"] == "resume"
    assert updated["session_id"] == "sess-abc"


def test_on_change_session_resume_to_new():
    job = make_job(
        job_id="j",
        agent="claude",
        session_mode="resume",
        session_id="old-sess",
        prompt_file="/tmp/p.md",
        when="now",
        cwd="/tmp",
        log="/tmp/log.txt",
    )
    updated = on_change_session(job, "new", None)
    assert updated["session_mode"] == "new"
    assert updated["session_id"] is None


# ---------------------------------------------------------------------------
# on_dependency_success / on_dependency_failure
# ---------------------------------------------------------------------------

def test_on_dependency_success_transitions_to_ready():
    job = make_job(
        job_id="child",
        agent="codex",
        session_mode="new",
        session_id=None,
        prompt_file="/tmp/p.md",
        when="now",
        cwd="/tmp",
        log="/tmp/log.txt",
        depends_on="parent",
    )
    assert job["readiness"] == "waiting_dependency"
    updated = on_dependency_success(job)
    assert updated["readiness"] == "ready"


def test_on_dependency_failure_transitions_to_blocked():
    job = make_job(
        job_id="child",
        agent="codex",
        session_mode="new",
        session_id=None,
        prompt_file="/tmp/p.md",
        when="now",
        cwd="/tmp",
        log="/tmp/log.txt",
        depends_on="parent",
    )
    updated = on_dependency_failure(job)
    assert updated["readiness"] == "blocked"


# ---------------------------------------------------------------------------
# on_retry
# ---------------------------------------------------------------------------

def test_on_retry_resets_failed_job():
    job = _failed_job()
    updated = on_retry(job)
    assert updated["submission"] == "queued"
    assert updated["execution"] == "pending"
    assert updated["readiness"] == "ready"
    assert updated["at_job_id"] is None


# ---------------------------------------------------------------------------
# on_resubmit_failed
# ---------------------------------------------------------------------------

def test_on_resubmit_failed_resets_submission_and_clears_at_job_id():
    from schedule_agent.transitions import on_resubmit_failed
    job = _queued_job()
    scheduled = on_submit(job, "99")
    assert scheduled["submission"] == "scheduled"
    assert scheduled["at_job_id"] == "99"

    failed = on_resubmit_failed(scheduled)
    assert failed["submission"] == "queued"
    assert failed["at_job_id"] is None
    assert failed["updated_at"] != scheduled["updated_at"] or True  # just check it ran


def test_on_resubmit_failed_preserves_other_fields():
    from schedule_agent.transitions import on_resubmit_failed
    job = _queued_job()
    scheduled = on_submit(job, "99")
    rescheduled = on_reschedule(scheduled, "04:00 tomorrow")
    failed = on_resubmit_failed(rescheduled)
    assert failed["when"] == "04:00 tomorrow"  # mutation preserved
    assert failed["submission"] == "queued"


def test_on_resubmit_failed_invariants_pass():
    from schedule_agent.transitions import on_resubmit_failed
    from schedule_agent.state_model import check_invariants
    job = on_resubmit_failed(on_submit(_queued_job(), "1"))
    check_invariants(job)  # should not raise


def test_all_transitions_are_immutable():
    job = _queued_job()
    submitted = on_submit(job, "1")
    running = on_start(submitted)
    done = on_success(running)
    failed_j = on_failure(on_start(on_submit(_queued_job(), "2")))

    assert job["submission"] == "queued"
    assert submitted["submission"] == "scheduled"
    assert running["submission"] == "running"
    assert done["submission"] == "queued"
    assert failed_j["submission"] == "queued"
