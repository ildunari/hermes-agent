# Canonical Communication Signals — Progress

## Phase A — Contract and schema

Status: complete; Gate A clear. Phase B has not started.

Commit: Phase A boundary commit containing this progress record (the final SHA is reported by the implementation thread).

### Delivered

- Added the typed canonical communication contract for events, URLs, attachment descriptors, relations, entity mentions, recommendation outcomes, lifecycle/retraction state, and ingest results.
- Added schema version 5 as an additive migration. Existing contact-memory tables and data remain intact; canonical communication evidence is physically isolated per contact database.
- Added atomic/idempotent ingress, additive enrichment, source uniqueness, exact conflict detection, authenticated actor checks, out-of-order reaction-removal reconciliation, and secure-delete coverage to `ContactMemoryStore`.
- Added focused tests for real-shaped v4 migration, every child type, replay/conflict/rollback, concurrent ingestion, enrichment replay, reaction add/remove ordering and target validation, raw-artifact rejection, per-contact isolation, and deletion.

### Gate A evidence

Final verification passed: `scripts/run_tests.sh tests/gateway/contact_memory tests/gateway/test_interest_ledger.py -q` completed with 155 passed and 0 failed across 11 files; `python3 -m py_compile gateway/contact_memory/schema.py gateway/contact_memory/store.py tests/gateway/contact_memory/test_communication_store.py` passed; and `git diff --check` passed.

Independent adversarial review ran after implementation and after each P0/P1 fix cycle. Final review found no unresolved P0 or P1 findings and declared Gate A clear.

### Review findings fixed

- Made post-ingress enrichment additive and idempotent while preserving source replay semantics, including one-way URL enrichment-state transitions.
- Preserved out-of-order reaction removals with pending tombstones; enforced one removal per target, one authenticated reaction target, matching actor/direction/platform, and consistent add/remove relation semantics.
- Closed durable raw-artifact paths across URL metadata, attachment MIME/UTI fields, entity labels, machine tokens, and pending target IDs. Durable semantic free text is slash-free by contract.
- Enforced exact integer/boolean persistence and prevented lossy SQLite round trips.
- Replaced the synthetic version-marker test with a real-shaped v4 database lacking the new communication tables.

### Current risks and boundaries

- No historical or live gateway adapter uses the contract yet; that is Phase B/C work. Existing reactive behavior and proactive observe-only behavior are unchanged.
- Canonical entity labels reject slash-bearing names as a deliberate privacy boundary. Phase B should normalize such display names before constructing durable labels rather than weakening the contract.
- Pending reaction removals remain pending until the matching authenticated add arrives. Phase B should report aggregate pending/unmatched counts without exposing source identifiers.
- This phase did not restart gateways, mutate live profile stores, enable sends, push, or open a pull request.

### Recommended Phase B handoff

Build the historical direct-thread adapter as a pure emitter into this contract. Use deterministic opaque hashes for every event/source/child identity; emit the original reacted-to message relation for both tapback add and removal; persist raw bodies, URLs, GUIDs, filenames, and paths only in private evidence artifacts; and apply through `ingest_communication_event`, `enrich_communication_event`, and `retract_communication_event` against temporary stores only. Gate B should validate direct/group/alias routing, handle `0` versus unknown nonzero handles, GUID variants, rich-link false positives, no-drop accounting, and aggregate-only manifests before any reviewed live-state proposal.
