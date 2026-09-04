"""Gateway-independent draining of restart-safe cron deliveries."""

from contextlib import contextmanager
import json
from types import SimpleNamespace

import cron.scheduler as scheduler
import gateway.run as gateway_run


class _OneTickStopEvent:
    def __init__(self):
        self.waited = False

    def is_set(self):
        return self.waited

    def wait(self, timeout=None):
        self.waited = True
        return True


def _write_external_owner(home, *, updated_at, stale_after_seconds=960):
    cron_dir = home / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "ticker_external.json").write_text(json.dumps({
        "kind": "profile-launchd",
        "updated_at": updated_at,
        "stale_after_seconds": stale_after_seconds,
    }))


def test_external_and_gateway_tickers_have_exactly_one_freshness_owner(
    tmp_path, monkeypatch
):
    fresh = tmp_path / "fresh"
    stale = tmp_path / "stale"
    absent = tmp_path / "absent"
    _write_external_owner(fresh, updated_at=995.0)
    _write_external_owner(stale, updated_at=1.0, stale_after_seconds=10)

    for home, external_owns in ((fresh, True), (stale, False), (absent, False)):
        external = gateway_run._external_cron_ticker_owns_profile(home, now=1000.0)
        gateway = gateway_run._in_process_cron_ticker_owns_profile(home, now=1000.0)
        assert external is external_owns
        assert int(external) + int(gateway) == 1

    monkeypatch.setattr(gateway_run.time, "time", lambda: 1000.0)
    assert gateway_run._gateway_cron_profile_gate("fresh", fresh) is False
    assert gateway_run._gateway_cron_profile_gate("stale", stale) is True
    assert gateway_run._gateway_cron_profile_gate("absent", absent) is True


def test_external_ticker_contract_fails_open_to_gateway_on_invalid_data(tmp_path):
    home = tmp_path / "invalid"
    _write_external_owner(home, updated_at="not-a-number")

    assert gateway_run._external_cron_ticker_owns_profile(home, now=1000.0) is False
    assert gateway_run._in_process_cron_ticker_owns_profile(home, now=1000.0) is True

    _write_external_owner(home, updated_at=1061.0)
    assert gateway_run._external_cron_ticker_owns_profile(home, now=1000.0) is False


def test_single_profile_gateway_dispatch_falls_back_after_external_stale(
    tmp_path, monkeypatch
):
    home = tmp_path / "profile"
    runner = SimpleNamespace(_draining=False, _external_drain_active=False)
    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: home)
    _write_external_owner(home, updated_at=100.0, stale_after_seconds=10)

    monkeypatch.setattr(gateway_run.time, "time", lambda: 105.0)
    assert gateway_run._gateway_cron_can_dispatch(runner, multiplex=False) is False

    monkeypatch.setattr(gateway_run.time, "time", lambda: 111.0)
    assert gateway_run._gateway_cron_can_dispatch(runner, multiplex=False) is True
    runner._draining = True
    assert gateway_run._gateway_cron_can_dispatch(runner, multiplex=False) is False


def test_gateway_housekeeping_drains_cron_delivery_with_live_adapters(monkeypatch):
    adapters = {"discord": object()}
    loop = object()
    calls = []
    monkeypatch.setattr(
        scheduler,
        "drain_delivery_queue",
        lambda live_adapters, live_loop: calls.append((live_adapters, live_loop)),
        raising=False,
    )

    gateway_run._start_gateway_housekeeping(
        _OneTickStopEvent(), adapters=adapters, loop=loop, interval=0
    )

    assert calls == [(adapters, loop)]


def test_gateway_housekeeping_drains_cron_delivery_without_connected_adapters(monkeypatch):
    adapters = {}
    loop = object()
    calls = []
    monkeypatch.setattr(
        scheduler,
        "drain_delivery_queue",
        lambda live_adapters, live_loop: calls.append((live_adapters, live_loop)),
        raising=False,
    )

    gateway_run._start_gateway_housekeeping(
        _OneTickStopEvent(), adapters=adapters, loop=loop, interval=0
    )

    assert calls == [(adapters, loop)]


def test_multiplex_housekeeping_scopes_primary_and_drains_each_profile(
    tmp_path, monkeypatch
):
    root_adapters = {}
    secondary_adapters = {}
    runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True),
        adapters=root_adapters,
        _profile_adapters={"secondary": secondary_adapters},
    )
    root_home = tmp_path / "root"
    secondary_home = tmp_path / "secondary"
    calls = []

    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: root_home)

    monkeypatch.setattr(
        gateway_run,
        "_handoff_watch_scopes",
        lambda _runner: [(None, None), ("secondary", secondary_home)],
    )

    @contextmanager
    def fake_scope(home, *, hydrate_secrets=True):
        calls.append(("scope", home, hydrate_secrets))
        yield

    monkeypatch.setattr(gateway_run, "_profile_runtime_scope", fake_scope)
    monkeypatch.setattr(
        scheduler,
        "drain_delivery_queue",
        lambda adapters, loop: calls.append(("drain", adapters)),
    )

    gateway_run._start_gateway_housekeeping(
        _OneTickStopEvent(),
        adapters=root_adapters,
        loop=object(),
        interval=0,
        runner=runner,
    )

    assert calls == [
        ("scope", root_home, False),
        ("drain", root_adapters),
        ("scope", secondary_home, False),
        ("drain", secondary_adapters),
    ]


def test_multiplex_housekeeping_uses_primary_routes_for_credentialless_satellite(
    tmp_path, monkeypatch
):
    root_adapters = {"slack": object()}
    secondary_home = tmp_path / "secondary"
    runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True),
        adapters=root_adapters,
        _profile_adapters={"secondary": {}},
    )
    calls = []
    routed = object()

    monkeypatch.setattr(
        gateway_run,
        "_handoff_watch_scopes",
        lambda _runner: [(None, None), ("secondary", secondary_home)],
    )

    @contextmanager
    def fake_scope(_home, *, hydrate_secrets=True):
        assert hydrate_secrets is False
        yield

    class FakeSharedRouteAdapters:
        def __new__(cls, adapters, routes):
            calls.append(("routed", adapters, routes))
            return routed

    monkeypatch.setattr(gateway_run, "_profile_runtime_scope", fake_scope)
    monkeypatch.setattr(scheduler, "SharedRouteAdapters", FakeSharedRouteAdapters)
    monkeypatch.setattr(
        scheduler,
        "_primary_profile_routes_for_current_home",
        lambda: ["route-to-secondary"],
    )
    monkeypatch.setattr(
        scheduler,
        "drain_delivery_queue",
        lambda adapters, _loop: calls.append(("drain", adapters)),
    )

    gateway_run._drain_restart_safe_cron_deliveries(
        root_adapters, object(), runner
    )

    assert calls == [
        ("drain", root_adapters),
        ("routed", root_adapters, ["route-to-secondary"]),
        ("drain", routed),
    ]
