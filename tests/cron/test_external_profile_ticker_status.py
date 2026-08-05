import json

from hermes_cli import cron as cron_cli


def _write_contract(tmp_path, *, updated_at=1_000.0, stale_after=960.0):
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()
    (cron_dir / "ticker_external.json").write_text(json.dumps({
        "kind": "profile-launchd",
        "interval_seconds": 300,
        "stale_after_seconds": stale_after,
        "updated_at": updated_at,
    }))
    return cron_dir


def _prepare_status(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cron_cli, "_active_cron_provider_name", lambda: "builtin")
    monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [])
    monkeypatch.setattr("cron.jobs.list_jobs", lambda **kwargs: [])


def test_external_profile_ticker_contract_is_cadence_aware(monkeypatch, tmp_path):
    cron_dir = _write_contract(tmp_path, updated_at=1_000.0)
    (cron_dir / "ticker_last_result.json").write_text(json.dumps({
        "status": "completed",
        "returncode": 0,
    }))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cron_cli.time, "time", lambda: 1_800.0)

    state = cron_cli._external_profile_ticker_state()

    assert state is not None
    assert state["healthy"] is True
    assert state["contract_age"] == 800.0
    assert state["interval"] == 300


def test_cron_status_reports_healthy_external_profile_ticker(monkeypatch, capsys, tmp_path):
    cron_dir = _write_contract(tmp_path)
    (cron_dir / "ticker_last_result.json").write_text(json.dumps({
        "status": "completed",
        "returncode": 0,
    }))
    _prepare_status(monkeypatch, tmp_path)
    monkeypatch.setattr(cron_cli.time, "time", lambda: 1_100.0)

    cron_cli.cron_status()

    out = capsys.readouterr().out
    assert "External profile cron ticker is running" in out
    assert "LaunchAgent cadence: 300s" in out
    assert "Gateway is not running" not in out


def test_cron_status_reports_stale_external_profile_ticker(monkeypatch, capsys, tmp_path):
    _write_contract(tmp_path, updated_at=1_000.0, stale_after=960.0)
    _prepare_status(monkeypatch, tmp_path)
    monkeypatch.setattr(cron_cli.time, "time", lambda: 2_001.0)

    cron_cli.cron_status()

    out = capsys.readouterr().out
    assert "looks STALLED" in out
    assert "Cron jobs may NOT be firing" in out


def test_cron_status_surfaces_external_child_failure(monkeypatch, capsys, tmp_path):
    cron_dir = _write_contract(tmp_path)
    (cron_dir / "ticker_last_result.json").write_text(json.dumps({
        "status": "timeout",
        "returncode": 124,
    }))
    _prepare_status(monkeypatch, tmp_path)
    monkeypatch.setattr(cron_cli.time, "time", lambda: 1_100.0)

    cron_cli.cron_status()

    out = capsys.readouterr().out
    assert "last child tick timeout (exit 124)" in out
