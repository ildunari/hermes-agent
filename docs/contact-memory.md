# Contact-aware retrieval

Contact memory is local, contact-scoped SQLite state used only by trusted gateway routes. It is disabled by default.

```yaml
agent:
  contact_memory:
    enabled: false
    lane_a: false
    embedding:
      backend: off # or embeddinggemma
      model: mlx-community/embeddinggemma-300m-4bit
      timeout_seconds: 120
    reranker:
      backend: off # set qwen3-mlx only after indexing EmbeddingGemma
      model: mlx-community/Qwen3-Reranker-0.6B-4bit
      timeout_seconds: 0.35
      startup_timeout_seconds: 120
```

When both feature switches are enabled, Lane A runs only for an immutable contact scope assigned after authenticated BlueBubbles routing. Approved Guest messages receive the contact's Guest scope. The owner/Poke route receives owner scope only when the trusted contact registry declares `owner_contact_id`. Forwarded unapproved group requests deliberately receive no scope.

Recall is appended to an API-call copy of the current user message through `AIAgent.per_turn_user_context`. It never changes the cached system prompt, cached-agent signature, caller-owned history, persisted transcript, or role alternation. Retrieval and optional embedding failures fail open to a normal reply.

Data lives under `$HERMES_HOME/contact-memory/contacts/` using SHA-256-derived opaque filenames. Guest visibility requires an active, stated, high-trust/high-confidence `guest_ok` or `public` fact. Restricted, sensitive, inferred, pending, quarantined, withdrawn, and superseded facts are excluded before scoring and checked again before rendering.

The trusted BlueBubbles contact registry can opt the authenticated owner/Poke route into one contact namespace:

```yaml
owner_contact_id: steve
```

This binding belongs in the trusted registry alongside `owner_identities`; it is never accepted from a message, `SessionSource`, or model tool argument.

## Import and review

Dry-run first (default):

```bash
python -m gateway.contact_memory.import_contacts dossier.jsonl --contact-id contact-id --manifest /tmp/review.json
```

Apply only after reviewing the manifest:

```bash
python -m gateway.contact_memory.import_contacts dossier.jsonl --contact-id contact-id --apply
```

The manifest contains counts, source IDs, and validation errors, not fact text. Existing sensitive, third-party, and ambiguous rows become owner-only/quarantined. Guest visibility additionally requires explicit `guest_reviewed: true` and `audience: guest_ok`.

Imports are one transaction and idempotent by source ID. Retrying a dossier skips existing source IDs; a quarantined proposal cannot supersede an already reviewed active fact.

Review pending extractor proposals and accept or reject them explicitly:

```bash
python -m gateway.contact_memory.admin --contact-id contact-id review
python -m gateway.contact_memory.admin --contact-id contact-id accept PROPOSAL_ID --audience owner_only
python -m gateway.contact_memory.admin --contact-id contact-id reject PROPOSAL_ID
```

Export owner-visible active facts or securely clear the namespace:

```bash
python -m gateway.contact_memory.admin --contact-id contact-id export --output /secure/path/export.json
python -m gateway.contact_memory.admin --contact-id contact-id delete --confirm contact-id
```

Deletion enables SQLite secure-delete, checkpoints/truncates WAL, and vacuums the database. Filesystem snapshots and backups are outside SQLite's deletion boundary and must be handled separately.

## EmbeddingGemma backend

Install the GPL `mlx-embeddings` runtime into its isolated worker environment (it is not a core dependency):

```bash
scripts/contact_memory/install_embeddinggemma.sh
```

The worker is persistent and communicates with Hermes over local JSON lines. It loads `mlx-community/embeddinggemma-300m-4bit`, uses the model's retrieval-query prefix at runtime and retrieval-document prefix while indexing, and returns normalized vectors to the core process. Embed all approved active facts after imports/reviews:

```bash
python -m gateway.contact_memory.admin --contact-id contact-id index
```

Use `--python-path` only for a nonstandard isolated environment. Runtime backend/model/timeout are profile-scoped `config.yaml` settings shown above. Missing environments, worker crashes, timeouts, model errors, and corrupt stores produce no recall rather than blocking the message.

EmbeddingGemma alone is not a production ranker (17/24 synthetic top-1). It is
retained as the candidate generator for the Qwen3 second stage below.

## Qwen3 MLX reranker

Install and cache the Apache-2.0 reranker in the same isolated MLX environment:

```bash
scripts/contact_memory/install_qwen3_reranker.sh
```

The persistent worker reranks the merged lexical + EmbeddingGemma top 10 using
Qwen3's yes/no relevance logits. It prefills the shared instruction/query once
and broadcasts the KV cache across candidates. Inference has a strict 350 ms
default timeout; timeout, crash, malformed output, or model failure preserves
the first-stage ordering rather than blocking a reply. Model startup has a
separate timeout because loading occurs once per gateway process.

The Mac Studio production-gate run passed at 23/24 top-1, direction 4/4,
negation 3/4, and 209.28 ms warm end-to-end p95 (50 samples). The candidate
generator covered all 24 targets. Enable `embeddinggemma` and `qwen3-mlx` in
the profile config shown above only after approved facts are indexed. The
reranker remains `off` in repository defaults, so the change is ready for live
activation without silently enabling contact recall.

Reproduce the privacy-safe benchmark (output contains aggregate synthetic
metrics only):

```bash
PY=~/.cache/hermes-contact-memory/embeddinggemma-venv/bin/python
$PY scripts/contact_memory/benchmark_qwen3_reranker.py --repeats 50 \
  --output docs/benchmarks/qwen3-reranker-mac-studio.json
```

Lane B is intentionally not shipped: a model-facing query tool was unnecessary for Lane A and no new core schema was justified. Pending extraction also remains operator-reviewed rather than being enabled asynchronously without a validated local extractor.

## Optional Model2Vec benchmark

Model2Vec and NumPy are intentionally not core dependencies. Install into an isolated local environment and benchmark only synthetic data:

```bash
scripts/contact_memory/install_model2vec.sh
~/.cache/hermes-contact-memory/model2vec-venv/bin/python scripts/contact_memory/benchmark_model2vec.py --offline-check
```

Pinned tooling: Model2Vec 0.8.2 (MIT), NumPy 2.4.3 (BSD-3-Clause). The candidate model is `minishlab/potion-retrieval-32M`; record the resolved Hugging Face revision from the local cache before promotion. Do not enable retrieval solely from this tiny synthetic smoke benchmark.

### Mac Studio result (2026-07-11)

- Model revision: `6fc8051fab2a1e0ee76689cf08c853792ac285e7`
- Python 3.13.12 / Darwin arm64; native install succeeded
- Cold load/download: 4259.51 ms; warm encode p50: 0.23 ms; max: 6.68 ms
- Process max RSS: 615.69 MiB; offline reload: passed
- Synthetic direction/negation/date/reference top-1: 3/5

This candidate fails the initial quality smoke gate despite excellent warm latency. It remains tooling-only and Lane A stays hard-blocked; no production embedding model is selected.

## EmbeddingGemma benchmark

A larger privacy-safe benchmark is available for MLX EmbeddingGemma. It uses a shared synthetic corpus with direction, negation, date, pronoun, reference, and paraphrase contrasts, records batch p50/p95, resolves the cached model revision, and verifies an actual offline reload. An optional dossier path emits aggregate metrics only and never fact text.

```bash
scripts/contact_memory/install_embeddinggemma.sh
PY=~/.cache/hermes-contact-memory/embeddinggemma-venv/bin/python
$PY scripts/contact_memory/benchmark_embeddinggemma.py --repeats 50 --offline-check
```

Mac Studio evaluation rejected both candidates for production retrieval. The 4-bit checkpoint reached 17/24 synthetic top-1 (direction 1/4, negation 2/4); 8-bit regressed to 16/24 while using more memory. Both reached 24/24 top-3 and had roughly 5 ms batch-1 warm p50, so 4-bit may be useful only for further candidate-generation/reranking experiments. Full methodology, revisions, results, and promotion gates are in [`benchmarks/embeddinggemma-mac-studio.md`](benchmarks/embeddinggemma-mac-studio.md). Lane A remains hard-blocked.
