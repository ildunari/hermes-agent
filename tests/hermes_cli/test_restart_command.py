import argparse
import json

import pytest

from hermes_cli.restart_surfaces import targets_for_scope
from hermes_cli.subcommands.restart import (
    _wait_for_completion,
    build_restart_parser,
    cmd_restart,
)


def _args(**overrides):
    values = {
        "dry_run": False,
        "wait": False,
        "detach": False,
        "delay": 1.0,
        "safe_wait_timeout": None,
        "wait_timeout": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_restart_parser_defaults_to_drain_aware_surface_scope():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_restart_parser(subparsers)

    args = parser.parse_args(["restart"])

    assert args.func is cmd_restart
    assert args.dry_run is False
    assert args.detach is False


@pytest.mark.parametrize(
    "argv",
    [
        ["restart", "--delay", "nan"],
        ["restart", "--safe-wait-timeout", "inf"],
        ["restart", "--wait-timeout", "-1"],
    ],
)
def test_restart_parser_rejects_nonfinite_and_negative_timeouts(argv):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_restart_parser(subparsers)

    with pytest.raises(SystemExit):
        parser.parse_args(argv)


def test_restart_dry_run_prints_plan_without_marker(monkeypatch, capsys):
    calls = []

    def fake_enqueue(scope, **kwargs):
        calls.append((scope, kwargs))
        return "safe plan"

    monkeypatch.setattr("hermes_cli.restart_surfaces.enqueue_detached_restart", fake_enqueue)
    monkeypatch.setattr(
        "hermes_cli.subcommands.restart._completion_marker",
        lambda: (_ for _ in ()).throw(AssertionError("dry-run must not create a marker")),
    )

    cmd_restart(_args(dry_run=True))

    assert capsys.readouterr().out.strip() == "safe plan"
    assert calls == [("gateways", {"dry_run": True})]


def test_restart_queues_detached_safe_scope_with_status_marker(monkeypatch, tmp_path, capsys):
    calls = []
    marker = tmp_path / "restart.json"

    def fake_enqueue(scope, **kwargs):
        calls.append((scope, kwargs))
        return "queued"

    monkeypatch.setattr("hermes_cli.restart_surfaces.enqueue_detached_restart", fake_enqueue)
    monkeypatch.setattr("hermes_cli.subcommands.restart._completion_marker", lambda: marker)
    monkeypatch.setattr("hermes_cli.subcommands.restart._notification_tty", lambda: None)

    cmd_restart(_args(delay=2.5, safe_wait_timeout=90.0, detach=True))

    output = capsys.readouterr().out
    assert "queued" in output
    assert str(marker) in output
    assert calls == [(
        "gateways",
        {
            "delay": 2.5,
            "completion_marker": str(marker),
            "notify_tty": None,
            "safe_wait_timeout": 90.0,
        },
    )]


def test_restart_requests_terminal_completion_notification(monkeypatch, tmp_path, capsys):
    calls = []
    marker = tmp_path / "restart.json"

    def fake_enqueue(scope, **kwargs):
        calls.append((scope, kwargs))
        return "queued"

    monkeypatch.setattr("hermes_cli.restart_surfaces.enqueue_detached_restart", fake_enqueue)
    monkeypatch.setattr("hermes_cli.subcommands.restart._completion_marker", lambda: marker)
    monkeypatch.setattr(
        "hermes_cli.subcommands.restart._notification_tty",
        lambda: "/dev/ttys001",
    )

    cmd_restart(_args(detach=True))

    assert capsys.readouterr().out.startswith("queued\n")
    assert calls[0][1]["notify_tty"] == "/dev/ttys001"


def test_restart_follow_does_not_duplicate_terminal_notification(monkeypatch, tmp_path):
    calls = []
    marker = tmp_path / "restart.json"
    marker.write_text(json.dumps({
        "status": "complete",
        "scope": "gateways",
        "exit_code": 0,
        "message": "restart finished",
    }))

    def fake_enqueue(scope, **kwargs):
        calls.append((scope, kwargs))
        return "queued"

    monkeypatch.setattr("hermes_cli.restart_surfaces.enqueue_detached_restart", fake_enqueue)
    monkeypatch.setattr("hermes_cli.subcommands.restart._completion_marker", lambda: marker)
    monkeypatch.setattr(
        "hermes_cli.subcommands.restart._notification_tty",
        lambda: "/dev/ttys001",
    )

    cmd_restart(_args())

    assert calls[0][1]["notify_tty"] is None


def test_restart_wait_propagates_worker_failure(monkeypatch, tmp_path):
    marker = tmp_path / "restart.json"
    marker.write_text(json.dumps({
        "status": "complete",
        "scope": "gateways",
        "exit_code": 1,
        "message": "restart failed",
    }))
    monkeypatch.setattr(
        "hermes_cli.restart_surfaces.enqueue_detached_restart",
        lambda scope, **kwargs: "queued",
    )
    monkeypatch.setattr("hermes_cli.subcommands.restart._completion_marker", lambda: marker)

    try:
        cmd_restart(_args())
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("worker failure must produce a nonzero CLI exit")


def test_restart_wait_is_bounded_when_worker_never_writes_marker(tmp_path):
    marker = tmp_path / "missing.json"

    assert _wait_for_completion(marker, scope="gateways", timeout=0) == 124


def test_restart_follow_streams_only_new_worker_log_lines(tmp_path, capsys):
    marker = tmp_path / "restart.json"
    log = tmp_path / "restart.log"
    log.write_text("old restart\n[time] gateway restarting\n", encoding="utf-8")
    marker.write_text(json.dumps({
        "status": "complete",
        "scope": "gateways",
        "exit_code": 0,
        "message": "restart finished",
    }))

    exit_code = _wait_for_completion(
        marker,
        scope="gateways",
        timeout=1,
        log_path=log,
        log_offset=len("old restart\n"),
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "old restart" not in output
    assert "  [time] gateway restarting" in output
    assert output.rstrip().endswith("restart finished")


def test_restart_rejects_non_macos_before_enqueue(monkeypatch):
    monkeypatch.setattr("hermes_cli.subcommands.restart.sys.platform", "linux")
    monkeypatch.setattr(
        "hermes_cli.restart_surfaces.enqueue_detached_restart",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not enqueue")),
    )

    try:
        cmd_restart(_args())
    except SystemExit as exc:
        assert "macOS" in str(exc)
    else:
        raise AssertionError("non-macOS restart must fail closed")


def test_safe_scope_excludes_task_bearing_sidecars_without_drain_protocols():
    labels = {target.label for target in targets_for_scope("gateways")}

    assert labels.isdisjoint({
        "ai.hermes.aside-mcp-proxy",
        "ai.hermes.cron-guest",
        "ai.hermes.poke-guest-reflection",
        "ai.hermes.vibeproxy-gpt-image-2-mcp",
        "ai.hermes.codex-subtask-supervisor",
    })
