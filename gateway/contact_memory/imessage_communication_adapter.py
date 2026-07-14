"""Deterministic historical Apple Messages adapter for canonical communication evidence.

The scanner is read-only and keeps source artifacts in an in-memory private evidence
map. Only HMAC-derived identities and bounded typed descriptors are emitted to the
Phase A communication store APIs. Aggregate manifests contain counts only.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
import hashlib
import hmac
import json
import re
from typing import Any, Mapping, Sequence
import urllib.parse

from .imessage_bootstrap import ResolvedChat, _apple_timestamp
from .imessage_link_review import _row_urls, classify_url
from .schema import (
    CommunicationActorRole,
    CommunicationAttachment,
    CommunicationBundle,
    CommunicationDirection,
    CommunicationEvent,
    CommunicationKind,
    CommunicationPrivacy,
    CommunicationReactionSubtype,
    CommunicationRelation,
    CommunicationRelationType,
    CommunicationUrl,
)
from .store import ContactMemoryStore

_CANONICAL_AUTHORS = ("kosta-owner", "stephen-lucier")
_REACTION_TYPES: dict[int, CommunicationReactionSubtype] = {
    2000: CommunicationReactionSubtype.LOVE,
    2001: CommunicationReactionSubtype.LIKE,
    2002: CommunicationReactionSubtype.DISLIKE,
    2003: CommunicationReactionSubtype.LAUGH,
    2004: CommunicationReactionSubtype.EMPHASIS,
    2005: CommunicationReactionSubtype.QUESTION,
}
_REACTION_REMOVALS = {code + 1000: subtype for code, subtype in _REACTION_TYPES.items()}
_ACCOUNTING_CATEGORIES = (
    "text", "links", "attachments", "reactions", "replies",
    "explicit_nonsemantic", "rejected",
)
_BATCH_WINDOW_SECONDS = 60.0


def _opaque(secret: bytes, namespace: str, value: str) -> str:
    if len(secret) < 16:
        raise ValueError("HMAC secret must be at least 16 bytes")
    return hmac.new(secret, (namespace + "\0" + value).encode("utf-8"), hashlib.sha256).hexdigest()


def parse_associated_guid(value: object) -> str:
    """Return the original message GUID from modern and legacy Apple wrappers."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    match = re.match(r"^(?:bp|p):(?:(?:\d+)(?:/|:))?(.+)$", raw)
    return match.group(1) if match else raw


def _columns(con: Any, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})")}


def _has_table(con: Any, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _source_key(row: Mapping[str, Any]) -> str:
    return str(row.get("guid") or f"rowid:{int(row['rowid'])}")


def _source_identity(secret: bytes, source_key: str) -> str:
    return _opaque(secret, "imessage-source-v1", source_key)


def _event_identity(secret: bytes, author: str, source_key: str) -> str:
    return _opaque(secret, "imessage-event-v1", author + "\0" + source_key)


def _actor(row: Mapping[str, Any]) -> str:
    return "kosta-owner" if bool(row["is_from_me"]) else "stephen-lucier"


def _visible_text(row: Mapping[str, Any]) -> str | None:
    text = str(row["text"]).strip() if row.get("text") is not None else ""
    if text:
        return text
    # _row_urls intentionally decodes only an explicit attributed-string root.
    # Recover the same visible root without recursively mining preview archives.
    from .imessage_link_review import _attributed_visible_string

    return _attributed_visible_string(row.get("attributedBody"))


def _safe_mime(value: object) -> str | None:
    normalized = str(value or "").strip().casefold()
    return normalized if re.fullmatch(r"[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*", normalized) else None


def _safe_uti(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", normalized) else None


def _media_kind(mime_type: str | None, uti: str | None) -> str:
    marker = (mime_type or uti or "").casefold()
    if marker.startswith("image/") or "image" in marker:
        return "image"
    if marker.startswith("video/") or "movie" in marker or "video" in marker:
        return "video"
    if marker.startswith("audio/") or "audio" in marker:
        return "audio"
    if marker.startswith(("text/", "application/")) or "document" in marker or "pdf" in marker:
        return "document"
    return "other"


@dataclass(frozen=True)
class HistoricalCommunicationRecord:
    author: str
    category: str
    bundle: CommunicationBundle
    private_evidence: Mapping[str, Any]
    retraction_target_event_id: str | None = None


@dataclass(frozen=True)
class HistoricalCommunicationScan:
    records: tuple[HistoricalCommunicationRecord, ...]
    accounting: dict[str, int]
    secondary_counts: dict[str, int]
    private_header: Mapping[str, Any]
    rejected_evidence: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class HistoricalStoreTargets:
    owner: ContactMemoryStore
    guest: ContactMemoryStore

    def for_author(self, author: str) -> ContactMemoryStore:
        if author == "kosta-owner":
            return self.owner
        if author == "stephen-lucier":
            return self.guest
        raise ValueError("unknown canonical historical author")


def _attachment_rows(con: Any) -> dict[int, list[dict[str, Any]]]:
    if not (_has_table(con, "attachment") and _has_table(con, "message_attachment_join")):
        return {}
    columns = _columns(con, "attachment")

    def field(name: str) -> str:
        return f"a.{name}" if name in columns else "NULL"

    guid = field("guid")
    filename = field("filename")
    transfer_name = field("transfer_name")
    mime = field("mime_type")
    uti = field("uti")
    size = field("total_bytes")
    rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in con.execute(
        f"""SELECT maj.message_id,a.ROWID attachment_rowid,{guid} guid,
                   {filename} filename,{transfer_name} transfer_name,{mime} mime_type,
                   {uti} uti,{size} total_bytes
            FROM message_attachment_join maj
            JOIN attachment a ON a.ROWID=maj.attachment_id
            ORDER BY maj.message_id,a.ROWID"""
    ):
        rows[int(row["message_id"])].append(dict(row))
    return dict(rows)


def _message_rows(con: Any, chat: ResolvedChat) -> list[dict[str, Any]]:
    columns = _columns(con, "message")
    required = {"guid", "date", "is_from_me", "text", "associated_message_type"}
    if not required <= columns:
        raise ValueError(f"message table lacks required historical fields: {sorted(required - columns)}")

    def field(name: str) -> str:
        return f"m.{name}" if name in columns else "NULL"

    result = []
    for row in con.execute(
        f"""SELECT m.ROWID rowid,m.guid,m.date,m.is_from_me,m.text,
                   {field('attributedBody')} attributedBody,
                   COALESCE(m.associated_message_type,0) associated_type,
                   {field('associated_message_guid')} associated_guid,
                   {field('thread_originator_guid')} thread_guid,
                   {field('handle_id')} handle_id
            FROM message m JOIN chat_message_join cmj ON cmj.message_id=m.ROWID
            WHERE cmj.chat_id=? ORDER BY m.date,m.ROWID""",
        (chat.chat_id,),
    ):
        result.append(dict(row))
    return result


def scan_historical_communication(
    con: Any,
    chat: ResolvedChat,
    *,
    secret: bytes,
) -> HistoricalCommunicationScan:
    """Scan one authenticated direct chat without writing any contact store."""
    if len(secret) < 16:
        raise ValueError("HMAC secret must be at least 16 bytes")
    rows = _message_rows(con, chat)
    message_columns = _columns(con, "message")
    approved_ids = set(chat.approved_handle_ids or (chat.handle_id,))
    rejected: list[Mapping[str, Any]] = []
    authenticated: list[dict[str, Any]] = []
    for row in rows:
        if bool(row["is_from_me"]):
            authenticated.append(row)
            continue
        if "handle_id" not in message_columns:
            raise ValueError("message.handle_id is required for incoming identity validation")
        incoming_id = int(row.get("handle_id") or 0)
        if incoming_id == 0:
            rejected.append({
                "source_key": _source_key(row),
                "reason": "anonymous_service_handle",
                "text": _visible_text(row),
            })
            continue
        if incoming_id not in approved_ids:
            raise ValueError("incoming message has an unapproved handle_id")
        authenticated.append(row)

    attachments_by_message = _attachment_rows(con)
    by_guid = {
        _source_key(row): row for row in authenticated
    }
    records: list[HistoricalCommunicationRecord] = []
    category_counts: Counter[str] = Counter()
    active_reactions: dict[
        tuple[str, CommunicationReactionSubtype, str], list[str]
    ] = defaultdict(list)

    for row in authenticated:
        source_key = _source_key(row)
        author = _actor(row)
        source_id = _source_identity(secret, source_key)
        event_id = _event_identity(secret, author, source_key)
        occurred_at = _apple_timestamp(row["date"])
        text = _visible_text(row)
        urls = _row_urls(row.get("text"), row.get("attributedBody"))
        raw_attachments = attachments_by_message.get(int(row["rowid"]), [])
        associated_type = int(row.get("associated_type") or 0)
        associated_target = parse_associated_guid(row.get("associated_guid"))
        reply_target = parse_associated_guid(row.get("thread_guid"))
        private: dict[str, Any] = {
            "source_key": source_key,
            "rowid": int(row["rowid"]),
            "author": author,
            "occurred_at": occurred_at,
            "text": text,
            "urls": list(urls),
            "associated_guid": str(row.get("associated_guid") or ""),
            "thread_originator_guid": str(row.get("thread_guid") or ""),
            "attachments": [
                {
                    "rowid": item["attachment_rowid"],
                    "guid": item.get("guid"),
                    "filename": item.get("filename"),
                    "transfer_name": item.get("transfer_name"),
                }
                for item in raw_attachments
            ],
        }

        kind: CommunicationKind
        reaction_subtype: CommunicationReactionSubtype | None = None
        category: str
        relations: list[CommunicationRelation] = []
        retraction_target: str | None = None
        if associated_type in _REACTION_TYPES or associated_type in _REACTION_REMOVALS:
            category = "reactions"
            reaction_subtype = (
                _REACTION_TYPES.get(associated_type) or _REACTION_REMOVALS[associated_type]
            )
            target_row = by_guid.get(associated_target)
            if target_row is None or _actor(target_row) == author:
                rejected.append({**private, "reason": "unauthenticated_reaction_target"})
                category_counts["rejected"] += 1
                continue
            target_source_id = _source_identity(secret, associated_target)
            kind = (
                CommunicationKind.REACTION_ADD
                if associated_type in _REACTION_TYPES
                else CommunicationKind.REACTION_REMOVE
            )
            relations.append(CommunicationRelation(
                relation_id=_opaque(secret, "imessage-relation-v1", event_id + "\0reaction_to\0" + target_source_id),
                event_id=event_id,
                relation_type=CommunicationRelationType.REACTION_TO,
                target_source_id=target_source_id,
                target_actor_role=CommunicationActorRole.COUNTERPART,
            ))
            reaction_key = (author, reaction_subtype, target_source_id)
            if kind is CommunicationKind.REACTION_ADD:
                active_reactions[reaction_key].append(event_id)
            else:
                if not active_reactions[reaction_key]:
                    rejected.append({**private, "reason": "unmatched_reaction_removal"})
                    category_counts["rejected"] += 1
                    continue
                retraction_target = active_reactions[reaction_key].pop()
            event_text = None
        elif reply_target:
            target_row = by_guid.get(reply_target)
            if target_row is None or _actor(target_row) == author:
                rejected.append({**private, "reason": "unauthenticated_reply_target"})
                category_counts["rejected"] += 1
                continue
            category = "replies"
            kind = CommunicationKind.REPLY
            target_source_id = _source_identity(secret, reply_target)
            relations.append(CommunicationRelation(
                relation_id=_opaque(secret, "imessage-relation-v1", event_id + "\0reply_to\0" + target_source_id),
                event_id=event_id,
                relation_type=CommunicationRelationType.REPLY_TO,
                target_source_id=target_source_id,
                target_actor_role=CommunicationActorRole.COUNTERPART,
            ))
            event_text = text
        elif raw_attachments:
            category = "attachments"
            kind = CommunicationKind.ATTACHMENT_SHARE
            event_text = text
        elif urls:
            category = "links"
            kind = CommunicationKind.LINK_SHARE
            event_text = text
        elif text:
            category = "text"
            kind = CommunicationKind.TEXT
            event_text = text
        else:
            category = "explicit_nonsemantic"
            kind = CommunicationKind.TEXT
            event_text = None

        text_hash = (
            _opaque(secret, "imessage-text-v1", event_text) if event_text is not None else None
        )
        event = CommunicationEvent(
            event_id=event_id,
            platform="imessage",
            source_id=source_id,
            occurred_at=occurred_at,
            direction=CommunicationDirection.INBOUND,
            kind=kind,
            actor_role=CommunicationActorRole.CONTACT,
            reaction_subtype=reaction_subtype,
            privacy=CommunicationPrivacy.PRIVATE,
            text_hash=text_hash,
            text_present=event_text is not None,
            text_length=len(event_text) if event_text is not None else 0,
            provenance="historical-imessage-v1",
        )
        url_children = tuple(
            CommunicationUrl(
                url_id=_opaque(secret, "imessage-url-row-v1", event_id + "\0" + url),
                event_id=event_id,
                url_identity=_opaque(secret, "imessage-url-v1", url),
                domain=(urllib.parse.urlsplit(url).hostname or "").casefold(),
                sharer_role=CommunicationActorRole.CONTACT,
                platform=classify_url(url)[0],
            )
            for url in urls
        )
        attachment_children = []
        for item in raw_attachments:
            attachment_source = str(
                item.get("guid") or f"attachment-rowid:{int(item['attachment_rowid'])}"
            )
            identity = _opaque(secret, "imessage-attachment-v1", attachment_source)
            mime_type = _safe_mime(item.get("mime_type"))
            uti = _safe_uti(item.get("uti"))
            raw_size = item.get("total_bytes")
            size = int(raw_size) if isinstance(raw_size, int) and 0 <= raw_size <= 10_000_000_000 else None
            attachment_children.append(CommunicationAttachment(
                attachment_id=_opaque(secret, "imessage-attachment-row-v1", event_id + "\0" + identity),
                event_id=event_id,
                attachment_identity=identity,
                media_kind=_media_kind(mime_type, uti),
                mime_type=mime_type,
                uti=uti,
                size_bytes=size,
                caption_present=event_text is not None,
                caption_hash=(
                    _opaque(secret, "imessage-caption-v1", event_text)
                    if event_text is not None else None
                ),
            ))
        bundle = CommunicationBundle(
            event=event,
            urls=url_children,
            attachments=tuple(attachment_children),
            relations=tuple(relations),
        )
        records.append(HistoricalCommunicationRecord(
            author=author,
            category=category,
            bundle=bundle,
            private_evidence=private,
            retraction_target_event_id=retraction_target,
        ))
        category_counts[category] += 1

    # Add deterministic rapid-message batch relations after the full sequence is
    # known. A batch is same-actor chronological evidence separated by <=60 s.
    groups: list[list[int]] = []
    current: list[int] = []
    for index, record in enumerate(records):
        if record.bundle.event.kind in {
            CommunicationKind.REACTION_ADD, CommunicationKind.REACTION_REMOVE
        }:
            if current:
                groups.append(current)
                current = []
            continue
        if current and (
            records[current[-1]].author != record.author
            or record.bundle.event.occurred_at
            - records[current[-1]].bundle.event.occurred_at
            > _BATCH_WINDOW_SECONDS
        ):
            groups.append(current)
            current = []
        current.append(index)
    if current:
        groups.append(current)
    batch_members = 0
    for group in groups:
        if len(group) < 2:
            continue
        author = records[group[0]].author
        source_ids = [records[index].bundle.event.source_id for index in group]
        batch_id = _opaque(secret, "imessage-batch-v1", author + "\0" + "\0".join(source_ids))
        for index in group:
            record = records[index]
            relation = CommunicationRelation(
                relation_id=_opaque(
                    secret, "imessage-relation-v1",
                    record.bundle.event.event_id + "\0batch_member_of\0" + batch_id,
                ),
                event_id=record.bundle.event.event_id,
                relation_type=CommunicationRelationType.BATCH_MEMBER_OF,
                target_source_id=batch_id,
            )
            records[index] = replace(
                record,
                bundle=replace(
                    record.bundle,
                    relations=tuple((*record.bundle.relations, relation)),
                ),
            )
            batch_members += 1

    category_counts["rejected"] += len([
        item for item in rejected if item.get("reason") == "anonymous_service_handle"
    ])
    accounting = {"selected": len(rows)}
    accounting.update({key: int(category_counts[key]) for key in _ACCOUNTING_CATEGORIES})
    if sum(accounting[key] for key in _ACCOUNTING_CATEGORIES) != accounting["selected"]:
        raise AssertionError("historical communication no-drop accounting failed")
    secondary = {
        "canonical_events": len(records),
        "batch_members": batch_members,
        "url_evidence": sum(len(record.bundle.urls) for record in records),
        "attachment_evidence": sum(len(record.bundle.attachments) for record in records),
        "reaction_retractions": sum(record.retraction_target_event_id is not None for record in records),
    }
    return HistoricalCommunicationScan(
        records=tuple(records),
        accounting=accounting,
        secondary_counts=secondary,
        private_header={
            "chat_id": chat.chat_id,
            "chat_guid": chat.chat_guid,
            "approved_handle_ids": list(chat.approved_handle_ids),
            "approved_handle": chat.handle,
        },
        rejected_evidence=tuple(rejected),
    )


def ingest_historical_scan(
    scan: HistoricalCommunicationScan,
    targets: HistoricalStoreTargets,
) -> dict[str, dict[str, int]]:
    """Apply a prevalidated scan through only the Phase A append/retract APIs."""
    counts = {
        author: {"inserted": 0, "deduplicated": 0}
        for author in _CANONICAL_AUTHORS
    }
    for record in scan.records:
        store = targets.for_author(record.author)
        bundle = record.bundle
        if bundle.event.kind is CommunicationKind.REACTION_REMOVE:
            if record.retraction_target_event_id is None:
                raise ValueError("reaction removal lacks a deterministic add target")
            result = store.retract_communication_event(
                bundle.event,
                target_event_id=record.retraction_target_event_id,
                relations=bundle.relations,
            )
        else:
            result = store.ingest_communication_event(
                bundle.event,
                urls=bundle.urls,
                attachments=bundle.attachments,
                relations=bundle.relations,
                entity_mentions=bundle.entity_mentions,
                recommendation_events=bundle.recommendation_events,
            )
        counts[record.author]["inserted"] += int(result.inserted)
        counts[record.author]["deduplicated"] += int(result.deduplicated)
    return counts


def build_aggregate_manifest(
    scan: HistoricalCommunicationScan,
    *,
    secret: bytes,
) -> dict[str, Any]:
    """Build a signed, aggregate-only review manifest without source identities."""
    actors = Counter(record.author for record in scan.records)
    rejection_reasons = Counter(
        str(item.get("reason") or "unspecified") for item in scan.rejected_evidence
    )
    categories_by_actor: dict[str, Counter[str]] = defaultdict(Counter)
    for record in scan.records:
        categories_by_actor[record.author][record.category] += 1
    manifest: dict[str, Any] = {
        "schema": 1,
        "kind": "imessage-canonical-communication-review",
        "apply_supported": False,
        "evidence_commitment": _opaque(
            secret,
            "imessage-private-evidence-v1",
            json.dumps(
                build_private_evidence_manifest(scan),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
        "accounting": dict(scan.accounting),
        "rejection_reasons": dict(sorted(rejection_reasons.items())),
        "secondary_counts": dict(scan.secondary_counts),
        "actors": {
            author: {
                "canonical_events": actors[author],
                "categories": dict(sorted(categories_by_actor[author].items())),
            }
            for author in _CANONICAL_AUTHORS
        },
    }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    manifest["review_id"] = _opaque(secret, "imessage-communication-review-v1", payload)
    return manifest


def verify_aggregate_manifest(manifest: Mapping[str, Any], *, secret: bytes) -> bool:
    """Verify the review HMAC without reading or exposing private evidence."""
    supplied = manifest.get("review_id")
    if not isinstance(supplied, str) or not re.fullmatch(r"[0-9a-f]{64}", supplied):
        return False
    unsigned = dict(manifest)
    unsigned.pop("review_id", None)
    payload = json.dumps(unsigned, sort_keys=True, separators=(",", ":"))
    expected = _opaque(secret, "imessage-communication-review-v1", payload)
    return hmac.compare_digest(supplied, expected)


def build_private_evidence_manifest(scan: HistoricalCommunicationScan) -> dict[str, Any]:
    """Build the operator-only drill-down artifact containing raw source evidence."""
    return {
        "schema": 1,
        "kind": "private-imessage-canonical-communication-evidence",
        "chat": dict(scan.private_header),
        "records": [
            {
                **dict(record.private_evidence),
                "category": record.category,
                "canonical_event_id": record.bundle.event.event_id,
                "canonical_source_id": record.bundle.event.source_id,
                "retraction_target_event_id": record.retraction_target_event_id,
            }
            for record in scan.records
        ],
        "rejected": [dict(item) for item in scan.rejected_evidence],
    }


__all__ = [
    "HistoricalCommunicationRecord",
    "HistoricalCommunicationScan",
    "HistoricalStoreTargets",
    "build_aggregate_manifest",
    "build_private_evidence_manifest",
    "ingest_historical_scan",
    "parse_associated_guid",
    "scan_historical_communication",
    "verify_aggregate_manifest",
]
