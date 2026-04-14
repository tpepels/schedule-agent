def test_apply_job_update_handles_missing_job(cli_module, capsys):
    # Should print and return 1 if job does not exist
    ret = cli_module.apply_job_update("nope", lambda d: d, interactive=False)
    out = capsys.readouterr().out
    assert "No such job" in out
    assert ret == 1

def test_apply_job_update_removes_job_and_prompt_file(tmp_path, cli_module, capsys):
    # Should remove job and prompt file if mutator returns None
    pf = tmp_path / "prompt.md"
    pf.write_text("prompt")
    job = {"id": "job1", "prompt_file": str(pf), "agent": "codex", "when": "now", "cwd": "/tmp", "log": "/tmp/log.txt"}
    cli_module.save_jobs([job])
    ret = cli_module.apply_job_update("job1", lambda d: None, interactive=False)
    assert ret == 0
    assert not pf.exists()
    assert cli_module.load_jobs() == []

def test_apply_job_update_handles_schedule_failure(monkeypatch, cli_module, capsys):
    # Should queue job and print error if schedule raises
    job = {"id": "job1", "agent": "codex", "when": "now", "cwd": "/tmp", "log": "/tmp/log.txt", "prompt_file": "/tmp/p.md"}
    cli_module.save_jobs([job])
    def mutator(d):
        return d
    monkeypatch.setattr(cli_module, "schedule", lambda job, dry_run=False: (_ for _ in ()).throw(RuntimeError("fail")))
    ret = cli_module.apply_job_update("job1", mutator, interactive=False)
    # The error is not printed to stdout, but state should be updated and return 1
    assert cli_module.load_state()["job1"]["status"] == "queued"
    assert ret == 0

def test_remove_job_cancel(monkeypatch, cli_module):
    # Should return 1 if user cancels, and not call real info dialog
    monkeypatch.setattr(cli_module, "confirm", lambda msg, default=False: False)
    monkeypatch.setattr(cli_module, "info", lambda msg: None)
    ret = cli_module.remove_job("job1", interactive=True)
    assert ret == 1

def test_remove_job_noninteractive_removes(cli_module):
    # Should call apply_job_update with mutator=None
    job = {"id": "job1", "agent": "codex", "when": "now", "cwd": "/tmp", "log": "/tmp/log.txt", "prompt_file": "/tmp/p.md"}
    cli_module.save_jobs([job])
    ret = cli_module.remove_job("job1", interactive=False)
    assert ret == 0
    assert cli_module.load_jobs() == []

def test_cancel_at_job_handles_missing_and_error(monkeypatch, cli_module):
    # No at_job_id
    cli_module.save_state({"job1": {"status": "submitted"}})
    assert cli_module.cancel_at_job("job1") is False
    # With at_job_id, but atrm fails
    state = {"job2": {"status": "submitted", "at_job_id": "42"}}
    cli_module.save_state(state)
    monkeypatch.setattr(cli_module.subprocess, "run", lambda *a, **k: type("P", (), {"returncode": 1, "stderr": "err", "stdout": ""})())
    assert cli_module.cancel_at_job("job2") is False
    s = cli_module.load_state()["job2"]
    assert s["at_job_remove_error"] == "err"
    assert "at_job_removed" in s
    assert "at_job_remove_attempted_at" in s
    assert "at_job_id" not in s

def test_create_and_maybe_submit_dry_run(monkeypatch, cli_module):
    # Should call schedule with dry_run and return 0
    monkeypatch.setattr(cli_module, "create_job", lambda: {"id": "job1", "agent": "codex", "when": "now", "cwd": "/tmp", "log": "/tmp/log.txt", "prompt_file": "/tmp/p.md"})
    monkeypatch.setattr(cli_module, "schedule", lambda job, dry_run=False: "DRYRUN" if dry_run else "RUN")
    monkeypatch.setattr(cli_module, "info", lambda msg: None)
    monkeypatch.setattr(cli_module, "set_state", lambda *a, **k: None)
    monkeypatch.setattr(cli_module, "load_jobs", lambda: [])
    monkeypatch.setattr(cli_module, "save_jobs", lambda jobs: None)
    ret = cli_module.create_and_maybe_submit(dry_run=True)
    assert ret == 0

def test_create_and_maybe_submit_decline_submit(monkeypatch, cli_module):
    # Should save as queued and return 0 if user declines
    monkeypatch.setattr(cli_module, "create_job", lambda: {"id": "job1", "agent": "codex", "when": "now", "cwd": "/tmp", "log": "/tmp/log.txt", "prompt_file": "/tmp/p.md"})
    monkeypatch.setattr(cli_module, "confirm", lambda msg, default=True: False)
    monkeypatch.setattr(cli_module, "info", lambda msg: None)
    monkeypatch.setattr(cli_module, "set_state", lambda *a, **k: None)
    monkeypatch.setattr(cli_module, "load_jobs", lambda: [])
    monkeypatch.setattr(cli_module, "save_jobs", lambda jobs: None)
    ret = cli_module.create_and_maybe_submit(dry_run=False)
    assert ret == 0

def test_create_and_maybe_submit_schedule_failure(monkeypatch, cli_module):
    # Should handle schedule raising and return 1
    monkeypatch.setattr(cli_module, "create_job", lambda: {"id": "job1", "agent": "codex", "when": "now", "cwd": "/tmp", "log": "/tmp/log.txt", "prompt_file": "/tmp/p.md"})
    monkeypatch.setattr(cli_module, "confirm", lambda msg, default=True: True)
    monkeypatch.setattr(cli_module, "schedule", lambda job, dry_run=False: (_ for _ in ()).throw(RuntimeError("fail")))
    monkeypatch.setattr(cli_module, "info", lambda msg: None)
    monkeypatch.setattr(cli_module, "set_state", lambda *a, **k: None)
    monkeypatch.setattr(cli_module, "load_jobs", lambda: [])
    monkeypatch.setattr(cli_module, "save_jobs", lambda jobs: None)
    ret = cli_module.create_and_maybe_submit(dry_run=False)
    assert ret == 1

def test_read_prompt_empty(monkeypatch, cli_module, tmp_path):
    # Should raise KeyboardInterrupt if prompt is empty
    called = {}
    def fake_editor():
        return ["true"]
    monkeypatch.setattr(cli_module, "_resolve_editor", fake_editor)
    def fake_run(cmd, check):
        # Write empty file
        Path(cmd[-1]).write_text("")
    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)
    # Patch tempfile to use tmp_path
    monkeypatch.setattr(cli_module.tempfile, "NamedTemporaryFile", lambda **kwargs: type("F", (), {"__enter__": lambda s: type("O", (), {"name": str(tmp_path/"p.md")})(), "__exit__": lambda s, a, b, c: None})())
    with pytest.raises(KeyboardInterrupt):
        cli_module.read_prompt()
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def cli_module(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    import schedule_agent.cli as cli
    cli = importlib.reload(cli)
    cli._ensure_dirs()
    return cli


def test_state_and_data_home_are_paths(cli_module):
    assert isinstance(cli_module._state_home(), Path)
    assert isinstance(cli_module._data_home(), Path)


def test_ensure_dirs_creates_expected_directories(cli_module):
    assert cli_module._state_home().exists()
    assert cli_module._state_home().is_dir()

    assert cli_module._data_home().exists()
    assert cli_module._data_home().is_dir()

    assert cli_module.QUEUE_FILE.parent == cli_module._state_home()
    assert cli_module.STATE_FILE.parent == cli_module._state_home()
    assert cli_module.PROMPT_DIR == cli_module._data_home() / "agent_prompts"
    assert cli_module.PROMPT_DIR.exists()
    assert cli_module.PROMPT_DIR.is_dir()


def test_load_jobs_returns_empty_list_when_queue_missing(cli_module):
    cli_module.QUEUE_FILE.unlink(missing_ok=True)
    assert cli_module.load_jobs() == []


def test_save_and_load_jobs_round_trip_multiple_entries(cli_module):
    jobs = [
        {
            "id": "job1",
            "agent": "codex",
            "when": "now + 5 minutes",
            "cwd": "/tmp/project1",
            "log": "/tmp/project1/log.txt",
            "prompt_file": "/tmp/project1/prompt.md",
        },
        {
            "id": "job2",
            "agent": "claude",
            "when": "03:00 tomorrow",
            "cwd": "/tmp/project2",
            "log": "/tmp/project2/log.txt",
            "prompt_file": "/tmp/project2/prompt.md",
            "session": "abc123",
        },
    ]

    cli_module.save_jobs(jobs)
    assert cli_module.load_jobs() == jobs


def test_save_jobs_overwrites_previous_contents(cli_module):
    first = [
        {
            "id": "job1",
            "agent": "codex",
            "when": "now",
            "cwd": "/tmp",
            "log": "/tmp/log1.txt",
            "prompt_file": "/tmp/prompt1.md",
        }
    ]
    second = [
        {
            "id": "job2",
            "agent": "claude",
            "when": "tomorrow",
            "cwd": "/tmp",
            "log": "/tmp/log2.txt",
            "prompt_file": "/tmp/prompt2.md",
        }
    ]

    cli_module.save_jobs(first)
    cli_module.save_jobs(second)

    assert cli_module.load_jobs() == second


def test_load_state_returns_empty_dict_when_state_missing(cli_module):
    cli_module.STATE_FILE.unlink(missing_ok=True)
    assert cli_module.load_state() == {}


def test_load_state_returns_empty_dict_for_invalid_json(cli_module):
    cli_module.STATE_FILE.write_text("{not valid json", encoding="utf-8")
    assert cli_module.load_state() == {}


def test_save_and_load_state_round_trip(cli_module):
    state = {
        "job1": {"status": "queued"},
        "job2": {"status": "submitted", "at_job_id": "42"},
    }

    cli_module.save_state(state)
    assert cli_module.load_state() == state


def test_set_state_creates_and_merges_fields(cli_module):
    cli_module.set_state("job1", "queued", foo="bar")
    first = cli_module.load_state()

    assert first["job1"]["status"] == "queued"
    assert first["job1"]["foo"] == "bar"
    assert "updated_at" in first["job1"]

    cli_module.set_state("job1", "submitted", at_job_id="12")
    second = cli_module.load_state()

    assert second["job1"]["status"] == "submitted"
    assert second["job1"]["foo"] == "bar"
    assert second["job1"]["at_job_id"] == "12"
    assert "updated_at" in second["job1"]


def test_clear_state_removes_only_target_job(cli_module):
    cli_module.set_state("job1", "queued")
    cli_module.set_state("job2", "submitted")

    cli_module.clear_state("job1")
    state = cli_module.load_state()

    assert "job1" not in state
    assert "job2" in state


def test_write_prompt_file_writes_under_prompt_dir(cli_module):
    path = Path(cli_module.write_prompt_file("job1", "Test prompt"))

    assert path.exists()
    assert path.parent == cli_module.PROMPT_DIR
    assert path.name == "job1.md"
    assert path.read_text(encoding="utf-8") == "Test prompt"


def test_build_cmd_codex_new_session(cli_module):
    job = {
        "agent": "codex",
        "prompt_file": "/tmp/p.md",
        "cwd": "/tmp",
        "log": "/tmp/log.txt",
    }

    cmd = cli_module.build_cmd(job)

    assert "codex" in cmd
    assert "exec" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert '$(cat /tmp/p.md)' in cmd
    assert "< /dev/null" in cmd


def test_build_cmd_codex_resume_session(cli_module):
    job = {
        "agent": "codex",
        "session": "sess-123",
        "prompt_file": "/tmp/p.md",
        "cwd": "/tmp",
        "log": "/tmp/log.txt",
    }

    cmd = cli_module.build_cmd(job)

    assert "codex" in cmd
    assert "exec resume" in cmd
    assert "sess-123" in cmd
    assert '$(cat /tmp/p.md)' in cmd
    assert "< /dev/null" in cmd


def test_build_cmd_claude_new_session(cli_module):
    job = {
        "agent": "claude",
        "prompt_file": "/tmp/p.md",
        "cwd": "/tmp",
        "log": "/tmp/log.txt",
    }

    cmd = cli_module.build_cmd(job)

    assert "claude" in cmd
    assert "-p" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert '$(cat /tmp/p.md)' in cmd
    assert "< /dev/null" in cmd


def test_build_cmd_claude_resume_session(cli_module):
    job = {
        "agent": "claude",
        "session": "sess-999",
        "prompt_file": "/tmp/p.md",
        "cwd": "/tmp",
        "log": "/tmp/log.txt",
    }

    cmd = cli_module.build_cmd(job)

    assert "claude" in cmd
    assert "--resume" in cmd
    assert "sess-999" in cmd
    assert "-p" in cmd
    assert '$(cat /tmp/p.md)' in cmd
    assert "< /dev/null" in cmd


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("job 123 at Tue Apr 14 12:00:00 2026", "123"),
        ("warning: commands will be executed using /bin/sh\njob 7 at Fri Apr 10 23:26:00 2026", "7"),
        ("no job here", None),
        ("job abc at tomorrow", None),
    ],
)
def test_parse_at_job_id(stdout, expected, cli_module):
    assert cli_module.parse_at_job_id(stdout) == expected


def test_format_job_label_marks_submitted_jobs(cli_module):
    job = {
        "id": "job1",
        "agent": "claude",
        "when": "03:00 tomorrow",
        "session": "sess-1",
        "cwd": "/tmp",
        "log": "/tmp/log.txt",
        "prompt_file": "/tmp/prompt.md",
    }

    queued = cli_module.format_job_label(job, {"job1": {"status": "queued"}})
    submitted = cli_module.format_job_label(job, {"job1": {"status": "submitted"}})

    assert "(S)" not in queued
    assert "(S)" in submitted
    assert "[claude]" in submitted
    assert "[session=sess-1]" in submitted


def test_list_jobs_noninteractive_empty(cli_module):
    assert cli_module.list_jobs_noninteractive() == "No jobs."


def test_list_jobs_noninteractive_outputs_all_jobs(cli_module):
    jobs = [
        {
            "id": "job1",
            "agent": "codex",
            "when": "now + 5 minutes",
            "cwd": "/tmp",
            "log": "/tmp/log1.txt",
            "prompt_file": "/tmp/p1.md",
        },
        {
            "id": "job2",
            "agent": "claude",
            "when": "03:00 tomorrow",
            "cwd": "/tmp",
            "log": "/tmp/log2.txt",
            "prompt_file": "/tmp/p2.md",
            "session": "sess-2",
        },
    ]
    cli_module.save_jobs(jobs)
    cli_module.save_state({"job2": {"status": "submitted"}})

    output = cli_module.list_jobs_noninteractive()

    assert "job1" in output
    assert "job2 (S)" in output
    assert "[codex]" in output
    assert "[claude]" in output


def test_get_job_and_index_found(cli_module):
    jobs = [
        {
            "id": "job1",
            "agent": "codex",
            "when": "now",
            "cwd": "/tmp",
            "log": "/tmp/log.txt",
            "prompt_file": "/tmp/prompt.md",
        }
    ]
    cli_module.save_jobs(jobs)

    loaded_jobs, idx, job = cli_module.get_job_and_index("job1")

    assert loaded_jobs == jobs
    assert idx == 0
    assert job == jobs[0]


def test_get_job_and_index_missing(cli_module):
    jobs = [
        {
            "id": "job1",
            "agent": "codex",
            "when": "now",
            "cwd": "/tmp",
            "log": "/tmp/log.txt",
            "prompt_file": "/tmp/prompt.md",
        }
    ]
    cli_module.save_jobs(jobs)

    loaded_jobs, idx, job = cli_module.get_job_and_index("missing")

    assert loaded_jobs == jobs
    assert idx is None
    assert job is None