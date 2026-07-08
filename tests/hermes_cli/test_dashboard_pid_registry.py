from __future__ import annotations

import argparse
import os
import sys
import time

import pytest

from hermes_cli import main as main_mod


def _args(**kw):
    defaults = dict(
        command="dashboard",
        port=0,
        host="127.0.0.1",
        no_open=True,
        insecure=False,
        stop=False,
        status=False,
        replace=False,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def test_bound_server_writes_live_pidfile_and_cleanup_removes_it(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(main_mod, "_dashboard_active_profile_name", lambda: "default")
    monkeypatch.setattr(main_mod.atexit, "register", lambda fn: None)
    monkeypatch.setattr(
        main_mod, "_install_dashboard_pidfile_signal_cleanup", lambda fn: None
    )
    monkeypatch.setattr(
        main_mod,
        "_read_dashboard_process_cmdline",
        lambda pid: (
            f"{sys.executable} -m hermes_cli.main dashboard --port 0"
            if pid == os.getpid()
            else None
        ),
    )

    cleanup = main_mod._write_dashboard_pid_record_for_bound_port(
        _args(port=0), actual_port=52341
    )

    path = tmp_path / "run" / f"dashboard-auto-{os.getpid()}.pid"
    assert path.exists()
    live = main_mod._scan_dashboard_pid_registry(remove_dead=True)
    assert [record["pid"] for record in live] == [os.getpid()]
    assert live[0]["port"] == 52341
    assert live[0]["requested_port"] == 0
    assert "process_start_time" in live[0]
    assert live[0]["venv"] == sys.executable

    assert cleanup is not None
    cleanup()
    assert not path.exists()


def test_registry_scan_deletes_dead_pidfile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    path = run_dir / "dashboard-auto-999999.pid"
    path.write_text(
        '{"pid": 999999, "port": 54321, "requested_port": 0, '
        '"profile": "default", "mode": "dashboard", "argv": [], '
        f'"start_ts": {time.time()}, "venv": "{sys.executable}"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(main_mod, "_dashboard_pid_exists", lambda pid: False)

    assert main_mod._scan_dashboard_pid_registry(remove_dead=True) == []
    assert not path.exists()


@pytest.mark.parametrize(
    ("cmdline", "mode"),
    [
        ("hermes --profile default dashboard --port 9119", "dashboard"),
        ("python -m hermes_cli.main --profile worker serve --port 0", "serve"),
        ("/repo/hermes_cli/main.py --safe-mode dashboard", "dashboard"),
    ],
)
def test_dashboard_cmdline_mode_allows_top_level_flags(cmdline, mode):
    assert main_mod._dashboard_cmdline_mode(cmdline) == mode


def test_registry_scan_rejects_reused_pid_with_different_start_time(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    path = run_dir / "dashboard-auto-1234.pid"
    path.write_text(
        '{"pid": 1234, "port": 54321, "requested_port": 0, '
        '"profile": "default", "mode": "dashboard", "argv": [], '
        f'"start_ts": {time.time()}, "process_start_time": 111, '
        f'"venv": "{sys.executable}"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(main_mod, "_dashboard_pid_exists", lambda pid: True)
    monkeypatch.setattr(
        main_mod,
        "_read_dashboard_process_cmdline",
        lambda pid: "python -m hermes_cli.main dashboard --port 0",
    )
    monkeypatch.setattr(main_mod, "_dashboard_process_start_time", lambda pid: 222)

    assert main_mod._scan_dashboard_pid_registry(remove_dead=True) == []
    assert not path.exists()


def test_dashboard_stop_kills_registered_and_unregistered_pids(monkeypatch):
    calls = []
    registry_scans = iter([[4242], []])
    process_scans = iter([[5252], []])
    monkeypatch.setattr(
        main_mod,
        "_dashboard_live_registry_pids",
        lambda exclude_pids=None: next(registry_scans),
    )
    monkeypatch.setattr(
        main_mod,
        "_find_stale_dashboard_pids",
        lambda *a, **kw: next(process_scans),
    )

    def fake_kill(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(main_mod, "_kill_stale_dashboard_processes", fake_kill)

    with pytest.raises(SystemExit) as exc:
        main_mod.cmd_dashboard(_args(stop=True))

    assert exc.value.code == 0
    assert calls == [
        {"reason": "requested via --stop", "target_pids": [4242, 5252]},
    ]


def test_desktop_child_pid_is_excluded_from_registry_kills(monkeypatch):
    pid = os.getpid()
    monkeypatch.setenv("HERMES_DESKTOP_CHILD_PID", str(pid))
    monkeypatch.setattr(
        main_mod,
        "_scan_dashboard_pid_registry",
        lambda remove_dead=True: [{"pid": pid}],
    )

    assert main_mod._dashboard_live_registry_pids(
        exclude_pids=main_mod._dashboard_excluded_pids_from_env()
    ) == []


def test_replace_kills_same_profile_auto_port_duplicate(monkeypatch):
    killed = []
    monkeypatch.setattr(main_mod, "_dashboard_active_profile_name", lambda: "default")
    monkeypatch.setattr(
        main_mod,
        "_scan_dashboard_pid_registry",
        lambda remove_dead=True: [
            {
                "pid": 7777,
                "port": 53001,
                "requested_port": 0,
                "profile": "default",
                "mode": "serve",
                "start_ts": time.time() - 120,
                "venv": "/old/venv/bin/python",
            }
        ],
    )
    monkeypatch.setattr(
        main_mod,
        "_kill_stale_dashboard_processes",
        lambda **kwargs: killed.append(kwargs),
    )

    main_mod._warn_or_replace_dashboard_duplicates(
        _args(command="serve", port=0, replace=True)
    )

    assert killed == [
        {
            "reason": "requested via --replace for duplicate serve backends",
            "target_pids": [7777],
        }
    ]
