from pathlib import Path
from gateway.contact_memory.schema import Interest, InterestState, InterestValence
from gateway.contact_memory.store import ContactMemoryStore
from gateway.proactive_scheduler import ProactiveConfig, ProactiveScheduler
from gateway.proactive_status import health_snapshot


def config(mode='observe'):
    lane={'provider':'openai-codex','model':'gpt-5.6-sol','reasoning_effort':'medium','fallback':False}
    proactive={'enabled':True,'mode':mode,'transport_owner_profile':'poke','allowed_contacts':[{'profile':'poke','contact_id':'kosta-owner','principal':'owner'},{'profile':'guest','contact_id':'stephen-lucier','principal':'guest'}], 'compose_model':{'provider':'openai-codex','model':'gpt-5.6-sol','reasoning_effort':'low','fallback':False}}
    return {'auxiliary':{'proactive_gate':lane,'proactive_semantic':lane},'agent':{'proactive':proactive}}


def test_stale_then_recovered_watcher_and_model_mismatch(tmp_path: Path):
    raw=config(); cfg=ProactiveConfig.from_mapping(raw)
    scheduler=ProactiveScheduler(state_db_path=tmp_path/'state.db',profile_home=tmp_path,profile_name='poke',config=cfg)
    scheduler.record_health('watcher',{'ok':True,'participant_registry_ready':True},now=100)
    stale=health_snapshot(profile_home=tmp_path,profile='poke',config=raw,now=100+3901,adapter_ready=True,cron_fresh=True)
    assert stale['dead'] and 'watcher_stale' in stale['reasons']
    scheduler.record_health('watcher',{'ok':True,'participant_registry_ready':True},now=5000)
    healthy=health_snapshot(profile_home=tmp_path,profile='poke',config=raw,now=5001,adapter_ready=True,cron_fresh=True)
    assert not healthy['dead']
    broken=config(); broken['auxiliary']['proactive_gate']['model']='other'
    assert 'model_lane_mismatch' in health_snapshot(profile_home=tmp_path,profile='poke',config=broken,now=5001)['reasons']


def test_enabled_unknown_adapter_and_cron_fail_closed(tmp_path: Path):
    raw=config()
    status=health_snapshot(profile_home=tmp_path,profile='poke',config=raw,now=5001)
    assert status['dead']
    assert {'adapter_unavailable','participant_registry_unavailable','maintenance_cron_stale'} <= set(status['reasons'])


def test_real_ingress_drift_and_extraction_health_fail_closed(tmp_path: Path):
    raw=config(); cfg=ProactiveConfig.from_mapping(raw)
    scheduler=ProactiveScheduler(state_db_path=tmp_path/'state.db',profile_home=tmp_path,profile_name='poke',config=cfg)
    scheduler.record_health('watcher',{
        'adapter_ready':True,
        'participant_registry_ready':True,
        'extraction':{'dead_workers':1,'queue_full':True},
    },now=5000)
    from gateway.proactive_scheduler import ProactiveStateStore
    ProactiveStateStore(tmp_path/'state.db').record_ingress_observed('untracked',observed_at=5000)
    status=health_snapshot(profile_home=tmp_path,profile='poke',config=raw,now=5001,cron_fresh=True)
    assert {'inbound_tracking_drift','extraction_worker_dead','extraction_queue_full'} <= set(status['reasons'])


def test_status_reads_real_contact_store_and_matching_digest_paths(tmp_path: Path):
    raw=config(); cfg=ProactiveConfig.from_mapping(raw)
    scheduler=ProactiveScheduler(state_db_path=tmp_path/'state.db',profile_home=tmp_path,profile_name='poke',config=cfg)
    scheduler.record_health('watcher',{'adapter_ready':True,'participant_registry_ready':True,'extraction':{}},now=5000)
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
