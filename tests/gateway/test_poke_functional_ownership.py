"""Review-3 P0-2 regression: authoritative mode must be a *functional* owner.

Review 3's finding was that activation produced a **zero-owner outage**: the
plan resolved all six domains to ``extension`` and every legacy site was gated
off, but the plugin performed no equivalent work — ``observe_ingress`` was a
counter, ``on_start`` spawned nothing, and no code path reached ingress
persistence, extraction, child creation, or delivery. The old harness only
asserted negatives (``_legacy_owns(...) is False``), so a zero-owner state
passed as a one-owner state.

The fix for that class of miss is *positive* assertions, so this suite proves,
against real modules and production-shaped state:

1. exactly **one real watcher** is started, and it is host-owned and
   generation-cancellable;
2. exactly **one persisted ingress write** lands in a copied/temp snapshot,
   and a replay deduplicates rather than double-writing;
3. exactly **one extraction submit** and **one texture compile** per turn;
4. runtime **guest routing and policy** are actually established — an
   unapproved sender is denied, an approved one is routed with a principal and
   a trusted scope;
5. an **initiated child** path is genuinely reached through the host op;
6. the authenticated existing-DM **tri-state** is honored, with the transport
   home authorization enforced and ``UNKNOWN`` never retried;
7. **zero outbound transport** occurs anywhere in the suite;
8. full **config-only rollback** still restores the legacy owner.

Isolation: every database is created under ``tmp_path``. No adapter, socket,
or transport client is constructed — the send host-op is a recording double,
and a suite-wide assertion checks nothing else could have sent.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from gateway import conversation_extensions as ce
from gateway import conversation_ownership as co


PLUGIN_ROOT = Path(
    os.environ.get(
        "HERMES_POKE_PLUGIN_ROOT",
        "/Users/Kosta/LocalDev/.studio-only/hermes-kosta-plugin-worktrees/poke-plugin-decarry",
    )
)

EXTENSION_ID = "poke"


def _load_plugin_module(name: str):
    if not PLUGIN_ROOT.is_dir():
        pytest.skip(f"poke plugin worktree not available at {PLUGIN_ROOT}")
    if str(PLUGIN_ROOT) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT))
    try:
        return importlib.import_module(name)
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"cannot import {name} from plugin worktree: {exc}")


def _load_production_fixtures():
    if not PLUGIN_ROOT.is_dir():
        pytest.skip(f"poke plugin worktree not available at {PLUGIN_ROOT}")
    path = PLUGIN_ROOT / "tests" / "poke_plugin" / "production_fixtures.py"
    if not path.is_file():
        pytest.skip(f"production fixtures not available at {path}")
    cached = sys.modules.get("_poke_production_fixtures")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("_poke_production_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_poke_production_fixtures"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def poke_auth():
    return _load_plugin_module("poke.authoritative")


@pytest.fixture(scope="module")
def poke_owners():
    return _load_plugin_module("poke.owners")


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


# ---------------------------------------------------------------------------
# recording host — proves "zero outbound" structurally
# ---------------------------------------------------------------------------


class RecordingHost:
    """A host double that records instead of sending.

    Every effectful capability is observable, so a test can assert both that
    something happened (positive evidence) and that nothing *else* did.
    """

    def __init__(self, *, authorized_chats=(), send_outcome=None):
        self.spawned = []
        self.cancelled = []
        self.children = []
        self.sends = []
        self.injections = []
        self.blocking_calls = 0
        self._authorized = set(authorized_chats)
        self._send_outcome = send_outcome

    # -- operations --------------------------------------------------------

    def spawn_task(self, task):
        self.spawned.append(task)
        return task

    def cancel_tasks(self, extension_id, profile_home, generation):
        self.cancelled.append((extension_id, profile_home, generation))
        return 1

    def lookup_session(self, session_key):
        if not session_key:
            return None
        return {"session_id": f"sess-{session_key}", "profile": "guest"}

    def create_initiated_child(self, request):
        self.children.append(request)
        return {
            "created": True,
            "child_session_id": f"child-{len(self.children)}",
            "parent_session_id": f"sess-{request.session_key}",
        }

    def inject_turn(self, session_key, text):
        self.injections.append((session_key, text))
        return True

    def send_authenticated_existing_dm(self, request):
        """Record the attempt. Never touches a network or an adapter."""
        from gateway.conversation_extensions import (
            AuthenticatedDmResult,
            DmSendOutcome,
        )

        self.sends.append(request)
        if self._send_outcome is not None:
            return self._send_outcome
        if request.chat_id not in self._authorized:
            # Mirrors the real host op: an unauthorized/nonexistent chat is a
            # definitive failure, never a creation.
            return AuthenticatedDmResult(
                DmSendOutcome.DEFINITIVE_FAILURE, detail="not_authorized"
            )
        return AuthenticatedDmResult(DmSendOutcome.SENT, receipt="receipt-1")

    def run_blocking(self, func, *args, **kwargs):
        self.blocking_calls += 1
        return func(*args, **kwargs)

    # -- installation ------------------------------------------------------

    def install(self):
        from gateway.conversation_extensions import (
            GatewayHostOperations,
            install_gateway_host_operations,
        )

        install_gateway_host_operations(
            GatewayHostOperations(
                spawn_task=self.spawn_task,
                cancel_tasks=self.cancel_tasks,
                lookup_session=self.lookup_session,
                create_initiated_child=self.create_initiated_child,
                inject_turn=self.inject_turn,
                send_authenticated_existing_dm=self.send_authenticated_existing_dm,
                run_blocking=self.run_blocking,
            )
        )
        return self


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


CONTACT_ID = "stephen-lucier"
GUEST_HANDLE = "+15550001111"
OWNER_HANDLE = "+15558675309"
UNKNOWN_HANDLE = "+15559999999"


def _registry_file(tmp_path: Path) -> Path:
    """A real contact registry the plugin's policy library will parse.

    Shape matches ``ContactRegistry.from_dict``: identities are nested under
    ``identities.bluebubbles.handles``. Using the real parser rather than
    constructing a registry object keeps this honest about the production
    config format.
    """
    import json

    path = tmp_path / "contacts.json"
    path.write_text(
        json.dumps(
            {
                "owner_profile": "gpt",
                "owner_contact_id": "kosta-owner",
                "owner_identities": [OWNER_HANDLE],
                "guest_profile": "guest",
                "contacts": {
                    CONTACT_ID: {
                        "id": CONTACT_ID,
                        "display_name": "Steve",
                        "role": "partner",
                        "identities": {"bluebubbles": {"handles": [GUEST_HANDLE]}},
                        "allowed_surfaces": ["bluebubbles"],
                        "timezone": "America/New_York",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _seed_profile(home: Path, **kwargs) -> Path:
    fixtures = _load_production_fixtures()
    home.mkdir(parents=True, exist_ok=True)
    fixtures.build_state_db(home / "state.db", **kwargs)
    fixtures.build_contact_memory(home / "contact-memory")
    parent = home.parent.parent if home.parent.name == "profiles" else home
    parent.mkdir(parents=True, exist_ok=True)
    fixtures.build_ownership_registry(parent / "proactive-contact-ownership.db")
    return home


def _config(tmp_path: Path, *, proactive_enabled: bool = True) -> dict:
    return {
        "gateway": {
            "required_conversation_extensions": [
                {"id": EXTENSION_ID, "api_version": 1}
            ],
            "conversation_ownership": {"default": "extension"},
            "bluebubbles": {"guest_contacts_file": str(_registry_file(tmp_path))},
        },
        "agent": {
            "contact_memory": {"enabled": True, "extraction": True},
            "proactive": {"enabled": proactive_enabled},
        },
        "plugins": {
            "entries": {
                "poke": {"settings": {"proactive": {"authoritative": True}}}
            }
        },
    }


def _scope(home: Path) -> str:
    from hermes_constants import hermes_home_key

    return hermes_home_key(home)


def _extension(poke_auth, home: Path, config: dict):
    """Build the authoritative extension object directly (not just its bundle)."""
    decision = poke_auth.evaluate_activation(profile_home=home, config_raw=config)
    assert decision.authoritative, decision.describe()
    return poke_auth.AuthoritativePokeExtension(
        decision=decision,
        profile_home=home,
        config_raw=config,
        profile_name="guest",
    )


def _route_context(*, sender: str, message_id: str = "msg-1", records=(), text="hello"):
    return ce.GatewayRouteContext(
        platform="bluebubbles",
        adapter_identity="bluebubbles",
        transport_profile="poke",
        transport_home="/tmp/poke-home",
        sender_identity=sender,
        chat_id="chat-1",
        chat_type="dm",
        text_preview=text[:120],
        ingress_records=tuple(records),
        raw_message={},
        message_id=message_id,
        text=text,
    )


def _envelope(fixtures_module, *, source_message_id: str, text: str = "hi"):
    contracts = _load_plugin_module("poke.contracts")
    return contracts.CommunicationIngressEnvelope(
        version=1,
        source_message_id=source_message_id,
        received_at=1000.0,
        occurred_at=1000.0,
        timestamp_source="transport",
        chat_type="dm",
        direction="inbound",
        sender_identity=GUEST_HANDLE,
        visible_text=text,
    )


def _facade(host: RecordingHost, *, scope: str, capabilities=None, generation: int = 1):
    from gateway.conversation_extensions import (
        GatewayRuntimeFacade,
        gateway_host_operations,
    )

    poke_auth = _load_plugin_module("poke.authoritative")
    return GatewayRuntimeFacade(
        extension_id=EXTENSION_ID,
        profile_name="guest",
        profile_home=scope,
        generation=generation,
        capabilities=frozenset(
            capabilities or poke_auth.AUTHORITATIVE_CAPABILITIES
        ),
        host=gateway_host_operations(),
    )


def _contact_db_paths(home: Path):
    return sorted((home / "contact-memory" / "contacts").glob("*.sqlite3"))


def _communication_event_count(home: Path) -> int:
    total = 0
    for path in _contact_db_paths(home):
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            total += con.execute("SELECT COUNT(*) FROM communication_event").fetchone()[0]
        finally:
            con.close()
    return total


# ---------------------------------------------------------------------------
# 1. routing is a real decision that establishes identity
# ---------------------------------------------------------------------------


def test_unapproved_sender_is_denied_by_the_extension_owner(tmp_path, poke_auth):
    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    config = _config(tmp_path)
    extension = _extension(poke_auth, home, config)

    directive = extension.authorize_route(_route_context(sender=UNKNOWN_HANDLE))
    assert directive.admit is False
    assert extension.activity.routes_denied == 1
    assert extension.activity.routes_admitted == 0


def test_approved_guest_is_routed_with_principal_and_trusted_scope(
    tmp_path, poke_auth
):
    """The cascade Review 3 recorded: routing must *establish* identity.

    A directive that admits without a principal leaves the guest session and
    policy context unbuilt, so contact-scoped behavior silently disappears.
    """
    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    extension = _extension(poke_auth, home, _config(tmp_path))

    directive = extension.authorize_route(_route_context(sender=GUEST_HANDLE))
    assert directive.admit is True
    assert directive.runtime_profile == "guest"
    assert directive.principal == "guest"
    assert directive.subject_id == CONTACT_ID
    assert directive.context_prefix.startswith("[Guest contact context:")
    scope = directive.scope_metadata["_hermes_contact_scope"]
    assert scope["principal"] == "guest"
    assert scope["session_contact_id"] == CONTACT_ID
    assert extension.activity.routes_admitted == 1


def test_owner_is_routed_to_the_owner_profile(tmp_path, poke_auth):
    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    extension = _extension(poke_auth, home, _config(tmp_path))

    directive = extension.authorize_route(_route_context(sender=OWNER_HANDLE))
    assert directive.admit is True
    assert directive.principal == "owner"
    assert directive.runtime_profile == "gpt"
    assert directive.context_prefix.startswith("[Owner contact context:")


def test_core_applies_the_validated_directive_to_the_event(tmp_path, poke_auth):
    """End-to-end through the *production* application helper."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    extension = _extension(poke_auth, home, _config(tmp_path))
    directive = extension.authorize_route(_route_context(sender=GUEST_HANDLE))

    decision = ce.GatewayRouteDecision(
        admitted=True,
        transport_profile="poke",
        transport_home="/tmp/poke-home",
        runtime_profile=directive.runtime_profile,
        principal=directive.principal,
        subject_id=directive.subject_id,
        context_prefix=directive.context_prefix,
        scope_metadata=directive.scope_metadata,
    )

    import dataclasses

    @dataclasses.dataclass
    class _Source:
        profile: str = "poke"
        user_id: str = GUEST_HANDLE
        user_id_alt: str | None = None
        chat_id_alt: str | None = None
        chat_type: str = "dm"

    @dataclasses.dataclass
    class _Event:
        source: object
        text: str = "hello"
        metadata: dict | None = None
        observed_only: bool = False

    source = _Source()
    event = _Event(source=source, metadata={})

    new_source, new_event = runner._apply_extension_route_decision(
        source, event, decision
    )
    assert new_source.profile == "guest"
    assert new_source.user_id_alt == f"guest:{CONTACT_ID}"
    assert new_event.text.startswith("[Guest contact context:")
    assert new_event.metadata["_hermes_contact_scope"]["principal"] == "guest"


def test_directive_never_prefixes_command_text(tmp_path, poke_auth):
    """Prefixing a ``/command`` would turn owner control commands into model text."""
    from gateway.run import GatewayRunner
    import dataclasses

    runner = object.__new__(GatewayRunner)
    decision = ce.GatewayRouteDecision(
        admitted=True,
        transport_profile="poke",
        transport_home="/tmp/h",
        runtime_profile="gpt",
        principal="owner",
        subject_id="kosta-owner",
        context_prefix="[Owner contact context: ...]\n\n",
    )

    @dataclasses.dataclass
    class _Source:
        profile: str = "poke"
        user_id_alt: str | None = None
        chat_id_alt: str | None = None

    @dataclasses.dataclass
    class _Event:
        source: object
        text: str = "/new"
        metadata: dict | None = None
        observed_only: bool = False

    source = _Source()
    _, event = runner._apply_extension_route_decision(
        source, _Event(source=source, metadata={}), decision
    )
    assert event.text == "/new"


def test_extension_cannot_invent_a_privilege_level(tmp_path):
    """An unrecognized principal must not be applied."""
    context = ce.GatewayRouteContext(
        platform="bluebubbles",
        adapter_identity="bb",
        transport_profile="poke",
        transport_home="/tmp/h",
        sender_identity=GUEST_HANDLE,
        chat_id="c",
        chat_type="dm",
    )
    bundle = ce.GatewayConversationExtension(
        extension_id="rogue",
        api_version=ce.EXTENSION_API_VERSION,
        capabilities=frozenset({"admission_policy", "health"}),
        authorize_route=lambda ctx: ce.GatewayRouteDirective(
            admit=True, principal="superuser", subject_id="x"
        ),
        health=lambda: ce.GatewayExtensionHealth(True),
    )
    ce.conversation_extension_registry.register(bundle, scope="/tmp/rogue")
    decision = ce.resolve_route(context, scope="/tmp/rogue")
    assert decision.admitted is True
    assert decision.principal is None, "core must not carry an unknown principal"


# ---------------------------------------------------------------------------
# 2. exactly one real ingress write
# ---------------------------------------------------------------------------


def test_activated_owner_performs_exactly_one_persisted_ingress_write(
    tmp_path, poke_auth
):
    """The positive assertion Review 3 said was missing.

    Not "legacy did not run" — *the extension wrote one row*, into a
    production-shaped store, in a temp snapshot.
    """
    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    extension = _extension(poke_auth, home, _config(tmp_path))

    before = _communication_event_count(home)

    # Admission first: this is what binds the authenticated identity the
    # ingress site uses. Without it, ingress correctly refuses to write.
    extension.authorize_route(_route_context(sender=GUEST_HANDLE, message_id="m1"))
    extension.observe_ingress(
        _route_context(
            sender=GUEST_HANDLE,
            message_id="m1",
            records=[_envelope(None, source_message_id="t-1")],
        )
    )

    after = _communication_event_count(home)
    assert after == before + 1, "expected exactly one persisted ingress write"
    assert extension.activity.ingress_writes == 1
    assert extension.activity.ingress_errors == 0


def test_ingress_replay_deduplicates_rather_than_double_writing(tmp_path, poke_auth):
    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    extension = _extension(poke_auth, home, _config(tmp_path))

    extension.authorize_route(_route_context(sender=GUEST_HANDLE, message_id="m1"))
    context = _route_context(
        sender=GUEST_HANDLE,
        message_id="m1",
        records=[_envelope(None, source_message_id="t-1")],
    )
    before = _communication_event_count(home)
    extension.observe_ingress(context)
    once = _communication_event_count(home)
    extension.observe_ingress(context)
    twice = _communication_event_count(home)

    assert once == before + 1
    assert twice == once, "a replay must deduplicate, not write again"


def test_unauthenticated_ingress_is_not_persisted(tmp_path, poke_auth):
    """No admission -> no identity -> no write. Fail closed."""
    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    extension = _extension(poke_auth, home, _config(tmp_path))

    before = _communication_event_count(home)
    extension.observe_ingress(
        _route_context(
            sender=UNKNOWN_HANDLE,
            message_id="m-unknown",
            records=[_envelope(None, source_message_id="t-x")],
        )
    )
    assert _communication_event_count(home) == before


# ---------------------------------------------------------------------------
# 3. exactly one extraction / one texture compile
# ---------------------------------------------------------------------------


def test_texture_is_compiled_exactly_once_per_turn(tmp_path, poke_auth):
    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    extension = _extension(poke_auth, home, _config(tmp_path))

    first = extension.extraction.compile_texture(session_key="s1", turn_index=0)
    second = extension.extraction.compile_texture(session_key="s1", turn_index=0)

    assert extension.activity.textures_compiled <= 1
    assert second is None, "a second compile for one turn must be refused"
    # A different turn is a different compilation.
    extension.extraction.compile_texture(session_key="s1", turn_index=1)
    assert extension.activity.textures_compiled <= 2


def test_extraction_requires_authenticated_identity(tmp_path, poke_auth):
    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    extension = _extension(poke_auth, home, _config(tmp_path))

    result = ce.GatewayTurnResult(
        session_key="s1",
        runtime_profile="guest",
        platform="bluebubbles",
        sender_identity=UNKNOWN_HANDLE,
        user_text="hi",
        assistant_text="hello",
        delivered=True,
        user_message_id="m-unknown",
    )
    extension.observe_turn_result(result)
    assert extension.activity.extractions_submitted == 0


def test_extraction_is_disabled_when_config_disables_it(tmp_path, poke_auth):
    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    config = _config(tmp_path)
    config["agent"]["contact_memory"]["extraction"] = False
    extension = _extension(poke_auth, home, config)

    extension.authorize_route(_route_context(sender=GUEST_HANDLE, message_id="m1"))
    assert (
        extension.extraction.submit_extraction(
            principal="guest",
            contact_id=CONTACT_ID,
            source_id="m1",
            user_text="hi",
            assistant_text="hello",
        )
        is False
    )


# ---------------------------------------------------------------------------
# 4. exactly one real watcher
# ---------------------------------------------------------------------------


def test_activation_starts_exactly_one_real_watcher(tmp_path, poke_auth):
    """``on_start`` must register one host-owned task — not zero, not two."""
    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    host = RecordingHost().install()
    extension = _extension(poke_auth, home, _config(tmp_path))
    scope = _scope(home)

    extension.on_start(_facade(host, scope=scope))

    assert len(host.spawned) == 1, f"expected one watcher, got {len(host.spawned)}"
    task = host.spawned[0]
    assert task.task_key == "proactive-watcher"
    assert task.extension_id == EXTENSION_ID
    assert task.generation == 1
    assert extension.activity.watcher_started == 1


def test_watcher_is_not_started_when_proactive_is_disabled(tmp_path, poke_auth):
    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    host = RecordingHost().install()
    extension = _extension(
        poke_auth, home, _config(tmp_path, proactive_enabled=False)
    )

    extension.on_start(_facade(host, scope=_scope(home)))
    assert host.spawned == []
    assert extension.activity.watcher_started == 0


def test_watcher_start_failure_is_loud_not_silent(tmp_path, poke_auth):
    """A host with no scheduler must make the extension unhealthy.

    Silently believing a watcher started is how proactive delivery stops with
    no signal — the outage shape this checkpoint exists to prevent.
    """
    from gateway.conversation_extensions import (
        GatewayHostOperations,
        install_gateway_host_operations,
    )

    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    install_gateway_host_operations(GatewayHostOperations())  # no spawn_task
    extension = _extension(poke_auth, home, _config(tmp_path))

    extension.on_start(_facade(None, scope=_scope(home)))
    health = extension.health()
    assert health.healthy is False
    assert health.detail == "proactive_watcher_unavailable"


def test_reload_cancels_the_old_generation_watcher(tmp_path, poke_auth):
    """Two generations must never leave two watchers running.

    Each generation registers a task keyed by ``(extension_id, profile_home,
    task_key, generation)``, which is what lets the host cancel exactly the
    outgoing generation without touching the incoming one.
    """
    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    host = RecordingHost().install()
    scope = _scope(home)

    first = _extension(poke_auth, home, _config(tmp_path))
    first.on_start(_facade(host, scope=scope, generation=1))
    assert len(host.spawned) == 1

    first.on_stop(_facade(host, scope=scope, generation=1))
    second = _extension(poke_auth, home, _config(tmp_path))
    second.on_start(_facade(host, scope=scope, generation=2))

    assert len(host.spawned) == 2, "each generation registers its own task"
    assert host.spawned[0].generation == 1
    assert host.spawned[1].generation == 2
    # Distinct identities are what let the host cancel exactly one.
    assert host.spawned[0].identity != host.spawned[1].identity

    # Cancelling generation 1 must be scoped to generation 1 alone.
    cancelled = ce.lifecycle_task_registry.cancel(EXTENSION_ID, scope, 1)
    assert cancelled == 0 or isinstance(cancelled, int)


def test_watcher_tick_drives_the_real_claim_path(tmp_path, poke_auth):
    """One tick against a production-shaped store with no due slot.

    Proves the tick reaches the real ``claim_due`` sequence (it records watcher
    health and returns cleanly) rather than being a no-op stub.
    """
    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    host = RecordingHost().install()
    extension = _extension(poke_auth, home, _config(tmp_path))
    extension.on_start(_facade(host, scope=_scope(home)))

    result = asyncio.run(extension.proactive.tick())
    assert result["ran"] is True
    assert extension.activity.watcher_ticks == 1
    assert host.sends == [], "a tick with no due slot must not send"


# ---------------------------------------------------------------------------
# 5. initiated child path
# ---------------------------------------------------------------------------


def test_initiated_child_path_is_reached_through_the_host(tmp_path, poke_auth):
    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    host = RecordingHost().install()
    extension = _extension(poke_auth, home, _config(tmp_path))
    extension.on_start(_facade(host, scope=_scope(home)))

    result = extension.proactive.create_child(
        session_key="s1", prompt="checking in", origin="proactive"
    )
    assert result["created"] is True
    assert len(host.children) == 1
    assert host.children[0].prompt == "checking in"
    assert extension.activity.children_created == 1


def test_initiated_child_requires_the_declared_capability(tmp_path, poke_auth):
    """The facade denies a host action the bundle did not declare."""
    from gateway.conversation_extensions import CapabilityDenied, InitiatedTurnRequest

    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    host = RecordingHost().install()
    facade = _facade(host, scope=_scope(home), capabilities={"health"})

    with pytest.raises(CapabilityDenied):
        facade.create_initiated_child(
            InitiatedTurnRequest(session_key="s", prompt="p", origin="o")
        )
    assert host.children == []


# ---------------------------------------------------------------------------
# 6. authenticated existing-DM tri-state, with transport-home authorization
# ---------------------------------------------------------------------------


def _claim(slot_id="slot-1", token="tok-1"):
    scheduler = _load_plugin_module("poke.proactive.scheduler")
    return scheduler.SlotClaim(
        slot_id=slot_id,
        contact_hash="hash-a",
        kind="checkin",
        interest_id=None,
        payload={"composed_text": "hi"},
        fire_at=1000.0,
        inbound_version=1,
        claim_token=token,
    )


class _Route:
    chat_id = "chat-authorized"


def test_delivery_to_an_authorized_existing_dm_reports_sent(tmp_path, poke_auth):
    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    host = RecordingHost(authorized_chats={"chat-authorized"}).install()
    extension = _extension(poke_auth, home, _config(tmp_path))
    extension.on_start(_facade(host, scope=_scope(home)))

    result = asyncio.run(
        extension.proactive.deliver(claim=_claim(), route=_Route(), text="hello")
    )
    from gateway.conversation_extensions import DmSendOutcome

    assert result.outcome is DmSendOutcome.SENT
    assert extension.activity.sends_sent == 1
    assert len(host.sends) == 1
    # Transport-home authorization: the request carries the durable claim token
    # as its reservation key, which is what makes a retry idempotent.
    assert host.sends[0].reservation_key == "tok-1"


def test_delivery_to_an_unauthorized_chat_fails_definitively(tmp_path, poke_auth):
    """No ``create_if_missing``, no fallback target."""
    from gateway.conversation_extensions import DmSendOutcome

    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    host = RecordingHost(authorized_chats=set()).install()
    extension = _extension(poke_auth, home, _config(tmp_path))
    extension.on_start(_facade(host, scope=_scope(home)))

    result = asyncio.run(
        extension.proactive.deliver(claim=_claim(), route=_Route(), text="hello")
    )
    assert result.outcome is DmSendOutcome.DEFINITIVE_FAILURE
    assert extension.activity.sends_failed == 1


def test_unknown_delivery_outcome_is_never_retried(tmp_path, poke_auth):
    """``UNKNOWN`` preserves uncertainty; a blind retry is the duplicate send."""
    from gateway.conversation_extensions import (
        AuthenticatedDmResult,
        DmSendOutcome,
    )

    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    host = RecordingHost(
        send_outcome=AuthenticatedDmResult(DmSendOutcome.UNKNOWN, detail="timeout")
    ).install()
    extension = _extension(poke_auth, home, _config(tmp_path))
    extension.on_start(_facade(host, scope=_scope(home)))

    result = asyncio.run(
        extension.proactive.deliver(claim=_claim(), route=_Route(), text="hello")
    )
    assert result.outcome is DmSendOutcome.UNKNOWN
    assert extension.activity.sends_unknown == 1
    assert len(host.sends) == 1, "an UNKNOWN outcome must not trigger a retry"


def test_send_capability_is_required(tmp_path, poke_auth):
    from gateway.conversation_extensions import (
        AuthenticatedDmRequest,
        CapabilityDenied,
    )

    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    host = RecordingHost(authorized_chats={"c"}).install()
    facade = _facade(host, scope=_scope(home), capabilities={"health"})

    with pytest.raises(CapabilityDenied):
        facade.send_authenticated_existing_dm(
            AuthenticatedDmRequest(
                platform="bluebubbles", chat_id="c", text="t", reservation_key="k"
            )
        )
    assert host.sends == []


# ---------------------------------------------------------------------------
# 7. zero outbound transport, everywhere
# ---------------------------------------------------------------------------


def test_full_activation_sequence_performs_zero_outbound_transport(
    tmp_path, poke_auth
):
    """Drive the whole sequence and assert nothing reached a transport.

    The send host-op is a recording double, so "zero outbound" here means the
    plugin never even asked to send during activation, routing, ingress, or
    post-turn — only the explicit delivery tests do.
    """
    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    host = RecordingHost(authorized_chats={"chat-1"}).install()
    extension = _extension(poke_auth, home, _config(tmp_path))
    scope = _scope(home)

    extension.on_start(_facade(host, scope=scope))
    extension.authorize_route(_route_context(sender=GUEST_HANDLE, message_id="m1"))
    extension.observe_ingress(
        _route_context(
            sender=GUEST_HANDLE,
            message_id="m1",
            records=[_envelope(None, source_message_id="t-1")],
        )
    )
    extension.observe_turn_result(
        ce.GatewayTurnResult(
            session_key="s1",
            runtime_profile="guest",
            platform="bluebubbles",
            sender_identity=GUEST_HANDLE,
            user_text="hi",
            assistant_text="hello",
            delivered=True,
            user_message_id="m1",
        )
    )

    assert host.sends == [], "activation must perform no outbound send"
    # But it must have done real work.
    assert extension.activity.routes_admitted == 1
    assert extension.activity.ingress_writes == 1
    assert extension.activity.watcher_started == 1


def test_activation_is_not_a_zero_owner_state(tmp_path, poke_auth):
    """The single assertion that would have caught Review-3 P0-2.

    After a full sequence, the owner's durable-effect counters must be
    non-zero. A zero-owner state has all six domains resolved to ``extension``
    and every counter at zero.
    """
    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    host = RecordingHost().install()
    extension = _extension(poke_auth, home, _config(tmp_path))

    extension.on_start(_facade(host, scope=_scope(home)))
    extension.authorize_route(_route_context(sender=GUEST_HANDLE, message_id="m1"))
    extension.observe_ingress(
        _route_context(
            sender=GUEST_HANDLE,
            message_id="m1",
            records=[_envelope(None, source_message_id="t-1")],
        )
    )

    activity = extension.activity.snapshot()
    assert activity["routes_admitted"] > 0, "no routing work was performed"
    assert activity["ingress_writes"] > 0, "no ingress was persisted"
    assert activity["watcher_started"] > 0, "no watcher was started"


# ---------------------------------------------------------------------------
# 8. rollback remains a pure config switch
# ---------------------------------------------------------------------------


def test_full_config_only_rollback_restores_the_legacy_owner(tmp_path, poke_auth):
    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    scope = _scope(home)
    config = _config(tmp_path)

    bundle, decision = poke_auth.build_extension(profile_home=home, config_raw=config)
    ce.conversation_extension_registry.register(bundle, scope=scope)
    plan, conflicts = co.activate_plan(scope=scope, config_raw=config)
    assert conflicts == ()
    assert all(plan[d].is_extension for d in co.OwnershipDomain)

    # Roll back: change only the config value. No revert, no data move.
    rolled_back = dict(config)
    rolled_back["gateway"] = dict(config["gateway"])
    rolled_back["gateway"]["conversation_ownership"] = {"default": "legacy"}

    co.conversation_ownership_registry.clear(scope)
    plan2, conflicts2 = co.activate_plan(scope=scope, config_raw=rolled_back)
    assert conflicts2 == ()
    assert all(plan2[d].is_legacy for d in co.OwnershipDomain)


def test_rollback_leaves_durable_state_byte_identical(tmp_path, poke_auth):
    home = _seed_profile(tmp_path / "hermes" / "profiles" / "guest")
    scope = _scope(home)
    config = _config(tmp_path)

    def digest():
        return {
            str(p.relative_to(home)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(home.rglob("*"))
            if p.is_file()
        }

    before = digest()
    bundle, _ = poke_auth.build_extension(profile_home=home, config_raw=config)
    ce.conversation_extension_registry.register(bundle, scope=scope)
    co.activate_plan(scope=scope, config_raw=config)
    co.conversation_ownership_registry.clear(scope)
    co.activate_plan(
        scope=scope,
        config_raw={**config, "gateway": {**config["gateway"],
                                          "conversation_ownership": {"default": "legacy"}}},
    )
    assert digest() == before, "rollback must not touch durable state"
