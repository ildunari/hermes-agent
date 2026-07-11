#!/usr/bin/env python3
"""Privacy-safe EmbeddingGemma + MLX Qwen3 reranker production-gate benchmark."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import platform
import statistics
import sys
import time

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from gateway.contact_memory.embeddings import EmbeddingGemmaBackend  # noqa: E402
from gateway.contact_memory.rerankers import Qwen3RerankerBackend  # noqa: E402
from scripts.contact_memory.benchmark_embeddinggemma import CASES, revision_for  # noqa: E402


def percentile(values: list[float], p: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), p))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--embedding-model", default="mlx-community/embeddinggemma-300m-4bit")
    parser.add_argument("--reranker-model", default="mlx-community/Qwen3-Reranker-0.6B-4bit")
    parser.add_argument("--candidate-limit", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=25)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    documents: list[str] = []
    targets: list[int] = []
    for case in CASES:
        targets.append(len(documents))
        documents.extend((case.positive, *case.negatives))

    embedder = EmbeddingGemmaBackend(args.embedding_model, timeout_seconds=120)
    reranker = Qwen3RerankerBackend(
        args.reranker_model, timeout_seconds=10, startup_timeout_seconds=120
    )
    try:
        doc_vectors = np.asarray(embedder.encode_documents(documents), dtype=np.float32)
        query_vectors = np.asarray(
            embedder.encode([case.query for case in CASES]), dtype=np.float32
        )
        category = Counter()
        ranks: list[int] = []
        coverage = 0
        for case, target, query_vector in zip(CASES, targets, query_vectors, strict=True):
            candidate_ids = np.argsort(-(doc_vectors @ query_vector))[: args.candidate_limit]
            coverage += target in candidate_ids
            candidate_docs = [documents[int(index)] for index in candidate_ids]
            scores = reranker.score(case.query, candidate_docs)
            order = sorted(range(len(scores)), key=scores.__getitem__, reverse=True)
            target_local = next((i for i, index in enumerate(candidate_ids) if index == target), -1)
            rank = order.index(target_local) + 1 if target_local >= 0 else len(documents) + 1
            ranks.append(rank)
            category[(case.category, "n")] += 1
            category[(case.category, "top1")] += rank == 1

        # Warm both models and compiled shapes before measuring the complete
        # candidate-generation + top-10 reranking path.
        probe_query = CASES[0].query
        probe_vector = np.asarray(embedder.encode([probe_query])[0], dtype=np.float32)
        probe_ids = np.argsort(-(doc_vectors @ probe_vector))[: args.candidate_limit]
        reranker.score(probe_query, [documents[int(index)] for index in probe_ids])
        timings: list[float] = []
        for index in range(args.repeats):
            query = CASES[index % len(CASES)].query
            started = time.perf_counter()
            query_vector = np.asarray(embedder.encode([query])[0], dtype=np.float32)
            candidate_ids = np.argsort(-(doc_vectors @ query_vector))[: args.candidate_limit]
            reranker.score(query, [documents[int(candidate)] for candidate in candidate_ids])
            timings.append((time.perf_counter() - started) * 1000)

        per_category = {
            name: {"n": category[(name, "n")], "top1": category[(name, "top1")]}
            for name in sorted({case.category for case in CASES})
        }
        top1 = sum(rank == 1 for rank in ranks)
        direction = per_category["direction"]["top1"]
        negation = per_category["negation"]["top1"]
        p95 = percentile(timings, 95)
        gate = top1 >= 22 and direction >= 3 and negation >= 3 and p95 < 400
        result = {
            "schema_version": 1,
            "platform": platform.platform(),
            "models": {
                "candidate": {"id": args.embedding_model, "revision": revision_for(args.embedding_model)},
                "reranker": {"id": args.reranker_model, "revision": revision_for(args.reranker_model)},
            },
            "candidate_limit": args.candidate_limit,
            "synthetic": {
                "cases": len(CASES), "corpus_documents": len(documents),
                "candidate_coverage": coverage, "top1": top1,
                "mrr": round(statistics.mean(1 / rank for rank in ranks), 4),
                "per_category": per_category,
            },
            "warm_retrieval_latency_ms": {
                "samples": len(timings), "p50": round(percentile(timings, 50), 2),
                "p95": round(p95, 2), "max": round(max(timings), 2),
            },
            "production_gate": {
                "requirements": {"top1": "at least 22/24", "direction": "at least 3/4", "negation": "at least 3/4", "warm_p95_ms": "below 400"},
                "passed": gate,
            },
        }
        payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, encoding="utf-8")
        print(payload, end="")
        return 0 if gate else 2
    finally:
        reranker.close()
        embedder.close()


if __name__ == "__main__":
    raise SystemExit(main())
