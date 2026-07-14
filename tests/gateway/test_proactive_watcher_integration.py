import json
from types import SimpleNamespace

import pytest

from gateway.run import GatewayRunner, TrustedContactScope, _record_proactive_arrival, _record_proactive_inbound
from gateway.config import Platform


@pytest.mark.asyncio
async def test_non_poke_runner_cannot_start_proactive_watcher(monkeypatch):
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "guest")
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    await GatewayRunner._proactive_scheduler_watcher(runner)
    assert runner._running is True


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
async def test_poke_watcher_runs_real_loop_records_health_and_wires_web(monkeypatch, tmp_path):
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
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "poke")
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda profile: profile_home if profile == "poke" else tmp_path / "profiles" / profile)
    monkeypatch.setattr("gateway.run._load_gateway_config_for_profile", lambda profile: raw if profile == "poke" else {})
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
    runner.config = SimpleNamespace(platforms={
        Platform.BLUEBUBBLES: SimpleNamespace(extra={"guest_contacts_file": str(registry_path)}),
    })

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
        ],"alarm_sink":{"configured":True,"type":"operator"}}}}


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
    monkeypatch.setattr("gateway.proactive_status.probe_model_readiness",lambda:{'ready':True,'checked_at':time.time(),'tasks':{},'sent_request':False})
    monkeypatch.setattr(GatewayRunner,"_proactive_compose_generate",staticmethod(lambda request:"safe prepared text"))
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
