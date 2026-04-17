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
        "scheduled_for": "2026-04-17T10:30:00+0100",
        "at_job_id": None,
        "log_dir": "/tmp/logs/job1",
        "last_run_at": None,
    }
    job.update(overrides)
    return job


def test_derive_display_state_maps_terminal_and_waiting_states(app_modules):
    model = app_modules.state_model
    assert model.derive_display_state(_job()) == "queued"
    assert model.derive_display_state(_job(submission="scheduled", at_job_id="12")) == "scheduled"
    assert model.derive_display_state(_job(submission="running", execution="running")) == "running"
    assert model.derive_display_state(_job(readiness="waiting_dependency", depends_on="x")) == "waiting"
    assert model.derive_display_state(_job(readiness="blocked", depends_on="x")) == "blocked"
    assert model.derive_display_state(_job(execution="success", last_run_at="2026-04-17T10:45:00+0100")) == "completed"
    assert model.derive_display_state(_job(execution="failed", last_run_at="2026-04-17T10:45:00+0100")) == "failed"
    assert model.derive_display_state({"id": "bad", "_invalid": True}) == "invalid"


def test_status_and_scheduler_labels_are_clear(app_modules):
    model = app_modules.state_model
    assert model.display_label(_job(execution="success", last_run_at="2026-04-17T10:45:00+0100")) == "Completed"
    assert model.scheduler_label("not_queued") == "Not queued"
    assert model.scheduler_label("drifted") == "Drifted"


def test_check_invariants_requires_new_schedule_fields(app_modules):
    model = app_modules.state_model
    with pytest.raises(ValueError, match="scheduled_for is required"):
        model.check_invariants(_job(scheduled_for=None))
    with pytest.raises(ValueError, match="title is required"):
        model.check_invariants(_job(title=""))
    with pytest.raises(ValueError, match="log_dir is required"):
        model.check_invariants(_job(log_dir=""))


def test_guard_functions_block_running_mutations_and_allow_retry_for_completed(app_modules):
    model = app_modules.state_model
    running = _job(submission="running", execution="running")
    completed = _job(execution="success", last_run_at="2026-04-17T10:45:00+0100")
    assert model.can_edit_prompt(running) is False
    assert model.can_delete(running) is False
    assert model.can_unschedule(running) is False
    assert model.can_retry(completed) is True
