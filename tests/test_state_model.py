import pytest

from schedule_agent.state_model import (
    can_cancel,
    can_delete,
    can_reschedule,
    can_retry,
    can_submit,
    check_invariants,
    derive_display_state,
)


# ---------------------------------------------------------------------------
# derive_display_state
# ---------------------------------------------------------------------------

def _base_job(**overrides):
    job = {
        "submission": "queued",
        "execution": "pending",
        "readiness": "ready",
        "session_mode": "new",
        "session_id": None,
        "at_job_id": None,
        "last_run_at": None,
    }
    job.update(overrides)
    return job


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "queued"),
        ({"submission": "scheduled", "at_job_id": "1"}, "scheduled"),
        ({"submission": "running", "execution": "running"}, "running"),
        ({"submission": "running", "execution": "running"}, "running"),
        ({"submission": "queued", "execution": "success", "last_run_at": "now"}, "done"),
        ({"submission": "queued", "execution": "failed", "last_run_at": "now"}, "failed"),
        ({"submission": "cancelled"}, "cancelled"),
        ({"readiness": "waiting_dependency", "depends_on": "job-a"}, "waiting"),
        ({"readiness": "blocked", "depends_on": "job-a"}, "blocked"),
    ],
)
def test_derive_display_state(overrides, expected):
    assert derive_display_state(_base_job(**overrides)) == expected


def test_derive_display_state_cancelled_beats_failed():
    job = _base_job(submission="cancelled", execution="failed", last_run_at="t")
    assert derive_display_state(job) == "cancelled"


def test_derive_display_state_running_beats_failed():
    job = _base_job(submission="running", execution="running")
    assert derive_display_state(job) == "running"


# ---------------------------------------------------------------------------
# check_invariants
# ---------------------------------------------------------------------------

def test_invariant_1_scheduled_requires_at_job_id():
    job = _base_job(submission="scheduled")  # no at_job_id
    with pytest.raises(ValueError, match="Invariant 1"):
        check_invariants(job)


def test_invariant_2_at_job_id_absent_when_not_scheduled():
    job = _base_job(submission="queued", at_job_id="5")
    with pytest.raises(ValueError, match="Invariant 2"):
        check_invariants(job)


def test_invariant_3_resume_requires_session_id():
    job = _base_job(session_mode="resume", session_id=None)
    with pytest.raises(ValueError, match="Invariant 3"):
        check_invariants(job)


def test_invariant_4_new_session_must_not_have_session_id():
    job = _base_job(session_mode="new", session_id="abc")
    with pytest.raises(ValueError, match="Invariant 4"):
        check_invariants(job)


def test_invariant_5_execution_running_requires_submission_running():
    job = _base_job(submission="queued", execution="running")
    with pytest.raises(ValueError, match="Invariant 5"):
        check_invariants(job)


def test_invariant_6_waiting_dependency_requires_depends_on():
    job = _base_job(readiness="waiting_dependency")
    with pytest.raises(ValueError, match="Invariant 6"):
        check_invariants(job)


def test_invariant_7_blocked_requires_depends_on():
    job = _base_job(readiness="blocked")
    with pytest.raises(ValueError, match="Invariant 7"):
        check_invariants(job)


def test_invariant_8_success_requires_last_run_at():
    job = _base_job(submission="queued", execution="success", last_run_at=None)
    with pytest.raises(ValueError, match="Invariant 8"):
        check_invariants(job)


def test_invariant_8_failed_requires_last_run_at():
    job = _base_job(submission="queued", execution="failed", last_run_at=None)
    with pytest.raises(ValueError, match="Invariant 8"):
        check_invariants(job)


def test_check_invariants_passes_for_valid_jobs():
    check_invariants(_base_job())
    check_invariants(_base_job(submission="scheduled", at_job_id="7"))
    check_invariants(_base_job(session_mode="resume", session_id="sess-x"))
    check_invariants(_base_job(readiness="waiting_dependency", depends_on="job-a"))
    check_invariants(_base_job(readiness="blocked", depends_on="job-a"))
    check_invariants(_base_job(submission="queued", execution="success", last_run_at="2026-01-01 00:00:00"))


# ---------------------------------------------------------------------------
# Guard functions
# ---------------------------------------------------------------------------

def test_can_submit_only_when_queued_pending_ready():
    assert can_submit(_base_job()) is True
    assert can_submit(_base_job(submission="scheduled", at_job_id="1")) is False
    assert can_submit(_base_job(execution="success", last_run_at="t")) is False
    assert can_submit(_base_job(readiness="waiting_dependency", depends_on="j")) is False


def test_can_reschedule_allows_all_except_cancelled():
    assert can_reschedule(_base_job()) is True
    assert can_reschedule(_base_job(submission="scheduled", at_job_id="1")) is True
    assert can_reschedule(_base_job(submission="cancelled")) is False


def test_can_cancel():
    assert can_cancel(_base_job()) is True
    assert can_cancel(_base_job(submission="scheduled", at_job_id="1")) is True
    assert can_cancel(_base_job(submission="running", execution="running")) is False
    assert can_cancel(_base_job(submission="cancelled")) is False


def test_can_delete_always_true():
    assert can_delete(_base_job()) is True
    assert can_delete(_base_job(submission="cancelled")) is True


def test_can_retry_only_when_failed():
    assert can_retry(_base_job(execution="failed", submission="queued", last_run_at="t")) is True
    assert can_retry(_base_job()) is False
    assert can_retry(_base_job(execution="success", last_run_at="t")) is False
