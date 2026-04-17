import pytest


def _job(**overrides):
    job = {
        "id": "job1",
        "title": "Test job",
        "submission": "queued",
        "execution": "pending",
        "readiness": "ready",
        "session_mode": "new",
        "session_id": None,
        "scheduled_for": "2026-04-17T10:30:00+0000",
        "at_job_id": None,
        "log_dir": "/tmp/logs/job1",
        "last_run_at": None,
    }
    job.update(overrides)
    return job


def test_check_invariants_reports_multiple_actionable_errors(app_modules):
    model = app_modules.state_model

    with pytest.raises(ValueError) as exc:
        model.check_invariants(
            _job(
                title="",
                submission="mystery",
                execution="done",
                readiness="later",
                session_mode="attach",
                scheduled_for="not-an-iso",
                log_dir="",
            )
        )

    message = str(exc.value)
    assert "Unknown submission state" in message
    assert "Unknown execution state" in message
    assert "Unknown session mode" in message
    assert "Unknown readiness state" in message
    assert "scheduled_for is not a valid ISO datetime" in message
    assert "title is required" in message
    assert "log_dir is required" in message


def test_transitions_cancel_and_change_session_keep_jobs_valid(app_modules):
    transitions = app_modules.transitions
    job = transitions.make_job(
        job_id="job1",
        title="Ship scheduler overhaul",
        agent="claude",
        session_mode="new",
        session_id=None,
        prompt_file="/tmp/prompt.md",
        scheduled_for="2026-04-18T09:00:00+0000",
        cwd="/tmp/project",
        log_dir="/tmp/project/logs/job1",
    )

    cancelled = transitions.on_cancel(job)
    resumed = transitions.on_change_session(job, "resume", "sess-123")

    assert cancelled["submission"] == "cancelled"
    assert cancelled["at_job_id"] is None
    assert resumed["session_mode"] == "resume"
    assert resumed["session_id"] == "sess-123"
