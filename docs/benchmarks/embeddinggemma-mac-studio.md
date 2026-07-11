# EmbeddingGemma contact-memory benchmark — Mac Studio

Date: 2026-07-11. Host: Apple M2 Max, 64 GiB, macOS 26.5, Python 3.13.12.

## Recommendation: reject for production retrieval

Do not promote either MLX checkpoint as the contact-memory retriever. The 4-bit model is operationally excellent but achieves only **17/24 top-1 (70.8%)** on the synthetic contrast suite. Its weakest categories are exactly the dangerous ones for personal facts: direction **1/4** and negation **2/4**. A wrong top-1 match can invert who did what or whether something happened. Top-3 is 24/24, so the model could be reconsidered as a candidate generator only if a reliable reranker and end-to-end privacy/visibility tests are added.

The 8-bit model does not rescue quality: **16/24 top-1 (66.7%)**, with effectively identical latency and 148 MiB more process RSS. Prefer 4-bit if this model is used in further offline experiments. Do not download or deploy bf16: the 8-bit comparison already shows no local quality gain, while the bf16 repository is about 624 MiB.

## Method

`benchmark_embeddinggemma.py` uses the model-card retrieval prefixes (`task: search result | query:` and `title: none | text:`), consumes normalized `text_embeds`, and searches one shared 72-document corpus. The 24 queries cover direction, negation, dates, pronouns, reference resolution, and paraphrase. Every answer has two deliberately confusable hard negatives. This makes top-3 meaningful rather than evaluating against only three documents.

Warm latency uses 50 measured iterations after a compilation/allocation warm-up at batch sizes 1, 2, 4, 8, 16, and 32. Load time is a fresh Python process with artifacts already in the filesystem cache; it is not a cache-drop or first-download measurement. The offline check starts another process with `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`, verifies a real embedding, and records its shape.

An optional local dossier check read 151 records and emitted only aggregate counts—never private fact text, labels, IDs, or rankings. Only two labels were unique enough to define a single expected document, producing top-1 0/2 and top-3 1/2. That sample is too small and its label-template query is too artificial to be a promotion gate; it is retained only as a privacy-path smoke test.

## Results

| Metric | 4-bit | 8-bit |
|---|---:|---:|
| Resolved revision | `5d9ef074df3957afc5c77127f208fddbc3c54187` | `c8316c9e35cf8830541b04d315398c67faa6e497` |
| Output shape | 2 × 768 | 2 × 768 |
| Process load, warm filesystem cache | 1320.49 ms | 1377.83 ms |
| Batch 1 p50 / p95 | 4.91 / 6.56 ms | 5.15 / 6.60 ms |
| Batch 8 p50 / p95 | 8.66 / 10.05 ms | 8.49 / 9.93 ms |
| Batch 32 p50 / p95 | 24.87 / 26.24 ms | 25.04 / 27.22 ms |
| Batch 32 throughput | 1286.71 items/s | 1277.71 items/s |
| Max process RSS | 1096.66 MiB | 1244.75 MiB |
| Synthetic top-1 | 17/24 (70.8%) | 16/24 (66.7%) |
| Synthetic top-3 | 24/24 | 24/24 |
| MRR | 0.8542 | 0.8333 |
| Minimum positive margin | -0.0592 | -0.0684 |
| Offline reload | pass, 1 × 768 | pass, 1 × 768 |

The full machine-readable outputs are:

- `docs/benchmarks/embeddinggemma-mac-studio-4bit.json`
- `docs/benchmarks/embeddinggemma-mac-studio-8bit.json`

## Reproduce

```bash
scripts/contact_memory/install_embeddinggemma.sh
PY=~/.cache/hermes-contact-memory/embeddinggemma-venv/bin/python

$PY scripts/contact_memory/benchmark_embeddinggemma.py \
  --repeats 50 --offline-check \
  --output docs/benchmarks/embeddinggemma-mac-studio-4bit.json

$PY scripts/contact_memory/benchmark_embeddinggemma.py \
  --model mlx-community/embeddinggemma-300m-8bit \
  --repeats 50 --offline-check \
  --output docs/benchmarks/embeddinggemma-mac-studio-8bit.json
```

A private aggregate smoke test is opt-in:

```bash
$PY scripts/contact_memory/benchmark_embeddinggemma.py \
  --offline-check --dossier /secure/path/facts.jsonl
```

The script defaults to synthetic-only operation. It never prints case text. The optional dossier mode reports only row count, count of eligible unique labels, and aggregate top-1/top-3 totals.

## Promotion gate for a future candidate

Before enabling contact-memory retrieval, require at minimum: a larger frozen benchmark with realistic multi-fact dossiers; top-1 reported separately for direction and negation; adversarial superseded/current-fact cases; end-to-end visibility filtering before and after scoring; and an explicit abstention/reranking strategy. A reasonable initial offline gate is at least 90% overall top-1 with no category below 85%, followed by shadow-mode evaluation. EmbeddingGemma 4-bit does not meet that gate.
