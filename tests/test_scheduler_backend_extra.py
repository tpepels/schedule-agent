def test_query_atq_reports_missing_binary(app_modules, monkeypatch):
    backend = app_modules.scheduler_backend

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("atq")

    monkeypatch.setattr(backend.subprocess, "run", fake_run)

    entries, error = backend.query_atq()

    assert entries == {}
    assert error == "atq unavailable"
