from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import sqlite3
from unittest.mock import patch as mock_patch

import pytest

from gateway.contact_memory.imessage_bootstrap import (
    open_messages_readonly,
    resolve_one_to_one_chat,
)
from gateway.contact_memory.imessage_communication_adapter import (
    HistoricalStoreTargets,
    build_aggregate_manifest,
    build_private_evidence_manifest,
    ingest_historical_scan,
    parse_associated_guid,
    scan_historical_communication,
    verify_aggregate_manifest,
)
from gateway.contact_memory.schema import (
    CommunicationActorRole,
    CommunicationKind,
    CommunicationLifecycle,
    CommunicationRelationType,
)
from gateway.contact_memory.store import ContactMemoryStore
from scripts.review_imessage_communication_signals import _temporary_store_root, main

SECRET = b"phase-b-synthetic-hmac-key-0001"


def _create_messages_db(path: Path, *, group: bool = False) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE chat(
      ROWID INTEGER PRIMARY KEY, guid TEXT, style INTEGER,
      display_name TEXT, group_id TEXT
    );
    CREATE TABLE handle(ROWID INTEGER PRIMARY KEY,id TEXT);
    CREATE TABLE chat_handle_join(chat_id INTEGER,handle_id INTEGER);
    CREATE TABLE message(
      ROWID INTEGER PRIMARY KEY,guid TEXT,date INTEGER,is_from_me INTEGER,text TEXT,
      attributedBody BLOB,associated_message_type INTEGER DEFAULT 0,
      associated_message_guid TEXT,thread_originator_guid TEXT,handle_id INTEGER
    );
    CREATE TABLE chat_message_join(chat_id INTEGER,message_id INTEGER);
    CREATE TABLE attachment(
      ROWID INTEGER PRIMARY KEY,guid TEXT,filename TEXT,mime_type TEXT,uti TEXT,total_bytes INTEGER
    );
    CREATE TABLE message_attachment_join(message_id INTEGER,attachment_id INTEGER);
    """)
    con.execute(
        "INSERT INTO chat VALUES(1,'iMessage;-;synthetic-direct-guid',45,NULL,'internal-direct-id')"
    )
    con.executemany(
        "INSERT INTO handle VALUES(?,?)",
        [(1, "+140****0100"), (2, "steve@example.test"), (9, "+199****9999")],
    )
    con.execute("INSERT INTO chat_handle_join VALUES(1,1)")
    if group:
        con.execute("INSERT INTO chat_handle_join VALUES(1,9)")

    visible_archive = plistlib.dumps(
        {
            "NSString": "https://music.example.test/visible-fallback",
            "preview": "https://cdn.example.test/hidden-preview.jpg",
        }
    )
    hidden_archive = plistlib.dumps(
        {
            "NSString": "https://must-not-win.example.test/archive",
            "preview": "https://cdn.example.test/also-hidden.jpg",
        }
    )
    rows = [
        (1, "msg-owner-text", 1_000_000_000, 1, "owner plain text", None, 0, None, None, None),
        (2, "msg-owner-link", 2_000_000_000, 1, "see https://example.com/visible?utm_source=x", hidden_archive, 0, None, None, None),
        (3, "msg-contact-attachment", 3_000_000_000, 0, None, None, 0, None, None, 1),
        (4, "tap-owner-add", 4_000_000_000, 1, "Loved", None, 2000, "p:0/msg-contact-attachment", None, None),
        (5, "tap-owner-remove", 5_000_000_000, 1, "Removed a heart", None, 3000, "bp:0:msg-contact-attachment", None, None),
        (6, "msg-contact-reply", 6_000_000_000, 0, "reply body", None, 0, None, "p:0/msg-owner-link", 1),
        (7, "msg-contact-batch", 7_000_000_000, 0, "second bubble", None, 0, None, None, 1),
        (8, "msg-contact-rich-link", 200_000_000_000, 0, None, visible_archive, 0, None, None, 1),
        (9, "msg-contact-nonsemantic", 400_000_000_000, 0, None, b"opaque archive", 0, None, None, 1),
        (10, "anonymous-service-row", 401_000_000_000, 0, "service text", None, 0, None, None, 0),
    ]
    con.executemany("INSERT INTO message VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    con.executemany(
        "INSERT INTO chat_message_join VALUES(1,?)", [(row[0],) for row in rows]
    )
    con.execute(
        "INSERT INTO attachment VALUES(1,'attachment-guid','/private/Attachments/photo-secret.jpg',"
        "'image/jpeg','public.jpeg',4096)"
    )
    con.execute("INSERT INTO message_attachment_join VALUES(3,1)")
    con.commit()
    con.close()


def _scan(path: Path, secret: bytes = SECRET):
    with open_messages_readonly(path) as con:
        chat = resolve_one_to_one_chat(
            con, ["+140****0100", "steve@example.test"]
        )
        return scan_historical_communication(con, chat, secret=secret)


def _targets(tmp_path: Path) -> HistoricalStoreTargets:
    return HistoricalStoreTargets(
        owner=ContactMemoryStore(tmp_path / "poke", "kosta-owner"),
        guest=ContactMemoryStore(tmp_path / "guest", "stephen-lucier"),
    )


def _all_bundles(store: ContactMemoryStore):
    with sqlite3.connect(store.path) as con:
        event_ids = [row[0] for row in con.execute("SELECT event_id FROM communication_event")]
    return [store.get_communication_bundle(event_id) for event_id in event_ids]


def test_historical_scan_ingests_all_signal_classes_with_exact_actor_routing(tmp_path: Path):
    source = tmp_path / "chat.db"
    _create_messages_db(source)
    scan = _scan(source)
    targets = _targets(tmp_path)

    result = ingest_historical_scan(scan, targets)

    assert scan.accounting == {
        "selected": 10,
        "text": 2,
        "links": 2,
        "attachments": 1,
        "reactions": 2,
        "replies": 1,
        "explicit_nonsemantic": 1,
        "rejected": 1,
    }
    assert sum(scan.accounting[key] for key in (
        "text", "links", "attachments", "reactions", "replies",
        "explicit_nonsemantic", "rejected",
    )) == scan.accounting["selected"]
    assert result["kosta-owner"] == {"inserted": 4, "deduplicated": 0}
    assert result["stephen-lucier"] == {"inserted": 5, "deduplicated": 0}

    owner_bundles = [bundle for bundle in _all_bundles(targets.owner) if bundle]
    guest_bundles = [bundle for bundle in _all_bundles(targets.guest) if bundle]
    assert {bundle.event.kind for bundle in owner_bundles} == {
        CommunicationKind.TEXT,
        CommunicationKind.LINK_SHARE,
        CommunicationKind.REACTION_ADD,
        CommunicationKind.REACTION_REMOVE,
    }
    assert {bundle.event.kind for bundle in guest_bundles} == {
        CommunicationKind.ATTACHMENT_SHARE,
        CommunicationKind.REPLY,
        CommunicationKind.TEXT,
        CommunicationKind.LINK_SHARE,
        CommunicationKind.TEXT,
    }
    assert all(bundle.event.actor_role.value == "contact" for bundle in owner_bundles + guest_bundles)
    assert all(bundle.event.direction.value == "inbound" for bundle in owner_bundles + guest_bundles)

    reaction_add = next(
        bundle for bundle in owner_bundles
        if bundle.event.kind is CommunicationKind.REACTION_ADD
    )
    reaction_remove = next(
        bundle for bundle in owner_bundles
        if bundle.event.kind is CommunicationKind.REACTION_REMOVE
    )
    assert reaction_add.event.lifecycle is CommunicationLifecycle.RETRACTED
    assert reaction_add.event.retracted_by_event_id == reaction_remove.event.event_id
    assert reaction_add.relations[0].target_source_id == reaction_remove.relations[0].target_source_id
    assert reaction_add.relations[0].relation_type is CommunicationRelationType.REACTION_TO

    reply = next(
        bundle for bundle in guest_bundles
        if bundle.event.kind is CommunicationKind.REPLY
    )
    owner_link = next(
        bundle for bundle in owner_bundles
        if bundle.event.kind is CommunicationKind.LINK_SHARE
    )
    assert any(
        relation.relation_type is CommunicationRelationType.REPLY_TO
        and relation.target_source_id == owner_link.event.source_id
        and relation.target_actor_role.value == "counterpart"
        for relation in reply.relations
    )
    batch_relations = [
        relation
        for bundle in guest_bundles
        for relation in bundle.relations
        if relation.relation_type is CommunicationRelationType.BATCH_MEMBER_OF
    ]
    assert len(batch_relations) == 2
    assert len({relation.target_source_id for relation in batch_relations}) == 1

    rich_link = next(
        bundle for bundle in guest_bundles
        if bundle.event.kind is CommunicationKind.LINK_SHARE
    )
    assert [item.domain for item in owner_link.urls] == ["example.com"]
    assert [item.domain for item in rich_link.urls] == ["music.example.test"]
    assert all("cdn.example" not in item.domain for bundle in owner_bundles + guest_bundles for item in bundle.urls)
    attachment = next(bundle for bundle in guest_bundles if bundle.attachments)
    assert attachment.attachments[0].media_kind == "image"
    assert attachment.attachments[0].mime_type == "image/jpeg"
    assert attachment.attachments[0].uti == "public.jpeg"
    assert attachment.attachments[0].size_bytes == 4096


def test_aggregate_manifest_and_canonical_rows_never_leak_private_artifacts(tmp_path: Path):
    source = tmp_path / "chat.db"
    _create_messages_db(source)
    scan = _scan(source)
    targets = _targets(tmp_path)
    ingest_historical_scan(scan, targets)

    manifest = build_aggregate_manifest(scan, secret=SECRET)
    private = build_private_evidence_manifest(scan)
    aggregate_dump = json.dumps(manifest, sort_keys=True)
    private_dump = json.dumps(private, sort_keys=True)
    forbidden = (
        "owner plain text",
        "reply body",
        "https://",
        "msg-owner-link",
        "tap-owner-add",
        "attachment-guid",
        "photo-secret.jpg",
        "/private/Attachments",
        "anonymous-service-row",
    )
    assert all(value not in aggregate_dump for value in forbidden)
    assert any(value in private_dump for value in forbidden)
    assert manifest["kind"] == "imessage-canonical-communication-review"
    assert manifest["apply_supported"] is False
    assert manifest["accounting"] == scan.accounting
    assert manifest["rejection_reasons"] == {"anonymous_service_handle": 1}
    assert len(manifest["review_id"]) == 64
    assert len(manifest["evidence_commitment"]) == 64
    assert verify_aggregate_manifest(manifest, secret=SECRET)
    tampered = {**manifest, "accounting": {**manifest["accounting"], "selected": 11}}
    assert not verify_aggregate_manifest(tampered, secret=SECRET)

    for store in (targets.owner, targets.guest):
        with sqlite3.connect(store.path) as con:
            durable_dump = json.dumps({
                table: [tuple(row) for row in con.execute(f"SELECT * FROM {table}")]
                for table in (
                    "communication_event", "communication_url",
                    "communication_attachment", "communication_relation",
                )
            }, default=str, sort_keys=True)
        assert all(value not in durable_dump for value in forbidden)
        assert "://" not in durable_dump


def test_scan_is_deterministic_and_replay_uses_only_phase_a_ingest_apis(tmp_path: Path):
    source = tmp_path / "chat.db"
    _create_messages_db(source)
    first = _scan(source)
    second = _scan(source)
    assert build_aggregate_manifest(first, secret=SECRET) == build_aggregate_manifest(second, secret=SECRET)
    assert [record.bundle.event for record in first.records] == [
        record.bundle.event for record in second.records
    ]

    targets = _targets(tmp_path)
    ingest_historical_scan(first, targets)
    replay = ingest_historical_scan(second, targets)
    assert replay == {
        "kosta-owner": {"inserted": 0, "deduplicated": 4},
        "stephen-lucier": {"inserted": 0, "deduplicated": 5},
    }

    changed = _scan(source, b"different-phase-b-hmac-key-0002")
    assert [record.bundle.event.event_id for record in first.records] != [
        record.bundle.event.event_id for record in changed.records
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("p:0/message-guid", "message-guid"),
        ("bp:0/message-guid", "message-guid"),
        ("p:0:message-guid", "message-guid"),
        ("bp:0:message-guid", "message-guid"),
        ("p:1/message-guid", "message-guid"),
        ("bp:2:message-guid", "message-guid"),
        ("p:37:message-guid", "message-guid"),
        ("p:message-guid", "message-guid"),
        ("bp:message-guid", "message-guid"),
        ("message-guid", "message-guid"),
        ("A1B2C3D4-5678-90AB-CDEF-0123456789AB", "A1B2C3D4-5678-90AB-CDEF-0123456789AB"),
    ],
)
def test_modern_and_legacy_associated_guid_variants(raw: str, expected: str):
    assert parse_associated_guid(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("p:a/message-guid", "message-guid"),
        ("p:x:message-guid", ""),
        ("p:/message-guid", ""),
        ("p:a/message/guid", ""),
        ("p:wat", ""),
        ("bp:/message-guid", ""),
        ("x:message-guid", ""),
        ("rp:1/message-guid", ""),
        ("p:-1/message-guid", ""),
        ("bp:2/message/guid", ""),
        ("message guid", ""),
        ("message-guid/child", ""),
        ("", ""),
    ],
)
def test_associated_guid_parser_supports_p_a_and_rejects_malformed_wrappers(
    raw: str, expected: str
):
    assert parse_associated_guid(raw) == expected


def test_selected_chat_approval_does_not_include_alias_from_another_chat(tmp_path: Path):
    source = tmp_path / "chat.db"
    _create_messages_db(source)
    con = sqlite3.connect(source)
    con.execute("INSERT INTO chat VALUES(2,'iMessage;-;alias-direct',43,NULL,NULL)")
    con.execute("INSERT INTO chat_handle_join VALUES(2,2)")
    con.execute("UPDATE message SET handle_id=2 WHERE ROWID=6")
    con.commit()
    con.close()

    with open_messages_readonly(source) as con:
        chat = resolve_one_to_one_chat(
            con, ["+140****0100", "steve@example.test"], chat_id=1
        )
        assert chat.approved_handle_ids == (1,)
        with pytest.raises(ValueError, match="unapproved handle_id"):
            scan_historical_communication(con, chat, secret=SECRET)


@pytest.mark.parametrize("associated_type", [2006, 3007, 2, 3, 1000, 987654])
def test_unsupported_associated_types_are_rejected_and_accounted(
    tmp_path: Path, associated_type: int
):
    source = tmp_path / "chat.db"
    _create_messages_db(source)
    con = sqlite3.connect(source)
    con.execute(
        "UPDATE message SET associated_message_type=?,associated_message_guid=? WHERE ROWID=7",
        (associated_type, "msg-owner-text"),
    )
    con.commit()
    con.close()

    scan = _scan(source)
    rejected = [item for item in scan.rejected_evidence if item["source_key"] == "msg-contact-batch"]
    assert [item["reason"] for item in rejected] == ["unsupported_associated_type"]
    assert scan.accounting["rejected"] == 2
    assert not any(
        record.private_evidence["source_key"] == "msg-contact-batch"
        for record in scan.records
    )


def test_reaction_type_matrix_is_complete():
    expected = {
        2000: "love", 2001: "like", 2002: "dislike", 2003: "laugh",
        2004: "emphasis", 2005: "question", 3000: "love", 3001: "like",
        3002: "dislike", 3003: "laugh", 3004: "emphasis", 3005: "question",
    }
    from gateway.contact_memory import imessage_communication_adapter as adapter

    actual = {
        **{key: value.value for key, value in adapter._REACTION_TYPES.items()},
        **{key: value.value for key, value in adapter._REACTION_REMOVALS.items()},
    }
    assert actual == expected


def test_all_supported_reaction_add_and_remove_types_are_scanned(tmp_path: Path):
    source = tmp_path / "chat.db"
    _create_messages_db(source)
    con = sqlite3.connect(source)
    con.execute("DELETE FROM chat_message_join")
    con.execute("DELETE FROM message")
    rows: list[tuple[object, ...]] = [
        (1, "reaction-target", 1_000_000_000, 0, "target", None, 0, None, None, 1)
    ]
    for offset, subtype in enumerate(range(6), start=2):
        rows.append((
            offset, f"reaction-add-{subtype}", offset * 1_000_000_000, 1,
            "reaction", None, 2000 + subtype, "p:0/reaction-target", None, None,
        ))
    for offset, subtype in enumerate(range(6), start=8):
        rows.append((
            offset, f"reaction-remove-{subtype}", offset * 1_000_000_000, 1,
            "removal", None, 3000 + subtype, "bp:0:reaction-target", None, None,
        ))
    con.executemany("INSERT INTO message VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    con.executemany(
        "INSERT INTO chat_message_join VALUES(1,?)", [(row[0],) for row in rows]
    )
    con.commit()
    con.close()

    scan = _scan(source)
    reactions = [record for record in scan.records if record.category == "reactions"]
    assert len(reactions) == 12
    assert scan.accounting["reactions"] == 12
    assert scan.accounting["rejected"] == 0
    assert {
        record.bundle.event.reaction_subtype.value
        for record in reactions
        if record.bundle.event.reaction_subtype is not None
    } == {
        "love", "like", "dislike", "laugh", "emphasis", "question",
    }
    assert sum(record.retraction_target_event_id is not None for record in reactions) == 6


@pytest.mark.parametrize("raw", ["p:x:reaction-target", "p:/reaction-target", "p:wat"])
def test_malformed_reaction_wrappers_are_explicitly_rejected(tmp_path: Path, raw: str):
    source = tmp_path / "chat.db"
    _create_messages_db(source)
    con = sqlite3.connect(source)
    con.execute(
        "UPDATE message SET associated_message_guid=? WHERE ROWID=4", (raw,)
    )
    con.commit()
    con.close()

    scan = _scan(source)
    rejected = [item for item in scan.rejected_evidence if item["source_key"] == "tap-owner-add"]
    assert [item["reason"] for item in rejected] == ["malformed_associated_target"]


def test_unknown_wrapper_cannot_authenticate_reaction_target(tmp_path: Path):
    source = tmp_path / "chat.db"
    _create_messages_db(source)
    con = sqlite3.connect(source)
    con.execute("UPDATE message SET guid='x:target-guid' WHERE ROWID=3")
    con.execute(
        "UPDATE message SET associated_message_guid='x:target-guid' WHERE ROWID=4"
    )
    con.commit()
    con.close()

    scan = _scan(source)
    rejected = [
        item for item in scan.rejected_evidence
        if item["source_key"] == "tap-owner-add"
    ]
    assert [item["reason"] for item in rejected] == ["malformed_associated_target"]
    assert not any(
        record.private_evidence["source_key"] == "tap-owner-add"
        for record in scan.records
    )


@pytest.mark.parametrize("wrapper", ["p:1/", "bp:2:", "p:37:"])
def test_nonzero_numeric_part_wrappers_authenticate_reaction_target(
    tmp_path: Path, wrapper: str
):
    source = tmp_path / "chat.db"
    _create_messages_db(source)
    con = sqlite3.connect(source)
    con.execute(
        "UPDATE message SET associated_message_guid=? WHERE ROWID=4",
        (wrapper + "msg-contact-attachment",),
    )
    con.commit()
    con.close()

    scan = _scan(source)
    reaction = next(
        record for record in scan.records
        if record.private_evidence["source_key"] == "tap-owner-add"
    )
    assert reaction.category == "reactions"
    assert reaction.bundle.relations[0].relation_type is CommunicationRelationType.REACTION_TO


@pytest.mark.parametrize(
    ("reply_to", "thread", "target", "same_speaker"),
    [
        ("p:0/msg-owner-link", None, "msg-owner-link", False),
        (None, "bp:0:msg-owner-link", "msg-owner-link", False),
        ("msg-owner-link", "p:a/msg-owner-link", "msg-owner-link", False),
        ("msg-contact-attachment", None, "msg-contact-attachment", True),
    ],
)
def test_reply_columns_agreement_and_authenticated_same_speaker_are_preserved(
    tmp_path: Path, reply_to: str | None, thread: str | None, target: str,
    same_speaker: bool,
):
    source = tmp_path / "chat.db"
    _create_messages_db(source)
    con = sqlite3.connect(source)
    con.execute("ALTER TABLE message ADD COLUMN reply_to_guid TEXT")
    con.execute(
        "UPDATE message SET reply_to_guid=?,thread_originator_guid=? WHERE ROWID=6",
        (reply_to, thread),
    )
    con.commit()
    con.close()

    scan = _scan(source)
    record = next(
        item for item in scan.records
        if item.private_evidence["source_key"] == "msg-contact-reply"
    )
    assert record.category == "replies"
    relation = next(
        item for item in record.bundle.relations
        if item.relation_type is CommunicationRelationType.REPLY_TO
    )
    assert relation.target_source_id == next(
        item.bundle.event.source_id for item in scan.records
        if item.private_evidence["source_key"] == target
    )
    assert relation.target_actor_role is (
        CommunicationActorRole.CONTACT if same_speaker else CommunicationActorRole.COUNTERPART
    )


@pytest.mark.parametrize(
    ("reply_to", "thread", "reason"),
    [
        ("msg-owner-text", "msg-owner-link", "conflicting_reply_targets"),
        ("missing-foreign-chat-guid", None, "unauthenticated_reply_target"),
        ("p:x:msg-owner-text", None, "malformed_reply_target"),
    ],
)
def test_conflicting_absent_and_malformed_reply_targets_are_rejected(
    tmp_path: Path, reply_to: str | None, thread: str | None, reason: str
):
    source = tmp_path / "chat.db"
    _create_messages_db(source)
    con = sqlite3.connect(source)
    con.execute("ALTER TABLE message ADD COLUMN reply_to_guid TEXT")
    con.execute(
        "UPDATE message SET reply_to_guid=?,thread_originator_guid=? WHERE ROWID=6",
        (reply_to, thread),
    )
    con.commit()
    con.close()

    scan = _scan(source)
    rejected = [item for item in scan.rejected_evidence if item["source_key"] == "msg-contact-reply"]
    assert [item["reason"] for item in rejected] == [reason]


def test_duplicate_and_hostile_attachment_joins_emit_safe_unique_children(tmp_path: Path):
    source = tmp_path / "chat.db"
    _create_messages_db(source)
    con = sqlite3.connect(source)
    con.execute("ALTER TABLE attachment ADD COLUMN transfer_name TEXT")
    con.execute("UPDATE message SET text='caption https://example.test/a' WHERE ROWID=3")
    con.executemany(
        "INSERT INTO attachment VALUES(?,?,?,?,?,?,?)",
        [
            (2, None, None, "not a mime", "bad uti!", "huge", None),
            (3, None, None, None, None, -1, None),
            (4, "attachment-guid", "/private/Attachments/conflict.mov", "video/quicktime", "public.movie", 8192, None),
        ],
    )
    con.executemany(
        "INSERT INTO message_attachment_join VALUES(?,?)",
        [(3, 1), (3, 2), (3, 3), (3, 4), (3, 9999)],
    )
    con.commit()
    con.close()

    scan = _scan(source)
    record = next(item for item in scan.records if item.category == "attachments")
    assert len(record.bundle.attachments) == 3
    assert len({item.attachment_id for item in record.bundle.attachments}) == 3
    assert len({item.attachment_identity for item in record.bundle.attachments}) == 3
    assert len(record.bundle.urls) == 1
    hostile = [item for item in record.bundle.attachments if item.mime_type is None]
    assert len(hostile) == 2
    assert all(item.size_bytes is None and item.media_kind == "other" for item in hostile)
    canonical = next(
        item for item in record.bundle.attachments if item.mime_type == "image/jpeg"
    )
    assert canonical.media_kind == "image"
    assert canonical.uti == "public.jpeg"
    assert canonical.size_bytes == 4096


@pytest.mark.parametrize("conflict", ["payload", "actor"])
def test_duplicate_source_guid_conflicts_fail_scan_before_either_store_is_written(
    tmp_path: Path, conflict: str
):
    source = tmp_path / "chat.db"
    _create_messages_db(source)
    con = sqlite3.connect(source)
    if conflict == "payload":
        con.execute("UPDATE message SET guid='msg-contact-reply' WHERE ROWID=7")
    else:
        con.execute("UPDATE message SET guid='msg-owner-text' WHERE ROWID=7")
    con.commit()
    con.close()
    targets = _targets(tmp_path / "stores")

    with pytest.raises(ValueError, match="conflicting historical source rows"):
        _scan(source)
    assert _all_bundles(targets.owner) == []
    assert _all_bundles(targets.guest) == []


def test_handle_zero_is_counted_but_unknown_nonzero_fails_closed_without_writes(tmp_path: Path):
    source = tmp_path / "chat.db"
    _create_messages_db(source)
    con = sqlite3.connect(source)
    con.execute("UPDATE message SET handle_id=9 WHERE ROWID=6")
    con.commit()
    con.close()
    targets = _targets(tmp_path)

    with pytest.raises(ValueError, match="unapproved handle_id"):
        _scan(source)
    assert _all_bundles(targets.owner) == []
    assert _all_bundles(targets.guest) == []


def test_direct_group_and_alias_routing_remains_strict(tmp_path: Path):
    source = tmp_path / "chat.db"
    _create_messages_db(source, group=True)
    with open_messages_readonly(source) as con:
        with pytest.raises(ValueError, match="exactly one"):
            resolve_one_to_one_chat(con, ["+140****0100", "steve@example.test"])

    alias_source = tmp_path / "alias.db"
    _create_messages_db(alias_source)
    con = sqlite3.connect(alias_source)
    con.execute("INSERT INTO chat VALUES(2,'iMessage;-;alias-direct',43,NULL,NULL)")
    con.execute("INSERT INTO chat_handle_join VALUES(2,2)")
    con.commit()
    con.close()
    with open_messages_readonly(alias_source) as con:
        with pytest.raises(ValueError, match="use chat_id"):
            resolve_one_to_one_chat(con, ["+140****0100", "steve@example.test"])
        resolved = resolve_one_to_one_chat(
            con, ["+140****0100", "steve@example.test"], chat_id=1
        )
    assert resolved.chat_id == 1


def test_cli_writes_only_0600_artifacts_and_temporary_contact_stores(tmp_path: Path, capsys):
    source = tmp_path / "chat.db"
    _create_messages_db(source)
    manifest = tmp_path / "aggregate.json"
    evidence = tmp_path / "private-evidence.json"
    key = tmp_path / "hmac-key"
    key.write_bytes(SECRET)
    os.chmod(key, 0o600)
    temporary_stores = tmp_path / "temporary-stores"

    assert main([
        "--chat-db", str(source),
        "--handle", "+140****0100",
        "--handle", "steve@example.test",
        "--aggregate-manifest", str(manifest),
        "--private-evidence", str(evidence),
        "--hmac-key", str(key),
        "--temporary-store-root", str(temporary_stores),
    ]) == 0

    output = capsys.readouterr().out
    assert "owner plain text" not in output
    assert "https://" not in output
    assert "msg-owner-link" not in output
    assert json.loads(output)["accounting"]["selected"] == 10
    for path in (manifest, evidence, key):
        assert path.stat().st_mode & 0o077 == 0
    aggregate_dump = manifest.read_text()
    assert "owner plain text" not in aggregate_dump
    assert "https://" not in aggregate_dump
    assert json.loads(aggregate_dump)["apply_supported"] is False
    contact_dbs = list(temporary_stores.glob("*/contacts/*.sqlite3"))
    assert len(contact_dbs) == 2
    assert all(path.stat().st_mode & 0o077 == 0 for path in contact_dbs)


def test_temporary_store_tooling_refuses_live_profile_roots():
    with pytest.raises(ValueError, match="live Hermes profile"):
        _temporary_store_root(Path.home() / ".hermes" / "profiles" / "poke")


def _cli_args(source: Path, tmp_path: Path) -> tuple[list[str], Path, Path, Path]:
    manifest = tmp_path / "aggregate.json"
    evidence = tmp_path / "private.json"
    key = tmp_path / "key"
    key.write_bytes(SECRET)
    os.chmod(key, 0o600)
    store_root = tmp_path / "published-stores"
    return ([
        "--chat-db", str(source), "--handle", "+140****0100",
        "--handle", "steve@example.test", "--aggregate-manifest", str(manifest),
        "--private-evidence", str(evidence), "--hmac-key", str(key),
        "--temporary-store-root", str(store_root),
    ], store_root, manifest, evidence)


def test_cli_cleans_all_outputs_when_second_store_fails(tmp_path: Path):
    source = tmp_path / "chat.db"
    _create_messages_db(source)
    args, store_root, manifest, evidence = _cli_args(source, tmp_path)
    original = ContactMemoryStore.ingest_communication_event

    def fail_guest(self, *call_args, **call_kwargs):
        if self.contact_id == "stephen-lucier":
            raise OSError("injected second-store failure")
        return original(self, *call_args, **call_kwargs)

    with mock_patch.object(ContactMemoryStore, "ingest_communication_event", fail_guest):
        with pytest.raises(SystemExit):
            main(args)
    assert not store_root.exists()
    assert not manifest.exists()
    assert not evidence.exists()


def test_cli_cleans_stores_and_first_artifact_when_second_artifact_fails(tmp_path: Path):
    source = tmp_path / "chat.db"
    _create_messages_db(source)
    args, store_root, manifest, evidence = _cli_args(source, tmp_path)
    from scripts import review_imessage_communication_signals as cli

    original = cli.os.replace

    def fail_second(source_path, destination_path) -> None:
        if Path(destination_path).resolve() == manifest.resolve():
            raise OSError("injected second-artifact failure")
        original(source_path, destination_path)

    with mock_patch.object(cli.os, "replace", fail_second):
        with pytest.raises(SystemExit):
            main(args)
    assert not store_root.exists()
    assert not manifest.exists()
    assert not evidence.exists()


def test_temporary_store_guard_covers_external_hermes_home_and_symlink(tmp_path: Path):
    active = tmp_path / "external-hermes"
    active.mkdir()
    alias = tmp_path / "active-alias"
    alias.symlink_to(active, target_is_directory=True)
    with mock_patch.dict(os.environ, {"HERMES_HOME": str(active)}):
        for candidate in (active, active / "descendant", alias / "equivalent"):
            with pytest.raises(ValueError, match="live Hermes profile"):
                _temporary_store_root(candidate)


def test_cli_resolution_failure_does_not_leak_handles_or_source_ids(
    tmp_path: Path, capsys
):
    source = tmp_path / "chat.db"
    _create_messages_db(source)
    con = sqlite3.connect(source)
    con.execute("INSERT INTO chat VALUES(2,'iMessage;-;alias-direct',43,NULL,NULL)")
    con.execute("INSERT INTO chat_handle_join VALUES(2,2)")
    con.commit()
    con.close()
    args, _, _, _ = _cli_args(source, tmp_path)

    with pytest.raises(SystemExit):
        main(args)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    for forbidden in (
        "+140****0100", "steve@example.test", "synthetic-direct-guid", "alias-direct"
    ):
        assert forbidden not in combined


def test_cli_scan_failure_does_not_leak_duplicate_source_guid(tmp_path: Path, capsys):
    source = tmp_path / "chat.db"
    _create_messages_db(source)
    con = sqlite3.connect(source)
    con.execute("UPDATE message SET guid='msg-contact-reply' WHERE ROWID=7")
    con.commit()
    con.close()
    args, _, _, _ = _cli_args(source, tmp_path)

    with pytest.raises(SystemExit):
        main(args)
    captured = capsys.readouterr()
    assert "msg-contact-reply" not in captured.out + captured.err
