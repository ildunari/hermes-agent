# Canonical Communication Signals — Progress

## Phase A — Contract and schema

Status: Phase A repair complete, including the final parent-review finding. Phase B has not started.

Commit: Phase A boundary commit containing this progress record (the final SHA is reported by the implementation thread).

### Delivered

- Added the typed canonical communication contract for events, URLs, attachment descriptors, relations, entity mentions, recommendation outcomes, lifecycle/retraction state, and ingest results.
- Added schema version 6 as an additive migration. Existing contact-memory tables and data remain intact; canonical communication evidence is physically isolated per contact database.
- Added atomic/idempotent ingress, additive enrichment, source uniqueness, exact conflict detection, authenticated actor checks, out-of-order reaction-removal reconciliation, and secure-delete coverage to `ContactMemoryStore`.
- Added focused tests for real-shaped v4 migration, every child type, replay/conflict/rollback, concurrent ingestion, enrichment replay, reaction add/remove ordering and target validation, raw-artifact rejection, per-contact isolation, and deletion.

### Gate A evidence

Final repair verification passed: the focused `scripts/run_tests.sh tests/gateway/contact_memory/test_communication_store.py -q` suite completed with 36 passed and 0 failed; the full Phase A `scripts/run_tests.sh tests/gateway/contact_memory tests/gateway/test_interest_ledger.py -q` suite completed with 180 passed and 0 failed across 11 files; `python3 -m py_compile gateway/contact_memory/schema.py gateway/contact_memory/store.py tests/gateway/contact_memory/test_communication_store.py` passed; `python3 -m ruff check gateway/contact_memory/schema.py gateway/contact_memory/store.py tests/gateway/contact_memory/test_communication_store.py` passed; and `git diff --check` passed.

The parent independently reviewed the pre-repair diff and identified three P1 findings, then found one remaining P1 in the final review. All are covered by focused regressions and repaired below; no delegated or additional review was launched from the repair turns.

### Review findings fixed

- Made post-ingress enrichment additive and idempotent while preserving source replay semantics, including one-way URL enrichment-state transitions.
- Preserved out-of-order reaction removals with pending tombstones; enforced one removal per target, one authenticated reaction target, matching actor/direction/platform, and consistent add/remove relation semantics.
- Closed durable raw-artifact paths across URL metadata, attachment MIME/UTI fields, entity labels, machine tokens, and pending target IDs. Durable semantic free text is slash-free by contract.
- Enforced exact integer/boolean persistence and prevented lossy SQLite round trips.
- Replaced the synthetic version-marker test with a real-shaped v4 database lacking the new communication tables.
- Migrated existing v5 reaction rows atomically as `legacy_untyped` historical evidence instead of making databases with reaction history unopenable; reopening the migrated database is idempotent.
- Reserved `legacy_untyped` for the internal historical-row restoration path. The public event contract and both reaction add/remove ingestion APIs reject it for new events and require a concrete subtype.
- Strengthened canonical-label credential filtering for service-qualified token, secret-access-key, and database-credential prose while preserving legitimate artist, show, platform, and place-like semantic labels.
- Closed the final generic credential-value gap for bare and possessive/service-qualified labels, including singular/plural credentials with `is`/`are`, while retaining representative legitimate semantic names.
- Preserved the accepted exact timestamp normalization and lossless SQLite round-trip boundary checks.

### Current risks and boundaries

- No historical or live gateway adapter uses the contract yet; that is Phase B/C work. Existing reactive behavior and proactive observe-only behavior are unchanged.
- Canonical entity labels reject slash-bearing names as a deliberate privacy boundary. Phase B should normalize such display names before constructing durable labels rather than weakening the contract.
- Pending reaction removals remain pending until the matching authenticated add arrives. Phase B should report aggregate pending/unmatched counts without exposing source identifiers.
- This phase did not restart gateways, mutate live profile stores, enable sends, push, or open a pull request.

### Recommended Phase B handoff

Build the historical direct-thread adapter as a pure emitter into this contract. Use deterministic opaque hashes for every event/source/child identity; emit the original reacted-to message relation for both tapback add and removal; persist raw bodies, URLs, GUIDs, filenames, and paths only in private evidence artifacts; and apply through `ingest_communication_event`, `enrich_communication_event`, and `retract_communication_event` against temporary stores only. Gate B should validate direct/group/alias routing, handle `0` versus unknown nonzero handles, GUID variants, rich-link false positives, no-drop accounting, and aggregate-only manifests before any reviewed live-state proposal.

## Phase B — Historical adapter

Status: Complete and locally verified. Phase C has not started.

Commit: Phase B boundary commit containing this progress record (the final SHA is reported by the implementation thread).

### Delivered

- Added a read-only deterministic Apple Messages scanner for one strictly resolved direct thread. It validates every incoming row against the approved handle set, counts handle `0` as rejected anonymous service evidence, and fails the scan on an unknown nonzero handle before any store write.
- Added actor-routed canonical emission into isolated temporary Poke/Guest stores. Text, visible links, safe attachment descriptors, reaction add/remove, replies, and rapid-message batch relations use HMAC-derived event/source/child identities and only the Phase A `ingest_communication_event` and `retract_communication_event` APIs.
- Added modern and legacy associated-GUID parsing, concrete tapback subtype mapping, exact counterpart target validation, removal-to-add retraction, and the original reacted-to `reaction_to` relation on both add and removal events.
- Added signed aggregate review manifests with an HMAC commitment to the separate private evidence artifact, HMAC verification, aggregate actor/category/rejection counts, and explicit no-drop accounting. The CLI requires a non-profile temporary store root and writes the aggregate manifest, private evidence artifact, HMAC-identified contact stores, and key inputs as owner-only files.

### Gate B evidence

Red-first proof: `scripts/run_tests.sh tests/gateway/contact_memory/test_imessage_communication_adapter.py -q` initially failed collection with `ModuleNotFoundError: gateway.contact_memory.imessage_communication_adapter` before implementation.

Final focused verification passed: `scripts/run_tests.sh tests/gateway/contact_memory/test_imessage_communication_adapter.py -q` completed with 14 passed and 0 failed. The historical/store surrounding set `scripts/run_tests.sh tests/gateway/contact_memory/test_imessage_communication_adapter.py tests/gateway/contact_memory/test_imessage_bootstrap.py tests/gateway/contact_memory/test_imessage_link_review.py tests/gateway/contact_memory/test_apply_reviewed_link_interests.py tests/gateway/contact_memory/test_communication_store.py -q` completed with 88 passed and 0 failed. The broader `scripts/run_tests.sh tests/gateway/contact_memory tests/gateway/test_interest_ledger.py -q` suite completed with 194 passed and 0 failed across 12 files.

`python3 -m py_compile gateway/contact_memory/imessage_communication_adapter.py scripts/review_imessage_communication_signals.py tests/gateway/contact_memory/test_imessage_communication_adapter.py` passed; `python3 -m ruff check` over those three files passed; and `git diff --check` passed.

Synthetic no-drop evidence was exact: `text 2 + links 2 + attachments 1 + reactions 2 + replies 1 + explicit_nonsemantic 1 + rejected 1 = selected 10`. Those rows produced 9 canonical events, 4 batch-member relations, 2 URL children, 1 attachment child, and 1 applied reaction retraction across two temporary per-contact stores. Replaying the same scan inserted 0 and deduplicated all 9 events.

### Privacy and routing evidence

- Tests prove direct/group/alias discrimination, modern style 45 with an internal direct-thread group ID, approved alias handle routing, handle `0` accounting, and fail-closed unknown nonzero handles.
- Tests prove populated visible text wins over rich-link archives, only an explicit attributed-string root is used as fallback, hidden preview/CDN URLs are excluded, attachment filenames/paths remain private, and canonical rows contain only bounded descriptors plus opaque HMAC identities.
- Aggregate manifests and CLI stdout are asserted free of raw bodies, URLs, GUIDs, attachment names/paths, and source identifiers. Exact artifacts appear only in the separately written `0600` private evidence file; no model is invoked and no prompt is built.
- The implementation and tests use synthetic temporary SQLite fixtures only. No live Messages database or profile contact store was scanned or mutated, and no gateway, send mode, push, or PR action occurred.

### Known boundaries

- Historical attachment evidence is deterministic metadata only: media kind, validated MIME/UTI, bounded byte size, and optional caption HMAC. Media content analysis remains out of scope.
- Batch membership uses consecutive same-actor, non-reaction events no more than 60 seconds apart. This deterministic rule is intentionally narrow and can be revised only with separate reviewed evidence.
- Reactions and replies whose original target is absent, same-actor, or otherwise unauthenticated are rejected and aggregate-counted. A removal without a matching prior add in the selected history is not guessed into a durable retraction.
- This phase generates review artifacts and disposable canonical stores only. It does not apply historical evidence to live Poke/Guest state or produce semantic projections.

### Phase C handoff

Add the immutable live ingress envelope at the platform boundary, freeze owner/guest scope after direct-thread routing, and persist authenticated BlueBubbles text/link/attachment/reaction/reply/batch evidence before dispatch through the same Phase A APIs. Preserve the Phase B identity, target-relation, actor-routing, privacy, and no-drop invariants; keep reactions non-replying and prove that suppressed, failed, and no-assistant-response turns still retain ingress. Do not reuse the private historical evidence artifact as a live ledger or begin semantic projection work from Phase D.
