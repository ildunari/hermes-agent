"""Canonical persistence for authenticated BlueBubbles live ingress.

This module converts immutable transport envelopes into the Phase A ledger. It
never accepts routing identifiers from an adapter or plugin: callers must supply
the already-authenticated contact/principal and the profile-local store root.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import stat
import tempfile
from typing import Sequence
import urllib.parse

from gateway.platforms.base import CommunicationIngressEnvelope

from .imessage_communication_adapter import _media_kind, _opaque, _safe_mime, _safe_uti
from .imessage_link_review import canonicalize_url, classify_url, is_safe_fetch_candidate
from .private_link_queue import PrivateLinkResearchQueue
from .schema import (
    CommunicationActorRole,
    CommunicationAttachment,
    CommunicationDirection,
    CommunicationEvent,
    CommunicationIngestResult,
    CommunicationKind,
    CommunicationPrivacy,
    CommunicationReactionSubtype,
    CommunicationRelation,
    CommunicationRelationType,
    CommunicationUrl,
)
from .store import ContactMemoryStore


_KEY_NAME = ".communication-hmac-key"
_REACTION_SUBTYPES = {
    "like": CommunicationReactionSubtype.LIKE,
    "love": CommunicationReactionSubtype.LOVE,
    "dislike": CommunicationReactionSubtype.DISLIKE,
    "laugh": CommunicationReactionSubtype.LAUGH,
    "emphasis": CommunicationReactionSubtype.EMPHASIS,
    "question": CommunicationReactionSubtype.QUESTION,
}
_KIND = {value.value: value for value in CommunicationKind}


@dataclass(frozen=True)
class LiveCommunicationIngestResult:
    event_ids: tuple[str, ...]
    inserted: int
    deduplicated: int
    queued_links: int = 0


def load_or_create_communication_key(root: str | Path) -> bytes:
    """Return one owner-only profile key shared by live and reviewed backfill."""
    directory = Path(root).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    path = directory / _KEY_NAME
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        secret = os.urandom(32)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{_KEY_NAME}.", dir=directory
        )
        try:
            try:
                written = 0
                while written < len(secret):
                    count = os.write(fd, secret[written:])
                    if count <= 0:
                        raise OSError("communication HMAC key write made no progress")
                    written += count
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(temporary_name, path, follow_symlinks=False)
            except FileExistsError:
                return load_or_create_communication_key(directory)
        finally:
            os.unlink(temporary_name)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return secret
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError("communication HMAC key must be an owner-only regular file")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise PermissionError("communication HMAC key has the wrong owner")
        secret = os.read(fd, 4096)
    finally:
        os.close(fd)
    if len(secret) < 16:
        raise ValueError("communication HMAC key is too short")
    return secret


def _source_identity(secret: bytes, source_key: str) -> str:
    return _opaque(secret, "imessage-source-v1", source_key)


def _event_identity(secret: bytes, contact_id: str, source_key: str) -> str:
    return _opaque(secret, "imessage-event-v1", contact_id + "\0" + source_key)


def _relation(
    secret: bytes,
    event_id: str,
    relation_type: CommunicationRelationType,
    target_source_id: str,
    *,
    target_actor_role: CommunicationActorRole | None = None,
) -> CommunicationRelation:
    return CommunicationRelation(
        relation_id=_opaque(
            secret,
            "imessage-relation-v1",
            event_id + "\0" + relation_type.value + "\0" + target_source_id,
        ),
        event_id=event_id,
        relation_type=relation_type,
        target_source_id=target_source_id,
        target_actor_role=target_actor_role,
    )


def _batch_identity(secret: bytes, contact_id: str, envelopes: Sequence[CommunicationIngressEnvelope]) -> str | None:
    if len(envelopes) < 2:
        return None
    source_ids = [_source_identity(secret, item.source_message_id) for item in envelopes]
    return _opaque(secret, "imessage-batch-v1", contact_id + "\0" + "\0".join(source_ids))


def persist_live_communication_ingress(
    *,
    root: str | Path,
    contact_id: str,
    principal: str,
    envelopes: Sequence[CommunicationIngressEnvelope],
    secret: bytes | None = None,
    enqueue_link_research: bool = False,
) -> LiveCommunicationIngestResult:
    """Persist an authenticated ordered direct-message envelope sequence."""
    if principal not in {"owner", "guest"}:
        raise ValueError("principal is not an authenticated direct contact")
    if not contact_id or any(item.chat_type != "dm" for item in envelopes):
        raise ValueError("canonical live ingress requires one direct contact")
    immutable = tuple(envelopes)
    if not immutable:
        return LiveCommunicationIngestResult((), 0, 0, 0)
    directions = {item.direction for item in immutable}
    if len(directions) != 1 or not directions <= {"inbound", "outbound"}:
        raise ValueError("live contact ingress cannot mix directions")
    owner_reactions = directions == {"outbound"}
    if owner_reactions and any(
        item.event_kind not in {"reaction_add", "reaction_remove"} for item in immutable
    ):
        raise ValueError("outbound canonical ingress accepts only owner reactions")
    if len({item.sender_identity for item in immutable}) != 1:
        raise ValueError("one ingress batch cannot mix transport senders")
    key = bytes(secret) if secret is not None else load_or_create_communication_key(root)
    if len(key) < 16:
        raise ValueError("HMAC secret must be at least 16 bytes")
    store = ContactMemoryStore(root, contact_id)
    target_roles: dict[str, CommunicationActorRole] = {}
    for envelope in immutable:
        source_id = _source_identity(key, envelope.source_message_id)
        existing = store.get_communication_event_by_source("imessage", source_id)
        target_roles[source_id] = existing.actor_role if existing is not None else (
            CommunicationActorRole.COUNTERPART
            if owner_reactions else CommunicationActorRole.CONTACT
        )
    for envelope in immutable:
        for relation_name, target in (
            ("reply", envelope.reply_target),
            ("reaction", envelope.reaction_target),
        ):
            if target is None:
                continue
            target_source_id = _source_identity(key, target)
            target_role = target_roles.get(target_source_id)
            if target_role is None:
                target_event = store.get_communication_event_by_source(
                    "imessage", target_source_id
                )
                target_role = target_event.actor_role if target_event is not None else None
            if target_role is None:
                raise ValueError(f"unauthenticated {relation_name} target")
            expected_target = (
                CommunicationActorRole.CONTACT
                if owner_reactions else CommunicationActorRole.COUNTERPART
            )
            if relation_name == "reaction" and target_role is not expected_target:
                if owner_reactions:
                    raise ValueError("owner reaction target is not contact-authored")
                raise ValueError("reaction target is not a counterpart")
            target_roles[target_source_id] = target_role
    batch_id = _batch_identity(key, contact_id, immutable)
    event_ids: list[str] = []
    inserted = deduplicated = queued_links = 0
    private_queue = PrivateLinkResearchQueue(root, contact_id) if enqueue_link_research else None

    for envelope in immutable:
        source_id = _source_identity(key, envelope.source_message_id)
        event_id = _event_identity(key, contact_id, envelope.source_message_id)
        event_ids.append(event_id)
        kind = _KIND[envelope.event_kind]
        reaction_subtype = (
            _REACTION_SUBTYPES[envelope.reaction_kind]
            if envelope.reaction_kind is not None else None
        )
        evidence_text = "" if reaction_subtype is not None else envelope.visible_text
        text_hash = _opaque(key, "imessage-text-v1", evidence_text) if evidence_text else None
        event = CommunicationEvent(
            event_id=event_id,
            platform="imessage",
            source_id=source_id,
            occurred_at=envelope.occurred_at,
            direction=(
                CommunicationDirection.OUTBOUND if owner_reactions
                else CommunicationDirection.INBOUND
            ),
            kind=kind,
            actor_role=(
                CommunicationActorRole.COUNTERPART if owner_reactions
                else CommunicationActorRole.CONTACT
            ),
            reaction_subtype=reaction_subtype,
            privacy=CommunicationPrivacy.PRIVATE,
            text_hash=text_hash,
            text_present=bool(evidence_text),
            text_length=len(evidence_text),
            provenance="live-bluebubbles-v1",
        )
        urls = tuple(
            CommunicationUrl(
                url_id=_opaque(key, "imessage-url-row-v1", event_id + "\0" + url),
                event_id=event_id,
                url_identity=_opaque(key, "imessage-url-v1", url),
                domain=(urllib.parse.urlsplit(url).hostname or "").casefold(),
                sharer_role=(
                    CommunicationActorRole.COUNTERPART if owner_reactions
                    else CommunicationActorRole.CONTACT
                ),
                platform=classify_url(url)[0],
            )
            for url in envelope.visible_urls
            if urllib.parse.urlsplit(url).hostname
        )
        attachments = []
        for descriptor in envelope.attachments:
            identity = _opaque(
                key, "imessage-attachment-v1", descriptor.source_attachment_id
            )
            mime_type = _safe_mime(descriptor.mime_type)
            uti = _safe_uti(descriptor.uti)
            attachments.append(CommunicationAttachment(
                attachment_id=_opaque(
                    key, "imessage-attachment-row-v1", event_id + "\0" + identity
                ),
                event_id=event_id,
                attachment_identity=identity,
                media_kind=_media_kind(mime_type, uti),
                mime_type=mime_type,
                uti=uti,
                size_bytes=descriptor.size_bytes,
                caption_present=bool(evidence_text),
                caption_hash=(
                    _opaque(key, "imessage-caption-v1", evidence_text)
                    if evidence_text else None
                ),
            ))
        relations: list[CommunicationRelation] = []
        if envelope.reply_target:
            reply_source_id = _source_identity(key, envelope.reply_target)
            relations.append(_relation(
                key,
                event_id,
                CommunicationRelationType.REPLY_TO,
                reply_source_id,
                target_actor_role=target_roles[reply_source_id],
            ))
        if envelope.reaction_target:
            reaction_source_id = _source_identity(key, envelope.reaction_target)
            relations.append(_relation(
                key,
                event_id,
                CommunicationRelationType.REACTION_TO,
                reaction_source_id,
                target_actor_role=target_roles[reaction_source_id],
            ))
        if batch_id is not None and reaction_subtype is None:
            relations.append(_relation(
                key, event_id, CommunicationRelationType.BATCH_MEMBER_OF, batch_id
            ))

        existing = store.get_communication_event_by_source("imessage", source_id)
        if existing is not None and existing.event_id != event_id:
            raise ValueError("live source identity belongs to another event")
        if existing is not None and kind is CommunicationKind.REACTION_REMOVE:
            if existing != event:
                raise ValueError("reaction removal replay conflicts with stored evidence")
            outcome = CommunicationIngestResult(existing, inserted=False, deduplicated=True)
        elif existing is not None:
            # A pre-ACK write can precede rapid-turn batching. Verify the frozen
            # event first, then add only the newly known batch relation/children.
            outcome = store.ingest_communication_event(event)
            store.enrich_communication_event(
                event_id,
                urls=urls,
                attachments=attachments,
                relations=relations,
            )
        elif kind is CommunicationKind.REACTION_REMOVE:
            assert reaction_subtype is not None and envelope.reaction_target is not None
            target_event_id = store.find_latest_reaction_add(
                platform="imessage",
                reaction_subtype=reaction_subtype,
                target_source_id=_source_identity(key, envelope.reaction_target),
            )
            if target_event_id is None:
                target_event_id = _opaque(
                    key,
                    "imessage-pending-reaction-v1",
                    contact_id + "\0" + reaction_subtype.value + "\0" + envelope.reaction_target,
                )
            outcome = store.retract_communication_event(
                event, target_event_id=target_event_id, relations=relations
            )
        else:
            outcome = store.ingest_communication_event(
                event, urls=urls, attachments=attachments, relations=relations
            )
        inserted += int(outcome.inserted)
        deduplicated += int(outcome.deduplicated)
        if envelope.reaction_target and reaction_subtype in {
            CommunicationReactionSubtype.LIKE,
            CommunicationReactionSubtype.LOVE,
        }:
            target = store.get_communication_event_by_source(
                "imessage", _source_identity(key, envelope.reaction_target)
            )
            if target is not None and private_queue is not None:
                engagement_count = store.positive_link_engagement_count(target.event_id)
                private_queue.engage_event(
                    target.event_id,
                    score=engagement_count * 4,
                    now=envelope.occurred_at,
                )
                if engagement_count == 0:
                    store.deactivate_projector_event_family(
                        target.event_id,
                        projector_prefix="phase-e-live-link-v4.e.",
                        now=envelope.occurred_at,
                    )
        if private_queue is None:
            continue
        for item in urls:
            exact = next((
                canonicalize_url(raw) for raw in envelope.visible_urls
                if _opaque(key, "imessage-url-row-v1", event_id + "\0" + raw) == item.url_id
            ), None)
            if exact is None or not is_safe_fetch_candidate(exact):
                continue
            queued_links += int(private_queue.enqueue(
                job_id=_opaque(key, "imessage-link-research-job-v1", item.url_id),
                event_id=event_id,
                url_id=item.url_id,
                exact_url=exact,
                occurred_at=envelope.occurred_at,
                recency_bucket=0,
                engagement_score=0,
                repeated_shares=1,
                distinct_days=1,
            ))

    return LiveCommunicationIngestResult(tuple(event_ids), inserted, deduplicated, queued_links)
