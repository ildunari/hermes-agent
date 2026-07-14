from pathlib import Path
from cron.jobs import list_jobs, mark_job_run, use_cron_store
from gateway.proactive_scheduler import ProactiveConfig
from gateway.proactive_status import probe_alarm_sink_readiness
from scripts.install_proactive_rollout_cron import ALARM_PROBE_NAME, WATCHDOG_NAME, install
from scripts.install_contact_memory_maintenance_cron import JOB_NAME


def test_rollout_cron_dry_run_and_idempotent_reconcile(tmp_path: Path):
    root = tmp_path / 'poke'
    plan=install(root=str(root),alarm_target='telegram:operator',dry_run=True)
    assert plan['task']=='proactive_semantic' and not (root/'scripts').exists()
    first=install(root=str(root),alarm_target='telegram:operator',dry_run=False)
    install(root=str(root),alarm_target='telegram:operator',dry_run=False)
    assert first['jobs']==['maintenance','watchdog','alarm_probe']
    with use_cron_store(root):
        jobs=list_jobs(include_disabled=True)
    assert [j['name'] for j in jobs].count(JOB_NAME)==1
    assert [j['name'] for j in jobs].count(WATCHDOG_NAME)==1
    assert next(j for j in jobs if j['name']==WATCHDOG_NAME)['deliver']=='telegram:operator'
    assert [j['name'] for j in jobs].count(ALARM_PROBE_NAME)==1
    runner=(root/'scripts'/'proactive_health_watchdog.py').read_text()
    assert 'gateway restart' not in runner and '--fail-on-dead' in runner
    cfg=ProactiveConfig(alarm_sink_configured=True,alarm_sink_type='hermes_cron',
                        alarm_sink_target='telegram:operator')
    assert not probe_alarm_sink_readiness(profile_home=root,config=cfg)['ready']
    probe_job=next(j for j in jobs if j['name']==ALARM_PROBE_NAME)
    with use_cron_store(root):
        mark_job_run(probe_job['id'],True,None,delivery_error=None)
    assert probe_alarm_sink_readiness(profile_home=root,config=cfg)['ready']


def test_rollout_cron_rejects_arbitrary_or_non_operator_sink(tmp_path: Path):
    for target in ('local','origin','definitely-not-real:target','telegram:'):
        import pytest
        with pytest.raises(ValueError):
            install(root=str(tmp_path/'poke'),alarm_target=target,dry_run=True)
