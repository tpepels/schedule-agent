import importlib
import json
from pathlib import Path

import pytest

import schedule_agent.persistence as persistence


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    importlib.reload(persistence)
    persistence._ensure_dirs()


# ---------------------------------------------------------------------------
# migrate_job
# ---------------------------------------------------------------------------

def test_migrate_job_already_new_model_returns_unchanged():
    job = {
        "id": "j1",
        "submission": "queued",
        "execution": "pending",
        "readiness": "ready",
        "session_mode": "new",
        "session_id": None,
    }
    result = persistence.migrate_job(job)
    assert result is job  # same object, untouched


def test_migrate_job_old_queued_status():
    job = {"id": "j1", "agent": "claude", "when": "now", "cwd": "/tmp", "log": "/tmp/log.txt", "prompt_file": "/tmp/p.md"}
    result = persistence.migrate_job(job, {"status": "queued"})
    assert result["submission"] == "queued"
    assert result["execution"] == "pending"
    assert result["readiness"] == "ready"
    assert result["session_mode"] == "new"
    assert result["session_id"] is None


def test_migrate_job_old_submitted_status_preserves_at_job_id():
    job = {"id": "j1", "agent": "claude", "when": "now", "cwd": "/tmp", "log": "/tmp/log.txt", "prompt_file": "/tmp/p.md"}
    result = persistence.migrate_job(job, {"status": "submitted", "at_job_id": "42"})
    assert result["submission"] == "scheduled"
    assert result["execution"] == "pending"
    assert result["at_job_id"] == "42"


def test_migrate_job_old_submitted_status_without_at_job_id():
    job = {"id": "j1", "agent": "claude", "when": "now", "cwd": "/tmp", "log": "/tmp/log.txt", "prompt_file": "/tmp/p.md"}
    result = persistence.migrate_job(job, {"status": "submitted"})
    assert result["submission"] == "scheduled"
    assert result["at_job_id"] is None


def test_migrate_job_old_running_status():
    job = {"id": "j1", "agent": "codex", "when": "now", "cwd": "/tmp", "log": "/tmp/log.txt", "prompt_file": "/tmp/p.md"}
    result = persistence.migrate_job(job, {"status": "running"})
    assert result["submission"] == "running"
    assert result["execution"] == "running"


def test_migrate_job_old_success_status():
    job = {"id": "j1", "agent": "codex", "when": "now", "cwd": "/tmp", "log": "/tmp/log.txt", "prompt_file": "/tmp/p.md"}
    result = persistence.migrate_job(job, {"status": "success"})
    assert result["submission"] == "queued"
    assert result["execution"] == "success"
    assert result["last_run_at"] is not None


def test_migrate_job_old_failed_status():
    job = {"id": "j1", "agent": "codex", "when": "now", "cwd": "/tmp", "log": "/tmp/log.txt", "prompt_file": "/tmp/p.md"}
    result = persistence.migrate_job(job, {"status": "failed"})
    assert result["submission"] == "queued"
    assert result["execution"] == "failed"
    assert result["last_run_at"] is not None


def test_migrate_job_old_session_field():
    job = {"id": "j1", "agent": "claude", "when": "now", "cwd": "/tmp", "log": "/tmp/log.txt", "prompt_file": "/tmp/p.md", "session": "sess-abc"}
    result = persistence.migrate_job(job)
    assert result["session_mode"] == "resume"
    assert result["session_id"] == "sess-abc"
    assert "session" not in result


def test_migrate_job_no_session_becomes_new():
    job = {"id": "j1", "agent": "claude", "when": "now", "cwd": "/tmp", "log": "/tmp/log.txt", "prompt_file": "/tmp/p.md"}
    result = persistence.migrate_job(job)
    assert result["session_mode"] == "new"
    assert result["session_id"] is None


def test_migrate_job_with_depends_on_sets_waiting_dependency():
    job = {
        "id": "j2",
        "agent": "claude",
        "when": "now",
        "cwd": "/tmp",
        "log": "/tmp/log.txt",
        "prompt_file": "/tmp/p.md",
        "depends_on": "j1",
    }
    result = persistence.migrate_job(job)
    assert result["readiness"] == "waiting_dependency"


def test_migrate_job_without_depends_on_sets_ready():
    job = {"id": "j1", "agent": "claude", "when": "now", "cwd": "/tmp", "log": "/tmp/log.txt", "prompt_file": "/tmp/p.md"}
    result = persistence.migrate_job(job)
    assert result["readiness"] == "ready"


# ---------------------------------------------------------------------------
# save_jobs / load_jobs
# ---------------------------------------------------------------------------

def test_save_and_load_jobs_round_trip():
    from schedule_agent.transitions import make_job
    job = make_job("j1", "claude", "new", None, "/tmp/p.md", "now + 5 minutes", "/tmp", "/tmp/log.txt")
    persistence.save_jobs([job])
    loaded = persistence.load_jobs()
    assert len(loaded) == 1
    assert loaded[0]["id"] == "j1"
    assert loaded[0]["submission"] == "queued"


def test_load_jobs_empty_when_file_missing():
    persistence._queue_file().unlink(missing_ok=True)
    assert persistence.load_jobs() == []


def test_load_jobs_migrates_legacy_jobs_on_the_fly(tmp_path):
    state_dir = persistence._state_home()
    queue_file = state_dir / "agent_queue.jsonl"
    legacy_state_file = state_dir / "agent_queue_state.json"

    old_job = {"id": "old1", "agent": "claude", "when": "now", "cwd": "/tmp", "log": "/tmp/log.txt", "prompt_file": "/tmp/p.md", "session": "sess-legacy"}
    queue_file.write_text(json.dumps(old_job), encoding="utf-8")
    legacy_state_file.write_text(json.dumps({"old1": {"status": "submitted", "at_job_id": "7"}}), encoding="utf-8")

    jobs = persistence.load_jobs()
    assert len(jobs) == 1
    j = jobs[0]
    assert j["submission"] == "scheduled"
    assert j["at_job_id"] == "7"
    assert j["session_mode"] == "resume"
    assert j["session_id"] == "sess-legacy"


# ---------------------------------------------------------------------------
# find_job / update_job_in_list
# ---------------------------------------------------------------------------

def test_find_job_found():
    jobs = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    idx, job = persistence.find_job(jobs, "b")
    assert idx == 1
    assert job["id"] == "b"


def test_find_job_not_found():
    jobs = [{"id": "a"}]
    idx, job = persistence.find_job(jobs, "x")
    assert idx is None
    assert job is None


def test_update_job_in_list_replaces_correct_entry():
    jobs = [{"id": "a", "x": 1}, {"id": "b", "x": 2}]
    updated = persistence.update_job_in_list(jobs, {"id": "a", "x": 99})
    assert updated[0]["x"] == 99
    assert updated[1]["x"] == 2
    assert jobs[0]["x"] == 1  # original unchanged


# ---------------------------------------------------------------------------
# write_prompt_file
# ---------------------------------------------------------------------------

def test_write_prompt_file(tmp_path):
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    path = persistence.write_prompt_file(prompt_dir, "job1", "Hello prompt")
    assert Path(path).read_text(encoding="utf-8") == "Hello prompt"
    assert Path(path).name == "job1.md"


# ---------------------------------------------------------------------------
# load_jobs – invalid record isolation
# ---------------------------------------------------------------------------

def test_load_jobs_isolates_invalid_records(tmp_path, monkeypatch):
    """A corrupt job record produces a sentinel instead of crashing load_jobs."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    from schedule_agent import persistence
    from schedule_agent.transitions import make_job

    good_job = make_job("good", "claude", "new", None, "/tmp/p.md", "now", "/tmp", "/tmp/log.txt")
    # Bad job: submission=scheduled but no at_job_id (violates invariant 1)
    bad_job = {
        "id": "bad",
        "submission": "scheduled",  # requires at_job_id
        "at_job_id": None,           # invariant violation
        "execution": "pending",
        "readiness": "ready",
        "session_mode": "new",
        "session_id": None,
        "agent": "claude",
        "when": "now",
        "cwd": "/tmp",
        "log": "/tmp/log.txt",
        "prompt_file": "/tmp/p.md",
        "created_at": "2026-01-01 00:00:00",
        "updated_at": "2026-01-01 00:00:00",
        "last_run_at": None,
    }

    import json
    queue_file = persistence._queue_file()
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    queue_file.write_text(
        json.dumps(good_job) + "\n" + json.dumps(bad_job),
        encoding="utf-8"
    )

    jobs = persistence.load_jobs()
    assert len(jobs) == 2
    assert jobs[0]["id"] == "good"
    assert jobs[0].get("_invalid") is None
    assert jobs[1]["id"] == "bad"
    assert jobs[1].get("_invalid") is True
    assert "_error" in jobs[1]
