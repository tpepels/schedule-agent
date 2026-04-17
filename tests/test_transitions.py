def _base_job(app_modules, **overrides):
    job = app_modules.transitions.make_job(
        job_id="job1",
        title="Ship scheduler overhaul",
        agent="claude",
        session_mode="new",
        session_id=None,
        prompt_file="/tmp/prompt.md",
        scheduled_for="2026-04-18T09:00:00+0100",
        cwd="/tmp/project",
        log_dir="/tmp/project/logs/job1",
    )
    job.update(overrides)
    return job


def test_make_job_sets_new_fields(app_modules):
    job = _base_job(app_modules)
    assert job["title"] == "Ship scheduler overhaul"
    assert job["scheduled_for"] == "2026-04-18T09:00:00+0100"
    assert job["log_dir"].endswith("/logs/job1")
    assert job["last_log_file"] is None
    assert job["last_started_at"] is None
    assert job["last_exit_code"] is None


def test_on_submit_updates_at_job_id_and_time(app_modules):
    updated = app_modules.transitions.on_submit(_base_job(app_modules), "42", scheduled_for="2026-04-18T09:05:00+0100")
    assert updated["submission"] == "scheduled"
    assert updated["at_job_id"] == "42"
    assert updated["scheduled_for"] == "2026-04-18T09:05:00+0100"


def test_on_start_records_log_metadata(app_modules):
    scheduled = app_modules.transitions.on_submit(_base_job(app_modules), "42")
    updated = app_modules.transitions.on_start(
        scheduled,
        started_at="2026-04-18T09:00:00+0100",
        log_file="/tmp/project/logs/job1/2026-04-18T09:00:00+0100.log",
    )
    assert updated["submission"] == "running"
    assert updated["execution"] == "running"
    assert updated["last_started_at"] == "2026-04-18T09:00:00+0100"
    assert updated["last_log_file"].endswith(".log")


def test_success_and_failure_record_exit_code(app_modules):
    scheduled = app_modules.transitions.on_submit(_base_job(app_modules), "42")
    running = app_modules.transitions.on_start(
        scheduled,
        started_at="2026-04-18T09:00:00+0100",
        log_file="/tmp/project/logs/job1/run.log",
    )
    done = app_modules.transitions.on_success(running, "2026-04-18T09:10:00+0100", 0, log_file="/tmp/project/logs/job1/run.log")
    failed = app_modules.transitions.on_failure(running, "2026-04-18T09:10:00+0100", 23, log_file="/tmp/project/logs/job1/run.log")
    assert done["execution"] == "success"
    assert done["last_exit_code"] == 0
    assert failed["execution"] == "failed"
    assert failed["last_exit_code"] == 23


def test_reschedule_and_retry_reset_terminal_state(app_modules):
    scheduled = app_modules.transitions.on_submit(_base_job(app_modules), "42")
    running = app_modules.transitions.on_start(scheduled, "2026-04-18T09:00:00+0100", "/tmp/project/logs/job1/run.log")
    completed = app_modules.transitions.on_success(running, "2026-04-18T09:10:00+0100", 0)

    rescheduled = app_modules.transitions.on_reschedule(completed, "2026-04-19T09:00:00+0100")
    retried = app_modules.transitions.on_retry(completed, "2026-04-20T09:00:00+0100")

    assert rescheduled["execution"] == "pending"
    assert rescheduled["last_run_at"] is None
    assert retried["execution"] == "pending"
    assert retried["readiness"] == "ready"
    assert retried["scheduled_for"] == "2026-04-20T09:00:00+0100"
