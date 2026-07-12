# Contact-memory local extractor benchmark

Date: 2026-07-11

## Decision

Use `mlx-community/Qwen3-4B-Instruct-2507-4bit` at revision
`50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b` as the production local structured
extractor. It is the smallest tested model that cleared both release gates:
90% or better safe extraction and zero unsafe auto-promotion candidates.

The model is installed in the existing isolated contact-memory MLX environment's
Hugging Face cache. Its cached footprint is 2.1 GB. The environment uses
`mlx-lm==0.31.3` and `mlx==0.32.0`; no Hermes runtime dependency was added.

## Reproduction

Run from the repository root on Apple Silicon:

```bash
~/.cache/hermes-contact-memory/embeddinggemma-venv/bin/python \
  scripts/benchmark_contact_memory_extractor.py
```

The script pins all three model revisions, uses greedy decoding, contains only
synthetic privacy-safe messages, and prints aggregate results by default. It
covers ordinary facts, corrections, unresolved contradictions, sensitive and
secret data, sarcasm/noise, third-party attribution, and standing versus
one-time recommendations. `--details` reports only synthetic case category,
pass/fail, and proposal count; it does not print model text.

A case passes only when the JSON envelope and proposal contract are valid and
the expected subject, canonical predicate, object fragments, sensitivity class,
and required mention policy match. An unsafe auto-promotion is any proposal from
an unsafe case that nevertheless looks like a high-confidence, normal,
first-party stated fact. This is suitability screening, not permission to bypass
the deterministic production validator.

## Aggregate results

| Model | Cached size | Passed | Safe extraction | Parse failures | Unsafe auto-promotions | Median/case | p95/case |
|---|---:|---:|---:|---:|---:|---:|---:|
| Qwen3 0.6B 4-bit | 335 MB | 13/28 | 46.43% | 0 | 0 | 249 ms | 268 ms |
| Qwen3 1.7B 4-bit | 938 MB | 17/28 | 60.71% | 0 | 3 | 537 ms | 902 ms |
| Qwen3 4B Instruct 2507 4-bit | 2.1 GB | 26/28 | **92.86%** | 0 | **0** | 1,852 ms | 1,929 ms |

Selected-model category totals were: facts 5/6, corrections 2/2,
contradictions 2/2, sensitivity 5/5, sarcasm/noise 5/5, third-party 3/4, and
recommendations 4/4. Extraction remains suitable only off the response path,
with proposals pending deterministic validation/review.

Timing is single-process sequential generation on the 64 GB Mac Studio after
model load. The benchmark intentionally measures conservative end-to-end case
latency rather than throughput from a future resident batched worker.
