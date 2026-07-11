"""Typed, asynchronous extraction proposal boundary.

Extractors cannot write active facts. They return untrusted dictionaries which
are validated into ``FactProposal`` objects and inserted only into pending_fact.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Awaitable, Callable, Mapping

from .schema import AssertionType, Audience, FactProposal, FactStatus, MentionPolicy
from .store import ContactMemoryStore

Extractor = Callable[[str, str, Mapping[str, Any]], Awaitable[list[Mapping[str, Any]]]]


def validate_pending_operation(raw: Mapping[str, Any], contact_id: str, source_id: str) -> FactProposal:
    allowed = {"logical_id", "subject_id", "predicate", "object_text", "audience", "mention_policy", "assertion_type", "trust", "confidence", "evidence_pointer", "metadata"}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unknown proposal fields: {sorted(unknown)}")
    return FactProposal(
        logical_id=str(raw.get("logical_id") or ""),
        subject_id=str(raw.get("subject_id") or ""), predicate=str(raw.get("predicate") or ""),
        object_text=str(raw.get("object_text") or ""),
        audience=Audience(str(raw.get("audience") or "owner_review")),
        mention_policy=MentionPolicy(str(raw.get("mention_policy") or "background")),
        assertion_type=AssertionType(str(raw.get("assertion_type") or "inferred")),
        source_id=source_id, source_contact_id=contact_id,
        evidence_pointer=str(raw.get("evidence_pointer") or ""),
        trust=float(raw.get("trust", 0.5)), confidence=float(raw.get("confidence", 0.5)),
        status=FactStatus.PENDING, metadata=dict(raw.get("metadata") or {}),
    )


async def propose_turn_memories(store: ContactMemoryStore, extractor: Extractor, user_text: str, assistant_text: str, metadata: Mapping[str, Any]) -> list[str]:
    source_id = str(metadata.get("source_id") or "").strip()
    if not source_id:
        raise ValueError("trusted source_id is required")
    raw_operations = await extractor(user_text, assistant_text, metadata)
    proposal_ids: list[str] = []
    for index, raw in enumerate(raw_operations):
        proposal = validate_pending_operation(raw, store.contact_id, source_id)
        stable = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        key = hashlib.sha256(f"{source_id}\0{index}\0{stable}".encode()).hexdigest()
        proposal_ids.append(await asyncio.to_thread(store.add_pending, proposal, key))
    return proposal_ids
