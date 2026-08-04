import json
from types import SimpleNamespace
from typing import Any

import pytest

from gateway.run import (
    GatewayRunner, TrustedContactScope, _bounded_serious_register,
    _record_proactive_arrival, _record_proactive_inbound,
)
from gateway.config import Platform


@pytest.mark.asyncio
async def test_non_poke_runner_cannot_start_proactive_watcher(monkeypatch):
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "guest")
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    await GatewayRunner._proactive_scheduler_watcher(runner)
    assert runner._running is True


@pytest.mark.asyncio
async def test_non_allowlisted_contact_ingress_is_noop_not_circuit_open(tmp_path):
    """An approved conversation contact outside the proactive allowlist (e.g.
    a family guest like ``mom``) must be invisible to the proactive layer.

    Regression: ``register_contact`` raised its allowlist ValueError inside
    ``_record_proactive_inbound``, which was misclassified as a persistence
    failure — opening the global circuit and failing the reactive turn."""
    from gateway.proactive_scheduler import ProactiveOwnershipRegistry, ProactiveStateStore

    home = tmp_path / "profiles" / "guest"
    raw = _proactive_config()
    source = SimpleNamespace(
        chat_type="dm", platform=SimpleNamespace(value="bluebubbles"),
        chat_id="dm-mom", user_id="mom",
    )
    scope = TrustedContactScope("guest", "mom")
    sequence = await _record_proactive_arrival(
        config_raw=raw, trusted_scope=scope, profile_home=home, source=source,
        source_id="mom-msg-1", received_at=100,
    )
    assert sequence is None
    result = await _record_proactive_inbound(
        config_raw=raw, trusted_scope=scope, profile_home=home, profile="guest",
        source=source, session_id="session", source_id="mom-msg-1", text="hello",
        received_at=100, arrival_sequence=1,
    )
    assert result is None
    # No ledger rows were written and the global circuit stayed closed.
    state_db = home / "state.db"
    if state_db.is_file():
        store = ProactiveStateStore(state_db)
        assert store.current_ingress_sequence() == 0
    registry = ProactiveOwnershipRegistry(tmp_path / "proactive-contact-ownership.db")
    circuit = registry.global_send_status(now=102.0).get("circuit") or {}
    assert circuit.get("state") != "open"

    # The allowlisted guest contact on the same profile still records normally.
    allowed_scope = TrustedContactScope("guest", "stephen-lucier")
    allowed_sequence = await _record_proactive_arrival(
        config_raw=raw, trusted_scope=allowed_scope, profile_home=home, source=source,
        source_id="steve-msg-1", received_at=101,
    )
    assert allowed_sequence == 1


def test_runtime_model_calls_are_exact_and_nonfallback(monkeypatch):
    seen = []

    def fake_call(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(
            _hermes_resolved_route={"provider": "openai-codex", "model": "gpt-5.6-sol"},
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"allow": false, "reason": "test"}'))],
        )

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call)
    assert GatewayRunner._proactive_model_text(task="proactive_gate", prompt="x", effort="medium")
    assert seen == [{
        "task": "proactive_gate", "provider": "openai-codex", "model": "gpt-5.6-sol",
        "messages": [{"role": "user", "content": "x"}], "max_tokens": 500,
        "request_overrides": {"reasoning_effort": "medium"}, "allow_fallback": False,
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runner_profile", "multiplex_profiles"),
    (("poke", False), pytest.param("default", True, id="root-multiplex")),
)
async def test_poke_watcher_runs_real_loop_records_health_and_wires_web(
    monkeypatch, tmp_path, runner_profile, multiplex_profiles
):
    lane = {"provider": "openai-codex", "model": "gpt-5.6-sol", "reasoning_effort": "medium", "fallback": False}
    raw = {"auxiliary": {"proactive_gate": lane, "proactive_semantic": lane}, "agent": {"proactive": {
        "enabled": True, "mode": "observe", "dry_run": True,
        "allowed_contacts": [
            {"profile": "poke", "contact_id": "kosta-owner", "principal": "owner"},
            {"profile": "guest", "contact_id": "stephen-lucier", "principal": "guest"},
        ],
        "compose_model": {"provider": "openai-codex", "model": "gpt-5.6-sol", "reasoning_effort": "low", "fallback": False},
    }}}
    profile_home = tmp_path / "profiles" / "poke"
    seen = {}
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: runner_profile
    )
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda profile: profile_home if profile == "poke" else tmp_path / "profiles" / profile)
    monkeypatch.setattr("gateway.run._load_gateway_config_for_profile", lambda profile: raw if profile == "poke" else {})
    monkeypatch.setattr("gateway.proactive_status.probe_model_readiness",lambda:{
        'ready':True,'sent_request':True,'provider':'openai-codex','resolved_model':'gpt-5.6-sol',
        'response_model':'gpt-5.6-sol','private_history_used':False,'checked_at':1,
    })
    monkeypatch.setattr("gateway.proactive_status.probe_alarm_sink_readiness",lambda **kwargs:{
        'ready':False,'delivery_ack':False,'type':'','target':'','checked_at':1,
    })
    monkeypatch.setattr("tools.web_tools.web_search_tool", lambda topic, limit: [{"title": topic, "limit": limit}])

    def tick(**kwargs):
        material = kwargs["web_fallback"].search("ordinary web")
        seen["web"] = material.payload
        return {"armed": 0, "fired": 0, "ignored": 0}

    monkeypatch.setattr("gateway.run._run_proactive_tick_once", tick)
    registry_path = tmp_path / "contacts.json"
    registry_path.write_text(json.dumps({
        "owner_profile": "poke", "owner_contact_id": "kosta-owner",
        "guest_profile": "guest", "owner_identities": ["owner@example.com"],
        "contacts": {"stephen-lucier": {
            "identities": {"bluebubbles": {"handles": ["stephen@example.com"]}},
            "allowed_surfaces": ["bluebubbles"],
        }},
    }))
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.BLUEBUBBLES: SimpleNamespace(is_connected=True)}
    runner.config = SimpleNamespace(
        multiplex_profiles=multiplex_profiles,
        platforms={
            Platform.BLUEBUBBLES: SimpleNamespace(
                extra={"guest_contacts_file": str(registry_path)}
            ),
        },
    )

    async def stop_after_tick(_seconds):
        runner._running = False

    monkeypatch.setattr("asyncio.sleep", stop_after_tick)
    await GatewayRunner._proactive_scheduler_watcher(runner)
    assert seen["web"] == [{"title": "ordinary web", "limit": 5}]

    import sqlite3
    con = sqlite3.connect(profile_home / "state.db")
    value = json.loads(con.execute("SELECT value_json FROM proactive_health WHERE key='watcher'").fetchone()[0])
    con.close()
    assert value["completed"] is True and value["adapter_ready"] is True
    assert value["participant_registry_ready"] is True
    assert value["extraction"]["dead_workers"] == 0


def _proactive_config(mode="observe"):
    return {"agent":{"proactive":{"enabled":True,"mode":mode,
        "allowed_contacts":[
            {"profile":"poke","contact_id":"kosta-owner","principal":"owner"},
            {"profile":"guest","contact_id":"stephen-lucier","principal":"guest"},
        ],"alarm_sink":{"configured":True,"type":"hermes_cron","target":"telegram:operator"}}}}


@pytest.mark.asyncio
async def test_proactive_inbound_uses_profile_contact_timezone_when_not_repeated_in_proactive(tmp_path):
    from gateway.proactive_scheduler import ProactiveStateStore

    home = tmp_path / "profiles" / "poke"
    raw = _proactive_config()
    raw["agent"]["contact_memory"] = {"timezone": "America/New_York"}
    source = SimpleNamespace(
        chat_type="dm", platform=SimpleNamespace(value="bluebubbles"),
        chat_id="dm", user_id="owner",
    )
    scope = TrustedContactScope("owner", "kosta-owner")
    sequence = await _record_proactive_arrival(
        config_raw=raw, trusted_scope=scope, profile_home=home, source=source,
        source_id="timezone", received_at=100,
    )
    await _record_proactive_inbound(
        config_raw=raw, trusted_scope=scope, profile_home=home, profile="poke",
        source=source, session_id="session", source_id="timezone", text="hello",
        received_at=100, arrival_sequence=sequence,
    )

    contact = ProactiveStateStore(home / "state.db").contacts()[0]
    assert contact.timezone_name == "America/New_York"


@pytest.mark.asyncio
async def test_proactive_inbound_ignores_malformed_optional_timezone_sections(tmp_path):
    from gateway.proactive_scheduler import ProactiveStateStore

    home = tmp_path / "profiles" / "poke"
    raw: Any = _proactive_config()
    raw["agent"]["contact_memory"] = "malformed"
    raw["agent"]["conversation_texture"] = ["malformed"]
    source = SimpleNamespace(
        chat_type="dm", platform=SimpleNamespace(value="bluebubbles"),
        chat_id="dm", user_id="owner",
    )
    scope = TrustedContactScope("owner", "kosta-owner")
    sequence = await _record_proactive_arrival(
        config_raw=raw, trusted_scope=scope, profile_home=home, source=source,
        source_id="malformed-timezone", received_at=100,
    )
    result = await _record_proactive_inbound(
        config_raw=raw, trusted_scope=scope, profile_home=home, profile="poke",
        source=source, session_id="session", source_id="malformed-timezone", text="hello",
        received_at=100, arrival_sequence=sequence,
    )

    assert result is not None and result["inserted"] is True
    assert ProactiveStateStore(home / "state.db").contacts()[0].timezone_name == "UTC"


@pytest.mark.asyncio
async def test_serious_register_store_failure_is_shielded_and_current_turn_fallback_survives(tmp_path):
    class BrokenStore:
        async def load_recent_user_turns(self, session_id, *, limit):
            assert session_id == "session" and limit == 3
            raise OSError("store unavailable")

    serious_register = await _bounded_serious_register(
        BrokenStore(), "session", "my dog died"
    )
    assert serious_register is False
    home = tmp_path / "profiles" / "poke"
    raw = _proactive_config()
    source = SimpleNamespace(
        chat_type="dm", platform=SimpleNamespace(value="bluebubbles"),
        chat_id="dm", user_id="owner",
    )
    scope = TrustedContactScope("owner", "kosta-owner")
    sequence = await _record_proactive_arrival(
        config_raw=raw, trusted_scope=scope, profile_home=home, source=source,
        source_id="serious", received_at=100,
    )
    result = await _record_proactive_inbound(
        config_raw=raw, trusted_scope=scope, profile_home=home, profile="poke",
        source=source, session_id="session", source_id="serious", text="my dog died",
        received_at=100, arrival_sequence=sequence, serious_register=serious_register,
    )
    assert result["inserted"] is True and result["serious"] is True


@pytest.mark.asyncio
async def test_production_inbound_queues_pinned_confirmation_and_projects_once(monkeypatch, tmp_path):
    from gateway.contact_memory.schema import (
        GateDecision, Interest, InterestState, InterestValence,
        ProactiveSend, ProactiveSendKind,
    )
    from gateway.contact_memory.store import ContactMemoryStore
    from gateway.proactive_scheduler import ContactRoute, ProactiveConfig, ProactiveScheduler

    home = tmp_path / "profiles" / "poke"
    raw = _proactive_config()
    raw["agent"]["contact_memory"] = {"extraction": True}
    source = SimpleNamespace(
        chat_type="dm", platform=SimpleNamespace(value="bluebubbles"),
        chat_id="dm", user_id="owner",
    )
    scope = TrustedContactScope("owner", "kosta-owner")
    cfg = ProactiveConfig.from_mapping(raw)
    scheduler = ProactiveScheduler(
        state_db_path=home / "state.db", profile_home=home,
        profile_name="poke", config=cfg,
    )
    route = ContactRoute("kosta-owner", "poke", "UTC", "owner", "dm", "dm", "owner", "session")
    scheduler.note_inbound(route, message_id="before", received_at=10)
    store = ContactMemoryStore(home / "contact-memory", "kosta-owner")
    store.put_interest(Interest(
        interest_id="cars", topic="sports cars", parent_id=None,
        raw_score=3, last_evidence_at=1, evidence_count=4,
        valence=InterestValence.POSITIVE, half_life_days=90,
        state=InterestState.ACTIVE, ts_alpha=2, ts_beta=1,
        created_at=1, updated_at=1, retired_at=None,
    ))
    store.record_proactive_send(ProactiveSend(
        send_id="prior", interest_id="cars", kind=ProactiveSendKind.INTEREST_SHARE,
        candidate_json='{"topic":"sports cars"}', gate_decision=GateDecision.SENT,
        gate_reason="passed", sent_at=20, outcome=None, outcome_at=None, created_at=20,
    ))
    with scheduler._connect() as con:
        con.execute(
            "INSERT INTO proactive_action VALUES(?,?,?,?,?,'sent',?,NULL,NULL,NULL,NULL,?,?,?)",
            ("prior", None, route.contact_hash, "cars", "interest_share", 20, 1, "sent", 20),
        )

    queued = []
    class Runtime:
        def submit_outcome_confirmation(self, job):
            queued.append(job)
            return True
    monkeypatch.setattr("gateway.contact_memory.runtime.get_extraction_runtime", lambda *a, **k: Runtime())
    sequence = await _record_proactive_arrival(
        config_raw=raw, trusted_scope=scope, profile_home=home, source=source,
        source_id="reply", received_at=30,
    )
    result = await _record_proactive_inbound(
        config_raw=raw, trusted_scope=scope, profile_home=home, profile="poke",
        source=source, session_id="session", source_id="reply", text="nah stop",
        received_at=30, arrival_sequence=sequence,
    )
    assert result["outcome_status"] == "provisional" and len(queued) == 1
    from gateway.proactive_status import health_snapshot
    stalled = health_snapshot(
        profile_home=home, profile="poke", config=raw, now=4000,
        adapter_ready=True, cron_fresh=True,
    )
    assert stalled["outcome_confirmation"]["provisional"] == 1
    assert stalled["outcome_confirmation"]["oldest_provisional_age_seconds"] == 3970
    assert "outcome_confirmation_stalled" in stalled["reasons"]

    class Backend:
        model_id = "mlx-community/Qwen3-4B-Instruct-2507-4bit"
        calls = 0
        async def confirm_outcome(self, text, provisional):
            self.calls += 1
            assert text == "nah stop" and provisional == "dismissed"
            return {"outcome": "dismissed", "valence": "negative"}
    backend = Backend()
    await queued[0].run(backend)
    await queued[0].run(backend)
    assert backend.calls == 1
    assert store.get_proactive_send("prior").outcome.value == "dismissed"
    assert store.get_interest("cars").ts_beta == 2


@pytest.mark.asyncio
async def test_ingress_arrival_sequence_is_durable_before_delivery_lock_and_failures_open_circuit(monkeypatch,tmp_path):
    from gateway.proactive_scheduler import ProactiveOwnershipRegistry, ProactiveStateStore
    home=tmp_path/'profiles'/'poke'
    source=SimpleNamespace(chat_type='dm',platform=SimpleNamespace(value='bluebubbles'),chat_id='dm',user_id='owner')
    scope=TrustedContactScope('owner','kosta-owner')
    sequence=await _record_proactive_arrival(
        config_raw=_proactive_config(),trusted_scope=scope,profile_home=home,source=source,
        source_id='arrival-1',received_at=100,
    )
    assert sequence>0
    with ProactiveStateStore(home/'state.db')._connect() as con:
        assert con.execute("SELECT rowid FROM proactive_ingress_observed WHERE message_id='arrival-1'").fetchone()[0]==sequence

    def fail(*args,**kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(ProactiveStateStore,'record_ingress_observed',fail)
    with pytest.raises(RuntimeError,match='failed closed'):
        await _record_proactive_arrival(
            config_raw=_proactive_config(),trusted_scope=scope,profile_home=home,source=source,
            source_id='arrival-2',received_at=101,
        )
    status=ProactiveOwnershipRegistry(tmp_path/'proactive-contact-ownership.db').global_send_status(now=102)
    assert status['circuit_state']=='open' and status['circuit_reason']=='ingress_arrival_persistence_failure'


@pytest.mark.asyncio
async def test_authenticated_arrival_then_inbound_commit_traverses_real_protocol(monkeypatch,tmp_path):
    home=tmp_path/'profiles'/'poke'
    source=SimpleNamespace(chat_type='dm',platform=SimpleNamespace(value='bluebubbles'),chat_id='dm',user_id='owner')
    scope=TrustedContactScope('owner','kosta-owner')
    sequence=await _record_proactive_arrival(
        config_raw=_proactive_config(),trusted_scope=scope,profile_home=home,source=source,
        source_id='arrival',received_at=100,
    )
    result=await _record_proactive_inbound(
        config_raw=_proactive_config(),trusted_scope=scope,profile_home=home,profile='poke',
        source=source,session_id='session',source_id='arrival',text='ordinary inbound',
        received_at=100,arrival_sequence=sequence,
    )
    assert result['inserted'] is True


@pytest.mark.asyncio
async def test_arrival_fences_are_idempotent_and_order_independent_and_visible_to_watcher(tmp_path):
    import asyncio
    from gateway.proactive_scheduler import ProactiveConfig
    from gateway.proactive_status import health_snapshot

    home = tmp_path / 'profiles' / 'poke'
    source = SimpleNamespace(chat_type='dm', platform=SimpleNamespace(value='bluebubbles'),
                             chat_id='dm', user_id='owner')
    scope = TrustedContactScope('owner', 'kosta-owner')

    async def observe(message_id, delay):
        await asyncio.sleep(delay)
        return await _record_proactive_arrival(
            config_raw=_proactive_config(), trusted_scope=scope, profile_home=home,
            source=source, source_id=message_id, received_at=100 + delay,
        )

    later_call, earlier_call = await asyncio.gather(observe('second', 0), observe('first', .01))
    duplicate = await observe('second', 0)
    assert duplicate == later_call
    assert {later_call, earlier_call} == {1, 2}

    snapshot = health_snapshot(
        profile_home=home, profile='poke', config=_proactive_config(), now=200,
        adapter_ready=True, cron_fresh=True,
    )
    assert snapshot['observed_ingress'] == 2


@pytest.mark.asyncio
async def test_live_watcher_traverses_real_tick_final_checks_and_authenticated_adapter_without_send(monkeypatch,tmp_path):
    import time
    from gateway.proactive_scheduler import ContactRoute, ProactiveConfig, ProactiveScheduler
    lane={"provider":"openai-codex","model":"gpt-5.6-sol","reasoning_effort":"medium","fallback":False}
    raw=_proactive_config("live")
    raw["auxiliary"]={"proactive_gate":lane,"proactive_semantic":lane}
    raw["agent"]["proactive"].update({
        "active_hours":{"start":"00:00","end":"23:59"},
        "compose_model":{"provider":"openai-codex","model":"gpt-5.6-sol","reasoning_effort":"low","fallback":False},
    })
    home=tmp_path/'profiles'/'poke'; now=time.time()
    from hermes_state import SessionDB
    sessions=SessionDB(home/'state.db')
    sessions.create_session('parent','bluebubbles',user_id='owner',session_key='agent:main:bluebubbles:dm:owner',
                            chat_id='existing',chat_type='dm',model='test',system_prompt='stable')
    sessions.append_message('parent','user','hello')
    sessions.append_message('parent','assistant','hey')
    sessions.close()
    cfg=ProactiveConfig.from_mapping(raw)
    scheduler=ProactiveScheduler(state_db_path=home/'state.db',profile_home=home,profile_name='poke',config=cfg)
    route=ContactRoute('kosta-owner','poke','UTC','owner','dm','existing','owner','parent')
    for index in range(5):
        scheduler.note_inbound(route,message_id=f'm-{index}',received_at=now-100+index)
    slot=scheduler.arm_slot(route,kind='checkin',fire_at=now-1,payload={'reason':'follow up','session_id':'parent'},now=now-2)
    with scheduler.ownership_registry._connect() as con:
        con.execute("INSERT INTO proactive_global_send VALUES(1,NULL,NULL,?,?) ON CONFLICT(singleton) DO UPDATE SET slot_id=NULL,reserved_until=NULL,last_visible_at=excluded.last_visible_at,updated_at=excluded.updated_at",(now-1,now-1))

    registry_path=tmp_path/'contacts-live.json'
    registry_path.write_text(json.dumps({
        "owner_profile":"poke","owner_contact_id":"kosta-owner","guest_profile":"guest",
        "owner_identities":["owner"],"contacts":{"stephen-lucier":{"identities":{"bluebubbles":{"handles":["stephen"]}},"allowed_surfaces":["bluebubbles"]}},
    }))
    class SafeAdapter:
        platform=SimpleNamespace(value='bluebubbles'); is_connected=True
        def __init__(self): self.auth=[]
        async def resolve_authenticated_existing_dm(self,chat_id,expected):
            self.auth.append((chat_id,expected)); return ('iMessage;-;existing','fingerprint')
        async def send(self,*args,**kwargs): raise AssertionError('spacing barrier must prevent external send')
    adapter=SafeAdapter()
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name",lambda:'poke')
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir",lambda profile: home if profile=='poke' else tmp_path/'profiles'/profile)
    monkeypatch.setattr("gateway.run._load_gateway_config_for_profile",lambda profile: raw if profile=='poke' else {})
    monkeypatch.setattr("gateway.proactive_status.probe_model_readiness",lambda:{
        'ready':True,'checked_at':time.time(),'sent_request':True,'provider':'openai-codex',
        'resolved_model':'gpt-5.6-sol','response_model':'gpt-5.6-sol','private_history_used':False,
    })
    monkeypatch.setattr("gateway.proactive_status.probe_alarm_sink_readiness",lambda **kwargs:{
        'ready':True,'delivery_ack':True,'type':'hermes_cron','target':'telegram:operator',
        'checked_at':time.time(),
    })
    monkeypatch.setattr(GatewayRunner,"_proactive_compose_generate",staticmethod(lambda request:"safe prepared text"))
    monkeypatch.setattr(
        GatewayRunner,
        "_proactive_gate_verdict",
        staticmethod(lambda _request: {"allow": True, "reason": "welcome"}),
    )
    runner=GatewayRunner.__new__(GatewayRunner); runner._running=True
    runner.adapters={Platform.BLUEBUBBLES:adapter}
    runner.config=SimpleNamespace(platforms={Platform.BLUEBUBBLES:SimpleNamespace(extra={'guest_contacts_file':str(registry_path)})})
    async def stop(_seconds): runner._running=False
    monkeypatch.setattr("asyncio.sleep",stop)
    await GatewayRunner._proactive_scheduler_watcher(runner)
    assert adapter.auth and adapter.auth[0][0]=='existing'
    with scheduler._connect() as con:
        delivery=con.execute("SELECT state,attempt_count,prepared_payload FROM proactive_delivery WHERE slot_id=?",(slot,)).fetchone()
    assert tuple(delivery)==('retry_wait',0,'safe prepared text')
