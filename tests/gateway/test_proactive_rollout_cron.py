from pathlib import Path
import copy
import os
import subprocess
import sys

import pytest

from cron.jobs import list_jobs, mark_job_run, use_cron_store
from gateway.proactive_scheduler import ProactiveConfig
from gateway.proactive_status import probe_alarm_sink_readiness
from scripts.install_proactive_rollout_cron import ALARM_PROBE_NAME, WATCHDOG_NAME, install
from scripts.install_contact_memory_maintenance_cron import JOB_NAME
from scripts.install_proactive_profile_config import install as install_profile_config


def _configure_poke_bluebubbles(parent: Path, target: str = "operator-guid") -> Path:
    poke = parent / "poke"
    poke.mkdir(parents=True, exist_ok=True)
    (poke / ".env").write_text(
        "BLUEBUBBLES_SERVER_URL=http://poke-bluebubbles.invalid\n"
        "BLUEBUBBLES_PASSWORD=owner-route-secret\n"
        f"BLUEBUBBLES_HOME_CHANNEL={target}\n",
        encoding="utf-8",
    )
    (poke / "config.yaml").write_text(
        "gateway:\n"
        "  platforms:\n"
        "    bluebubbles:\n"
        "      enabled: true\n"
        "      extra:\n"
        "        webhook_register: true\n",
        encoding="utf-8",
    )
    return poke


def test_guest_profile_config_names_poke_delivery_owner_without_enabling_ingress(tmp_path: Path):
    guest = tmp_path / "guest"
    install_profile_config(profile="guest", root=str(guest), apply=True)
    import yaml
    config = yaml.safe_load((guest / "config.yaml").read_text(encoding="utf-8"))
    assert config["gateway"]["platforms"]["bluebubbles"]["enabled"] is False
    assert config["agent"]["proactive"]["transport_owner_profile"] == "poke"
    assert config["agent"]["proactive"]["alarm_sink"]["delivery_profile"] == "poke"


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
    # Generic/manual run state is not delivery evidence.
    assert not probe_alarm_sink_readiness(profile_home=root,config=cfg)['ready']
    with use_cron_store(root):
        mark_job_run(probe_job['id'],True,None,delivery_error=None,
                     delivery_ack_metadata=probe_job['probe_binding'])
    assert probe_alarm_sink_readiness(profile_home=root,config=cfg)['ready']


def test_installed_watchdog_imports_its_installing_checkout_with_hostile_pythonpath(
    tmp_path: Path,
):
    root = tmp_path / "poke"
    install(root=str(root), alarm_target="telegram:operator")
    decoy = tmp_path / "other-checkout"
    (decoy / "gateway").mkdir(parents=True)
    (decoy / "gateway" / "__init__.py").write_text(
        "raise RuntimeError('cross-checkout gateway import')\n", encoding="utf-8"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(decoy)
    env["HERMES_HOME"] = str(tmp_path / "ambient-profile")

    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "proactive_health_watchdog.py")],
        capture_output=True, text=True, cwd=tmp_path, env=env,
    )
    # The exact checkout's status command sees the freshly installed rollout
    # as healthy and stays silent. Importing the hostile same-named package
    # instead would make the wrapper fail nonzero with its fallback alert.
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_rollout_cron_rejects_arbitrary_or_non_operator_sink(tmp_path: Path):
    for target in ('local','origin','definitely-not-real:target','telegram:'):
        import pytest
        with pytest.raises(ValueError):
            install(root=str(tmp_path/'poke'),alarm_target=target,dry_run=True)


def test_probe_ack_is_bound_to_target_nonce_script_and_generation(tmp_path: Path):
    root = tmp_path / "poke"
    install(root=str(root), alarm_target="telegram:first")
    with use_cron_store(root):
        old = next(j for j in list_jobs(include_disabled=True) if j["name"] == ALARM_PROBE_NAME)
        mark_job_run(old["id"], True, delivery_ack_metadata=old["probe_binding"])
    first_cfg = ProactiveConfig(alarm_sink_configured=True, alarm_sink_type="hermes_cron",
                                alarm_sink_target="telegram:first")
    assert probe_alarm_sink_readiness(profile_home=root, config=first_cfg)["ready"]

    install(root=str(root), alarm_target="telegram:second")
    with use_cron_store(root):
        changed = next(j for j in list_jobs(include_disabled=True) if j["name"] == ALARM_PROBE_NAME)
        # Reproduction: an old in-flight run finishing after the update cannot ACK.
        mark_job_run(changed["id"], True, delivery_ack_metadata=old["probe_binding"])
    assert changed["probe_binding"]["nonce"] != old["probe_binding"]["nonce"]
    assert changed["probe_binding"]["generation"] != old["probe_binding"]["generation"]
    second_cfg = ProactiveConfig(alarm_sink_configured=True, alarm_sink_type="hermes_cron",
                                 alarm_sink_target="telegram:second")
    assert not probe_alarm_sink_readiness(profile_home=root, config=second_cfg)["ready"]

    prior = changed["probe_binding"]
    (root / "scripts" / "proactive_alarm_sink_probe.py").write_text("print('tampered')\n")
    install(root=str(root), alarm_target="telegram:second")
    with use_cron_store(root):
        repaired = next(j for j in list_jobs(include_disabled=True) if j["name"] == ALARM_PROBE_NAME)
    assert repaired["probe_binding"]["generation"] != prior["generation"]
    assert not probe_alarm_sink_readiness(profile_home=root, config=second_cfg)["ready"]


def test_real_cron_delivery_path_persists_exact_probe_ack(monkeypatch, tmp_path: Path):
    from cron import scheduler

    root = tmp_path / "poke"
    install(root=str(root), alarm_target="telegram:operator")
    delivered = []
    with use_cron_store(root):
        job = next(j for j in list_jobs(include_disabled=True) if j["name"] == ALARM_PROBE_NAME)
        # Exercise the real no-agent script runner; only the external transport
        # is replaced. This proves the nonce/generation emitted by the installed
        # script and the target metadata travel through the actual cron path.
        monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: root)
        monkeypatch.setattr(scheduler, "save_job_output", lambda *a, **k: root / "out")
        monkeypatch.setattr(
            scheduler, "_deliver_result",
            lambda delivered_job, content, **kwargs: delivered.append((delivered_job, content)),
        )
        assert scheduler.run_one_job(job)
    assert delivered == [(job, (
        "HERMES_PROACTIVE_ALARM_PROBE_ACK_REQUEST "
        f"{job['probe_binding']['nonce']} {job['probe_binding']['generation']}"
    ))]
    cfg = ProactiveConfig(alarm_sink_configured=True, alarm_sink_type="hermes_cron",
                          alarm_sink_target="telegram:operator")
    assert probe_alarm_sink_readiness(profile_home=root, config=cfg)["ready"]


def test_guest_bluebubbles_probe_uses_only_poke_owner_and_records_ack(monkeypatch, tmp_path: Path):
    from cron import scheduler
    from tools import send_message_tool

    _configure_poke_bluebubbles(tmp_path)
    guest = tmp_path / "guest"
    install(root=str(guest), alarm_target="bluebubbles:operator-guid")
    delivered = []

    async def fake_send(platform, pconfig, chat_id, message, **kwargs):
        delivered.append((platform.value, dict(pconfig.extra), chat_id, message))
        return {"success": True, "message_id": "owner-message"}

    monkeypatch.setattr(send_message_tool, "_send_to_platform", fake_send)
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: guest)
    monkeypatch.setattr(scheduler, "save_job_output", lambda *a, **k: guest / "out")
    with use_cron_store(guest):
        job = next(j for j in list_jobs(include_disabled=True) if j["name"] == ALARM_PROBE_NAME)
        assert job["delivery_profile"] == "poke"
        assert job["probe_binding"]["delivery_profile"] == "poke"
        assert scheduler.run_one_job(job)
        completed = next(j for j in list_jobs(include_disabled=True) if j["id"] == job["id"])

    assert len(delivered) == 1
    platform, extra, chat_id, message = delivered[0]
    assert (platform, chat_id) == ("bluebubbles", "operator-guid")
    assert extra["server_url"] == "http://poke-bluebubbles.invalid"
    assert extra["password"] == "owner-route-secret"
    assert extra["webhook_register"] is True  # standalone helper overrides at adapter construction
    assert job["probe_binding"]["nonce"] in message
    assert all(
        completed["last_probe_delivery_ack"].get(key) == value
        for key, value in job["probe_binding"].items()
    )


def test_bluebubbles_alarm_owner_rejects_non_owner_target_or_profile(tmp_path: Path):
    _configure_poke_bluebubbles(tmp_path)
    guest = tmp_path / "guest"
    with pytest.raises(ValueError, match="exactly match"):
        install(root=str(guest), alarm_target="bluebubbles:not-the-owner", dry_run=True)

    from cron import scheduler
    bad_job = {
        "id": "tampered", "name": "tampered", "deliver": "bluebubbles:operator-guid",
        "delivery_profile": "guest",
    }
    token = scheduler.set_hermes_home_override(guest)
    try:
        error = scheduler._deliver_result(bad_job, "alarm")
    finally:
        scheduler.reset_hermes_home_override(token)
    assert error and "delivery_profile must be exactly 'poke'" in error


def test_cron_does_not_ack_wrong_probe_output_or_transport_metadata(monkeypatch, tmp_path: Path):
    from cron import scheduler

    root = tmp_path / "poke"
    install(root=str(root), alarm_target="telegram:operator")
    with use_cron_store(root):
        job = next(j for j in list_jobs(include_disabled=True) if j["name"] == ALARM_PROBE_NAME)
        monkeypatch.setattr(scheduler, "run_job", lambda *a, **k: (True, "out", "wrong", None))
        monkeypatch.setattr(scheduler, "save_job_output", lambda *a, **k: root / "out")
        monkeypatch.setattr(scheduler, "_deliver_result", lambda *a, **k: None)
        assert scheduler.run_one_job(job)
    cfg = ProactiveConfig(alarm_sink_configured=True, alarm_sink_type="hermes_cron",
                          alarm_sink_target="telegram:operator")
    assert not probe_alarm_sink_readiness(profile_home=root, config=cfg)["ready"]

    # A stale completion cannot erase a proof earned by the current generation.
    with use_cron_store(root):
        mark_job_run(job["id"], True, delivery_ack_metadata=job["probe_binding"])
        stale = dict(job["probe_binding"], generation="stale")
        mark_job_run(job["id"], True, delivery_ack_metadata=stale)
    assert probe_alarm_sink_readiness(profile_home=root, config=cfg)["ready"]


@pytest.mark.parametrize(
    ("success", "error", "delivery_error"),
    [
        (False, "probe failed", None),
        (True, None, None),  # successful run, but wrong output produced no ACK
        (False, "probe timed out", "transport timeout"),
    ],
    ids=["failure", "wrong-output", "timeout"],
)
def test_old_probe_non_ack_completion_after_new_ack_is_atomic_noop(
    tmp_path: Path, success: bool, error: str | None, delivery_error: str | None,
):
    root = tmp_path / "poke"
    install(root=str(root), alarm_target="telegram:first")
    with use_cron_store(root):
        old = next(j for j in list_jobs(include_disabled=True) if j["name"] == ALARM_PROBE_NAME)

    install(root=str(root), alarm_target="telegram:second")
    with use_cron_store(root):
        current = next(j for j in list_jobs(include_disabled=True) if j["name"] == ALARM_PROBE_NAME)
        mark_job_run(
            current["id"], True,
            delivery_ack_metadata=current["probe_binding"],
            probe_run_snapshot=current["probe_binding"],
        )
        before = copy.deepcopy(next(
            j for j in list_jobs(include_disabled=True) if j["id"] == current["id"]
        ))
        mark_job_run(
            current["id"], success, error, delivery_error=delivery_error,
            probe_run_snapshot=old["probe_binding"],
        )
        after = next(j for j in list_jobs(include_disabled=True) if j["id"] == current["id"])

    # Rejection is total: even current-generation claims/counters/schedule are
    # not released or recomputed by the stale physical run.
    assert after == before


@pytest.mark.parametrize(
    ("success", "error", "delivery_error"),
    [
        (False, "probe failed", None),
        (True, None, None),
        (False, "probe timed out", "transport timeout"),
    ],
    ids=["failure", "wrong-output", "timeout"],
)
def test_new_ack_after_old_probe_non_ack_completion_wins(
    tmp_path: Path, success: bool, error: str | None, delivery_error: str | None,
):
    root = tmp_path / "poke"
    install(root=str(root), alarm_target="telegram:first")
    with use_cron_store(root):
        old = next(j for j in list_jobs(include_disabled=True) if j["name"] == ALARM_PROBE_NAME)

    install(root=str(root), alarm_target="telegram:second")
    with use_cron_store(root):
        current = next(j for j in list_jobs(include_disabled=True) if j["name"] == ALARM_PROBE_NAME)
        pristine = copy.deepcopy(current)
        mark_job_run(
            current["id"], success, error, delivery_error=delivery_error,
            probe_run_snapshot=old["probe_binding"],
        )
        assert next(
            j for j in list_jobs(include_disabled=True) if j["id"] == current["id"]
        ) == pristine
        mark_job_run(
            current["id"], True,
            delivery_ack_metadata=current["probe_binding"],
            probe_run_snapshot=current["probe_binding"],
        )
        completed = next(j for j in list_jobs(include_disabled=True) if j["id"] == current["id"])

    assert completed["last_status"] == "ok"
    assert completed["last_probe_delivery_ack"]["generation"] == current["probe_binding"]["generation"]
