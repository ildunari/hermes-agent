"""Checkpoint 3 — isolated authoritative-activation harness.

This is the acceptance evidence for CP3, and it is **isolated**, not live. It
loads the real core modules and the real plugin package into one process and
drives the full activation sequence against temp-directory state, so every
claim below is a claim about code paths actually executed here — never about
the running gateway, which this suite does not touch.

What the harness holds itself to:

* **Copied / temp state only.** Databases are created under ``tmp_path``.
  Nothing opens a live profile home, and the preflight itself connects
  ``mode=ro`` so it could not write even if pointed somewhere real.
* **Zero outbound transport.** No adapter, client, socket, or send function is
  constructed. A test asserts the send capability is never exercised.
* **No live config.** Configuration is literal dicts built in the test.

Proved here:

1. the plugin becomes the single owner when all four conditions hold;
2. the legacy owner is retained and reachable by a config switch alone;
3. conflicts (ambiguous, split, unowned) fail readiness closed;
4. exactly one watcher / ingress write / texture compile can occur;
5. existing durable data is byte-identical before and after activation;
6. no send occurs anywhere in the sequence;
7. ownership is stable across a simulated restart and reload;
8. rollback restores the legacy owner exactly.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from gateway import conversation_extensions as ce
from gateway import conversation_ownership as co
from gateway.run import GatewayRunner


PLUGIN_ROOT = Path(
    os.environ.get(
        "HERMES_POKE_PLUGIN_ROOT",
        "/Users/Kosta/LocalDev/.studio-only/hermes-kosta-plugin-worktrees/poke-plugin-decarry",
    )
)


def _load_plugin_module(name: str):
    if not PLUGIN_ROOT.is_dir():
        pytest.skip(f"poke plugin worktree not available at {PLUGIN_ROOT}")
    if str(PLUGIN_ROOT) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT))
    try:
        return importlib.import_module(name)
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"cannot import {name} from plugin worktree: {exc}")


@pytest.fixture(scope="module")
def poke_auth():
    return _load_plugin_module("poke.authoritative")


@pytest.fixture(autouse=True)
def _clean():
    ce.conversation_extension_registry.reset_for_tests()
    ce.lifecycle_task_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()
    ce.reset_gateway_host_operations()
    co.conversation_ownership_registry.reset_for_tests()
    yield
    ce.conversation_extension_registry.reset_for_tests()
    ce.lifecycle_task_registry.reset_for_tests()
    ce.reset_request_policy_for_tests()
    ce.reset_gateway_host_operations()
    co.conversation_ownership_registry.reset_for_tests()


EXTENSION_ID = "poke"


# ---------------------------------------------------------------------------
# isolated fixture state
# ---------------------------------------------------------------------------


def _seed_profile(home: Path, *, claims: int = 0, pending: int = 0) -> Path:
    """Create an isolated profile home with a realistic durable shape."""
    home.mkdir(parents=True, exist_ok=True)
    state_db = home / "state.db"
    con = sqlite3.connect(state_db)
    con.execute(
        "CREATE TABLE proactive_slot_claims (id INTEGER PRIMARY KEY, released_at REAL)"
    )
    con.execute("CREATE TABLE proactive_sends (id INTEGER PRIMARY KEY, status TEXT)")
    con.execute("INSERT INTO proactive_sends (status) VALUES ('sent')")
    con.execute("INSERT INTO proactive_sends (status) VALUES ('definitive_failure')")
    for _ in range(claims):
        con.execute("INSERT INTO proactive_slot_claims (released_at) VALUES (NULL)")
    for _ in range(pending):
        con.execute("INSERT INTO proactive_sends (status) VALUES ('in_flight')")
    con.commit()
    con.close()

    cm_root = home / "contact-memory"
    cm_root.mkdir(exist_ok=True)
    cm_db = cm_root / "contact-a.db"
    con = sqlite3.connect(cm_db)
    con.execute("CREATE TABLE contacts (id INTEGER PRIMARY KEY, handle TEXT)")
    con.execute("CREATE TABLE communication_events (id INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE interests (id INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO contacts (handle) VALUES ('existing-contact')")
    con.execute("INSERT INTO communication_events DEFAULT VALUES")
    con.commit()
    con.close()
    return state_db


def _digest(home: Path) -> dict[str, str]:
    """Content hash of every durable file under *home*."""
    return {
        str(path.relative_to(home)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(home.rglob("*"))
        if path.is_file()
    }


def _activation_config(*, authoritative: bool = True, ownership: str = "extension"):
    return {
        "gateway": {
            "required_conversation_extensions": [
                {"id": EXTENSION_ID, "api_version": 1}
            ],
            "conversation_ownership": {"default": ownership},
        },
        "plugins": {
            "entries": {
                "poke": {
                    "settings": {"proactive": {"authoritative": authoritative}}
                }
            }
        },
    }


def _register(poke_auth, home: Path, config: dict, scope: str):
    bundle, decision = poke_auth.build_extension(
        profile_home=home, config_raw=config
    )
    ce.conversation_extension_registry.register(bundle, scope=scope)
    return bundle, decision


def _scope(home: Path) -> str:
    from hermes_constants import hermes_home_key

    return hermes_home_key(home)


# ---------------------------------------------------------------------------
# 1. the plugin becomes the single owner
# ---------------------------------------------------------------------------


def test_activated_plugin_is_the_single_owner_of_every_domain(tmp_path, poke_auth):
    home = tmp_path / "poke"
    _seed_profile(home)
    scope = _scope(home)
    config = _activation_config()

    bundle, decision = _register(poke_auth, home, config, scope)
    assert decision.authoritative, decision.describe()

    plan, conflicts = co.activate_plan(scope=scope, config_raw=config)
    assert conflicts == (), conflicts
    for domain in co.OwnershipDomain:
        selection = plan[domain]
        assert selection.is_extension, domain
        assert selection.extension_id == EXTENSION_ID


def test_activated_profile_passes_core_required_extension_readiness(tmp_path, poke_auth):
    from gateway import conversation_extension_runtime as ce_runtime

    home = tmp_path / "poke"
    _seed_profile(home)
    scope = _scope(home)
    config = _activation_config()
    _register(poke_auth, home, config, scope)

    ok, reason = ce_runtime.profile_requirements_satisfied(
        scope=scope, config_raw=config
    )
    assert ok, reason


def test_activated_owner_actually_enforces_tool_policy(tmp_path, poke_auth):
    """Dark mode always allowed. The activated owner must be able to deny."""
    home = tmp_path / "poke"
    _seed_profile(home)
    scope = _scope(home)
    _register(poke_auth, home, _activation_config(), scope)

    policy = ce.issue_request_policy(
        extension_id=EXTENSION_ID, profile_home=scope, route_id="route-1"
    )
    with ce.request_policy_scope(policy):
        denied = ce.authorize_tool_dispatch("video_generate", {})
        allowed = ce.authorize_tool_dispatch("read_file", {"path": "/tmp/x"})
    assert denied is not None, "the owner must be able to block a tool"
    assert allowed is None


# ---------------------------------------------------------------------------
# 2. legacy retained and selectable
# ---------------------------------------------------------------------------


def test_legacy_owner_is_still_present_and_selectable(tmp_path, poke_auth):
    """Rollback must not require reverting code at this checkpoint."""
    import gateway.guest_access as legacy_guest
    import gateway.proactive_scheduler as legacy_scheduler
    import gateway.proactive_transport as legacy_transport

    assert callable(legacy_guest.enforce_guest_tool_call)
    assert hasattr(legacy_scheduler, "ProactiveScheduler")
    assert hasattr(legacy_transport, "deliver_prepared_exactly_once")

    home = tmp_path / "poke"
    _seed_profile(home)
    scope = _scope(home)
    _register(poke_auth, home, _activation_config(), scope)

    legacy_config = _activation_config(authoritative=False, ownership="legacy")
    plan, conflicts = co.activate_plan(scope=scope, config_raw=legacy_config)
    assert conflicts == ()
    for domain in co.OwnershipDomain:
        assert plan[domain].is_legacy


def test_default_profile_is_untouched_by_the_activation(tmp_path, poke_auth):
    """An ordinary profile keeps the legacy owner with no configuration."""
    home = tmp_path / "poke"
    other = tmp_path / "ordinary"
    other.mkdir()
    _seed_profile(home)
    _register(poke_auth, home, _activation_config(), _scope(home))
    co.activate_plan(scope=_scope(home), config_raw=_activation_config())

    plan, conflicts = co.activate_plan(scope=_scope(other), config_raw={})
    assert conflicts == ()
    assert all(selection.is_legacy for selection in plan.values())


# ---------------------------------------------------------------------------
# 3. conflicts fail closed
# ---------------------------------------------------------------------------


def test_two_claimants_fail_activation_closed(tmp_path, poke_auth):
    home = tmp_path / "poke"
    _seed_profile(home)
    scope = _scope(home)
    config = _activation_config()
    bundle, _ = _register(poke_auth, home, config, scope)

    rival = ce.GatewayConversationExtension(
        extension_id="rival",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"admission_policy", "health"}),
        authorize_route=lambda ctx: ce.GatewayRouteDirective(admit=True),
        health=lambda: ce.GatewayExtensionHealth(True),
    )
    ce.conversation_extension_registry.register(rival, scope=scope)

    plan, conflicts = co.activate_plan(scope=scope, config_raw=config)
    assert conflicts
    assert scope not in co.conversation_ownership_registry.scopes()


def test_settings_conflict_keeps_the_plugin_dark(tmp_path, poke_auth):
    home = tmp_path / "poke"
    _seed_profile(home)
    config = _activation_config()
    config["agent"] = {"proactive": {"authoritative": False}}
    bundle, decision = poke_auth.build_extension(profile_home=home, config_raw=config)
    assert not decision.authoritative
    assert decision.reason == "settings_conflict"
    assert "admission_policy" not in bundle.capabilities


def test_in_flight_claim_blocks_activation(tmp_path, poke_auth):
    home = tmp_path / "poke"
    _seed_profile(home, claims=1)
    bundle, decision = poke_auth.build_extension(
        profile_home=home, config_raw=_activation_config()
    )
    assert not decision.authoritative
    assert decision.reason == "preflight_failed"


def test_split_owner_plan_is_refused(tmp_path, poke_auth):
    home = tmp_path / "poke"
    _seed_profile(home)
    scope = _scope(home)
    config = _activation_config()
    _register(poke_auth, home, config, scope)

    split = _activation_config()
    split["gateway"]["conversation_ownership"] = {
        "default": "legacy",
        "ingress": "extension",
    }
    plan, conflicts = co.activate_plan(scope=scope, config_raw=split)
    assert any("split_owner" in conflict for conflict in conflicts)
    assert scope not in co.conversation_ownership_registry.scopes()


def test_unhealthy_owner_fails_the_plan_closed(tmp_path, poke_auth):
    home = tmp_path / "poke"
    _seed_profile(home)
    scope = _scope(home)
    config = _activation_config()

    sick = ce.GatewayConversationExtension(
        extension_id=EXTENSION_ID,
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset(
            set(co.DOMAIN_REQUIRED_CAPABILITY.values()) | {"health"}
        ),
        authorize_route=lambda ctx: ce.GatewayRouteDirective(admit=True),
        observe_ingress=lambda ctx: None,
        observe_turn_result=lambda result: None,
        on_start=lambda facade: None,
        health=lambda: ce.GatewayExtensionHealth(False, "preflight_failed"),
    )
    ce.conversation_extension_registry.register(sick, scope=scope)
    plan, conflicts = co.activate_plan(scope=scope, config_raw=config)
    assert conflicts
    assert any("unhealthy_owner" in conflict for conflict in conflicts)


# ---------------------------------------------------------------------------
# 4. exactly one watcher / ingress write / texture compile
# ---------------------------------------------------------------------------


def test_activated_plugin_starts_no_second_watcher(tmp_path, poke_auth):
    """The plugin declares lifecycle but must not spawn work in this checkpoint."""
    home = tmp_path / "poke"
    _seed_profile(home)
    scope = _scope(home)
    _register(poke_auth, home, _activation_config(), scope)

    spawned: list[str] = []
    ce.install_gateway_host_operations(
        ce.GatewayHostOperations(
            spawn_task=lambda task: spawned.append(task.task_key),
        )
    )
    from gateway import conversation_extension_runtime as ce_runtime

    started = ce_runtime.fire_gateway_start(scope=scope, profile_name="poke")
    assert EXTENSION_ID in started
    assert spawned == [], "the activated plugin must not start a second watcher"


def test_legacy_watcher_stands_down_when_the_plugin_owns_proactive(tmp_path, poke_auth):
    home = tmp_path / "poke"
    _seed_profile(home)
    scope = _scope(home)
    config = _activation_config()
    _register(poke_auth, home, config, scope)
    co.activate_plan(scope=scope, config_raw=config)

    runner = object.__new__(GatewayRunner)
    assert runner._legacy_proactive_permitted(scope) is False


def test_exactly_one_ingress_writer(tmp_path, poke_auth):
    """Core's legacy writer stands down exactly when the plugin owns ingress."""
    home = tmp_path / "poke"
    _seed_profile(home)
    scope = _scope(home)
    config = _activation_config()
    _register(poke_auth, home, config, scope)
    co.activate_plan(scope=scope, config_raw=config)

    runner = object.__new__(GatewayRunner)
    assert runner._legacy_owns(scope, co.OwnershipDomain.INGRESS) is False

    # And after rollback exactly one writer is live again — the legacy one.
    co.activate_plan(
        scope=scope,
        config_raw=_activation_config(authoritative=False, ownership="legacy"),
    )
    assert runner._legacy_owns(scope, co.OwnershipDomain.INGRESS) is True


def test_exactly_one_texture_owner(tmp_path, poke_auth):
    """Texture compilation must have one owner, never two.

    The standalone conversation-texture plugin owns the per-turn hook. The
    activated Poke extension must not also declare turn augmentation, or a
    profile running both would compile texture twice.
    """
    home = tmp_path / "poke"
    _seed_profile(home)
    bundle, decision = poke_auth.build_extension(
        profile_home=home, config_raw=_activation_config()
    )
    assert decision.authoritative
    assert "turn_policy" not in bundle.capabilities
    assert bundle.augment_turn is None


# ---------------------------------------------------------------------------
# 5. existing data continuity
# ---------------------------------------------------------------------------


def test_activation_leaves_every_durable_file_byte_identical(tmp_path, poke_auth):
    home = tmp_path / "poke"
    _seed_profile(home)
    before = _digest(home)

    scope = _scope(home)
    config = _activation_config()
    bundle, decision = _register(poke_auth, home, config, scope)
    assert decision.authoritative
    co.activate_plan(scope=scope, config_raw=config)
    from gateway import conversation_extension_runtime as ce_runtime

    ce_runtime.fire_gateway_start(scope=scope, profile_name="poke")

    assert _digest(home) == before, "activation must not touch durable state"


def test_preflight_records_existing_row_counts_as_continuity_evidence(
    tmp_path, poke_auth
):
    home = tmp_path / "poke"
    _seed_profile(home)
    decision = poke_auth.evaluate_activation(
        profile_home=home, config_raw=_activation_config()
    )
    counts = {
        check.name: check.data
        for check in decision.preflight.checks
        if check.name.startswith("row_counts")
    }
    assert counts
    assert any(data.get("contacts") == 1 for data in counts.values())


def test_no_database_is_created_or_migrated(tmp_path, poke_auth):
    home = tmp_path / "poke"
    _seed_profile(home)
    files_before = {str(p) for p in home.rglob("*") if p.is_file()}
    poke_auth.evaluate_activation(
        profile_home=home, config_raw=_activation_config()
    )
    files_after = {str(p) for p in home.rglob("*") if p.is_file()}
    assert files_before == files_after


# ---------------------------------------------------------------------------
# 6. zero outbound sends
# ---------------------------------------------------------------------------


def test_activation_performs_no_outbound_send(tmp_path, poke_auth):
    """The send capability is declared but must never be exercised here."""
    home = tmp_path / "poke"
    _seed_profile(home)
    scope = _scope(home)
    config = _activation_config()
    _register(poke_auth, home, config, scope)

    sends: list[object] = []
    ce.install_gateway_host_operations(
        ce.GatewayHostOperations(
            send_authenticated_existing_dm=lambda request: sends.append(request),
            create_initiated_child=lambda request: sends.append(request) or {},
            inject_turn=lambda key, text: sends.append((key, text)) or True,
        )
    )
    co.activate_plan(scope=scope, config_raw=config)
    from gateway import conversation_extension_runtime as ce_runtime

    ce_runtime.fire_gateway_start(scope=scope, profile_name="poke")
    ce_runtime.observe_authenticated_ingress(
        ce.GatewayRouteContext(
            platform="bluebubbles",
            adapter_identity="bluebubbles",
            transport_profile="poke",
            transport_home=scope,
            sender_identity="s",
            chat_id="c",
            chat_type="dm",
        ),
        scope=scope,
    )
    ce_runtime.observe_turn_completion(
        scope=scope,
        session_key="sess",
        runtime_profile="poke",
        platform="bluebubbles",
        sender_identity="s",
        user_text="hi",
        assistant_text="hello",
        delivered=True,
    )
    ce_runtime.fire_gateway_stop(scope=scope, profile_name="poke")

    assert sends == [], "no send, child, or injection may occur during activation"


def test_send_capability_denies_without_a_host_implementation(tmp_path, poke_auth):
    """Even when declared, a send fails definitively with no host wiring."""
    facade = ce.GatewayRuntimeFacade(
        extension_id=EXTENSION_ID,
        profile_name="poke",
        profile_home=str(tmp_path),
        generation=1,
        capabilities=frozenset({"authenticated_dm"}),
        host=ce.GatewayHostOperations(),
    )
    result = facade.send_authenticated_existing_dm(
        ce.AuthenticatedDmRequest(
            platform="bluebubbles", chat_id="c", text="t", reservation_key="k"
        )
    )
    assert result.outcome is ce.DmSendOutcome.DEFINITIVE_FAILURE


# ---------------------------------------------------------------------------
# 7. restart / reload ownership stability
# ---------------------------------------------------------------------------


def test_ownership_is_identical_across_a_simulated_restart(tmp_path, poke_auth):
    home = tmp_path / "poke"
    _seed_profile(home)
    scope = _scope(home)
    config = _activation_config()

    _register(poke_auth, home, config, scope)
    first, _ = co.activate_plan(scope=scope, config_raw=config)

    # Simulated restart: drop every process registry and rebuild from the same
    # on-disk state and the same config.
    ce.conversation_extension_registry.reset_for_tests()
    co.conversation_ownership_registry.reset_for_tests()

    _register(poke_auth, home, config, scope)
    second, _ = co.activate_plan(scope=scope, config_raw=config)

    # The *ownership assignment* must be identical. The generation counter is
    # deliberately process-monotonic — a fresh registration is a new
    # generation by design, which is what makes stale-generation teardown
    # safe — so it is excluded from the comparison rather than pinned.
    def _assignment(plan):
        return {
            domain.value: (selection.kind.value, selection.extension_id)
            for domain, selection in plan.items()
        }

    assert _assignment(first) == _assignment(second)
    assert all(selection.is_extension for selection in second.values())


def test_reload_replaces_the_generation_without_splitting_ownership(
    tmp_path, poke_auth
):
    home = tmp_path / "poke"
    _seed_profile(home)
    scope = _scope(home)
    config = _activation_config()

    bundle, _ = _register(poke_auth, home, config, scope)
    co.activate_plan(scope=scope, config_raw=config)
    first_generation = ce.conversation_extension_registry.active_generation(
        EXTENSION_ID, scope=scope
    )

    # Plugin reload: a fresh bundle replaces the old generation atomically.
    bundle2, _ = _register(poke_auth, home, config, scope)
    second_generation = ce.conversation_extension_registry.active_generation(
        EXTENSION_ID, scope=scope
    )
    assert second_generation > first_generation
    assert ce.conversation_extension_registry.active_ids(scope=scope) == (EXTENSION_ID,)

    plan, conflicts = co.activate_plan(scope=scope, config_raw=config)
    assert conflicts == ()
    assert all(selection.is_extension for selection in plan.values())


def test_ownership_survives_the_extension_vanishing_mid_run(tmp_path, poke_auth):
    """A mid-run unload must not hand the domain back to the legacy owner."""
    home = tmp_path / "poke"
    _seed_profile(home)
    scope = _scope(home)
    config = _activation_config()
    _register(poke_auth, home, config, scope)
    co.activate_plan(scope=scope, config_raw=config)

    generation = ce.conversation_extension_registry.active_generation(
        EXTENSION_ID, scope=scope
    )
    ce.conversation_extension_registry.unregister(
        EXTENSION_ID, generation=generation, scope=scope
    )

    runner = object.__new__(GatewayRunner)
    assert runner._legacy_owns(scope, co.OwnershipDomain.INGRESS) is False


# ---------------------------------------------------------------------------
# 8. rollback
# ---------------------------------------------------------------------------


def test_rollback_is_config_plus_restart_with_no_data_effect(tmp_path, poke_auth):
    home = tmp_path / "poke"
    _seed_profile(home)
    scope = _scope(home)
    before = _digest(home)

    _register(poke_auth, home, _activation_config(), scope)
    co.activate_plan(scope=scope, config_raw=_activation_config())
    assert co.extension_owns(scope, co.OwnershipDomain.DELIVERY)

    # Rollback = flip config, restart. No revert, no data move.
    ce.conversation_extension_registry.reset_for_tests()
    co.conversation_ownership_registry.reset_for_tests()
    rolled_back = _activation_config(authoritative=False, ownership="legacy")
    bundle, decision = _register(poke_auth, home, rolled_back, scope)
    assert not decision.authoritative
    plan, conflicts = co.activate_plan(scope=scope, config_raw=rolled_back)
    assert conflicts == ()
    assert all(selection.is_legacy for selection in plan.values())

    runner = object.__new__(GatewayRunner)
    for domain in co.OwnershipDomain:
        assert runner._legacy_owns(scope, domain) is True

    assert _digest(home) == before, "rollback must not touch durable state"


def test_rolled_back_profile_still_satisfies_core_readiness(tmp_path, poke_auth):
    """Rollback keeps the required-extension declaration satisfiable.

    The rolled-back plugin still registers (dark), so a profile that declares
    the requirement stays ready. Removing the plugin entirely is a different,
    louder operation and correctly makes the profile unready.
    """
    from gateway import conversation_extension_runtime as ce_runtime

    home = tmp_path / "poke"
    _seed_profile(home)
    scope = _scope(home)
    rolled_back = _activation_config(authoritative=False, ownership="legacy")
    _register(poke_auth, home, rolled_back, scope)

    ok, reason = ce_runtime.profile_requirements_satisfied(
        scope=scope, config_raw=rolled_back
    )
    assert ok, reason


def test_removing_the_plugin_entirely_fails_readiness_closed(tmp_path):
    from gateway import conversation_extension_runtime as ce_runtime

    home = tmp_path / "poke"
    _seed_profile(home)
    scope = _scope(home)
    ok, reason = ce_runtime.profile_requirements_satisfied(
        scope=scope, config_raw=_activation_config()
    )
    assert not ok
    assert "missing" in reason
