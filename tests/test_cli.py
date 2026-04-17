from pathlib import Path


def test_cli_module_imports_without_prompt_toolkit_installed(app_modules):
    assert callable(app_modules.cli.main)
    assert app_modules.cli.PROMPT_DIR.exists()


def test_list_and_show_commands_render_new_fields(app_modules, capsys, monkeypatch):
    cli = app_modules.cli
    job = app_modules.transitions.make_job(
        job_id="job1",
        title="Do the thing",
        agent="claude",
        session_mode="resume",
        session_id="sess-1234567890",
        prompt_file="/tmp/prompt.md",
        scheduled_for="2026-04-18T09:00:00+0100",
        cwd="/tmp/project",
        log_dir="/tmp/project/logs/job1",
    )
    app_modules.persistence.save_jobs([job])

    monkeypatch.setattr(
        cli,
        "list_job_views",
        lambda filter_name="all": ([app_modules.operations._job_with_scheduler(job)], None),
    )
    cli.main(["list"])
    out = capsys.readouterr().out
    assert "Title | Status | Scheduler | Run At | Updated | Created | Session | Dependency" in out
    assert "Do the thing" in out

    monkeypatch.setattr(
        cli, "get_job_view", lambda job_id: app_modules.operations._job_with_scheduler(job)
    )
    cli.main(["show", "job1"])
    out = capsys.readouterr().out
    assert "title:         Do the thing" in out
    assert "run_at:" in out
    assert "last_log_file:" in out


def test_cli_subcommands_dispatch_to_operations(app_modules, monkeypatch, capsys):
    cli = app_modules.cli
    called = {}

    monkeypatch.setattr(
        cli,
        "cli_reschedule_job",
        lambda job_id, when: called.update({"job_id": job_id, "when": when}) or 0,
    )
    assert cli.main(["reschedule", "job1", "now + 1 hour"]) == 0
    assert called == {"job_id": "job1", "when": "now + 1 hour"}

    called.clear()
    monkeypatch.setattr(
        cli, "cli_unschedule_job", lambda job_id: called.update({"unschedule": job_id}) or 0
    )
    assert cli.main(["unschedule", "job1"]) == 0
    assert called == {"unschedule": "job1"}

    called.clear()
    monkeypatch.setattr(cli, "cli_edit_prompt", lambda job_id: called.update({"edit": job_id}) or 0)
    assert cli.main(["edit-prompt", "job1"]) == 0
    assert called == {"edit": "job1"}


def test_mark_commands_accept_metadata(app_modules, monkeypatch):
    cli = app_modules.cli
    called = {}

    monkeypatch.setattr(
        cli,
        "cli_mark_running",
        lambda job_id, started_at, log_file: (
            called.update({"job_id": job_id, "started_at": started_at, "log_file": log_file}) or 0
        ),
    )
    rc = cli.main(
        [
            "mark",
            "running",
            "job1",
            "--started-at",
            "2026-04-18T09:00:00+0100",
            "--log-file",
            "/tmp/run.log",
        ]
    )
    assert rc == 0
    assert called["started_at"] == "2026-04-18T09:00:00+0100"
    assert called["log_file"] == "/tmp/run.log"


def test_jobs_menu_requires_prompt_toolkit_when_run_interactively(app_modules, monkeypatch):
    cli = app_modules.cli
    monkeypatch.setattr(
        cli,
        "_require_prompt_toolkit",
        lambda: (_ for _ in ()).throw(cli.OperationError("prompt_toolkit missing")),
    )
    try:
        cli.jobs_menu()
    except cli.OperationError as exc:
        assert "prompt_toolkit" in str(exc)
    else:
        raise AssertionError("expected OperationError")


def test_cli_edit_prompt_reloads_real_prompt_file(app_modules, monkeypatch, tmp_path, capsys):
    cli = app_modules.cli
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Original title", encoding="utf-8")
    job = app_modules.transitions.make_job(
        job_id="job1",
        title="Original title",
        agent="claude",
        session_mode="new",
        session_id=None,
        prompt_file=str(prompt),
        scheduled_for="2026-04-18T09:00:00+0100",
        cwd="/tmp/project",
        log_dir="/tmp/project/logs/job1",
    )
    app_modules.persistence.save_jobs([job])
    monkeypatch.setattr(cli, "get_job_view", lambda job_id: job)
    monkeypatch.setattr(
        cli,
        "edit_file",
        lambda path: Path(path).write_text("Updated title\n\nbody", encoding="utf-8"),
    )
    monkeypatch.setattr(cli, "refresh_prompt", lambda job_id: {**job, "title": "Updated title"})

    rc = cli.cli_edit_prompt("job1")
    assert rc == 0
    assert "Updated title" in capsys.readouterr().out
