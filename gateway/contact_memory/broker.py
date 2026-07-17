"""Closed-scope orchestration for contact-aware retrieval."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from html import escape
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
    recommendation_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.fact_ids and not self.recommendation_ids


class ContactMemoryBroker:
    def __init__(self, root: str | Path, *, embedding_backend: EmbeddingBackend | None = None, reranker: Reranker | None = None, gate: LaneAGate | None = None, minimum_reranker_score: float = -6.5):
        self.root = Path(root)
        self.embedding_backend = embedding_backend
        self.reranker = reranker
        self.gate = gate or LaneAGate()
        self.minimum_reranker_score = float(minimum_reranker_score)

    def close(self) -> None:
        """Close cached local-model subprocesses owned by this broker."""
        seen: set[int] = set()
        for backend in (self.embedding_backend, self.reranker):
            if backend is None or id(backend) in seen:
                continue
            seen.add(id(backend))
            close = getattr(backend, "close", None)
            if callable(close):
                close()

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

    @staticmethod
    def _hybrid_candidates(lexical: Sequence[SearchResult], semantic: Sequence[SearchResult], recent: set[str], limit: int) -> list[SearchResult]:
        """Fuse unlike score spaces while preserving strong lexical categories."""
        by_id: dict[str, SearchResult] = {}
        fusion: defaultdict[str, float] = defaultdict(float)
        for results in (lexical, semantic):
            for rank, result in enumerate(results, 1):
                key = result.fact.version_id
                prior = by_id.get(key)
                by_id[key] = SearchResult(
                    result.fact, 0.0,
                    max(result.lexical_score, prior.lexical_score if prior else 0.0),
                    max(result.semantic_score, prior.semantic_score if prior else 0.0),
                )
                fusion[key] += 1.0 / (10.0 + rank)
        ranked = sorted(
            (
                result for result in by_id.values()
                if fusion[result.fact.version_id] - (1.0 if result.fact.version_id in recent else 0.0) > 0
            ),
            key=lambda result: (
                fusion[result.fact.version_id] - (1.0 if result.fact.version_id in recent else 0.0),
                result.fact.trust, result.fact.confidence,
            ),
            reverse=True,
        )
        selected = ranked[:limit]
        selected_ids = {result.fact.version_id for result in selected}
        lexical_quota = [
            result for result in lexical[:5]
            if result.fact.version_id not in selected_ids
            and result.fact.version_id not in recent
        ]
        if lexical_quota:
            selected = selected[:max(0, limit - len(lexical_quota))] + lexical_quota
        return [
            SearchResult(result.fact, fusion[result.fact.version_id], result.lexical_score, result.semantic_score)
            for result in selected
        ]

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
                SearchResult(
                    result.fact, float(score),
                    getattr(result, "lexical_score", 0.0),
                    getattr(result, "semantic_score", 0.0),
                )
                for score, result in sorted(
                    zip(scores, results), key=lambda pair: pair[0], reverse=True
                )
            ]
        except Exception:
            return list(results)

    def search(self, scope: RetrievalScope, query: str, limit: int = 3, *, turn_index: int = 0, direct_ask: bool = False) -> RecallBundle:
        if not query.strip():
            return RecallBundle()
        store = self._store(scope)
        search_limit = max(20, limit * 5)
        lexical = store.lexical_search(scope.principal, query, limit=search_limit)
        semantic: list[SearchResult] = []
        if self.embedding_backend is not None:
            vector = self.embedding_backend.encode([query])[0]
            semantic = store.vector_search(
                scope.principal, vector,
                model_id=self.embedding_backend.model_id,
                limit=search_limit,
            )
        recent = store.recently_retrieved(scope.session_key, turn_index)
        candidate_limit = max(10, limit * 3)
        ranked = self._hybrid_candidates(lexical, semantic, recent, candidate_limit)
        anchor = ranked[0] if ranked else None
        ranked = self._rerank(query, ranked)
        if self.reranker is not None and ranked and ranked[0].score < self.minimum_reranker_score:
            return RecallBundle()
        top = ranked[:3]
        if anchor is not None and all(item.fact.version_id != anchor.fact.version_id for item in top):
            ranked = top[:2] + [anchor] + ranked[3:]
        facts = [
            result.fact for result in ranked
            if can_retrieve(scope.principal, result.fact)
            and self.callback_available(
                scope, result.fact.version_id, turn_index=turn_index,
                direct_ask=direct_ask,
            )
        ][:max(0, min(int(limit), 3))]
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
        recommendation_text = ""
        recommendation_ids: tuple[str, ...] = ()
        store = self._store(scope)
        visible_ids = {
            fact.version_id for fact in store.active_facts(scope.principal)
        }
        recommendations = [
            row for row in store.active_recommendations(now=turn_state.now)
            if set(row["basis_fact_ids"] if isinstance(row["basis_fact_ids"], list) else ()).issubset(visible_ids)
            and not store.callback_on_cooldown(
                scope.session_key, str(row["recommendation_id"]),
                subject_type="recommendation", turn_index=turn_state.turn_index,
                now=turn_state.now,
            )
        ][:2]
        if recommendations:
            recommendation_ids = tuple(
                str(row["recommendation_id"]) for row in recommendations
            )
            lines = [
                f'<recommendation topic="{escape(str(row["topic"]), quote=True)}">'
                f'{escape(str(row["recommendation"]))}</recommendation>'
                for row in recommendations
            ]
            recommendation_text = (
                "<recommendations data-only=\"true\">\n"
                + "\n".join(lines)
                + "\n</recommendations>"
            )
        rendered = "\n".join(
            part for part in (bundle.rendered, recommendation_text) if part
        )
        return RecallBundle(
            rendered, bundle.fact_ids, recommendation_ids, decision.reasons
        )

    def record_usage(self, scope: RetrievalScope, fact_ids: Sequence[str], *, turn_index: int, recommendation_ids: Sequence[str] = (), now: float | None = None) -> None:
        """Record facts actually used as callbacks after a successful turn."""
        store = self._store(scope)
        store.record_recall(scope.session_key, turn_index, fact_ids, "used", now=now)
        for fact_id in dict.fromkeys(fact_ids):
            store.record_callback(scope.session_key, fact_id, turn_index=turn_index, now=now)
        for recommendation_id in dict.fromkeys(recommendation_ids):
            store.record_callback(
                scope.session_key, recommendation_id, subject_type="recommendation",
                turn_index=turn_index, now=now,
            )

    def callback_available(self, scope: RetrievalScope, fact_id: str, *, turn_index: int, direct_ask: bool = False, now: float | None = None) -> bool:
        """Direct questions bypass callback cooldown; unsolicited callbacks do not."""
        return direct_ask or not self._store(scope).callback_on_cooldown(
            scope.session_key, fact_id, turn_index=turn_index, now=now
        )

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
