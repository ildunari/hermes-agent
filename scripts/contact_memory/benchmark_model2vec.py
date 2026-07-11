#!/usr/bin/env python3
"""Privacy-safe local Model2Vec retrieval benchmark.

No private contact data is read. The synthetic suite includes direction,
negation, date, and reference-resolution contrasts.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import time

import numpy as np
from model2vec import StaticModel

CASES = [
    ("should I keep the car", "The current car is a green hatchback with low mileage.", ["The car was sold last year.", "A friend owns a green bicycle."]),
    ("did he text back", "Alex has not replied to the message yet.", ["Alex replied yesterday.", "Taylor sent Alex a message."]),
    ("when is the appointment", "The dentist appointment is July 18, 2026.", ["The dentist appointment was July 18, 2025.", "The July 18 dinner was cancelled."]),
    ("what did we decide about Lisbon", "The plan is to travel to Lisbon, not Oslo.", ["The plan is to travel to Oslo, not Lisbon.", "Lisbon is west of Madrid."]),
    ("is she still avoiding dairy", "Morgan does not eat dairy.", ["Morgan started eating dairy again.", "Morgan does not avoid gluten."]),
]


def rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / (1024 * 1024) if sys.platform == "darwin" else value / 1024


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="minishlab/potion-retrieval-32M")
    parser.add_argument("--offline-check", action="store_true")
    args = parser.parse_args()
    started = time.perf_counter()
    model = StaticModel.from_pretrained(args.model)
    load_ms = (time.perf_counter() - started) * 1000
    latencies = []
    correct = 0
    for query, positive, negatives in CASES:
        texts = [query, positive, *negatives]
        t0 = time.perf_counter()
        vectors = np.asarray(model.encode(texts), dtype=np.float32)
        latencies.append((time.perf_counter() - t0) * 1000)
        vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
        scores = vectors[1:] @ vectors[0]
        correct += int(int(np.argmax(scores)) == 0)
    result = {
        "model": args.model,
        "model2vec": __import__("model2vec").__version__,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "load_ms": round(load_ms, 2),
        "encode_ms_p50": round(statistics.median(latencies), 2),
        "encode_ms_max": round(max(latencies), 2),
        "rss_mb": round(rss_mb(), 2),
        "synthetic_top1": f"{correct}/{len(CASES)}",
    }
    if args.offline_check and not os.environ.get("HF_HUB_OFFLINE"):
        env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        command = [sys.executable, __file__, "--model", args.model]
        completed = subprocess.run(command, env=env, text=True, capture_output=True, timeout=120)
        result["offline_reload"] = completed.returncode == 0
        if completed.returncode != 0:
            result["offline_error"] = completed.stderr[-500:]
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
