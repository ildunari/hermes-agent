# Goal Prompt: Implement Contact-Aware Retrieval

Work in `/Users/Kosta/.hermes/hermes-agent` on branch `local/studio-slim`.

Read `AGENTS.md`, `docs/plans/contact-aware-retrieval.md`, the existing `gateway/conversation_texture.py`, `gateway/conversation_texture_v2.py`, Guest routing in `gateway/run.py`, and the Fable source specs under `/tmp/poke-phase2-kit-review/poke-phase2-kit/docs/`.

Implement the plan to verified completion in small reversible checkpoints. Preserve every unrelated dirty working-tree change. Do not restart any live Hermes surface inline. Do not use paid APIs, API keys, or metered services. Local MLX/CPU inference and existing subscription-authenticated review agents are allowed.

Non-negotiable requirements:

1. Per-contact SQLite stores with exact NumPy vector search and no vector daemon.
2. Audience and sensitivity enforced independently of contact namespace before candidate scoring and again on final rows.
3. Trusted gateway code assigns principal, contact ID, and session scope. The model cannot choose or override them.
4. Guest cannot read source dossiers or receive evidence/source text.
5. Pending/quarantined/inferred/restricted/superseded rows are invisible to Guest.
6. Complete provenance and bitemporal append-only supersession.
7. Retrieved facts rendered as escaped inert data, not instructions.
8. Per-turn recall enters only the execution ephemeral prompt and never the cache signature/transcript.
9. Retrieval and extraction fail open without blocking replies.
10. Async extractors only propose typed pending operations; deterministic code validates and commits.
11. Feature flags default off until deterministic, privacy, latency, and neutral evaluation gates pass.
12. Maintain V1 rollback and existing Poke V2 behavior.

Use TDD. Required adversarial coverage includes cross-contact leakage, mixed-subject Steve facts, namespace injection, confused deputy, prompt injection stored as memory, stale/superseded vectors, pending visibility, concurrent supersession, recommendation conflicts, revocation, no-result/unauthorized indistinguishability, cache stability, and backend failure.

Benchmark current local embedding candidates on the actual Mac and a privacy-safe held-out set. Prefer the smallest model that meets quality and latency gates; do not choose from public benchmarks alone. Record pinned versions, install commands, licenses, model revisions, cold/warm latency, RSS, and offline reload behavior.

Run an Opus 4.8 high adversarial architecture/diff review and a Sol high independent review after implementation. Resolve every P0/P1 and either resolve or explicitly document every P2/P3. Run focused and broader gateway tests, compile checks, diff checks, and an isolated temp-HERMES_HOME end-to-end test.

Do not expose or commit private Steve facts, raw texts, credentials, tokens, or generated contact databases. Test fixtures must be synthetic. Commit only verified code/docs/tests and report the commit hash, tests, model decision, latency evidence, remaining feature flags, and any intentionally deferred live rollout step.
