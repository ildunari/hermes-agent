# Implementation Plan: Canonical Communication Signals for Poke/Guest

Status: APPROVED HANDOFF — implement in staged, review-gated checkpoints.
Owner: Coding-profile orchestrator.
Implementation lane: Codex app-server, GPT-5.6 Sol, medium reasoning.
Repository: `/Users/Kosta/.hermes/hermes-agent`, branch `local/studio-slim`.
Date: 2026-07-14.

## 1. Objective

Complete the missing ingestion layer beneath the existing Fable proactive architecture. Historical and live Poke/Guest communication must produce the same sender-attributed, append-only communication evidence before facts, interests, recommendations, callbacks, maintenance, scheduling, fetch/gate, and texture delivery consume it.

This is an **upstream expansion of Fable Component 1**, not a replacement for the architecture in `docs/plans/poke-proactive-layer.md`:

1. authenticated communication evidence,
2. interest-event ledger,
3. offline maintenance/merge/decay and ≤300-token digest,
4. deterministic scheduler,
5. isolated fetch + silence-default gate,
6. texture-v2 delivery and outcome feedback.

The implementation must eliminate the current split where text goes through the normal extractor while links/reactions live in a special review sidecar. Private evidence maps may remain sidecars, but every approved semantic result must project through one canonical store API.

## 2. Source documents and current state

Read before editing:

- `AGENTS.md`
- `HERMES.md`
- `docs/plans/poke-proactive-layer.md` — original Claude Fable 5 architecture; binding downstream design.
- `docs/plans/poke-proactive-production-rollout.md` — later safety and rollout invariants.
- `docs/plans/poke-proactive-layer-phase1-report.md`
- `docs/plans/poke-proactive-layer-phase2-report.md`
- `gateway/contact_memory/imessage_bootstrap.py`
- `gateway/contact_memory/imessage_link_review.py`
- `scripts/review_imessage_link_signals.py`
- `scripts/apply_reviewed_link_interests.py`
- `gateway/contact_memory/schema.py`, `store.py`, `extractor.py`, `runtime.py`
- `gateway/platforms/base.py`, `gateway/platforms/bluebubbles.py`, `gateway/run.py`
- `/Users/Kosta/.hermes/profiles/coding/cache/delegation/subagent-summary-0-20260714_100607_213526.txt`
- `/Users/Kosta/.hermes/profiles/coding/cache/delegation/subagent-summary-1-20260714_100607_214422.txt`

Current verified gaps:

- Historical semantic extraction covered 100,455 Stephen-thread rows but produced prose facts/preferences rather than a reusable communication-event model.
- Links were recently recovered by a separate reviewed pipeline; one `music` topic was applied, but live links are still plain text.
- Attachments are accounted for as non-text but attachment kind/caption/interaction is not durable.
- Historical bootstrap excludes tapbacks; live BlueBubbles discards them.
- Reply IDs, rapid-message batch membership, initiation, recurrence, recommendation follow-through, shared jokes/callbacks, and named-entity recurrence are not maintained.
- Live contact-memory extraction is submitted only after a successful assistant turn and receives source text + assistant text, dropping rich ingress metadata and missing failed/suppressed/no-reply turns.
- Both live stores have zero entity edges and zero recommendations; historical sender separation is incomplete.

Relevant recent commits:

- `086dba6f3` private iMessage link review
- `1ea0ac493` secure link-topic review pipeline
- `9b7d20135` approved link-interest apply bridge
- `1f38fb17f` actor-scoped evidence and modern direct-chat fixes

## 3. Binding design principles

1. **Preserve Fable’s architecture.** Rich communication sources are adapters into Component 1. Do not create a competing memory, scheduler, digest, or delivery path.
2. **Authenticated attribution before semantics.** Canonical actor/contact/profile comes only from BlueBubbles routing, direct-chat membership, `is_from_me`, approved handles, and frozen trusted scope. Models never infer identity.
3. **Persist ingress independently of agent success.** A valid authenticated direct message/reaction/attachment must be durably represented even when no assistant reply is generated or model extraction fails.
4. **Hot path remains nonblocking.** Deterministic bounded persistence is allowed; network enrichment, media analysis, NER, topic classification, merge, and maintenance stay async/offline.
5. **Per-contact physical isolation.** Communication evidence is stored only in that contact’s existing `0600` SQLite DB. No cross-contact/global behavioral ledger.
6. **Raw artifacts do not sprawl.** Do not persist raw webhook payloads, local attachment paths, unapproved handles, message bodies, exact secret-bearing URLs, or fetched page bodies in durable semantic tables/logs.
7. **Private sidecars are evidence, not architecture.** Full URLs/GUID drill-down may remain in `0600` operator artifacts. Durable stores receive HMAC/source IDs and normalized semantic fields.
8. **Deterministic evidence before LLM proposals.** URLs, reactions, replies, attachment descriptors, batch membership, and explicit proactive/recommendation linkage are code-derived. Qwen proposes only semantic topics/entities/facts over bounded evidence.
9. **Retractions and idempotency matter.** Tapback removals, message replays, retries, edits, and repeated bootstrap runs cannot double-count evidence.
10. **No live sends.** Keep Poke and Guest in `observe`; this project must not enable proactive delivery. No gateway restart without explicit owner instruction during this job.
11. **Small blast radius.** Additive migrations, compatibility with existing extractor callbacks, and reversible profile backfills only after reviewed dry runs and backups.
12. **Independent review gates.** Every phase must pass focused tests, `py_compile`, and `git diff --check`; fix all P0/P1 findings before proceeding. Feedback must be based on the latest diff, never a stale snapshot.

## 4. Canonical data model

Prefer additive schema versioning. Exact version number must follow the current repository, not this document’s stale assumptions.

### 4.1 `communication_event`

One authenticated source event per message/reaction/service action.

Required semantics:

- stable `event_id`
- unique platform/source ID (or deterministic HMAC fallback when truly absent)
- occurred timestamp
- direction: inbound/outbound
- kind: text, link_share, attachment_share, reaction_add, reaction_remove, reply, batch_member, recommendation, follow_through, callback/joke
- actor role/contact namespace
- optional target source/event ID
- text hash and bounded text-presence/length metadata, not raw message text
- privacy/sensitivity class
- lifecycle/retraction state
- source provenance/version

### 4.2 Child evidence tables

Use normalized child tables or an equally strict typed representation:

- `communication_url`: event ID, keyed/HMAC URL identity, broad domain/platform, sharer role, enrichment state; no raw signed URL.
- `communication_attachment`: event ID, keyed attachment identity, media kind, safe MIME/UTI, bounded size, caption-presence hash/flags; no local path.
- `communication_relation`: event ID, relation type, target event/source ID, target actor where authenticated.
- `entity_mention`: event ID, entity identity/type, normalized canonical label or private surface hash, confidence, source method.
- `recommendation_event`: recommendation ID, event ID, proposed/accepted/rejected/revisited/fulfilled, confidence, explicit-vs-inferred linkage.

Avoid a generic unvalidated `metadata_json` dumping ground. If bounded JSON is unavoidable, define an allowlisted schema and size limit.

### 4.3 Projections

Communication signals project through one transactional API into existing:

- `fact` / fact proposals
- `interest_event`
- `entity` and `edge`
- `recommendation`
- `callback_event`
- proactive outcome records

A source event may yield zero projections. The communication ledger is evidence, not a mandate to infer an interest.

## 5. Typed ingress contract

Add an immutable authenticated communication envelope to the platform/gateway boundary rather than trusting free-form metadata. It should carry only transport facts:

- platform/source message ID and received timestamp
- chat type and direction
- immutable sender transport identity (not a contact ID)
- visible text fragments and rapid-message batch member IDs
- canonical visible URLs
- sanitized attachment descriptors
- reply target
- reaction add/remove target and reaction kind
- event kind

Gateway routing freezes canonical profile/contact/principal after guest policy resolution. Platform adapters may parse transport facts but may not assign contact memory namespaces.

Persist authenticated direct ingress before plugin/model dispatch. Group/forwarded/unapproved/internal events remain excluded unless a separately reviewed policy says otherwise.

## 6. Historical adapter

Extend historical read-only Messages processing to emit the same canonical communication signal shape:

- exact direct-chat resolution across approved aliases
- per-row incoming `handle_id` validation; anonymous service rows omitted and counted
- visible text or conservative attributed-string decoding only
- canonical visible URLs without scanning serialized rich-link archives
- attachment joins and safe descriptors from `message_attachment_join`/`attachment`
- tapback add/remove state with colon/slash associated-GUID forms
- reply relations and target actor when mechanically authenticated
- batch/sequence and topic-initiation features where deterministic
- chronological recurrence/day counts

Produce no-drop accounting by category:

`text + links + attachments + reactions + replies + explicit_nonsemantic + rejected == selected source events`

Do not rerun broad LLM semantic extraction over 100k messages. Reuse reviewed artifacts, deterministic adapters, bounded metadata enrichment, and focused reviewed projections.

## 7. Live adapter

Bring live BlueBubbles parity without adding model latency:

1. Parse rich ingress in the adapter.
2. Route/authenticate in `gateway/run.py`.
3. Freeze canonical scope.
4. Persist the canonical event immediately and idempotently.
5. Continue ordinary dispatch or suppression.
6. Submit a reference to the stored event to async Qwen extraction after a successful turn; no longer make post-turn extraction the only durable ingestion path.
7. Persist tapbacks/replies/attachments even when they produce no agent response.
8. Preserve rapid-message members rather than retaining only the final message ID.

Do not let reactions trigger ordinary agent replies unless existing product policy explicitly requires it.

## 8. Semantic projection rules

### 8.1 Interests

Use existing Fable signal types and weights. Add source-specific mapping only where semantically sound:

- repeated spontaneous text/entity/topic raises → `spontaneous_raise`
- positive reaction by the actor to a classified item → `engaged_mention` or a narrowly named deterministic equivalent only if schema evolution is justified
- long reply → deterministic rolling-median calculation
- plain reply target → neutral relation, not positive interest
- one-time share without reaction → insufficient by default
- explicit dislike/reaction removal alone → not necessarily negative; require text or unambiguous semantic evidence

Promotion remains score ≥ threshold plus evidence on separate days. Do not bootstrap active interests from one noisy message.

### 8.2 Entities and topic flavor

Named artists, shows, celebrities, athletes, venues, festivals, products, hobbies, foods, pets, vehicles, and places should be maintained as normalized entities/mentions when repeated or high-confidence. Use them to flavor/child interests during maintenance; do not flood the top-level digest with every name.

### 8.3 Recommendations and follow-through

Model recommendation lifecycle explicitly. Prefer mechanical reply/reaction target linkage. If using temporal inference, mark it inferred with confidence and never automatically fulfill/close a recommendation from an unrelated next inbound message.

### 8.4 Humor and callbacks

Represent recurring shared jokes/memes/callbacks as privacy-restricted callback evidence, not generic facts exposed to all audiences. Require recurrence or explicit response evidence. Preserve the distinction between “they sent a joke” and “the recipient found it funny.”

## 9. Link and attachment enrichment

Reuse the corrected bounded link-review primitives rather than reimplementing them.

- Select before network access.
- Hard request budgets.
- Reject private/local/signed/token/action URLs.
- Use trusted external web extraction or platform metadata; direct in-process fetching remains disabled unless DNS-pinned peer verification exists.
- Treat metadata as hostile data; schema-constrained deterministic taxonomy only.
- Store raw metadata only in `0600` private cache.
- Cache failures; do not guess login-walled content.

For attachments:

- Phase 1 supports deterministic media kind/MIME/size/caption evidence.
- Optional image/video/audio semantic analysis is a later bounded offline adapter, never in the hot path.
- Perceptual hashes and local paths stay private; durable output is normalized topic/entity evidence only after review.

## 10. File-by-file implementation map

Expected files; adjust based on current code after inspection:

| File | Action | Responsibility |
|---|---|---|
| `gateway/contact_memory/schema.py` | Modify | Add communication ledger tables, enums/dataclasses, migration and indexes. |
| `gateway/contact_memory/store.py` | Modify | Atomic idempotent ingest + projection APIs; retraction and query helpers. |
| `gateway/platforms/base.py` | Modify | Immutable typed communication ingress envelope. |
| `gateway/platforms/bluebubbles.py` | Modify | Parse URLs, attachments, reactions, reply targets, batch members without assigning contact IDs. |
| `gateway/run.py` | Modify | Freeze routed scope; persist authenticated ingress before dispatch; submit event references asynchronously. |
| `gateway/contact_memory/extractor.py` | Modify | Consume stored-event references and bounded evidence; semantic topic/entity proposals only. |
| `gateway/contact_memory/qwen3_extractor_worker.py` | Modify if needed | Strict semantic schema for topic/entity/recommendation proposals without identity inference. |
| `gateway/contact_memory/imessage_bootstrap.py` | Modify | Historical canonical communication adapter and no-drop accounting. |
| `gateway/contact_memory/imessage_link_review.py` | Refactor | Reuse as URL enrichment/projection helper, not a parallel ledger. |
| `scripts/review_imessage_link_signals.py` | Refactor | Historical/private review adapter backed by canonical signals. |
| New reviewed backfill CLI | Create | Dry-run manifest, exact approval HMAC, per-subject atomic apply, backup guidance. |
| `gateway/contact_memory/interest_maintenance.py` | Modify only if required | Consume richer entity/topic flavor without raw evidence. |
| Tests | Add/modify | Historical/live parity, privacy, attribution, migration, idempotency, no-drop, failure paths. |

## 11. Phased implementation and gates

### Phase A — Contract and schema

- Write typed enums/dataclasses and additive migration.
- Implement atomic idempotent store ingest with source uniqueness and retraction support.
- No gateway integration yet.

Gate A:

- migration from real-shaped prior schema
- concurrent replay
- conflicting source IDs fail/rollback
- no raw secrets in durable rows
- per-contact physical isolation

### Phase B — Historical adapter

- Emit canonical signals from direct Stephen history.
- Integrate links, reactions, replies, and attachment descriptors.
- Generate aggregate/private evidence manifests.
- Do not apply live state.

Gate B:

- direct/group/alias fixtures
- modern style 45/internal group ID
- incoming handle validation
- tapback add/remove and GUID variants
- rich-link archive false positives excluded
- no-drop accounting
- no raw body/URL leakage

### Phase C — Live ingress parity

- Add typed envelope and routed persistence.
- Keep existing reactive behavior unchanged.
- Persist no-reply/failed/suppressed ingress.
- Preserve rapid-message membership.

Gate C:

- owner/guest/group routing isolation
- hooks cannot forge trusted fields
- retries/replays idempotent
- adapter tapbacks do not trigger replies
- post-turn failure does not lose ingress
- hot-path latency benchmark/regression bound

### Phase D — Semantic projections

- Connect deterministic signals and bounded Qwen proposals to interests/entities/recommendations/callbacks.
- Ensure actor-specific evidence rules.
- Refactor link sidecar apply through canonical API.

Gate D:

- sender vs reactor attribution
- neutral replies do not imply liking
- recurrence/separate-day promotion
- entity dedupe and taxonomy caps
- recommendation explicit/inferred outcome behavior
- shared-joke privacy policy

### Phase E — Reviewed backfill

- Run deterministic historical scan against Steve’s direct thread.
- Produce counts and aggregate topic/entity candidates.
- Enrich only a bounded safe subset.
- Independent adversarial review of proposed projections.
- Back up Poke/Guest stores.
- Apply only explicitly approved, subject-correct candidates atomically.
- Run maintenance and verify digests/interests.

Gate E:

- Poke receives only Kosta-authored/actor-supported evidence
- Guest receives only Stephen-authored/actor-supported evidence
- no duplicate/recount of existing reviewed seeds
- restore rehearsal in temporary roots
- observe mode remains enabled; no visible delivery

### Phase F — Final verification and documentation

- Full relevant test suites.
- Proactive status and silent maintenance verification.
- Documentation update explaining canonical flow and private sidecars.
- Final P0–P3 review; fix P0/P1, re-review latest diff.
- Commit clean, scoped checkpoints.

## 12. Test matrix

At minimum cover:

- additive migration/idempotency on existing DB
- strict direct-chat and alias resolution
- cross-speaker/cross-profile rejection
- inbound handle `0` omission vs nonzero unknown rejection
- message replay and duplicate webhook
- reaction add/remove and target validation
- reply target missing/foreign
- attachment without text, multiple attachments, unsafe filename/path
- visible URL vs hidden rich-link/CDN URL
- signed/private/action URL exclusion
- rapid-message batching provenance
- model/extractor failure after ingress persistence
- no assistant response path
- recommendation follow-through explicit/inferred/unrelated
- entity recurrence and alias normalization
- actor-specific interest evidence and separate-day promotion
- no raw text/full URL/GUID/path in digest, logs, or semantic ledger
- private evidence artifacts mode `0600`
- maintenance fold idempotency and digest ≤300 tokens
- Poke/Guest observe-only and no outbound send

## 13. Execution discipline for the implementation agent

- Start by creating/updating an internal plan/todo and inspecting current code; do not code from this document’s assumptions alone.
- Coordinate with other agents through `hey.md`; never overwrite unexpected concurrent changes.
- Make small commits at phase boundaries.
- Run an adversarial review after each substantial phase. If a review reports issues, fix them and re-review the **latest** diff before proceeding.
- Never report a test as passing without real output.
- If blocked, document the exact failure and continue on unblocked phases.
- Do not push, open a PR, restart gateways, enable live proactive sends, or mutate live Poke/Guest stores during implementation. Historical live-state apply requires the parent orchestrator’s explicit reviewed approval.
- Keep a concise progress log at `docs/plans/poke-canonical-communication-signals-progress.md` with phase, commit, tests, review findings fixed, and current blockers. Do not place private message/link content in it.

## 14. Parent-orchestrator review protocol

The parent session will poll at milestone intervals, not continuously. At each poll it will:

1. read the worker’s latest plan/progress and git diff,
2. update the Hermes todo list with the current phase and completed evidence,
3. check whether previously observed issues are already fixed,
4. send only non-stale feedback,
5. commission independent adversarial review at meaningful phase boundaries,
6. verify final tests and live-state boundaries before accepting completion.

## 15. Definition of done

This project is complete only when:

- historical and live authenticated communication use the same canonical event contract,
- links/attachments/reactions/replies/batch membership are first-class evidence,
- text/entity/topic/recommendation/callback projections use one store API,
- actor attribution and per-contact isolation are mechanically enforced,
- raw artifacts remain private and out of model context/durable semantic rows,
- historical reviewed backfill produces materially richer but evidence-backed Steve/Kosta interests,
- existing Fable maintenance/scheduler/fetch/gate/texture architecture remains intact,
- Poke/Guest remain observe-only,
- relevant suites pass and final independent review has no unresolved P0/P1 findings.
