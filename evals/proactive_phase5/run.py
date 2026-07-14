#!/usr/bin/env python3
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.contact_memory.imessage_bootstrap import AuthoritativeSource, validate_semantic_items
from gateway.contact_memory.import_contacts import _classify
from gateway.contact_memory.schema import FactStatus
from gateway.platforms.base import SendResult
from gateway.proactive_scheduler import ContactRoute, ProactiveConfig, ProactiveMode, ProactiveScheduler, ProactiveStateStore
from gateway.proactive_status import health_snapshot
from gateway.proactive_transport import BlueBubblesProactiveDelivery, deliver_prepared_exactly_once


ALLOW = (("poke", "kosta-owner", "owner"), ("guest", "stephen-lucier", "guest"))
ROUTE = ContactRoute("kosta-owner", "poke", "UTC", "owner", "dm", "iMessage;-;one", "owner@example.com", "parent")


class Adapter:
    platform = SimpleNamespace(value="bluebubbles")

    def __init__(self, result):
        self.result = result

    async def resolve_authenticated_existing_dm(self, chat_id, user_id):
        return (chat_id, "authenticated")

    async def send(self, chat_id, text, **kwargs):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def config():
    lane = {"provider": "openai-codex", "model": "gpt-5.6-sol", "reasoning_effort": "medium", "fallback": False}
    proactive = {
        "enabled": True, "mode": "live", "dry_run": False,
        "transport_owner_profile": "poke",
        "alarm_sink": {"configured": True, "type": "eval"},
        "allowed_contacts": [
            {"profile": "poke", "contact_id": "kosta-owner", "principal": "owner"},
            {"profile": "guest", "contact_id": "stephen-lucier", "principal": "guest"},
        ],
        "active_start": "00:00", "active_end": "23:59",
        "compose_model": {"provider": "openai-codex", "model": "gpt-5.6-sol", "reasoning_effort": "low", "fallback": False},
    }
    return {"auxiliary": {"proactive_gate": lane, "proactive_semantic": lane}, "agent": {"proactive": proactive}}


async def transport_eval(root: Path, result) -> str:
    now = time.time()
    state = ProactiveStateStore(root / "state.db")
    route = {"platform": "bluebubbles", "chat_type": "dm", "chat_id": ROUTE.chat_id,
             "user_id": ROUTE.user_id, "session_id": ROUTE.session_id}
    for index in range(5):
        state.register_inbound(profile="poke", contact_id="kosta-owner", route=route,
                               timezone_name="UTC", source_id=f"eval-{index}", received_at=now-10+index)
    scheduler = ProactiveScheduler(
        state_db_path=root / "state.db", profile_home=root, profile_name="poke",
        config=ProactiveConfig(enabled=True, dry_run=False, mode=ProactiveMode.LIVE,
                               allowed_contacts=ALLOW, active_start="00:00", active_end="23:59",
                               alarm_sink_configured=True),
        ownership_registry_path=root / "ownership.db",
    )
    slot = scheduler.arm_slot(ROUTE, kind="checkin", fire_at=now-1, now=now-2)
    claim = scheduler.claim_due(worker_id="eval", now=now)[0]
    scheduler.ownership_registry.acquire_transport("eval-runner", "eval-adapter", now=now)
    delivery = BlueBubblesProactiveDelivery(
        Adapter(result), ownership_registry=scheduler.ownership_registry,
        runner_id="eval-runner", adapter_id="eval-adapter",
        participant_identities={("poke", "kosta-owner"): frozenset({"owner@example.com"})},
        scheduler=scheduler,
    )
    state_name = await deliver_prepared_exactly_once(
        scheduler=scheduler, delivery=delivery, route=ROUTE, claim=claim,
        text="private eval payload", correlation_id=slot, now=now,
    )
    return state_name


def main() -> int:
    value=json.loads(Path(__file__).with_name("cases.json").read_text())
    cases=value.get("cases",[]); ids=[case.get("id") for case in cases]
    required={"attribution","privacy","gate","transport","diagnostics"}
    assert value.get("schema")==1 and len(ids)==len(set(ids)) and all(ids)
    assert required <= {case.get("suite") for case in cases}
    passed=[]
    with tempfile.TemporaryDirectory(prefix="proactive-eval-") as temporary:
        root=Path(temporary)
        for case in cases:
            case_id=case["id"]
            if case_id=="cross-speaker-fact":
                source=AuthoritativeSource("kosta-owner",hashlib.sha256(b"fact").hexdigest(),"fact")
                try:
                    validate_semantic_items([{"kind":"fact","source_key":"row","author":"kosta-owner","text":"fact","confidence":.9}],
                                            subject="stephen-lucier",sources={"row":source})
                except ValueError:
                    passed.append(case_id)
            elif case_id=="guest-sensitive":
                fact,_=_classify({"source_id":"eval","subject_id":"stephen-lucier","text":"private",
                                  "sensitive":True,"audience":"guest_ok","guest_reviewed":False,
                                  "confidence":.9,"trust":.9},"stephen-lucier")
                if fact.status is FactStatus.QUARANTINED:
                    passed.append(case_id)
            elif case_id=="prompt-injection":
                from gateway.proactive_fetch import ProactiveCandidate
                try:
                    ProactiveCandidate.parse({"topic":"security","concrete_item":case["candidate"],
                                              "why_now":"today","source_url":"https://example.com",
                                              "freshness_ts":time.time()})
                except Exception:
                    passed.append(case_id)
                else:
                    raise AssertionError("instruction-like candidate was accepted")
            elif case_id=="timeout-unknown":
                if asyncio.run(transport_eval(root/"timeout",TimeoutError("timeout")))=="delivery_unknown":
                    passed.append(case_id)
            elif case_id=="connect-retry":
                if asyncio.run(transport_eval(root/"retry",SendResult(False,error="connect",retryable=True)))=="retry_wait":
                    passed.append(case_id)
            elif case_id=="stale-heartbeat":
                status_root=root/"status"; raw=config(); now=time.time()
                scheduler=ProactiveScheduler(state_db_path=status_root/"state.db",profile_home=status_root,
                                             profile_name="poke",config=ProactiveConfig.from_mapping(raw),
                                             ownership_registry_path=status_root/"ownership.db")
                scheduler.record_health("watcher",{"adapter_ready":True},now=now-float(case["age_seconds"]))
                status=health_snapshot(profile_home=status_root,profile="poke",config=raw,now=now,
                                       adapter_ready=True,cron_fresh=True)
                if case["expect"] in status["reasons"]:
                    passed.append(case_id)
    assert passed==ids, {"passed":passed,"expected":ids}
    print(json.dumps({"cases":len(cases),"executed":len(passed),"suites":sorted(required),"valid":True},sort_keys=True))
    return 0
if __name__ == "__main__": raise SystemExit(main())
