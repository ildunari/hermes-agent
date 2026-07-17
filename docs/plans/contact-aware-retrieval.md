# Contact-Aware Retrieval for Poke and Guest

Status: default-off storage/review foundation; retrieval injection blocked
Date: 2026-07-11
Branch: `local/studio-slim`

## Objective

Build a local, privacy-safe contact-memory foundation. Storage, import, review, and offline broker evaluation exist. Live retrieval injection remains hard-blocked until Hermes has a genuinely cache-safe provider-request suffix.

The current routing candidate is Guest BlueBubbles DM-only. Poke/owner routing, Lane B, owner shadow mode, extractor integration, and a production semantic backend are not implemented and must not be inferred from this plan.

## Non-negotiable invariants

1. No API-key or metered retrieval/extraction service.
2. Guest never selects a contact namespace and never reads source dossier files.
3. Namespace and audience are independent. A fact in Steve's store may still be owner-only.
4. Pending, quarantined, superseded, withdrawn, inferred, restricted, or unauthorized rows never enter Guest candidates.
5. Authorization bindings stay in trusted gateway config and are never inferred from memories.
6. Retrieved data is escaped and rendered as inert data, never instructions.
7. The system prompt/cache signature stays byte-stable; per-turn recall is execution-only ephemeral context.
8. Every fact and edge has complete provenance and bitemporal lifecycle fields.
9. Writes are append-only supersession under `BEGIN IMMEDIATE`; at most one active version exists per logical fact.
10. Retrieval/extractor failure is fail-open for texting: the reply continues with no recall.

## Data layout

```text
$HERMES_HOME/contact-memory/
  registry.sqlite3                 # global canonical entities; no facts
  contacts/
    <opaque-contact-id>.sqlite3    # facts/edges/vectors/ledgers for one contact
  models/
    embeddings/
    extractor/
  review/
    pending-export.jsonl
```

The filename is derived from a trusted contact ID through a one-way stable hash, never a display name or caller argument.

### Global registry

Canonical entities only: people, places, organizations, things, and events. It contains no retrievable fact text and no authorization bindings.

### Contact database

Tables:

- `fact`: normalized statements with subject, predicate, object, audience, sensitivity, assertion type, provenance, trust, confidence, status, and bitemporal fields.
- `edge`: typed relations with independent provenance/audience/lifecycle.
- `embedding`: normalized little-endian float32 vectors keyed by immutable fact version and model ID.
- `recommendation`: append-only per-topic recommendation state.
- `recall_event`: retrieved/used callback ledger and cooldown state.
- `pending_fact`: extractor proposals; invisible to retrieval.
- `schema_meta`: schema and embedding generations.

Audience: `owner_only | owner_review | guest_ok | public`.
Sensitivity/mention policy: `background | mentionable | sensitive | restricted`.
Subjects: canonical entity IDs, including Steve, Kosta, relationship, shared event, and third parties.

## Retrieval broker contract

```python
class RetrievalPrincipal(Enum):
    OWNER = "owner"
    GUEST = "guest"

@dataclass(frozen=True)
class RetrievalScope:
    principal: RetrievalPrincipal
    contact_id: str
    session_key: str

class ContactMemoryBroker:
    def prefetch(scope, message, history, turn_state) -> RecallBundle: ...
    def search(scope, query, limit=3) -> RecallBundle: ...
    def record_usage(scope, fact_ids) -> None: ...
    def propose_turn_memories(scope, user_text, assistant_text, metadata) -> None: ...
```

The gateway does not currently propagate a live `RetrievalScope`. Offline broker tests construct it explicitly. There is no model-facing Lane B surface yet.

## Lane A: automatic prefetch

Implementation status: broker/gate evaluation exists, but gateway injection is hard-blocked. The current API assembly appends ephemeral context to the cache-marked system block, so it does not satisfy invariant 7. Enabling the reserved flags cannot bypass this block.

Retrieve when any trigger is true:

- embedding topic shift;
- V2 register is advice, serious, or task;
- unresolved pronoun/definite reference lacks an in-thread antecedent.

Skip when any condition is true:

- closure or reaction plan;
- less than 90 seconds since prior prefetch;
- fewer than five messages since prior prefetch;
- no approved contact scope;
- backend unavailable.

Time-gap fusion:

- a gap greater than 45 minutes is a segment boundary;
- otherwise require semantic drift plus a moderate gap;
- do not update the topic centroid from messages shorter than four words.

Budget: at most three nodes, at most 200 rendered characters per node, at most 600 characters total.

## Lane B: explicit retrieval

Implementation status: not implemented.

A service-gated tool appears only for Poke/Guest when contact retrieval is configured. Its schema contains a query and optional limit only. The broker obtains principal/contact/session from request context; crafted arguments cannot change them. Guest results omit source text, evidence spans, restricted counts, and authorization metadata.

## Ranking

1. SQL prefilter by active lifecycle, namespace file, audience, sensitivity, assertion type, trust, temporal validity, and embedding model generation.
2. Lexical score over normalized fact text/entity aliases.
3. Exact semantic cosine using a contiguous NumPy matrix of normalized vectors.
4. Recency/open-loop/recommendation boosts only when relevant.
5. Penalty for facts used within the prior ten turns.
6. Optional one-hop graph expansion from high-confidence entity matches, followed by the same final-row visibility filter.

Restricted rows are excluded before vector scoring, not post-filtered.

## Model decision and benchmark gate

Candidates:

- Primary quality candidate: EmbeddingGemma 300M through MLX at 256 dimensions. Its Gemma terms and gated initial download are operational drawbacks; `mlx-embeddings` is GPL-3.0 and should be isolated from distributable core code.
- Primary low-friction candidate: `minishlab/potion-retrieval-32M` via Model2Vec 0.8.2 (MIT, Python 3.13). Use `potion-base-32M` for classifier features.
- Permissive transformer challenger: `Alibaba-NLP/gte-modernbert-base`.

Production chooses the smallest candidate meeting held-out retrieval/topic-shift accuracy and latency gates on this Mac. No model is selected from public MTEB alone.

Initial storage uses SQLite plus NumPy exact search. No FAISS, Chroma, HNSW, LanceDB, or sqlite-vec unless measured candidate count/latency proves exact search insufficient.

Async extractor candidate: resident `mlx-community/Qwen3-4B-Instruct-2507-4bit` through MLX, with schema-constrained output. It only proposes typed operations to `pending_fact` and `recommendation`; deterministic code validates and commits. If the MLX structured-output path is unreliable, use a mature Phi-4-mini GGUF through llama.cpp grammar as fallback. Extraction never blocks a reply.

## Importing Steve

The existing `stephen-lucier.jsonl` is a relationship-thread dossier, not Steve-only data. Import steps:

1. Preserve original source IDs/evidence pointers.
2. Normalize subject as Steve, Kosta, relationship, shared event, or third party.
3. Default third-party and ambiguous facts to owner-only quarantine.
4. Default existing sensitive facts to owner-only until reviewed.
5. Guest-visible facts must be stated, high-confidence, non-actionable, and explicitly `guest_ok`.
6. Never infer routing/approval from dossier content.

The first import runs in dry-run mode and emits a review manifest before any Guest retrieval is enabled.

## Recommendation and callback ledgers

Recommendations are append-only typed state with basis fact IDs and `proposed | active | withdrawn | fulfilled`. A model may propose `add | supersede | confirm | reject | no_change`; application code performs the mutation. A style complaint cannot change recommendation content without a new basis fact.

The callback ledger stores retrieved and actually-used fact IDs separately. Initial implementation records deterministic retrieval and provides an internal usage marker path; unconfirmed usage is never treated as surfaced. Facts surfaced in the prior ten turns receive a hard repeat penalty.

## File-by-file implementation

| File | Action | Purpose |
|---|---|---|
| `gateway/contact_memory/schema.py` | create | Strict schemas, migrations, lifecycle enums |
| `gateway/contact_memory/store.py` | create | Per-contact SQLite repository, exact vector search, supersession |
| `gateway/contact_memory/security.py` | create | Principal/audience policy and inert rendering |
| `gateway/contact_memory/gating.py` | create | Lane A cooldown/reference/topic gates |
| `gateway/contact_memory/broker.py` | create | Closed-scope retrieval orchestration |
| `gateway/contact_memory/embeddings.py` | create | Pluggable local backend and lexical fallback |
| `gateway/contact_memory/import_contacts.py` | create | Dry-run/import of extracted contact artifacts |
| `gateway/contact_memory/extractor.py` | create | Async pending-fact/recommendation proposal interface |
| `gateway/run.py` | modify | Hard-block injection and scope derivation pending cache-safe suffix/trusted handoff |
| `model_tools.py` / gated tool registration | modify only if required | Lane B query-only tool, absent without config |
| `tests/gateway/contact_memory/*` | create | Schema, privacy, race, injection, retrieval, latency tests |
| `docs/contact-memory.md` | create | Config, model install, review/delete/export operations |

## Implementation order and validation

1. Schema/store/security with no model and no gateway wiring. Validate migrations, per-contact files, audience filtering, supersession, secure deletion/export, concurrent writers.
2. Steve dry-run importer. Validate all 151 rows accounted for; ambiguous and sensitive rows quarantine; no Guest-visible row without explicit policy.
3. Embedding benchmark in an isolated uv environment. Validate native arm64/Python 3.13 installation, offline reload, latency, negation/direction/date cases, and exact-search reopen/delete cycle.
4. Broker/Lane A with lexical fallback and feature flag default-off. Validate cache signature unchanged and fail-open behavior.
5. Lane B service-gated query-only tool with immutable server-assigned scope. Validate namespace injection and confused-deputy attacks.
6. Async extractor and recommendation ledger. Validate pending invisibility, deterministic schema validation, idempotency, and overlapping-write races.
7. Enable owner-only shadow mode, record retrieval decisions without injecting them, then evaluate.
8. Enable Poke owner injection after gates pass; enable Steve/Guest only after reviewed `guest_ok` seed data and adversarial approval.

## Promotion gates

- Zero cross-contact/audience leaks in adversarial suite.
- Zero pending/restricted/superseded rows in retrieval candidates.
- Identical Guest response contract for unavailable vs unauthorized facts.
- All 151 Steve source facts accounted for by imported/quarantined/rejected status.
- p50 gate overhead <120 ms; retrieval including fetch <400 ms.
- Recall improves held-out reference resolution without forced-callback regression.
- Gateway continues when model/store/extractor is absent or corrupt.
- Prompt cache signature remains stable.

## Freshness receipts

- Model2Vec 0.8.2 (2026-05-29): https://pypi.org/project/model2vec/
- EmbeddingGemma model card: https://ai.google.dev/gemma/docs/embeddinggemma/model_card
- MLX-LM: https://github.com/ml-explore/mlx-lm
- sqlite-vec docs (evaluated but not initial dependency): https://github.com/asg017/sqlite-vec
- SQLite WAL: https://sqlite.org/wal.html
