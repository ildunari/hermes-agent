#!/usr/bin/env python3
"""Privacy-safe MLX EmbeddingGemma retrieval and performance benchmark.

The default suite is entirely synthetic. Optional dossier evaluation reports only
aggregate label-retrieval metrics and never emits private text, labels, or IDs.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import time
from typing import Iterable

import mlx.core as mx
import numpy as np
from huggingface_hub import scan_cache_dir
from mlx_embeddings.utils import load

QUERY_PREFIX = "task: search result | query: "
DOCUMENT_PREFIX = "title: none | text: "


@dataclass(frozen=True)
class Case:
    category: str
    query: str
    positive: str
    negatives: tuple[str, ...]


CASES = (
    Case("direction", "Who lent the camera to Elena?", "Marcus lent his camera to Elena for the weekend.", ("Elena lent her camera to Marcus for the weekend.", "Marcus returned Elena's tripod after the weekend.")),
    Case("direction", "Who does Noor supervise?", "Noor supervises Daniel on the robotics project.", ("Daniel supervises Noor on the robotics project.", "Noor and Daniel attended the robotics conference.")),
    Case("direction", "Who owes Ava money?", "Ben owes Ava forty dollars for concert tickets.", ("Ava owes Ben forty dollars for concert tickets.", "Ben bought Ava concert tickets as a gift.")),
    Case("direction", "Who sent the parcel to Theo?", "Rina sent Theo a parcel containing two books.", ("Theo sent Rina a parcel containing two books.", "Rina asked Theo to buy two books.")),
    Case("negation", "Has Alex replied to the message?", "Alex has not replied to the message yet.", ("Alex replied to the message yesterday.", "Taylor has not sent Alex a message yet.")),
    Case("negation", "Is Morgan currently eating dairy?", "Morgan does not eat dairy.", ("Morgan started eating dairy again.", "Morgan avoids gluten but still eats dairy.")),
    Case("negation", "Did the Lisbon plan get cancelled?", "The Lisbon trip remains booked and was not cancelled.", ("The Lisbon trip was cancelled yesterday.", "The Oslo trip remains booked and was not cancelled.")),
    Case("negation", "Does Imani still work remotely?", "Imani no longer works remotely and now goes to the office.", ("Imani still works remotely every weekday.", "Imani's manager no longer works remotely.")),
    Case("date", "When is Priya's dentist appointment?", "Priya's dentist appointment is July 18, 2026.", ("Priya's dentist appointment was July 18, 2025.", "Priya's dinner reservation is July 18, 2026.")),
    Case("date", "What year did Omar move to Denver?", "Omar moved to Denver in 2022.", ("Omar moved away from Denver in 2022.", "Omar visited Denver in 2021.")),
    Case("date", "Which meeting is scheduled for March 4 at 9 AM?", "The budget review is scheduled for March 4 at 9 AM.", ("The budget review is scheduled for March 4 at 3 PM.", "The design review is scheduled for March 4 at 9 AM.")),
    Case("date", "When does Mei's passport expire?", "Mei's passport expires on November 12, 2028.", ("Mei renewed her passport on November 12, 2023.", "Mei's visa expires on November 12, 2028.")),
    Case("pronoun", "What instrument does Sofia say her brother plays?", "Sofia said that her brother Luis plays the cello.", ("Sofia plays the cello while her brother Luis plays piano.", "Luis said that his sister Sofia plays the cello.")),
    Case("pronoun", "Whose keys did Jamal put on the desk?", "Jamal found Erin's keys and put them on the desk.", ("Erin found Jamal's keys and put them on the desk.", "Jamal put his notebook beside Erin's desk.")),
    Case("pronoun", "Who will Clara call after she lands?", "Clara told Nina, 'I will call you after I land.'", ("Nina told Clara, 'I will call you after I land.'", "Clara will email Nina before the flight departs.")),
    Case("pronoun", "Which pet needs medication, according to Leo?", "Leo said his dog Pepper needs medication each morning.", ("Leo said his cat Miso needs medication each morning.", "Pepper belongs to Leo's neighbor and needs no medication.")),
    Case("reference", "What color is Sam's current car?", "Sam's current vehicle, a 2024 hatchback, is forest green.", ("Sam's previous car, sold last year, was forest green.", "Sam's current bicycle is forest green.")),
    Case("reference", "Where is the package called Project Aurora stored?", "The Project Aurora materials are in locker C17.", ("Aurora, the sailboat, is docked at pier C17.", "The Project Borealis materials are in locker C17.")),
    Case("reference", "What did we decide about the venue nicknamed The Glasshouse?", "The team chose the Riverside Conservatory, known as The Glasshouse, for the reception.", ("The team rejected the Riverside Conservatory for the reception.", "The Glass House cafe will cater the office lunch.")),
    Case("reference", "Which doctor is Maya referring to as her specialist?", "Maya's specialist is Dr. Chen, her cardiologist.", ("Maya's primary-care doctor is Dr. Patel.", "Dr. Chen is Maya's neighbor, not her physician.")),
    Case("paraphrase", "Where can I find the spare apartment key?", "An extra key to the flat is hidden inside the blue flowerpot.", ("The blue flowerpot was moved to the balcony.", "The spare garage remote is inside a kitchen drawer.")),
    Case("paraphrase", "What food allergy does Diego have?", "Diego must avoid peanuts because they trigger an allergic reaction.", ("Diego dislikes almonds but has no allergy to them.", "Diego's sister has a severe peanut allergy.")),
    Case("paraphrase", "How does Hana prefer to be contacted for urgent matters?", "For anything time-sensitive, Hana wants a phone call rather than email.", ("Hana prefers email for routine updates.", "Hana calls her manager about urgent matters.")),
    Case("paraphrase", "What is the Wi-Fi password at the workshop?", "The workshop wireless network credential is copper-river-27.", ("The workshop alarm code is 2727.", "The office wireless network credential is copper-river-27.")),
)


def percentile(values: list[float], p: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), p))


def rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / (1024 * 1024) if sys.platform == "darwin" else value / 1024


def revision_for(repo_id: str) -> dict[str, str] | None:
    try:
        matches = [r for r in scan_cache_dir().repos if r.repo_id == repo_id]
        if not matches:
            return None
        revisions = sorted(matches[0].revisions, key=lambda r: r.last_modified, reverse=True)
        rev = revisions[0]
        return {
            "commit": rev.commit_hash,
            "snapshot_path": str(rev.snapshot_path).replace(str(Path.home()), "~", 1),
        }
    except Exception:
        return None


class Embedder:
    def __init__(self, model_id: str, dimension: int = 768):
        t0 = time.perf_counter()
        self.model, self.tokenizer = load(model_id)
        mx.eval(self.model.parameters())
        self.load_ms = (time.perf_counter() - t0) * 1000
        self.dimension = dimension

    def encode(self, texts: Iterable[str], kind: str) -> np.ndarray:
        prefix = QUERY_PREFIX if kind == "query" else DOCUMENT_PREFIX
        encoded = self.tokenizer(
            [prefix + text for text in texts], padding=True, truncation=True,
            max_length=2048, return_tensors="mlx",
        )
        output = self.model(encoded["input_ids"], encoded["attention_mask"])
        vectors = output.text_embeds[:, : self.dimension]
        vectors = vectors / mx.maximum(mx.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
        mx.eval(vectors)
        return np.asarray(vectors, dtype=np.float32)


def synthetic_quality(embedder: Embedder) -> dict:
    category = Counter()
    category_top3 = Counter()
    ranks: list[int] = []
    margins: list[float] = []
    # Search one shared corpus, not a three-document toy set. Every case contributes
    # its answer and two deliberately confusable hard negatives (72 documents).
    corpus: list[str] = []
    targets: list[int] = []
    for case in CASES:
        targets.append(len(corpus))
        corpus.extend((case.positive, *case.negatives))
    documents = embedder.encode(corpus, "document")
    queries = embedder.encode([case.query for case in CASES], "query")
    for case, target, query in zip(CASES, targets, queries, strict=True):
        scores = documents @ query
        order = np.argsort(-scores)
        rank = int(np.where(order == target)[0][0]) + 1
        ranks.append(rank)
        category[(case.category, "n")] += 1
        category[(case.category, "top1")] += rank == 1
        category_top3[case.category] += rank <= 3
        non_target = np.delete(scores, target)
        margins.append(float(scores[target] - np.max(non_target)))
    per_category = {}
    for name in sorted({c.category for c in CASES}):
        n = category[(name, "n")]
        per_category[name] = {
            "n": n,
            "top1": category[(name, "top1")],
            "top3": category_top3[name],
        }
    return {
        "cases": len(CASES), "corpus_documents": len(corpus),
        "top1": sum(r == 1 for r in ranks), "top3": sum(r <= 3 for r in ranks),
        "mrr": round(sum(1 / r for r in ranks) / len(ranks), 4),
        "positive_margin_p50": round(statistics.median(margins), 4),
        "positive_margin_min": round(min(margins), 4), "per_category": per_category,
    }


def dossier_quality(embedder: Embedder, path: Path) -> dict:
    """Evaluate retrieval of fact types without returning private content or labels."""
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
                if isinstance(row.get("fact_type"), str) and isinstance(row.get("fact_value"), str):
                    rows.append(row)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
    counts = Counter(row["fact_type"] for row in rows)
    # A label must have one unambiguous target; repeated types cannot define top-1 truth.
    eligible = [row for row in rows if counts[row["fact_type"]] == 1]
    if not eligible:
        return {"rows_read": len(rows), "eligible_unique_labels": 0, "top1": None, "top3": None}
    docs = embedder.encode([row["fact_value"] for row in rows], "document")
    top1 = top3 = 0
    index_by_identity = {id(row): i for i, row in enumerate(rows)}
    for row in eligible:
        query = f"Find the contact fact whose category is {row['fact_type'].replace('_', ' ')}"
        scores = docs @ embedder.encode([query], "query")[0]
        order = np.argsort(-scores)
        target = index_by_identity[id(row)]
        rank = int(np.where(order == target)[0][0]) + 1
        top1 += rank == 1
        top3 += rank <= 3
    return {"rows_read": len(rows), "eligible_unique_labels": len(eligible), "top1": top1, "top3": top3}


def latency(embedder: Embedder, repeats: int) -> dict:
    samples = [case.query for case in CASES]
    result = {}
    # Prime compilation and allocator before measuring warm runs.
    embedder.encode(samples[:2], "query")
    for batch in (1, 2, 4, 8, 16, 32):
        texts = [samples[i % len(samples)] for i in range(batch)]
        timings = []
        for _ in range(repeats):
            t0 = time.perf_counter()
            embedder.encode(texts, "query")
            timings.append((time.perf_counter() - t0) * 1000)
        result[str(batch)] = {
            "p50_ms": round(percentile(timings, 50), 2),
            "p95_ms": round(percentile(timings, 95), 2),
            "items_per_second_p50": round(batch / (percentile(timings, 50) / 1000), 2),
        }
    return result


def offline_check(args: argparse.Namespace) -> dict:
    env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    command = [sys.executable, __file__, "--model", args.model, "--dimension", str(args.dimension), "--offline-child"]
    completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=180)
    result: dict[str, object] = {"passed": completed.returncode == 0}
    if completed.returncode == 0:
        try:
            child = json.loads(completed.stdout)
            result["load_ms"] = child["load_ms"]
            result["embedding_shape"] = child["embedding_shape"]
        except (json.JSONDecodeError, KeyError):
            result["passed"] = False
            result["error"] = "offline child returned invalid JSON"
    else:
        result["error"] = completed.stderr[-500:].replace(str(Path.home()), "~")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="mlx-community/embeddinggemma-300m-4bit")
    parser.add_argument("--dimension", type=int, choices=(128, 256, 512, 768), default=768)
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--offline-check", action="store_true")
    parser.add_argument("--offline-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dossier", type=Path, help="Optional private JSONL; output remains aggregate-only")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    embedder = Embedder(args.model, args.dimension)
    if args.offline_child:
        vectors = embedder.encode(["offline cache verification"], "query")
        print(json.dumps({"load_ms": round(embedder.load_ms, 2), "embedding_shape": list(vectors.shape)}))
        return 0

    probe = embedder.encode(["shape probe", "second shape probe"], "query")
    result = {
        "schema_version": 1, "model": args.model, "revision": revision_for(args.model),
        "dimension": args.dimension, "embedding_shape": list(probe.shape),
        "python": platform.python_version(), "platform": platform.platform(),
        "packages": {name: version(name) for name in ("mlx", "mlx-embeddings", "numpy", "huggingface-hub")},
        "load_ms": round(embedder.load_ms, 2),
        "latency": latency(embedder, args.repeats),
        "synthetic": synthetic_quality(embedder),
        "max_rss_mb": round(rss_mb(), 2),
    }
    if args.dossier:
        result["dossier_aggregate"] = dossier_quality(embedder, args.dossier)
    if args.offline_check:
        result["offline_reload"] = offline_check(args)
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
