"""Closed-scope orchestration for contact-aware retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .embeddings import EmbeddingBackend
from .gating import LaneAGate, TurnState
from .rerankers import Reranker
from .schema import RetrievalPrincipal, SearchResult
from .security import can_retrieve, render_recall
from .store import ContactMemoryStore


@dataclass(frozen=True)
class RetrievalScope:
    principal: RetrievalPrincipal
    contact_id: str
    session_key: str

    def __post_init__(self) -> None:
        if not self.contact_id.strip() or not self.session_key.strip():
            raise ValueError("trusted contact_id and session_key are required")


@dataclass(frozen=True)
class RecallBundle:
    rendered: str = ""
    fact_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.fact_ids


class ContactMemoryBroker:
    def __init__(self, root: str | Path, *, embedding_backend: EmbeddingBackend | None = None, reranker: Reranker | None = None, gate: LaneAGate | None = None):
        self.root = Path(root)
        self.embedding_backend = embedding_backend
        self.reranker = reranker
        self.gate = gate or LaneAGate()

    def _store(self, scope: RetrievalScope) -> ContactMemoryStore:
        return ContactMemoryStore(self.root, scope.contact_id)

    @staticmethod
    def _merge(results: Sequence[SearchResult], recent: set[str], limit: int) -> list[SearchResult]:
        by_id: dict[str, SearchResult] = {}
        for result in results:
            prior = by_id.get(result.fact.version_id)
            lexical = max(result.lexical_score, prior.lexical_score if prior else 0.0)
            semantic = max(result.semantic_score, prior.semantic_score if prior else 0.0)
            score = 0.45 * lexical + 0.55 * max(0.0, semantic)
            score += 0.05 * result.fact.trust + 0.05 * result.fact.confidence
            if result.fact.version_id in recent:
                score -= 1.0
            by_id[result.fact.version_id] = SearchResult(result.fact, score, lexical, semantic)
        visible = [
            result for result in by_id.values()
            if result.score > 0 and can_retrieve(RetrievalPrincipal.OWNER, result.fact)
        ]
        # The broad owner check above only rejects invalid lifecycle/restricted rows;
        # caller applies its principal again before rendering.
        visible.sort(key=lambda result: result.score, reverse=True)
        return visible[:limit]

    def _rerank(self, query: str, results: Sequence[SearchResult]) -> list[SearchResult]:
        """Rerank candidates, failing open to first-stage order on any error."""
        if self.reranker is None or len(results) < 2:
            return list(results)
        documents = [
            f"{result.fact.subject_id} {result.fact.predicate} {result.fact.object_text}"
            for result in results
        ]
        try:
            scores = self.reranker.score(query, documents)
            if len(scores) != len(results):
                return list(results)
            # Stable sorting preserves candidate order for equal quantized logits.
            return [
                result for _, result in sorted(
                    zip(scores, results), key=lambda pair: pair[0], reverse=True
                )
            ]
        except Exception:
            return list(results)

    def search(self, scope: RetrievalScope, query: str, limit: int = 3, *, turn_index: int = 0) -> RecallBundle:
        if not query.strip():
            return RecallBundle()
        store = self._store(scope)
        results = store.lexical_search(scope.principal, query, limit=max(10, limit * 3))
        if self.embedding_backend is not None:
            vector = self.embedding_backend.encode([query])[0]
            results += store.vector_search(scope.principal, vector, model_id=self.embedding_backend.model_id, limit=max(10, limit * 3))
        recent = store.recently_retrieved(scope.session_key, turn_index)
        candidate_limit = max(10, limit * 3)
        ranked = self._merge(results, recent, candidate_limit)
        ranked = self._rerank(query, ranked)[:max(0, min(int(limit), 3))]
        facts = [result.fact for result in ranked if can_retrieve(scope.principal, result.fact)]
        rendered = render_recall(facts, scope.principal)
        ids = tuple(fact.version_id for fact in facts) if rendered else ()
        return RecallBundle(rendered, ids)

    def prefetch(self, scope: RetrievalScope, message: str, history: Sequence[Mapping[str, object]], turn_state: TurnState) -> RecallBundle:
        vector = turn_state.current_vector
        if vector is None and self.embedding_backend is not None and len(message.split()) >= 4:
            vector = self.embedding_backend.encode([message])[0]
            turn_state = TurnState(
                register=turn_state.register, closure=turn_state.closure,
                reaction=turn_state.reaction, turn_index=turn_state.turn_index,
                now=turn_state.now, current_vector=vector,
            )
        decision = self.gate.evaluate(scope.session_key, message, history, turn_state)
        if not decision.retrieve:
            return RecallBundle(reasons=decision.reasons)
        bundle = self.search(scope, message, 3, turn_index=turn_state.turn_index)
        if bundle.fact_ids:
            self._store(scope).record_recall(scope.session_key, turn_state.turn_index, bundle.fact_ids)
        return RecallBundle(bundle.rendered, bundle.fact_ids, decision.reasons)

    def record_usage(self, scope: RetrievalScope, fact_ids: Sequence[str], *, turn_index: int) -> None:
        self._store(scope).record_recall(scope.session_key, turn_index, fact_ids, "used")

    def index_approved_facts(self, contact_id: str) -> int:
        """Embed active owner-visible facts using document retrieval prefixes."""
        if self.embedding_backend is None:
            raise RuntimeError("an embedding backend is required for indexing")
        store = ContactMemoryStore(self.root, contact_id)
        facts = store.active_facts(RetrievalPrincipal.OWNER)
        if not facts:
            return 0
        texts = [f"{fact.subject_id} {fact.predicate} {fact.object_text}" for fact in facts]
        encode_documents = getattr(self.embedding_backend, "encode_documents", None)
        vectors: Any = (
            encode_documents(texts)
            if callable(encode_documents)
            else self.embedding_backend.encode(texts)
        )
        if len(vectors) != len(facts):
            raise RuntimeError("embedding backend returned the wrong vector count")
        for fact, vector in zip(facts, vectors):
            store.put_embedding(fact.version_id, self.embedding_backend.model_id, vector)
        return len(facts)
