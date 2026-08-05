from pathlib import Path
import sqlite3
from gateway.contact_memory.schema import Interest, InterestState, InterestValence
from gateway.contact_memory.store import ContactMemoryStore
from gateway.proactive_scheduler import ContactRoute, ProactiveConfig, ProactiveScheduler
from gateway.proactive_status import health_snapshot, probe_model_readiness


def config(mode='observe'):
    lane={'provider':'openai-codex','model':'gpt-5.6-sol','reasoning_effort':'medium','fallback':False}
    proactive={'enabled':True,'mode':mode,'transport_owner_profile':'poke','allowed_contacts':[{'profile':'poke','contact_id':'kosta-owner','principal':'owner'},{'profile':'guest','contact_id':'stephen-lucier','principal':'guest'}], 'alarm_sink':{'configured':True,'type':'hermes_cron','target':'telegram:operator'}, 'compose_model':{'provider':'openai-codex','model':'gpt-5.6-sol','reasoning_effort':'low','fallback':False}}
    return {'auxiliary':{'proactive_gate':lane,'proactive_semantic':lane},'agent':{'proactive':proactive}}


def record_probes(scheduler, when):
    scheduler.record_health('model_probe',{
        'ready':True,'sent_request':True,'provider':'openai-codex',
        'resolved_model':'gpt-5.6-sol','response_model':'gpt-5.6-sol',
        'private_history_used':False,
    },now=when)
    scheduler.record_health('alarm_sink_probe',{
        'ready':True,'delivery_ack':True,'type':'hermes_cron','target':'telegram:operator',
    },now=when)


def test_stale_then_recovered_watcher_and_model_mismatch(tmp_path: Path):
    raw=config(); cfg=ProactiveConfig.from_mapping(raw)
    scheduler=ProactiveScheduler(state_db_path=tmp_path/'state.db',profile_home=tmp_path,profile_name='poke',config=cfg)
    scheduler.record_health('watcher',{'ok':True,'participant_registry_ready':True,'model_probe':{'ready':True,'checked_at':100}},now=100)
    record_probes(scheduler,100)
    stale=health_snapshot(profile_home=tmp_path,profile='poke',config=raw,now=100+3901,adapter_ready=True,cron_fresh=True)
    assert stale['dead'] and 'watcher_stale' in stale['reasons']
    scheduler.record_health('watcher',{'ok':True,'participant_registry_ready':True,'model_probe':{'ready':True,'checked_at':5000}},now=5000)
    record_probes(scheduler,5000)
    healthy=health_snapshot(profile_home=tmp_path,profile='poke',config=raw,now=5001,adapter_ready=True,cron_fresh=True)
    assert not healthy['dead']
    broken=config(); broken['auxiliary']['proactive_gate']['model']='other'
    assert 'model_lane_mismatch' in health_snapshot(profile_home=tmp_path,profile='poke',config=broken,now=5001)['reasons']


def test_enabled_unknown_adapter_and_cron_fail_closed(tmp_path: Path):
    raw=config()
    status=health_snapshot(profile_home=tmp_path,profile='poke',config=raw,now=5001)
    assert status['dead']
    assert {'adapter_unavailable','participant_registry_unavailable','maintenance_cron_stale'} <= set(status['reasons'])


def test_invalid_allowlist_and_unconfigured_alarm_are_reported_fail_closed(tmp_path: Path):
    invalid=config(); invalid['agent']['proactive']['allowed_contacts']=[]
    status=health_snapshot(profile_home=tmp_path,profile='poke',config=invalid,now=5001)
    assert status['dead'] and 'proactive_config_invalid' in status['reasons']
    no_alarm=config(); no_alarm['agent']['proactive']['alarm_sink']={'configured':False}
    status=health_snapshot(profile_home=tmp_path,profile='poke',config=no_alarm,now=5001)
    assert status['dead'] and 'alarm_sink_unconfigured' in status['reasons']


def test_real_ingress_drift_and_extraction_health_fail_closed(tmp_path: Path):
    raw=config(); cfg=ProactiveConfig.from_mapping(raw)
    scheduler=ProactiveScheduler(state_db_path=tmp_path/'state.db',profile_home=tmp_path,profile_name='poke',config=cfg)
    scheduler.record_health('watcher',{
        'adapter_ready':True,
        'participant_registry_ready':True,
        'model_probe':{'ready':True,'checked_at':5000},
        'extraction':{'dead_workers':1,'queue_full':True},
    },now=5000)
    record_probes(scheduler,5000)
    from gateway.proactive_scheduler import ProactiveStateStore
    ProactiveStateStore(tmp_path/'state.db').record_ingress_observed('untracked',observed_at=5000)
    status=health_snapshot(profile_home=tmp_path,profile='poke',config=raw,now=5001,cron_fresh=True)
    assert {'inbound_tracking_drift','extraction_worker_dead','extraction_queue_full'} <= set(status['reasons'])


def test_transport_failure_health_tracks_active_circuit_window_not_all_history(tmp_path: Path):
    raw=config(); cfg=ProactiveConfig.from_mapping(raw)
    ProactiveScheduler(
        state_db_path=tmp_path/'state.db',profile_home=tmp_path,
        profile_name='poke',config=cfg,
    )
    with sqlite3.connect(tmp_path/'state.db') as con:
        for index in range(cfg.circuit_breaker_failures):
            con.execute(
                "INSERT INTO proactive_delivery(slot_id,state,payload_hash,last_error_class,updated_at) "
                "VALUES(?,?,?,?,?)",
                (f'failed-{index}','failed',f'hash-{index}','route_auth_failed',4990 + index),
            )

    failed=health_snapshot(profile_home=tmp_path,profile='poke',config=raw,now=5001)
    assert 'transport_failure_exhaustion' in failed['reasons']

    with sqlite3.connect(tmp_path/'state.db') as con:
        con.execute(
            "INSERT INTO proactive_delivery(slot_id,state,payload_hash,last_error_class,updated_at) "
            "VALUES(?,?,?,?,?)",
            ('recovered','sent','recovery-hash','sent',5002),
        )

    recovered=health_snapshot(profile_home=tmp_path,profile='poke',config=raw,now=5003)
    assert recovered['deliveries']['failed'] == cfg.circuit_breaker_failures
    assert 'transport_failure_exhaustion' not in recovered['reasons']


def test_status_reads_real_contact_store_and_matching_digest_paths(tmp_path: Path):
    raw=config(); cfg=ProactiveConfig.from_mapping(raw)
    scheduler=ProactiveScheduler(state_db_path=tmp_path/'state.db',profile_home=tmp_path,profile_name='poke',config=cfg)
    scheduler.register_contact(ContactRoute('kosta-owner','poke'))
    scheduler.record_health('watcher',{'adapter_ready':True,'participant_registry_ready':True,'extraction':{},'model_probe':{'ready':True,'checked_at':5000}},now=5000)
    record_probes(scheduler,5000)
    scheduler.record_health('planning_attempt',{'state':'completed'},now=5000)
    store=ContactMemoryStore(tmp_path/'contact-memory','kosta-owner')
    store.put_interest(Interest(
        interest_id='status-interest',topic='release notes',parent_id=None,raw_score=4,
        last_evidence_at=5000,evidence_count=3,valence=InterestValence.POSITIVE,
        half_life_days=90,state=InterestState.ACTIVE,ts_alpha=2,ts_beta=1,
        created_at=4900,updated_at=5000,retired_at=None,
    ))
    digest=tmp_path/'contact-memory'/'digests'/f'{store.contact_namespace}.md'
    digest.parent.mkdir(parents=True); digest.write_text('bounded digest')
    status=health_snapshot(profile_home=tmp_path,profile='poke',config=raw,now=5001,
                           adapter_ready=True,cron_fresh=True)
    assert status['contacts']==1 and status['interest_count']==1
    assert status['eligible_interests']==1 and status['digest_count']==1
    assert 'digest_missing' not in status['reasons']


def test_status_ignores_unowned_shadow_contact_for_digest_health(tmp_path: Path):
    raw=config(); cfg=ProactiveConfig.from_mapping(raw)
    scheduler=ProactiveScheduler(state_db_path=tmp_path/'state.db',profile_home=tmp_path,
                                 profile_name='poke',config=cfg)
    scheduler.register_contact(ContactRoute('kosta-owner','poke'))
    guest_home=tmp_path/'guest'
    guest_scheduler=ProactiveScheduler(
        state_db_path=guest_home/'state.db',profile_home=guest_home,
        profile_name='guest',config=cfg,
        ownership_registry_path=tmp_path/'proactive-contact-ownership.db',
    )
    guest_scheduler.register_contact(ContactRoute('stephen-lucier','guest',principal='guest'))
    scheduler.record_health('watcher',{
        'adapter_ready':True,'participant_registry_ready':True,'extraction':{},
        'model_probe':{'ready':True,'checked_at':5000},
    },now=5000)
    record_probes(scheduler,5000)
    scheduler.record_health('planning_attempt',{'state':'completed'},now=5000)

    def add_interest(contact_id: str, interest_id: str):
        store=ContactMemoryStore(tmp_path/'contact-memory',contact_id)
        store.put_interest(Interest(
            interest_id=interest_id,topic='release notes',parent_id=None,raw_score=4,
            last_evidence_at=5000,evidence_count=3,valence=InterestValence.POSITIVE,
            half_life_days=90,state=InterestState.ACTIVE,ts_alpha=2,ts_beta=1,
            created_at=4900,updated_at=5000,retired_at=None,
        ))
        return store

    owner=add_interest('kosta-owner','owner-interest')
    add_interest('stephen-lucier','shadow-interest')
    digest=tmp_path/'contact-memory'/'digests'/f'{owner.contact_namespace}.md'
    digest.parent.mkdir(parents=True); digest.write_text('owner digest')

    status=health_snapshot(profile_home=tmp_path,profile='poke',config=raw,now=5001,
                           adapter_ready=True,cron_fresh=True)
    assert status['contacts']==2 and status['interest_count']==2
    assert status['eligible_interests']==1 and status['digest_count']==1
    assert 'digest_missing' not in status['reasons']


def test_status_falls_back_to_all_contacts_when_ownership_registry_is_partial(tmp_path: Path):
    raw=config()
    store=ContactMemoryStore(tmp_path/'contact-memory','kosta-owner')
    store.put_interest(Interest(
        interest_id='missing-digest',topic='release notes',parent_id=None,raw_score=4,
        last_evidence_at=5000,evidence_count=3,valence=InterestValence.POSITIVE,
        half_life_days=90,state=InterestState.ACTIVE,ts_alpha=2,ts_beta=1,
        created_at=4900,updated_at=5000,retired_at=None,
    ))
    registry=sqlite3.connect(tmp_path/'proactive-contact-ownership.db')
    registry.execute(
        "CREATE TABLE proactive_contact_owner("
        "contact_hash TEXT PRIMARY KEY, profile_name TEXT NOT NULL, "
        "state_db TEXT NOT NULL, updated_at REAL NOT NULL)"
    )
    registry.execute(
        "INSERT INTO proactive_contact_owner VALUES(?,?,?,?)",
        (store.contact_namespace,'guest','guest/state.db',5000),
    )
    registry.commit(); registry.close()

    status=health_snapshot(profile_home=tmp_path,profile='poke',config=raw,now=5001,
                           adapter_ready=True,cron_fresh=True)
    assert 'ownership_registry_unreadable' in status['reasons']
    assert status['eligible_interests']==1
    assert 'digest_missing' in status['reasons']


def test_model_probe_uses_strict_minimal_request_without_private_history():
    calls=[]
    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return type('Response',(),{
                'model':'gpt-5.6-sol',
                'usage':type('Usage',(),{'completion_tokens':2})(),
            })()
    client=type('Client',(),{'chat':type('Chat',(),{'completions':Completions()})()})()
    def resolver(provider, **kwargs):
        calls.append((provider,kwargs))
        return client,'gpt-5.6-sol'
    probe=probe_model_readiness(now=123,resolver=resolver)
    assert probe['ready'] and probe['sent_request'] is True and probe['private_history_used'] is False
    assert calls[0][0]=='openai-codex' and calls[0][1]['model']=='gpt-5.6-sol'
    request=calls[1]
    assert request['max_completion_tokens']==4 and request['messages']==[{'role':'user','content':'Reply exactly OK.'}]


def test_model_probe_invalid_credentials_and_wrong_resolved_model_fail_closed():
    def invalid(*args,**kwargs):
        raise PermissionError('revoked')
    assert not probe_model_readiness(resolver=invalid)['ready']
    def wrong(*args,**kwargs):
        return object(),'other-model'
    probe=probe_model_readiness(resolver=wrong)
    assert not probe['ready'] and probe['error_class']=='StrictRouteResolutionError'


def test_default_ownership_registry_is_isolated_between_standalone_roots(tmp_path: Path):
    from gateway.proactive_scheduler import ContactRoute
    poke_root=tmp_path/'case-a'; guest_root=tmp_path/'case-b'
    poke=ProactiveScheduler(state_db_path=poke_root/'state.db',profile_home=poke_root,
                            profile_name='poke',config=ProactiveConfig())
    guest=ProactiveScheduler(state_db_path=guest_root/'state.db',profile_home=guest_root,
                             profile_name='guest',config=ProactiveConfig())
    poke.register_contact(ContactRoute('same-id','poke'))
    guest.register_contact(ContactRoute('same-id','guest',principal='guest'))
    assert (poke_root/'proactive-contact-ownership.db').is_file()
    assert (guest_root/'proactive-contact-ownership.db').is_file()
    assert not (tmp_path/'proactive-contact-ownership.db').exists()
