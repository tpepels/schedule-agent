import json


def test_write_prompt_file_and_log_dir_helpers(app_modules, tmp_path):
    persistence = app_modules.persistence
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    path = persistence.write_prompt_file(prompt_dir, "job1", "hello")
    assert path.endswith("job1.md")
    assert persistence.job_log_dir("job1").endswith("/logs/job1")


def test_migrate_job_resolves_schedule_from_atq_when_present(app_modules):
    persistence = app_modules.persistence
    atq_entries = {
        "42": type("Entry", (), {"scheduled_for": "2026-04-18T09:00:00+0100"})(),
    }
    job = {
        "id": "job1",
        "agent": "claude",
        "prompt_file": "/tmp/prompt.md",
        "cwd": "/tmp/project",
        "log": "/tmp/project/log.txt",
        "session": "sess-123",
    }
    legacy = {"status": "submitted", "at_job_id": "42"}
    migrated = persistence.migrate_job(job, legacy, atq_entries)
    assert migrated["session_mode"] == "resume"
    assert migrated["session_id"] == "sess-123"
    assert migrated["scheduled_for"] == "2026-04-18T09:00:00+0100"
    assert migrated["title"] == "(untitled job)"


def test_load_jobs_returns_invalid_sentinel_for_unresolvable_legacy_schedule(app_modules):
    persistence = app_modules.persistence
    queue_file = persistence._queue_file()
    queue_file.write_text(
        json.dumps(
            {
                "id": "broken",
                "agent": "claude",
                "prompt_file": "/tmp/prompt.md",
                "cwd": "/tmp/project",
                "log": "/tmp/project/log.txt",
                "when": "not a real time",
            }
        ),
        encoding="utf-8",
    )

    jobs = persistence.load_jobs()
    assert jobs[0]["_invalid"] is True
    assert "Could not resolve legacy schedule" in jobs[0]["_error"]


def test_load_jobs_preserves_new_model_records(app_modules):
    persistence = app_modules.persistence
    job = {
        "id": "job1",
        "title": "hello",
        "agent": "claude",
        "prompt_file": "/tmp/prompt.md",
        "session_mode": "new",
        "session_id": None,
        "scheduled_for": "2026-04-18T09:00:00+0100",
        "cwd": "/tmp/project",
        "log_dir": "/tmp/project/logs/job1",
        "last_log_file": None,
        "submission": "queued",
        "execution": "pending",
        "readiness": "ready",
        "at_job_id": None,
        "created_at": "2026-04-17T10:00:00+0100",
        "updated_at": "2026-04-17T10:00:00+0100",
        "last_started_at": None,
        "last_run_at": None,
        "last_exit_code": None,
    }
    persistence.save_jobs([job])
    loaded = persistence.load_jobs()
    assert loaded[0]["scheduled_for"] == "2026-04-18T09:00:00+0100"
    assert loaded[0]["title"] == "hello"
