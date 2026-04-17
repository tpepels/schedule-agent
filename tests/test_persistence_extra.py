import json


def test_migrate_job_sets_dependency_readiness_and_ignores_missing_legacy_log(
    app_modules, monkeypatch
):
    persistence = app_modules.persistence
    monkeypatch.setattr(
        persistence.legacy_compat,
        "normalize_legacy_when",
        lambda value: "2026-04-18T09:00:00+0000",
    )

    migrated = persistence.migrate_job(
        {
            "id": "job1",
            "agent": "claude",
            "prompt_file": "/tmp/missing-prompt.md",
            "cwd": "/tmp/project",
            "log": "/tmp/missing-run.log",
            "when": "tomorrow 09:00",
            "depends_on": "parent",
            "created_at": "2026-04-17 08:30:00",
        },
        legacy_state={"updated_at": "2026-04-17 08:45:00"},
    )

    assert migrated["readiness"] == "waiting_dependency"
    assert migrated["last_log_file"] is None
    assert migrated["scheduled_for"] == "2026-04-18T09:00:00+0000"
    assert migrated["created_at"] == "2026-04-17T08:30:00+0000"
    assert migrated["updated_at"] == "2026-04-17T08:45:00+0000"


def test_load_jobs_queries_atq_only_for_missing_schedules_and_marks_bad_rows_invalid(
    app_modules, monkeypatch
):
    persistence = app_modules.persistence
    persistence._queue_file().write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "legacy",
                        "agent": "claude",
                        "prompt_file": "/tmp/legacy.md",
                        "cwd": "/tmp/project",
                        "log": "/tmp/project/legacy.log",
                        "session": "sess-123",
                    }
                ),
                json.dumps(
                    {
                        "id": "current",
                        "title": "Current job",
                        "agent": "claude",
                        "prompt_file": "/tmp/current.md",
                        "session_mode": "new",
                        "session_id": None,
                        "scheduled_for": "2026-04-18T10:00:00+0000",
                        "cwd": "/tmp/project",
                        "log_dir": "/tmp/project/logs/current",
                        "last_log_file": None,
                        "submission": "queued",
                        "execution": "pending",
                        "readiness": "ready",
                        "at_job_id": None,
                        "created_at": "2026-04-17T10:00:00+0000",
                        "updated_at": "2026-04-17T10:00:00+0000",
                        "last_started_at": None,
                        "last_run_at": None,
                        "last_exit_code": None,
                    }
                ),
                json.dumps(
                    {
                        "id": "broken",
                        "title": "Broken job",
                        "agent": "claude",
                        "prompt_file": "/tmp/broken.md",
                        "session_mode": "resume",
                        "session_id": None,
                        "scheduled_for": "2026-04-18T11:00:00+0000",
                        "cwd": "/tmp/project",
                        "log_dir": "/tmp/project/logs/broken",
                        "last_log_file": None,
                        "submission": "queued",
                        "execution": "pending",
                        "readiness": "ready",
                        "at_job_id": None,
                        "created_at": "2026-04-17T10:00:00+0000",
                        "updated_at": "2026-04-17T10:00:00+0000",
                        "last_started_at": None,
                        "last_run_at": None,
                        "last_exit_code": None,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    persistence.save_legacy_state({"legacy": {"status": "submitted", "at_job_id": "42"}})

    called = {}

    def fake_query_atq(job_ids=None):
        called["job_ids"] = job_ids
        return (
            {
                "42": type(
                    "Entry",
                    (),
                    {"scheduled_for": "2026-04-18T09:15:00+0000"},
                )()
            },
            None,
        )

    monkeypatch.setattr(persistence, "query_atq", fake_query_atq)

    loaded = persistence.load_jobs()

    assert called["job_ids"] == ["42"]
    assert loaded[0]["scheduled_for"] == "2026-04-18T09:15:00+0000"
    assert loaded[0]["session_mode"] == "resume"
    assert loaded[1]["title"] == "Current job"
    assert loaded[2]["_invalid"] is True
    assert "Invariant 3" in loaded[2]["_error"]


def test_load_legacy_state_returns_empty_mapping_for_corrupt_json(app_modules):
    persistence = app_modules.persistence
    persistence._legacy_state_file().write_text("{broken json", encoding="utf-8")

    assert persistence.load_legacy_state() == {}
