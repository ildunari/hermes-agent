# Contact-memory completion plan

Date: 2026-07-11
Branch: `local/studio-slim`

## Objective

Finish the live contact-memory system rather than stopping at retrieval: repair all contact-scope regressions, add asynchronous local fact extraction and safe promotion, activate recommendation/callback ledgers, add a trusted-scope explicit search lane without growing the permanent core tool schema, validate on private held-out Steve queries, and leave the gateway suite's related paths green.

## Invariants

1. System prompt and stored history remain byte-stable; per-turn recall is API-call-only.
2. Contact ID and principal come only from authenticated gateway routing.
3. Guest never sees owner-only, sensitive, restricted, inferred, quarantined, superseded, or cross-contact data.
4. Group messages and queued/forwarded work receive no private contact scope.
5. Local model failure never blocks a reply.
6. Extractors write proposals, not active facts. Automatic promotion is limited to explicitly low-risk, high-confidence, stated facts with evidence and deterministic validation.
7. Private corpus text and model outputs never enter git artifacts.
8. Preserve unrelated dirty files and commits.

## Workstreams

### 1. Gateway correctness

- Fix undefined `trusted_contact_scope` in `_handle_message_with_agent` by deriving it once from authenticated event metadata at the handler boundary.
- Add regression tests for normal, early-failure, duplicate-message, owner, Guest DM, group, and queued paths.
- Preserve session-store `skip_db` behavior.
- Fix contact-related profile/CWD binding failures only where caused by this feature.

### 2. Local extractor worker

- Use a resident isolated MLX Qwen instruct worker with strict JSON schema.
- Benchmark candidate models on privacy-safe extraction cases covering subject assignment, correction, contradiction, sensitivity, sarcasm/noise, third parties, dates, and recommendations.
- Worker returns bounded proposals with source evidence IDs.
- Deterministic validator rejects instructions, secrets, ambiguous subjects, low confidence, and unsupported enum values.
- Asynchronous post-turn queue; response path never waits for extraction.

### 3. Promotion and lifecycle

- Deduplicate by normalized subject/predicate/object and evidence.
- Supersede corrected facts transactionally.
- Auto-promote only normal, stated, first-party facts at high confidence and trust; all sensitive, relationship-risk, health, legal, financial, sexual, third-party, or ambiguous facts remain pending.
- Add retry/idempotency and bounded queue state.

### 4. Recommendation and callback ledgers

- Extract standing recommendations with basis fact IDs and change requirements.
- Record actually injected fact IDs after successful recall.
- Enforce callback cooldowns and avoid repeating the same fact unless the message directly asks.
- Expire stale/open-loop entries deterministically.

### 5. Explicit Lane B

- Implement as a request-scoped service/plugin capability or internal agent callback, not a permanent core tool.
- Contact/principal are immutable runtime bindings; model supplies only the query.
- Same broker, audience, sensitivity, and rendering filters as Lane A.
- No availability on unscoped, group, forwarded, or queued turns.

### 6. Evaluation and activation

- Create a private, gitignored held-out query set from Steve's reviewed facts.
- Measure candidate coverage, top-1/top-3, abstention, sensitive leakage, stale-fact rejection, and latency.
- Adversarial Claude/GPT reviews of scope provenance, extraction poisoning, contradiction handling, and diff.
- Run contact-memory, gateway routing, session, BlueBubbles, prompt-cache, and complete gateway collection/tests.
- Commit coherent changes, run safe detached gateway restart, verify process and both profile self-checks.

## Gates

- Zero P0/P1 adversarial findings.
- Contact-memory and affected gateway tests green.
- No `trusted_contact_scope` NameError in any route.
- Extraction precision >= 90% on safe synthetic cases; zero unsafe auto-promotions.
- Retrieval held-out top-1 >= 90%, top-3 >= 98%, zero owner/Guest leakage.
- Warm Lane A p95 < 550 ms; extraction off response path.
- Live owner and Guest self-checks pass after restart.
