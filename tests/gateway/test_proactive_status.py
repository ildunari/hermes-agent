from pathlib import Path
from gateway.proactive_scheduler import ProactiveConfig, ProactiveScheduler
from gateway.proactive_status import health_snapshot


def config(mode='observe'):
    lane={'provider':'openai-codex','model':'gpt-5.6-sol','reasoning_effort':'medium','fallback':False}
    proactive={'enabled':True,'mode':mode,'transport_owner_profile':'poke','allowed_contacts':[{'profile':'poke','contact_id':'kosta-owner','principal':'owner'},{'profile':'guest','contact_id':'stephen-lucier','principal':'guest'}], 'compose_model':{'provider':'openai-codex','model':'gpt-5.6-sol','reasoning_effort':'low','fallback':False}}
    return {'auxiliary':{'proactive_gate':lane,'proactive_semantic':lane},'agent':{'proactive':proactive}}


def test_stale_then_recovered_watcher_and_model_mismatch(tmp_path: Path):
    raw=config(); cfg=ProactiveConfig.from_mapping(raw)
    scheduler=ProactiveScheduler(state_db_path=tmp_path/'state.db',profile_home=tmp_path,profile_name='poke',config=cfg)
    scheduler.record_health('watcher',{'ok':True},now=100)
    stale=health_snapshot(profile_home=tmp_path,profile='poke',config=raw,now=100+3901,adapter_ready=True,cron_fresh=True)
    assert stale['dead'] and 'watcher_stale' in stale['reasons']
    scheduler.record_health('watcher',{'ok':True},now=5000)
    healthy=health_snapshot(profile_home=tmp_path,profile='poke',config=raw,now=5001,adapter_ready=True,cron_fresh=True)
    assert not healthy['dead']
    broken=config(); broken['auxiliary']['proactive_gate']['model']='other'
    assert 'model_lane_mismatch' in health_snapshot(profile_home=tmp_path,profile='poke',config=broken,now=5001)['reasons']
