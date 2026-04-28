import importlib
import os
import time
from types import SimpleNamespace

import pytest


def pytest_configure(config):
    os.environ["TZ"] = "UTC"
    time.tzset()


@pytest.fixture
def app_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(tmp_path / "codex-sqlite"))

    import schedule_agent.cli as cli
    import schedule_agent.operations as operations
    import schedule_agent.persistence as persistence
    import schedule_agent.scheduler_backend as scheduler_backend
    import schedule_agent.state_model as state_model
    import schedule_agent.transitions as transitions

    persistence = importlib.reload(persistence)
    scheduler_backend = importlib.reload(scheduler_backend)
    transitions = importlib.reload(transitions)
    state_model = importlib.reload(state_model)
    operations = importlib.reload(operations)
    cli = importlib.reload(cli)
    persistence._ensure_dirs()

    return SimpleNamespace(
        persistence=persistence,
        scheduler_backend=scheduler_backend,
        operations=operations,
        cli=cli,
        transitions=transitions,
        state_model=state_model,
    )
