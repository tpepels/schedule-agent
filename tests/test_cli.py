import pytest
import tempfile
import os
import json
from pathlib import Path
from schedule_agent import cli

def test_state_home_and_data_home_are_paths():
    assert isinstance(cli._state_home(), Path)
    assert isinstance(cli._data_home(), Path)

def test_ensure_dirs_creates_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cli._ensure_dirs()
    assert cli._state_home().exists()
    assert cli._data_home().exists()
    assert (cli._data_home() / "agent_prompts").exists()

def test_save_and_load_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cli._ensure_dirs()
    jobs = [{"id": "job1", "agent": "codex", "when": "now", "cwd": "/tmp", "log": "/tmp/log.txt", "prompt_file": "/tmp/prompt.md"}]
    cli.save_jobs(jobs)
    loaded = cli.load_jobs()
    assert loaded == jobs

def test_save_and_load_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cli._ensure_dirs()
    state = {"job1": {"status": "queued"}}
    cli.save_state(state)
    loaded = cli.load_state()
    assert loaded == state

def test_set_and_clear_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cli._ensure_dirs()
    cli.set_state("job1", "queued", foo="bar")
    state = cli.load_state()
    assert state["job1"]["status"] == "queued"
    assert state["job1"]["foo"] == "bar"
    cli.clear_state("job1")
    state = cli.load_state()
    assert "job1" not in state

def test_write_prompt_file_and_read(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cli._ensure_dirs()
    job_id = "job1"
    prompt = "Test prompt"
    path = cli.write_prompt_file(job_id, prompt)
    assert Path(path).exists()
    assert Path(path).read_text(encoding="utf-8") == prompt

def test_build_cmd_codex_and_claude():
    job_codex = {"agent": "codex", "prompt_file": "/tmp/p.md", "cwd": "/tmp", "log": "/tmp/log.txt"}
    job_claude = {"agent": "claude", "prompt_file": "/tmp/p.md", "cwd": "/tmp", "log": "/tmp/log.txt"}
    assert "codex exec" in cli.build_cmd(job_codex)
    assert "claude -p" in cli.build_cmd(job_claude)

def test_parse_at_job_id():
    out = "job 123 at Tue Apr 14 12:00:00 2026"
    assert cli.parse_at_job_id(out) == "123"
    assert cli.parse_at_job_id("no job here") is None
