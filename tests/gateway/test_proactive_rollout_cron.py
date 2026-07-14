from pathlib import Path
from cron.jobs import list_jobs, use_cron_store
from scripts.install_proactive_rollout_cron import WATCHDOG_NAME, install
from scripts.install_contact_memory_maintenance_cron import JOB_NAME


def test_rollout_cron_dry_run_and_idempotent_reconcile(tmp_path: Path):
    root = tmp_path / 'poke'
    plan=install(root=str(root),dry_run=True)
    assert plan['task']=='proactive_semantic' and not (root/'scripts').exists()
    first=install(root=str(root),dry_run=False); install(root=str(root),dry_run=False)
    assert first['jobs']==['maintenance','watchdog']
    with use_cron_store(root):
        jobs=list_jobs(include_disabled=True)
    assert [j['name'] for j in jobs].count(JOB_NAME)==1
    assert [j['name'] for j in jobs].count(WATCHDOG_NAME)==1
    runner=(root/'scripts'/'proactive_health_watchdog.py').read_text()
    assert 'gateway restart' not in runner and '--fail-on-dead' in runner
