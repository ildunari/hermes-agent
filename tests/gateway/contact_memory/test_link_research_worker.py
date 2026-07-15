from __future__ import annotations

from pathlib import Path
import sqlite3

from gateway.contact_memory.link_research import (
    LinkResearchProvider,
    LinkResearchRequest,
    LinkResearchResult,
)
from gateway.contact_memory.link_research_worker import process_one_link_job
from gateway.contact_memory.live_ingress import persist_live_communication_ingress
from gateway.contact_memory.private_link_queue import PrivateLinkResearchQueue, queue_path
from gateway.contact_memory.schema import CommunicationEnrichmentState
from gateway.contact_memory.store import ContactMemoryStore
from gateway.platforms.base import CommunicationIngressEnvelope

_SECRET = b"phase-e-v4-live-worker-test-key"


def _projected_topics(store: ContactMemoryStore) -> list[str]:
    with sqlite3.connect(store.path) as con:
        return [str(row[0]) for row in con.execute(
            "SELECT topic_text FROM interest_event "
            "WHERE origin_communication_event_id IS NOT NULL AND active=1 "
            "ORDER BY topic_text,event_id"
        )]


class _FakeProvider(LinkResearchProvider):
    def research(self, request: LinkResearchRequest) -> LinkResearchResult:
        return LinkResearchResult(
            evidence_id=request.evidence_id,
            status="ok",
            ontology_ids=("training.strength",),
            source_quality="established",
            recency_band="recent",
            support_ids=("a" * 64,),
            provider_commitment="b" * 64,
        )


class _StatusProvider(LinkResearchProvider):
    def __init__(self, status: str) -> None:
        self.status = status

    def research(self, request: LinkResearchRequest) -> LinkResearchResult:
        return LinkResearchResult(evidence_id=request.evidence_id, status=self.status)


class _SequenceProvider(LinkResearchProvider):
    def __init__(self, results: list[tuple[tuple[str, ...], tuple[tuple[str, str], ...]]]) -> None:
        self.results = iter(results)

    def research(self, request: LinkResearchRequest) -> LinkResearchResult:
        ontology_ids, public_entities = next(self.results)
        return LinkResearchResult(
            evidence_id=request.evidence_id, status="ok", ontology_ids=ontology_ids,
            public_entities=public_entities, source_quality="established",
            recency_band="recent", support_ids=("a" * 64,), provider_commitment="b" * 64,
        )


class _RetractingProvider(_FakeProvider):
    def __init__(self, queue: PrivateLinkResearchQueue, event_id: str) -> None:
        self.queue = queue
        self.event_id = event_id

    def research(self, request: LinkResearchRequest) -> LinkResearchResult:
        self.queue.retract_event(self.event_id, now=2.0)
        return super().research(request)


def _envelope(
    source_message_id: str = "live-link-one",
    url: str = "https://example.com/gym",
) -> CommunicationIngressEnvelope:
    return CommunicationIngressEnvelope(
        version=1,
        source_message_id=source_message_id,
        received_at=1_720_000_001.0,
        occurred_at=1_720_000_000.0,
        timestamp_source="source",
        chat_type="dm",
        direction="inbound",
        sender_identity="guest@example.test",
        visible_text=f"look {url}",
        visible_urls=(url,),
        event_kind="link_share",
    )


def _reaction(source_message_id: str, *, remove: bool) -> CommunicationIngressEnvelope:
    return CommunicationIngressEnvelope(
        version=1, source_message_id=source_message_id,
        received_at=1_720_000_010.0, occurred_at=1_720_000_009.0,
        timestamp_source="source", chat_type="dm", direction="outbound",
        sender_identity="owner", visible_text="", visible_urls=(),
        event_kind="reaction_remove" if remove else "reaction_add",
        reaction_target="live-link-one", reaction_kind="love",
    )


def test_disabled_live_research_does_not_retain_exact_url_sidecar(tmp_path: Path) -> None:
    root = tmp_path / "contact-memory"
    result = persist_live_communication_ingress(
        root=root, contact_id="stephen-lucier", principal="guest",
        envelopes=(_envelope(),), secret=_SECRET,
    )
    assert result.queued_links == 0
    assert not queue_path(root, "stephen-lucier").exists()


def test_live_ingress_queue_worker_enriches_and_projects_without_reply_surface(tmp_path: Path) -> None:
    root = tmp_path / "contact-memory"
    ingress = persist_live_communication_ingress(
        root=root,
        contact_id="stephen-lucier",
        principal="guest",
        envelopes=(_envelope(),),
        secret=_SECRET, enqueue_link_research=True,
    )
    assert ingress.queued_links == 1

    queue = PrivateLinkResearchQueue(root, "stephen-lucier")
    assert queue.pending_count() == 1
    assert process_one_link_job(
        root=root, contact_id="stephen-lucier", provider=_FakeProvider(), now=1_720_000_002.0,
    )["projected_events"] == 0
    second = persist_live_communication_ingress(
        root=root, contact_id="stephen-lucier", principal="guest",
        envelopes=(_envelope("live-link-two"),), secret=_SECRET,
        enqueue_link_research=True,
    )

    result = process_one_link_job(
        root=root,
        contact_id="stephen-lucier",
        provider=_FakeProvider(),
        now=1_720_000_003.0,
    )
    assert result == {"processed": True, "status": "ok", "projected_events": 2}
    assert queue.pending_count() == 0

    store = ContactMemoryStore(root, "stephen-lucier")
    bundle = store.get_communication_bundle(ingress.event_ids[0])
    assert bundle is not None
    assert bundle.urls[0].enrichment_state is CommunicationEnrichmentState.REVIEWED
    assert ingress.event_ids + second.event_ids
    assert _projected_topics(store) == [
        "bodybuilding and strength training", "bodybuilding and strength training",
    ]


def test_one_time_share_research_stays_neutral(tmp_path: Path) -> None:
    root = tmp_path / "contact-memory"
    ingress = persist_live_communication_ingress(
        root=root, contact_id="stephen-lucier", principal="guest",
        envelopes=(_envelope(),), secret=_SECRET, enqueue_link_research=True,
    )

    result = process_one_link_job(
        root=root, contact_id="stephen-lucier", provider=_FakeProvider(),
        now=1_720_000_003.0,
    )

    assert result == {"processed": True, "status": "ok", "projected_events": 0}
    store = ContactMemoryStore(root, "stephen-lucier")
    assert _projected_topics(store) == []
    bundle = store.get_communication_bundle(ingress.event_ids[0])
    assert bundle is not None
    assert bundle.urls[0].enrichment_state is CommunicationEnrichmentState.REVIEWED


def test_repeated_share_research_projects_only_after_qualifying_recurrence(tmp_path: Path) -> None:
    root = tmp_path / "contact-memory"
    first = persist_live_communication_ingress(
        root=root, contact_id="stephen-lucier", principal="guest",
        envelopes=(_envelope("live-link-one"),), secret=_SECRET, enqueue_link_research=True,
    )
    assert process_one_link_job(
        root=root, contact_id="stephen-lucier", provider=_FakeProvider(), now=10.0,
    )["projected_events"] == 0
    second = persist_live_communication_ingress(
        root=root, contact_id="stephen-lucier", principal="guest",
        envelopes=(_envelope("live-link-two"),), secret=_SECRET, enqueue_link_research=True,
    )

    result = process_one_link_job(
        root=root, contact_id="stephen-lucier", provider=_FakeProvider(), now=11.0,
    )

    assert result["projected_events"] == 2
    assert set(first.event_ids + second.event_ids)
    assert _projected_topics(ContactMemoryStore(root, "stephen-lucier")) == [
        "bodybuilding and strength training",
        "bodybuilding and strength training",
    ]


def test_distinct_urls_resolving_to_same_topic_are_recurrent(tmp_path: Path) -> None:
    root = tmp_path / "contact-memory"
    results = []
    for index, url in enumerate(("https://example.com/strength", "https://other.test/lifting"), 1):
        persist_live_communication_ingress(
            root=root, contact_id="stephen-lucier", principal="guest",
            envelopes=(_envelope(f"distinct-topic-{index}", url),), secret=_SECRET,
            enqueue_link_research=True,
        )
        results.append(process_one_link_job(
            root=root, contact_id="stephen-lucier", provider=_FakeProvider(), now=float(index),
        ))
    assert results[-1]["projected_events"] == 2
    assert len(_projected_topics(ContactMemoryStore(root, "stephen-lucier"))) == 2


def test_distinct_urls_resolving_to_same_entity_are_recurrent(tmp_path: Path) -> None:
    root = tmp_path / "contact-memory"
    provider = _SequenceProvider([
        ((), (("music_artist", "Lady Gaga"),)),
        ((), (("music_artist", "Lady Gaga"),)),
    ])
    results = []
    for index, url in enumerate(("https://example.com/gaga", "https://other.test/concert"), 1):
        persist_live_communication_ingress(
            root=root, contact_id="stephen-lucier", principal="guest",
            envelopes=(_envelope(f"distinct-entity-{index}", url),), secret=_SECRET,
            enqueue_link_research=True,
        )
        results.append(process_one_link_job(
            root=root, contact_id="stephen-lucier", provider=provider, now=float(index),
        ))
    assert results[-1]["projected_events"] == 2
    store = ContactMemoryStore(root, "stephen-lucier")
    with sqlite3.connect(store.path) as con:
        assert con.execute(
            "SELECT count(*) FROM projected_entity WHERE active=1 AND canonical_label='Lady Gaga'"
        ).fetchone()[0] == 2


def test_transient_retry_and_preprocessing_retraction_remain_neutral(tmp_path: Path) -> None:
    root = tmp_path / "contact-memory"
    ingress = persist_live_communication_ingress(
        root=root, contact_id="stephen-lucier", principal="guest",
        envelopes=(_envelope(),), secret=_SECRET, enqueue_link_research=True,
    )
    queue = PrivateLinkResearchQueue(root, "stephen-lucier")

    retry = process_one_link_job(
        root=root, contact_id="stephen-lucier", provider=_StatusProvider("fetch_timeout"),
        now=20.0,
    )
    assert retry == {"processed": True, "status": "fetch_timeout"}
    assert queue.stats() == {"retry_wait": 1}
    assert queue.retract_event(ingress.event_ids[0], now=21.0) == 1
    assert process_one_link_job(
        root=root, contact_id="stephen-lucier", provider=_FakeProvider(), now=22.0,
    ) == {"processed": False, "status": "empty"}
    assert _projected_topics(ContactMemoryStore(root, "stephen-lucier")) == []


def test_permanent_failure_is_not_reused_as_successful_recurrence(tmp_path: Path) -> None:
    root = tmp_path / "contact-memory"
    persist_live_communication_ingress(
        root=root, contact_id="stephen-lucier", principal="guest",
        envelopes=(_envelope("live-link-one"),), secret=_SECRET,
        enqueue_link_research=True,
    )
    assert process_one_link_job(
        root=root, contact_id="stephen-lucier", provider=_StatusProvider("blocked_action"), now=1.0,
    )["status"] == "blocked_action"
    persist_live_communication_ingress(
        root=root, contact_id="stephen-lucier", principal="guest",
        envelopes=(_envelope("live-link-two"),), secret=_SECRET,
        enqueue_link_research=True,
    )
    result = process_one_link_job(
        root=root, contact_id="stephen-lucier", provider=_FakeProvider(), now=2.0,
    )
    assert result["projected_events"] == 0
    assert _projected_topics(ContactMemoryStore(root, "stephen-lucier")) == []


def test_inflight_retraction_invalidates_claim_before_canonical_mutation(tmp_path: Path) -> None:
    root = tmp_path / "contact-memory"
    ingress = persist_live_communication_ingress(
        root=root, contact_id="stephen-lucier", principal="guest",
        envelopes=(_envelope(),), secret=_SECRET, enqueue_link_research=True,
    )
    queue = PrivateLinkResearchQueue(root, "stephen-lucier")
    result = process_one_link_job(
        root=root, contact_id="stephen-lucier",
        provider=_RetractingProvider(queue, ingress.event_ids[0]), now=1.0,
    )
    assert result == {"processed": True, "status": "stale_claim"}
    assert _projected_topics(ContactMemoryStore(root, "stephen-lucier")) == []


def test_sidecar_engagement_score_cannot_bypass_canonical_share_gate(tmp_path: Path) -> None:
    root = tmp_path / "contact-memory"
    ingress = persist_live_communication_ingress(
        root=root, contact_id="stephen-lucier", principal="guest",
        envelopes=(_envelope(),), secret=_SECRET, enqueue_link_research=True,
    )
    queue = PrivateLinkResearchQueue(root, "stephen-lucier")
    assert queue.engage_event(ingress.event_ids[0], score=4, now=1_720_000_010.0) == 1
    try:
        process_one_link_job(
            root=root, contact_id="stephen-lucier", provider=_FakeProvider(),
            now=1_720_000_011.0,
        )
    except ValueError as exc:
        assert str(exc) == "one-time shares cannot directly create interest evidence"
    else:
        raise AssertionError("sidecar-only score bypassed the canonical share gate")
    store = ContactMemoryStore(root, "stephen-lucier")
    assert _projected_topics(store) == []


def test_worker_restart_reclaims_expired_claim_and_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "contact-memory"
    ingress = persist_live_communication_ingress(
        root=root,
        contact_id="stephen-lucier",
        principal="guest",
        envelopes=(_envelope(),),
        secret=_SECRET, enqueue_link_research=True,
    )
    queue = PrivateLinkResearchQueue(root, "stephen-lucier")
    claim = queue.claim_next(now=100.0, lease_seconds=10.0)
    assert claim is not None

    result = process_one_link_job(
        root=root,
        contact_id="stephen-lucier",
        provider=_FakeProvider(),
        now=111.0,
    )
    assert result["status"] == "ok"
    assert process_one_link_job(
        root=root,
        contact_id="stephen-lucier",
        provider=_FakeProvider(),
        now=112.0,
    ) == {"processed": False, "status": "empty"}
    assert ingress.event_ids


def test_recurrent_topic_does_not_admit_one_time_entity(tmp_path: Path) -> None:
    root = tmp_path / "contact-memory"
    provider = _SequenceProvider([
        (("training.strength",), (("music_artist", "Lady Gaga"),)),
        (("training.strength",), ()),
    ])
    for index in (1, 2):
        persist_live_communication_ingress(
            root=root, contact_id="stephen-lucier", principal="guest",
            envelopes=(_envelope(f"semantic-{index}"),), secret=_SECRET,
            enqueue_link_research=True,
        )
        process_one_link_job(
            root=root, contact_id="stephen-lucier", provider=provider, now=float(index),
        )
    store = ContactMemoryStore(root, "stephen-lucier")
    assert len(_projected_topics(store)) == 2
    with sqlite3.connect(store.path) as con:
        assert con.execute("SELECT count(*) FROM projected_entity WHERE active=1").fetchone()[0] == 0


def test_entity_only_owner_engagement_is_admitted(tmp_path: Path) -> None:
    root = tmp_path / "contact-memory"
    ingress = persist_live_communication_ingress(
        root=root, contact_id="stephen-lucier", principal="guest",
        envelopes=(_envelope(),), secret=_SECRET, enqueue_link_research=True,
    )
    reaction = persist_live_communication_ingress(
        root=root, contact_id="stephen-lucier", principal="guest",
        envelopes=(_reaction("owner-love", remove=False),), secret=_SECRET,
        enqueue_link_research=True,
    )
    result = process_one_link_job(
        root=root, contact_id="stephen-lucier",
        provider=_SequenceProvider([((), (("music_artist", "Lady Gaga"),))]),
        now=1_720_000_011.0,
    )
    assert result["projected_events"] == 1
    store = ContactMemoryStore(root, "stephen-lucier")
    bundle = store.get_communication_bundle(reaction.event_ids[0])
    assert bundle is not None and bundle.event.actor_role.value == "counterpart"
    with sqlite3.connect(store.path) as con:
        assert con.execute("SELECT canonical_label FROM projected_entity WHERE active=1").fetchone()[0] == "Lady Gaga"
    assert ingress.event_ids


def test_bluebubbles_normalization_persists_owner_reaction_without_dispatch(tmp_path: Path) -> None:
    from gateway.config import PlatformConfig
    from gateway.platforms.bluebubbles import BlueBubblesAdapter

    adapter = BlueBubblesAdapter(PlatformConfig(enabled=True, extra={
        "server_url": "http://localhost:1234", "password": "secret",
    }))
    shared = adapter._normalize_ingress_record({
        "guid": "bb-contact-share", "isFromMe": False,
        "text": "https://example.com/gym", "sender": "guest@example.test",
        "chatIdentifier": "guest@example.test", "dateCreated": 1_720_000_000,
    }, received_at=1_720_000_001.0)
    owner_reaction = adapter._normalize_ingress_record({
        "guid": "bb-owner-love", "isFromMe": True, "text": "",
        "chatIdentifier": "guest@example.test", "associatedMessageType": 2000,
        "associatedMessageGuid": "p:0/bb-contact-share", "dateCreated": 1_720_000_002,
    }, received_at=1_720_000_003.0)
    assert shared.direction == "inbound" and owner_reaction.direction == "outbound"
    root = tmp_path / "contact-memory"
    share_result = persist_live_communication_ingress(
        root=root, contact_id="stephen-lucier", principal="guest", envelopes=(shared,),
        secret=_SECRET, enqueue_link_research=True,
    )
    reaction_result = persist_live_communication_ingress(
        root=root, contact_id="stephen-lucier", principal="guest",
        envelopes=(owner_reaction,), secret=_SECRET, enqueue_link_research=True,
    )
    queue = PrivateLinkResearchQueue(root, "stephen-lucier")
    state = queue.event_state(share_result.event_ids[0])
    assert state is not None and state[:2] == ("pending", 4)
    bundle = ContactMemoryStore(root, "stephen-lucier").get_communication_bundle(
        reaction_result.event_ids[0]
    )
    assert bundle is not None
    assert bundle.event.actor_role.value == "counterpart"
    assert bundle.relations[0].target_actor_role is not None
    assert bundle.relations[0].target_actor_role.value == "contact"


def test_stale_third_completion_preserves_prior_recurrent_projections(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "contact-memory"
    prior_event_ids = []
    for index in (1, 2):
        ingress = persist_live_communication_ingress(
            root=root, contact_id="stephen-lucier", principal="guest",
            envelopes=(_envelope(f"stale-{index}"),), secret=_SECRET,
            enqueue_link_research=True,
        )
        prior_event_ids.extend(ingress.event_ids)
        process_one_link_job(
            root=root, contact_id="stephen-lucier", provider=_FakeProvider(), now=float(index),
        )

    third = persist_live_communication_ingress(
        root=root, contact_id="stephen-lucier", principal="guest",
        envelopes=(_envelope("stale-3"),), secret=_SECRET, enqueue_link_research=True,
    )

    def lose_lease(*args, **kwargs):
        raise ValueError("claim token is stale or does not own the job")

    monkeypatch.setattr(PrivateLinkResearchQueue, "complete", lose_lease)
    assert process_one_link_job(
        root=root, contact_id="stephen-lucier", provider=_FakeProvider(), now=3.0,
    )["status"] == "stale_claim"
    store = ContactMemoryStore(root, "stephen-lucier")
    assert len(_projected_topics(store)) == 2
    third_bundle = store.get_communication_bundle(third.event_ids[0])
    assert third_bundle is not None
    assert third_bundle.urls[0].enrichment_state is CommunicationEnrichmentState.PENDING
    with sqlite3.connect(store.path) as con:
        assert con.execute(
            "SELECT count(*) FROM communication_projection_receipt WHERE active=1"
        ).fetchone()[0] == 2
        assert {
            str(row[0]) for row in con.execute(
                "SELECT communication_event_id FROM communication_projection_receipt WHERE active=1"
            )
        } == set(prior_event_ids)
