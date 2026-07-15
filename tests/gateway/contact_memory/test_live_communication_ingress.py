"""Phase C tests for immutable BlueBubbles canonical live ingress."""
from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import FrozenInstanceError, replace
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from gateway.contact_memory.live_ingress import (
    _event_identity,
    _source_identity,
    load_or_create_communication_key,
    persist_live_communication_ingress,
)
from gateway.contact_memory.schema import (
    CommunicationActorRole,
    CommunicationDirection,
    CommunicationEvent,
    CommunicationKind,
    CommunicationLifecycle,
    CommunicationPrivacy,
    CommunicationReactionSubtype,
    CommunicationRelationType,
    CommunicationEnrichmentState,
)
from gateway.contact_memory.store import ContactMemoryStore
from gateway.platforms.base import CommunicationIngressEnvelope
from gateway.platforms.bluebubbles import BlueBubblesAdapter


_SECRET = b"phase-c-synthetic-key-material"


def _adapter(monkeypatch) -> BlueBubblesAdapter:
    monkeypatch.setenv("BLUEBUBBLES_SERVER_URL", "http://localhost:1234")
    monkeypatch.setenv("BLUEBUBBLES_PASSWORD", "secret")
    return BlueBubblesAdapter(PlatformConfig(enabled=True, extra={
        "server_url": "http://localhost:1234",
        "password": "secret",
        "text_batch_delay_seconds": 0,
        "send_read_receipts": False,
    }))


def _record(guid: str, text: str = "hello", **overrides):
    value = {
        "guid": guid,
        "text": text,
        "dateCreated": "1720962000000",
        "handle": {"address": "guest@example.com"},
        "isFromMe": "false",
        "isGroup": "false",
        "chats": [{"guid": "iMessage;-;guest@example.com"}],
    }
    value.update(overrides)
    return value


def test_envelope_is_frozen_and_versioned(monkeypatch):
    envelope = _adapter(monkeypatch)._normalize_ingress_record(_record("msg-1"), received_at=1720962001.0)

    assert envelope.version == 1
    assert envelope.source_message_id == "msg-1"
    assert envelope.sender_identity == "guest@example.com"
    assert envelope.occurred_at == 1720962000.0
    assert envelope.timestamp_source == "dateCreated"
    with pytest.raises(FrozenInstanceError):
        envelope.visible_text = "forged"  # type: ignore[misc]


def test_payload_wrappers_and_multi_record_lists_are_not_truncated(monkeypatch):
    adapter = _adapter(monkeypatch)

    with pytest.raises(ValueError, match="data list members must be message records"):
        adapter._extract_payload_records({"data": [_record("a"), "bad", _record("b")]})
    assert [item["guid"] for item in adapter._extract_payload_records({"data": _record("c")})] == ["c"]
    assert [item["guid"] for item in adapter._extract_payload_records({"message": _record("d")})] == ["d"]
    assert [item["guid"] for item in adapter._extract_payload_records(_record("e"))] == ["e"]


def test_alias_boolean_reaction_and_timestamp_normalization(monkeypatch):
    adapter = _adapter(monkeypatch)
    envelope = adapter._normalize_ingress_record({
        "messageGuid": "reaction-1",
        "body": "ignored reaction body",
        "date": 1720962000,
        "sender": "guest@example.com",
        "fromMe": "0",
        "is_group": "no",
        "chat_guid": "iMessage;-;guest@example.com",
        "associated_message_type": "2003",
        "associated_message_guid": "p:9/target-guid-1",
    }, received_at=1720962002.0)

    assert envelope.direction == "inbound"
    assert envelope.chat_type == "dm"
    assert envelope.event_kind == "reaction_add"
    assert envelope.reaction_kind == "laugh"
    assert envelope.reaction_target == "target-guid-1"
    assert envelope.occurred_at == 1720962000.0


@pytest.mark.parametrize(
    ("field", "alias", "zero_expected", "one_expected"),
    [
        ("isFromMe", "isFromMe", "inbound", "outbound"),
        ("isFromMe", "fromMe", "inbound", "outbound"),
        ("isFromMe", "is_from_me", "inbound", "outbound"),
        ("isGroup", "isGroup", "dm", "group"),
        ("isGroup", "is_group", "dm", "group"),
    ],
)
def test_exact_integer_boolean_aliases_are_accepted(
    monkeypatch, field, alias, zero_expected, one_expected
):
    adapter = _adapter(monkeypatch)
    zero = _record(f"{field}-zero")
    zero.pop(field)
    zero[alias] = 0
    one = _record(f"{field}-one")
    one.pop(field)
    one[alias] = 1

    zero_envelope = adapter._normalize_ingress_record(zero, received_at=1.0)
    one_envelope = adapter._normalize_ingress_record(one, received_at=1.0)

    attribute = "direction" if field == "isFromMe" else "chat_type"
    assert getattr(zero_envelope, attribute) == zero_expected
    assert getattr(one_envelope, attribute) == one_expected


@pytest.mark.parametrize("field", ["isFromMe", "isGroup"])
@pytest.mark.parametrize("malformed", [2, -1, 0.0, 1.0, "maybe", [], {}])
def test_malformed_boolean_values_fail_closed(monkeypatch, field, malformed):
    adapter = _adapter(monkeypatch)

    with pytest.raises(ValueError, match=f"{field} is not a boolean"):
        adapter._normalize_ingress_record(
            _record(f"malformed-{field}", **{field: malformed}), received_at=1.0
        )


def test_unsupported_associated_type_and_conflicting_reply_aliases_fail_closed(monkeypatch):
    adapter = _adapter(monkeypatch)

    with pytest.raises(ValueError, match="unsupported associated message type"):
        adapter._normalize_ingress_record(_record(
            "unsupported-associated", associatedMessageType="42",
            associatedMessageGuid="p:0/target-guid-1",
        ), received_at=1.0)
    with pytest.raises(ValueError, match="conflicting reply targets"):
        adapter._normalize_ingress_record(_record(
            "conflicting-reply", threadOriginatorGuid="p:0/target-guid-1",
            replyToGuid="p:0/other-guid-2",
        ), received_at=1.0)


def test_direct_chat_identifier_remains_transport_identity_fallback(monkeypatch):
    adapter = _adapter(monkeypatch)
    record = _record("identifier-fallback")
    record.pop("handle")
    record["chatIdentifier"] = "guest@example.com"

    envelope = adapter._normalize_ingress_record(record, received_at=1.0)

    assert envelope.sender_identity == "guest@example.com"


def test_visible_urls_and_safe_attachment_descriptors_are_frozen_before_download(monkeypatch):
    adapter = _adapter(monkeypatch)
    envelope = adapter._normalize_ingress_record(_record(
        "attachment-1",
        "look https://Example.com/path?token=secret",
        attachments=[
            {"guid": "att-1", "mimeType": "image/jpeg", "uti": "public.jpeg", "totalBytes": "2048", "transferName": "../../secret.jpg"},
            {"id": "att-2", "mime_type": "application/pdf", "size": 4096, "path": "/private/tmp/file"},
        ],
    ), received_at=1720962002.0)

    assert envelope.event_kind == "attachment_share"
    assert envelope.visible_urls == ("https://example.com/path?token=secret",)
    assert len(envelope.attachments) == 2
    assert envelope.attachments[0].source_attachment_id == "att-1"
    assert envelope.attachments[0].media_kind == "image"
    assert envelope.attachments[0].size_bytes == 2048
    assert not hasattr(envelope.attachments[0], "transfer_name")
    assert not hasattr(envelope.attachments[1], "path")


def test_persistence_uses_original_facts_and_store_uniqueness(tmp_path, monkeypatch):
    envelope = _adapter(monkeypatch)._normalize_ingress_record(
        _record("msg-original", "original https://example.com/watch?v=1"),
        received_at=1720962002.0,
    )
    root = tmp_path / "contact-memory"

    first = persist_live_communication_ingress(
        root=root,
        contact_id="stephen-lucier",
        principal="guest",
        envelopes=(envelope,),
        secret=_SECRET,
    )
    replay = persist_live_communication_ingress(
        root=root,
        contact_id="stephen-lucier",
        principal="guest",
        envelopes=(envelope,),
        secret=_SECRET,
    )

    assert first.inserted == 1 and first.deduplicated == 0
    assert replay.inserted == 0 and replay.deduplicated == 1
    store = ContactMemoryStore(root, "stephen-lucier")
    bundle = store.get_communication_bundle(first.event_ids[0])
    assert bundle is not None
    assert bundle.event.kind is CommunicationKind.LINK_SHARE
    assert bundle.event.text_present is True
    assert bundle.event.text_length == len("original https://example.com/watch?v=1")
    assert len(bundle.urls) == 1 and bundle.urls[0].domain == "example.com"
    with sqlite3.connect(store.path) as con:
        rows = " ".join(str(value) for row in con.execute("SELECT * FROM communication_event") for value in row)
    assert "original" not in rows and "https://" not in rows and "msg-original" not in rows


def test_reviewed_url_survives_pre_ack_replay_with_late_batch_relation(tmp_path, monkeypatch):
    adapter = _adapter(monkeypatch)
    root = tmp_path / "contact-memory"
    contact_id = "stephen-lucier"
    link = adapter._normalize_ingress_record(
        _record("review-race-link", "https://example.com/watch?v=1"), received_at=1.0
    )
    first = persist_live_communication_ingress(
        root=root, contact_id=contact_id, principal="guest",
        envelopes=(link,), secret=_SECRET,
    )
    store = ContactMemoryStore(root, contact_id)
    bundle = store.get_communication_bundle(first.event_ids[0])
    assert bundle is not None and len(bundle.urls) == 1
    reviewed = replace(
        bundle.urls[0], enrichment_state=CommunicationEnrichmentState.REVIEWED
    )
    store.enrich_communication_event(first.event_ids[0], urls=(reviewed,))

    followup = adapter._normalize_ingress_record(
        _record("review-race-followup", "what do you think?"), received_at=2.0
    )
    replay = persist_live_communication_ingress(
        root=root, contact_id=contact_id, principal="guest",
        envelopes=(link, followup), secret=_SECRET,
    )

    assert replay.event_ids[0] == first.event_ids[0]
    persisted = store.get_communication_bundle(first.event_ids[0])
    assert persisted is not None
    assert persisted.urls[0].enrichment_state is CommunicationEnrichmentState.REVIEWED
    assert any(
        relation.relation_type is CommunicationRelationType.BATCH_MEMBER_OF
        for relation in persisted.relations
    )


@pytest.mark.asyncio
async def test_gateway_recovers_fully_durable_ingress_instead_of_dropping_turn(
    tmp_path, monkeypatch
):
    from gateway.run import TrustedContactScope, _persist_authenticated_communication_ingress
    import gateway.contact_memory.live_ingress as ingress_module

    adapter = _adapter(monkeypatch)
    envelope = adapter._normalize_ingress_record(
        _record("durable-before-enrichment-failure"), received_at=1.0
    )
    real_persist = ingress_module.persist_live_communication_ingress

    def persist_then_fail(**kwargs):
        real_persist(**kwargs)
        raise ValueError("synthetic post-ingest enrichment failure")

    monkeypatch.setattr(ingress_module, "persist_live_communication_ingress", persist_then_fail)
    result = await _persist_authenticated_communication_ingress(
        trusted_scope=TrustedContactScope("guest", "stephen-lucier"),
        profile_home=tmp_path,
        source=SimpleNamespace(
            chat_type="dm", platform=SimpleNamespace(value="bluebubbles")
        ),
        event=SimpleNamespace(communication_ingress=(envelope,)),
    )

    assert len(result) == 1
    assert ContactMemoryStore(
        tmp_path / "contact-memory", "stephen-lucier"
    ).get_communication_event(result[0]) is not None


def test_batch_members_persist_in_order_without_collapsing(tmp_path, monkeypatch):
    adapter = _adapter(monkeypatch)
    envelopes = tuple(
        adapter._normalize_ingress_record(_record(f"batch-{index}", f"part {index}"), received_at=1720962000.0 + index)
        for index in range(3)
    )
    result = persist_live_communication_ingress(
        root=tmp_path / "contact-memory",
        contact_id="stephen-lucier",
        principal="guest",
        envelopes=envelopes,
        secret=_SECRET,
    )

    assert result.inserted == 3
    assert len(result.event_ids) == 3
    store = ContactMemoryStore(tmp_path / "contact-memory", "stephen-lucier")
    bundles = [store.get_communication_bundle(event_id) for event_id in result.event_ids]
    batch_targets = [
        next(relation.target_source_id for relation in bundle.relations if relation.relation_type is CommunicationRelationType.BATCH_MEMBER_OF)
        for bundle in bundles if bundle is not None
    ]
    assert len(batch_targets) == 3 and len(set(batch_targets)) == 1
    assert [bundle.event.occurred_at for bundle in bundles if bundle is not None] == sorted(
        bundle.event.occurred_at for bundle in bundles if bundle is not None
    )


def _persist_counterpart_target(root, contact_id: str, source_key: str) -> CommunicationEvent:
    source_id = _source_identity(_SECRET, source_key)
    event = CommunicationEvent(
        event_id=_event_identity(_SECRET, contact_id, source_key),
        platform="imessage",
        source_id=source_id,
        occurred_at=0.5,
        direction=CommunicationDirection.INBOUND,
        kind=CommunicationKind.TEXT,
        actor_role=CommunicationActorRole.COUNTERPART,
        privacy=CommunicationPrivacy.PRIVATE,
        text_present=False,
        text_length=0,
        provenance="synthetic-counterpart-v1",
    )
    ContactMemoryStore(root, contact_id).ingest_communication_event(event)
    return event


def test_all_twelve_tapback_codes_normalize_and_persist_through_retraction(
    tmp_path, monkeypatch
):
    adapter = _adapter(monkeypatch)
    root = tmp_path / "contact-memory"
    contact_id = "stephen-lucier"
    expected = {
        0: CommunicationReactionSubtype.LOVE,
        1: CommunicationReactionSubtype.LIKE,
        2: CommunicationReactionSubtype.DISLIKE,
        3: CommunicationReactionSubtype.LAUGH,
        4: CommunicationReactionSubtype.EMPHASIS,
        5: CommunicationReactionSubtype.QUESTION,
    }
    add_ids = []

    for subtype, expected_subtype in expected.items():
        target = f"target-guid-{subtype}"
        _persist_counterpart_target(root, contact_id, target)
        add = adapter._normalize_ingress_record(_record(
            f"tap-add-{subtype}", "", associatedMessageType=2000 + subtype,
            associatedMessageGuid=f"bp:4/{target}",
        ), received_at=2.0 + subtype)
        remove = adapter._normalize_ingress_record(_record(
            f"tap-remove-{subtype}", "", associatedMessageType=str(3000 + subtype),
            associatedMessageGuid=f"p:4/{target}",
        ), received_at=20.0 + subtype)
        initial = persist_live_communication_ingress(
            root=root, contact_id=contact_id, principal="guest",
            envelopes=(add,), secret=_SECRET,
        )
        removed = persist_live_communication_ingress(
            root=root, contact_id=contact_id, principal="guest",
            envelopes=(remove,), secret=_SECRET,
        )
        add_ids.append(initial.event_ids[0])
        remove_event = ContactMemoryStore(root, contact_id).get_communication_event(
            removed.event_ids[0]
        )
        assert remove_event is not None
        assert remove_event.kind is CommunicationKind.REACTION_REMOVE
        assert remove_event.reaction_subtype is expected_subtype

    store = ContactMemoryStore(root, contact_id)
    add_events = [store.get_communication_event(event_id) for event_id in add_ids]
    assert all(event is not None for event in add_events)
    assert all(
        event.lifecycle is CommunicationLifecycle.RETRACTED
        for event in add_events if event is not None
    )


def test_live_reply_target_roles_match_historical_same_and_counterpart_semantics(
    tmp_path, monkeypatch
):
    adapter = _adapter(monkeypatch)
    root = tmp_path / "contact-memory"
    contact_id = "stephen-lucier"
    same_target = adapter._normalize_ingress_record(
        _record("same-speaker-target", "same"), received_at=1.0
    )
    persist_live_communication_ingress(
        root=root, contact_id=contact_id, principal="guest",
        envelopes=(same_target,), secret=_SECRET,
    )
    counterpart = _persist_counterpart_target(root, contact_id, "counterpart-target")

    replies = tuple(
        adapter._normalize_ingress_record(_record(
            f"reply-{index}", "reply", replyToGuid=f"p:0/{target}"
        ), received_at=2.0 + index)
        for index, target in enumerate(("same-speaker-target", "counterpart-target"))
    )
    result = persist_live_communication_ingress(
        root=root, contact_id=contact_id, principal="guest",
        envelopes=replies, secret=_SECRET,
    )
    store = ContactMemoryStore(root, contact_id)
    bundles = [store.get_communication_bundle(event_id) for event_id in result.event_ids]
    assert all(bundle is not None for bundle in bundles)
    relations = [
        next(
            relation for relation in bundle.relations
            if relation.relation_type is CommunicationRelationType.REPLY_TO
        )
        for bundle in bundles if bundle is not None
    ]

    assert relations[0].target_actor_role is CommunicationActorRole.CONTACT
    assert relations[1].target_actor_role is CommunicationActorRole.COUNTERPART
    assert relations[1].target_source_id == counterpart.source_id


def test_contact_reply_to_authenticated_owner_outbound_is_preserved(tmp_path, monkeypatch):
    adapter = _adapter(monkeypatch)
    root = tmp_path / "contact-memory"
    contact_id = "stephen-lucier"
    outbound = adapter._normalize_ingress_record(
        _record(
            "owner-outbound-target", "did you see this?", isFromMe=True,
            handle={"address": "guest@example.com"},
        ),
        received_at=1.0,
    )
    target = persist_live_communication_ingress(
        root=root, contact_id=contact_id, principal="guest",
        envelopes=(outbound,), secret=_SECRET,
    )
    reply = adapter._normalize_ingress_record(
        _record(
            "contact-reply-to-owner", "yes", replyToGuid="p:0/owner-outbound-target"
        ),
        received_at=2.0,
    )
    result = persist_live_communication_ingress(
        root=root, contact_id=contact_id, principal="guest",
        envelopes=(reply,), secret=_SECRET,
    )

    store = ContactMemoryStore(root, contact_id)
    target_event = store.get_communication_event(target.event_ids[0])
    bundle = store.get_communication_bundle(result.event_ids[0])
    assert target_event is not None
    assert target_event.direction is CommunicationDirection.OUTBOUND
    assert target_event.actor_role is CommunicationActorRole.COUNTERPART
    assert bundle is not None
    relation = next(
        item for item in bundle.relations
        if item.relation_type is CommunicationRelationType.REPLY_TO
    )
    assert relation.target_actor_role is CommunicationActorRole.COUNTERPART


@pytest.mark.asyncio
async def test_owner_outbound_webhook_persists_without_agent_dispatch(monkeypatch):
    adapter = _adapter(monkeypatch)
    persisted = []

    async def ingress(event):
        persisted.extend(event.communication_ingress)
        return ("stored",)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("owner outbound must not dispatch to the agent")

    adapter.set_ingress_handler(ingress)
    monkeypatch.setattr(adapter, "handle_message", forbidden)
    response = await adapter._handle_webhook(_Request({
        "type": "new-message",
        "data": _record(
            "owner-outbound-webhook", "outbound evidence", isFromMe=True,
        ),
    }))

    assert response.status == 200
    assert len(persisted) == 1
    assert persisted[0].direction == "outbound"
    assert persisted[0].event_kind == "text"


@pytest.mark.parametrize("kind", ["reply", "reaction"])
@pytest.mark.parametrize("target_scope", ["missing", "foreign"])
def test_missing_and_foreign_live_targets_fail_before_any_batch_write(
    tmp_path, monkeypatch, kind, target_scope
):
    adapter = _adapter(monkeypatch)
    root = tmp_path / "contact-memory"
    contact_id = "stephen-lucier"
    target = f"{target_scope}-target"
    if target_scope == "foreign":
        _persist_counterpart_target(root, "other-contact", target)
    valid = adapter._normalize_ingress_record(_record("valid-before-invalid"), received_at=1.0)
    overrides = (
        {"replyToGuid": f"p:0/{target}"}
        if kind == "reply"
        else {"associatedMessageType": 2001, "associatedMessageGuid": f"p:0/{target}"}
    )
    invalid = adapter._normalize_ingress_record(
        _record("invalid-target", "", **overrides), received_at=2.0
    )

    with pytest.raises(ValueError, match=f"unauthenticated {kind} target"):
        persist_live_communication_ingress(
            root=root, contact_id=contact_id, principal="guest",
            envelopes=(valid, invalid), secret=_SECRET,
        )

    assert ContactMemoryStore(root, contact_id).get_communication_event_by_source(
        "imessage", _source_identity(_SECRET, "valid-before-invalid")
    ) is None


def test_same_speaker_live_reaction_target_is_rejected_like_historical_ingress(
    tmp_path, monkeypatch
):
    adapter = _adapter(monkeypatch)
    root = tmp_path / "contact-memory"
    contact_id = "stephen-lucier"
    target = adapter._normalize_ingress_record(
        _record("same-speaker-reaction-target"), received_at=1.0
    )
    persist_live_communication_ingress(
        root=root, contact_id=contact_id, principal="guest",
        envelopes=(target,), secret=_SECRET,
    )
    reaction = adapter._normalize_ingress_record(_record(
        "same-speaker-reaction", "", associatedMessageType=2000,
        associatedMessageGuid="p:0/same-speaker-reaction-target",
    ), received_at=2.0)

    with pytest.raises(ValueError, match="reaction target is not a counterpart"):
        persist_live_communication_ingress(
            root=root, contact_id=contact_id, principal="guest",
            envelopes=(reaction,), secret=_SECRET,
        )


def test_no_guid_fallback_is_deterministic_and_unique_per_list_member(
    tmp_path, monkeypatch
):
    adapter = _adapter(monkeypatch)
    records = []
    for _ in range(2):
        record = _record("unused", "", attachments=[{"mimeType": "image/jpeg"}])
        record.pop("guid")
        records.append(record)
    first = tuple(
        adapter._normalize_ingress_record(record, received_at=10.0, record_index=index)
        for index, record in enumerate(records)
    )
    replay = tuple(
        adapter._normalize_ingress_record(record, received_at=99.0, record_index=index)
        for index, record in enumerate(records)
    )

    assert len({item.source_message_id for item in first}) == 2
    assert [item.source_message_id for item in replay] == [
        item.source_message_id for item in first
    ]
    result = persist_live_communication_ingress(
        root=tmp_path / "contact-memory", contact_id="stephen-lucier",
        principal="guest", envelopes=first, secret=_SECRET,
    )
    assert result.inserted == 2
    assert len(set(result.event_ids)) == 2


def test_principal_selects_actor_identity_and_physical_store(tmp_path, monkeypatch):
    envelope = _adapter(monkeypatch)._normalize_ingress_record(_record("same-source"), received_at=1.0)
    root = tmp_path / "contact-memory"

    owner = persist_live_communication_ingress(
        root=root, contact_id="kosta-owner", principal="owner",
        envelopes=(envelope,), secret=_SECRET,
    )
    guest = persist_live_communication_ingress(
        root=root, contact_id="stephen-lucier", principal="guest",
        envelopes=(envelope,), secret=_SECRET,
    )

    assert owner.event_ids != guest.event_ids
    assert ContactMemoryStore(root, "kosta-owner").path != ContactMemoryStore(root, "stephen-lucier").path


def test_envelope_type_rejects_mutable_members():
    with pytest.raises((TypeError, ValueError)):
        CommunicationIngressEnvelope(  # type: ignore[arg-type]
            version=1,
            source_message_id="x",
            received_at=1.0,
            occurred_at=1.0,
            timestamp_source="received_at",
            chat_type="dm",
            direction="inbound",
            sender_identity="guest@example.com",
            visible_text="hello",
            visible_urls=[],
            attachments=(),
            reply_target=None,
            reaction_target=None,
            reaction_kind=None,
            event_kind="text",
        )


def test_concurrent_first_ingress_creates_one_shared_hmac_key(tmp_path, monkeypatch):
    root = tmp_path / "contact-memory"
    import os

    real_open = os.open
    barrier = threading.Barrier(2)
    guarded_reads = 0
    guard = threading.Lock()

    def racing_open(path, flags, mode=0o777):
        nonlocal guarded_reads
        if str(path).endswith(".communication-hmac-key") and not flags & os.O_WRONLY:
            with guard:
                should_race = guarded_reads < 2
                guarded_reads += 1
            if should_race:
                barrier.wait(timeout=2)
                raise FileNotFoundError(path)
        return real_open(path, flags, mode)

    monkeypatch.setattr("gateway.contact_memory.live_ingress.os.open", racing_open)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        keys = list(pool.map(lambda _: load_or_create_communication_key(root), range(2)))

    assert keys[0] == keys[1]


def test_first_use_key_is_not_visible_until_fully_written(tmp_path, monkeypatch):
    root = tmp_path / "contact-memory"
    import os

    real_write = os.write
    writer_blocked = threading.Event()
    release_writer = threading.Event()
    guard = threading.Lock()
    blocked_once = False

    def blocked_write(fd, data):
        nonlocal blocked_once
        with guard:
            should_block = not blocked_once
            if should_block:
                blocked_once = True
        if should_block:
            writer_blocked.set()
            assert release_writer.wait(timeout=2)
        return real_write(fd, data)

    monkeypatch.setattr("gateway.contact_memory.live_ingress.os.write", blocked_write)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(load_or_create_communication_key, root)
        assert writer_blocked.wait(timeout=2)
        reader = pool.submit(load_or_create_communication_key, root)
        reader_key = reader.result(timeout=2)
        release_writer.set()
        writer_key = writer.result(timeout=2)

    assert writer_key == reader_key


class _Request:
    query = {"password": "secret"}
    headers = {}

    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    async def read(self):
        return self.body


@pytest.mark.asyncio
async def test_webhook_persists_all_list_members_before_reactive_dispatch(monkeypatch):
    adapter = _adapter(monkeypatch)
    order = []
    handled = []

    async def ingress(event):
        order.append(("ingress", tuple(item.source_message_id for item in event.communication_ingress)))
        return ("stored",)

    async def handle(event):
        order.append(("reactive", event.text))
        handled.append(event)

    adapter.set_ingress_handler(ingress)
    monkeypatch.setattr(adapter, "handle_message", handle)
    response = await adapter._handle_webhook(_Request({
        "type": "new-message",
        "data": [_record("batch-a", "first"), _record("batch-b", "second")],
    }))
    if adapter._background_tasks:
        await asyncio.gather(*adapter._background_tasks)

    assert response.status == 200
    assert order[0] == ("ingress", ("batch-a", "batch-b"))
    assert order[1] == ("reactive", "first\nsecond")
    assert handled[0].communication_ingress[0].visible_text == "first"
    assert handled[0].communication_ingress[1].visible_text == "second"


@pytest.mark.asyncio
async def test_webhook_rejects_mixed_malformed_data_list_without_ack_or_ingress(monkeypatch):
    adapter = _adapter(monkeypatch)
    called = False

    async def ingress(_event):
        nonlocal called
        called = True
        return ()

    adapter.set_ingress_handler(ingress)
    response = await adapter._handle_webhook(_Request({
        "type": "new-message",
        "data": [_record("valid-member"), "malformed-member"],
    }))

    assert response.status == 400
    assert called is False


@pytest.mark.asyncio
async def test_attachment_descriptor_is_persisted_before_failed_download(monkeypatch):
    adapter = _adapter(monkeypatch)
    order = []
    handled = []

    async def ingress(event):
        assert event.communication_ingress[0].attachments[0].source_attachment_id == "att-guid"
        order.append("ingress")
        return ("stored",)

    async def download(_guid, _metadata):
        order.append("download")
        return None

    async def handle(event):
        order.append("reactive")
        handled.append(event)

    adapter.set_ingress_handler(ingress)
    monkeypatch.setattr(adapter, "_download_attachment", download)
    monkeypatch.setattr(adapter, "handle_message", handle)
    response = await adapter._handle_webhook(_Request({
        "type": "new-message",
        "data": _record(
            "attachment-only", "",
            attachments=[{"guid": "att-guid", "mimeType": "image/jpeg"}],
        ),
    }))
    if adapter._background_tasks:
        await asyncio.gather(*adapter._background_tasks)

    assert response.status == 200
    assert order == ["ingress", "download", "reactive"]
    assert handled[0].text == "(attachment)"
    assert handled[0].media_urls == []


@pytest.mark.asyncio
async def test_reaction_ack_persists_without_download_or_agent(monkeypatch):
    adapter = _adapter(monkeypatch)
    persisted = []

    async def ingress(event):
        persisted.extend(event.communication_ingress)
        return ("stored",)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("reaction must not download or dispatch")

    adapter.set_ingress_handler(ingress)
    monkeypatch.setattr(adapter, "_download_attachment", forbidden)
    monkeypatch.setattr(adapter, "handle_message", forbidden)
    response = await adapter._handle_webhook(_Request({
        "type": "new-message",
        "data": _record(
            "tapback-only", "", associatedMessageType="2000",
            associatedMessageGuid="p:4/target-guid-1",
        ),
    }))

    assert response.status == 200
    assert len(persisted) == 1 and persisted[0].event_kind == "reaction_add"


def test_cold_and_contended_attachment_ingress_are_bounded_without_media_or_model(
    tmp_path, monkeypatch
):
    adapter = _adapter(monkeypatch)
    envelope = adapter._normalize_ingress_record(_record(
        "latency-guid", "bounded ingress",
        attachments=[
            {"guid": f"latency-att-{index}", "mimeType": "image/jpeg", "totalBytes": 2048}
            for index in range(4)
        ],
    ), received_at=1720962002.0)
    root = tmp_path / "contact-memory"
    started = time.perf_counter()
    result = persist_live_communication_ingress(
        root=root,
        contact_id="stephen-lucier",
        principal="guest",
        envelopes=(envelope,),
        secret=_SECRET,
    )
    cold_elapsed = time.perf_counter() - started

    contended = tuple(
        adapter._normalize_ingress_record(_record(
            f"contended-{index}", f"bounded {index}",
            attachments=[{
                "guid": f"contended-att-{index}", "mimeType": "application/pdf",
                "totalBytes": 4096,
            }],
        ), received_at=1720962010.0 + index)
        for index in range(8)
    )

    def persist(item):
        started_one = time.perf_counter()
        outcome = persist_live_communication_ingress(
            root=root, contact_id="stephen-lucier", principal="guest",
            envelopes=(item,), secret=_SECRET,
        )
        return time.perf_counter() - started_one, outcome

    contention_started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        contention_results = list(pool.map(persist, contended))
    contention_elapsed = time.perf_counter() - contention_started
    max_worker_elapsed = max(elapsed for elapsed, _ in contention_results)
    print(
        f"phase-c-ingress-benchmark cold_ms={cold_elapsed * 1000:.3f} "
        f"contended_total_ms={contention_elapsed * 1000:.3f} "
        f"contended_max_worker_ms={max_worker_elapsed * 1000:.3f}"
    )

    assert result.inserted == 1
    assert sum(outcome.inserted for _, outcome in contention_results) == 8
    assert cold_elapsed < 1.0
    assert contention_elapsed < 3.0
    assert max_worker_elapsed < 3.0
