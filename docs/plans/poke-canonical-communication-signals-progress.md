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

Status: Gate B repair complete and locally verified. Phase C has not started.

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
- Reactions whose target is absent, same-actor, or otherwise unauthenticated are rejected and aggregate-counted. Replies preserve authenticated same-speaker targets with the contact role, reject absent/foreign targets, and reject malformed or conflicting `reply_to_guid`/`thread_originator_guid` evidence. A removal without a matching prior add in the selected history is not guessed into a durable retraction.
- This phase generates review artifacts and disposable canonical stores only. It does not apply historical evidence to live Poke/Guest state or produce semantic projections.

### Gate B repair

The independent Gate B review rejected commit `e741e337b` with three P0 and five P1 findings. The repair binds incoming identity only to the selected direct chat participant, explicitly rejects every unsupported nonzero associated-message type, strictly parses bare/`p:0`/`bp`/`p:a` targets, reconciles both reply GUID columns, preserves authenticated same-speaker replies, deduplicates attachment joins, and prevalidates duplicate source identities before any write.

The review CLI now stages both disposable stores and both artifacts before publication, removes every published output on an injected later failure, rejects default and active external `HERMES_HOME` roots through descendants and symlink equivalents, and emits generic failure text that does not expose approved handles or source identifiers. Focused regressions cover every reproduced finding, the complete supported reaction matrix, known and unknown unsupported associated types, malformed wrappers, reply agreement/conflict/target cases, hostile attachments, duplicate actor/payload identities, injected store/artifact failures, external-home guards, and stderr privacy.

Repair verification passed: the focused adapter suite completed with 48 passed and 0 failed; the historical/store surrounding set completed with 121 passed and 0 failed; and the full contact-memory plus interest-ledger suite completed with 228 passed and 0 failed across 12 files. `py_compile`, focused Ruff checks, and `git diff --check` passed. No live Messages database or profile store was scanned or mutated, and no gateway was restarted.

### Final narrow Gate B repair

The phase-boundary repair strictly rejects unknown associated-GUID wrappers instead of accepting them as bare targets, accepts authenticated `p`/`bp` wrappers with nonzero numeric parts, and deduplicates attachment joins by canonical attachment GUID while deterministically retaining the lowest-rowid metadata. Regressions cover malformed and unknown wrappers, nonzero numeric wrappers, and conflicting duplicate attachment rows.

The parent focused adapter rerun completed with 62 passed and 0 failed; `py_compile`, focused Ruff checks, and `git diff --check` passed. Final phase-boundary verification then completed the five-file historical/store surrounding set with 136 passed and 0 failed, and the full contact-memory plus interest-ledger suite with 242 passed and 0 failed across 12 files. No live data was touched, no gateway was restarted, and no Phase C work began.

### Phase C handoff

Add the immutable live ingress envelope at the platform boundary, freeze owner/guest scope after direct-thread routing, and persist authenticated BlueBubbles text/link/attachment/reaction/reply/batch evidence before dispatch through the same Phase A APIs. Preserve the Phase B identity, target-relation, actor-routing, privacy, and no-drop invariants; keep reactions non-replying and prove that suppressed, failed, and no-assistant-response turns still retain ingress. Do not reuse the private historical evidence artifact as a live ledger or begin semantic projection work from Phase D.

## Phase C — Live ingress parity

Status: Independent Gate C findings repaired and locally verified; parent Gate C rerun is pending. Phase D has not started.

Commit: Phase C boundary commit containing this progress record (the final SHA is reported by the implementation thread).

### Delivered

- Added a frozen, versioned transport envelope at the platform boundary. BlueBubbles now normalizes every supported payload wrapper and list member, source timestamps, strict boolean/integer aliases, visible URLs, safe attachment descriptors, reply targets, concrete tapback add/remove subtypes, and ordered rapid-message members without assigning a contact namespace.
- Added pre-acknowledgement owner/guest routing and profile-local canonical persistence through the Phase A store APIs. Authenticated direct ingress commits before attachment download, plugin hooks, commands, model dispatch, or suppression; unknown senders, groups, forwarded/internal events, and mixed principals remain excluded.
- Kept reactive behavior intact while making tapbacks evidence-only and making store source uniqueness authoritative across retries/restarts. Historical/live HMAC namespaces are shared, attachment download failure cannot erase descriptor evidence, exact replays deduplicate, and post-commit batching adds deterministic membership without collapsing source events.
- Closed final audit gaps for concurrent first-use HMAC-key creation, strict rejection of unsupported associated-message types and conflicting reply aliases, direct-chat identifier fallback, and explicit owner/guest/group/unknown route isolation.

### Gate C evidence

The focused Phase C set `scripts/run_tests.sh tests/gateway/contact_memory/test_live_communication_ingress.py tests/gateway/test_bluebubbles.py tests/gateway/test_bluebubbles_guest_policy.py -q` completed with 136 passed and 0 failed. The surrounding canonical/contact-memory/BlueBubbles set completed with 378 passed and 0 failed across 15 files.

The bounded synthetic ingress benchmark covered cold SQLite open/migration with four attachment descriptors plus eight concurrent attachment-bearing writes. It measured 27.544 ms cold, 141.751 ms total under contention, and 134.352 ms for the slowest worker; the enforced bounds are under 1 second cold and under 3 seconds for total and per-worker contention, with no download, network enrichment, or model work.

`python3 -m py_compile` over all eight Phase C source/test files passed, focused Ruff checks passed, and `git diff --check` passed. Tests use synthetic temporary payloads, registries, keys, and stores only; no live Messages/contact store was accessed, no gateway was restarted, and no send mode was changed.

The requested full `tests/gateway` run was executed but is not globally green on this branch: it reported 130 failures across 37 files plus teardown/config errors in one additional file. The failures are outside the Phase C BlueBubbles/contact-memory set (including existing async-session-store policy, API toolset, Telegram metadata, Slack, voice, and cwd/config expectations); no Phase C focused or surrounding test failed.

### Current boundaries

- Phase C persists authenticated transport evidence only. It does not infer interests, entities, recommendations, follow-through, or callbacks; those remain Phase D.
- Poke and Guest remain observe-only. This phase did not access live stores/messages, restart a gateway, enable sends, push, or open a pull request.

### Gate C repair

Independent Gate C review rejected boundary commit `88e8b62ff` with six P1 findings. The repair accepts only exact boolean values (including integer `0`/`1`) and rejects malformed direction/group aliases, uses the canonical `emphasis` tapback subtype through all 12 add/remove persistence paths, atomically publishes a fully written first-use HMAC key, and validates reply/reaction targets against the per-contact canonical store before any batch write. Reply relations now preserve authenticated same-speaker versus counterpart roles, while reactions require an authenticated counterpart target exactly as the historical adapter does.

Mixed malformed `data` lists now fail the webhook without ACK or partial ingress. Missing-GUID fallback identities commit to the deterministic full record plus stable list position, keeping same-timestamp attachment-only members distinct across retries. Focused regressions cover exact and alias boolean boundaries, malformed values, the blocked-writer key race, missing/foreign/same-speaker targets, historical/live actor-role parity, all tapback codes through retraction, malformed mixed wrappers, and identical no-GUID list members.

Red-first verification reproduced 27 failures in the focused live-ingress file before implementation. Final verification completed the focused three-file Phase C set with 164 passed and 0 failed, and the 15-file surrounding canonical/contact-memory/BlueBubbles set with 406 passed and 0 failed. The broad `tests/gateway` run again reported the same pre-existing boundary as the original Phase C run: 130 failures across 37 files, with all Phase C focused and surrounding files green. Focused `py_compile`, Ruff, and `git diff --check` passed. No live data was touched, no gateway was restarted, no send mode was changed, and no Phase D work began.

## Phase D — Semantic projections

Status: Final migration-only Gate D findings repaired and locally verified; parent Gate D rerun is pending. Phase E has not started.

Commit: Phase D boundary commit containing this progress record (the final SHA is reported by the implementation thread).

### Delivered

- Added one transactional, replay-safe projection boundary over complete authenticated communication bundles. Semantic identities are derived from projector version, communication event ID, projection kind, and normalized semantic key; a small receipt/version cursor provides idempotency and deterministic chronological replay without creating another evidence ledger.
- Kept `interest_event` as the only semantic interest-evidence ledger. Projection provenance, confidence/method, original topic, and active/retracted state support exact version supersession and reversal while preserving existing weights, folding, decay, distinct-day promotion, legacy evidence, and merge aliases.
- Enforced mechanical actor, initiator, reply, share, reaction, and confidence rules before interest writes. Bounded Qwen output is now an untrusted typed proposal and cannot write an interest without one authenticated canonical event reference.
- Added per-contact entity projections, explicitly linked recommendation lifecycle/follow-through records, and privacy-restricted recurrent callback evidence with canonical support linkage. Raw bodies, URLs, GUIDs, paths, and credential-like semantic labels are rejected from projection rows.
- Replaced the aggregate reviewed-link apply path with occurrence-level canonical projection. Legacy schema-2 aggregate reviews remain valid for dry-run review but are refused for direct state application because they cannot preserve actor, retraction, or distinct-day provenance.

### Gate D evidence

Red-first development covered projection idempotency, transaction rollback, versioning, chronological replay, retraction after fold, actor/reply/reaction rules, entity normalization and per-contact isolation, merge aliases, recommendation follow-through, recurrent restricted callbacks, raw-artifact rejection, canonical Qwen routing, and single-ledger behavior.

The final focused projection plus interest-ledger run completed with 41 passed and 0 failed. The complete surrounding set `scripts/run_tests.sh tests/gateway/contact_memory tests/gateway/test_interest_ledger.py -q` completed with 297 passed and 0 failed across 14 files after the final callback and migration edits. Final focused `py_compile`, Ruff, and `git diff --check` passed.

The requested broad `scripts/run_tests.sh tests/gateway -q` run is not globally green on this branch: it completed with 129 failures across 37 files. The failures remain outside the Phase D contact-memory and interest suites and cover the existing async session-store policy, API toolsets, command handlers/config expectations, Telegram metadata/documents, Slack, voice, and other unrelated gateway surfaces; every contact-memory file in the broad run passed.

Independent Gate D rejected the boundary commit with three P0 defects. The repair removes the extractor recommendation bypass and sends recommendation, interest, entity, follow-through, and callback model proposals through one authenticated `CommunicationProjection` transaction and receipt. Exact regressions prove unauthenticated recommendation output writes nothing and injected mixed-output failure rolls back every semantic table before a successful one-receipt retry.

Reaction removal now deactivates receipts and every semantic projection while applying the canonical retraction transaction, including pending removal-before-add handling. Regression coverage removes the caller-triggered empty reprojection and verifies removal before fold, after fold, and before add. A schema-8 projector-baseline ledger preserves migrated/reviewed/legacy aggregate score, count, valence, bandit, timestamp, state, and identity while projector-owned folded contributions are added, superseded, or retracted.

Repair verification completed the focused extractor/projection/interest set with 65 passed and 0 failed, then the complete 14-file contact-memory plus interest-ledger set with 302 passed and 0 failed. Focused `py_compile`, Ruff, and `git diff --check` passed. No live data was touched, no gateway was restarted, no send mode changed, and Phase E/F did not begin.

The final Gate D rerun found two remaining P0 defects and one P1. Schema-v7 migration now transactionally and idempotently backfills projection baselines for already-folded communication evidence by subtracting active projector-owned score, count, and bandit contributions before the schema-8 version bump; interruption rolls back both DDL and semantic backfill. Supersession and reaction removal restore the legacy aggregate without double counting. Baselines now preserve lifecycle state and `retired_at`, so a projection-promoted `candidate` returns exactly to its prior lifecycle after removal. Standalone GUID/UUID and `sk-...` credential-token labels are rejected while representative artist, show, and place names remain accepted.

Final repair verification completed the focused projection/extractor/interest set with 72 passed and 0 failed, the migration/store/maintenance surrounding set with 88 passed and 0 failed, and the complete 14-file contact-memory plus interest-ledger set with 309 passed and 0 failed. Focused `py_compile`, Ruff, and `git diff --check` passed. Tests use temporary synthetic stores only; no live data was touched, no gateway was restarted, no send mode changed, and Phase E/F did not begin.

The migration-only Gate D follow-up found one remaining lifecycle P0 and one label-validation P1. Schema-v7 backfill no longer copies projector-contaminated lifecycle fields: it derives the projection-free aggregate, preserves a retirement proven to predate projected evidence, restores aggregates that cannot satisfy legacy promotion semantics to `candidate`, and preserves plausible `active` state when incomplete legacy event history makes the distinct-day decision unknowable rather than fabricating missing evidence. Exact migration regressions cover the promoted 1.2/7 candidate retracting to `candidate`, a preexisting active baseline, and a preexisting retired baseline. Compact 32-hex UUID labels are now rejected alongside hyphenated/braced UUIDs and credential tokens without rejecting representative legitimate names.

Follow-up verification completed the focused projection/extractor/interest set with 88 passed and 0 failed, the migration/store/maintenance surrounding set with 110 passed and 0 failed, and the complete 14-file contact-memory plus interest-ledger set with 311 passed and 0 failed. Focused `py_compile`, Ruff, and `git diff --check` passed. Tests used temporary synthetic stores only; no live data was touched, no gateway was restarted, no send mode changed, and Phase E/F did not begin.

### Current boundaries

- Phase D defines and exercises semantic projection only. It does not scan historical messages, apply reviewed evidence to live Poke/Guest stores, run the Phase E backfill, or change observe-only delivery behavior.
- Parent Gate D rerun remains pending. This phase did not access live stores/messages, restart a gateway, enable sends, push, or open a pull request.

## Phase E — Reviewed backfill preparation

Status: Gate E tooling repair complete after independent review rejected the first schema-1 approval boundary. No live apply, maintenance, or Phase F work was performed; the new schema-2 subject snapshots and candidate allowlists await parent/user approval.

Commit: Phase E preparation boundary commit containing this progress record (the final SHA is reported by the implementation thread).

### Delivered

- Added a reviewed canonical backfill builder and CLI that bind one read-only historical direct-thread scan to the Phase B evidence commitment and Phase D projector. The manifest carries occurrence-level opaque event IDs, exact subject routing, aggregate topic/entity candidates, explicit zero recommendation/callback lanes when typed linkage is absent, and an exact HMAC review ID.
- Added a single-transaction per-subject store import that writes canonical evidence, semantic projections, and the subject-filtered import marker atomically. Exact retries do not recount; mixed projection failures roll back the canonical events, semantic rows, receipts, and import marker together. The transaction now verifies the signed target-state snapshot before mutation, while recognizing an exact prior approval retry before the stale-state check.
- Replaced global-state apply revalidation with independently signed Kosta and Stephen source/target snapshots. Applying one subject no longer invalidates the unchanged subject, while a changed source scan or target namespace fails closed for that subject. Approval requires an explicit duplicate-free candidate-ID allowlist, and only those candidates' occurrence bundles and typed projections are materialized.
- The subject HMAC now covers each candidate's semantic identity, event/source identity, canonical-bundle commitment, and complete typed occurrence projection payload, including signal type, valence, confidence, source method, entity type, and every persisted projection field. Apply reconstructs projections only from the signed payload; the separate mutable projection map was removed.
- Added owner-only artifact publication, disposable Poke/Guest stores, live-store SQLite backup, and backup/apply/restore rehearsal confined to temporary coding-profile artifact roots. Raw bodies, URLs, GUIDs, filenames, paths, and internal handle IDs remain only in the `0600` private evidence artifact.

### Historical review evidence

The read-only scan resolved the reviewed Steve direct thread with its explicitly approved historical alias-handle IDs and selected 109,281 source rows. Exact no-drop accounting was `1,827 text + 1 link + 3,613 attachments + 7,992 reactions + 19,601 replies + 75,475 explicit nonsemantic + 772 rejected = 109,281 selected`.

The signed aggregate manifest proposes 12 subject-scoped candidates across 71 projection events: Kosta has five topics and three platform entities across 43 events; Stephen has one topic and three platform entities across 28 events. One existing reviewed topic was excluded, along with eight insufficient-recurrence topic groups and one insufficient-recurrence entity group. Recommendation and callback output remains empty because the Phase B historical events do not provide the typed linkage required by Phase D; the CLI records those exclusions instead of inferring lifecycle or shared-joke semantics.

The rejected schema-1 review ID `8ce521b7cde167ffc581f6b1b639895de84f46b955e42affd51b72041b2aafb1` is explicitly retired from acceptance. The repaired global review ID is `b3330deb7bbbd107e7b3c26a8d78e0416b4ee3c4619d356f24f0e4a90f972647`; subject review IDs are `9cb055ec2e79c9dc2a7cc32a6000f71cdfa14319e099f924cb496db55bcebc27` for Kosta and `d91a2fbc79b5a48c679f3247769dfafd4b92f39dc60b91d9c132e3ce8bd8d275` for Stephen. New aggregate review, candidate summary, private source evidence, disposable stores, and restore evidence are under `/Users/Kosta/.hermes/profiles/coding/artifacts/phase-e-canonical-review-repair-20260714-165047/`; the root is `0700` and all JSON/SQLite files are `0600`.

### Verification and boundary

Focused red-first coverage now includes typed-payload tamper rejection, candidate subsets, empty/duplicate/cross-subject allowlist rejection, sequential cross-subject applies, stale source/target rejection, rollback, exact retry idempotency, explicit retired-ID rejection, owner-only CLI output, and restore rehearsal. The focused/surrounding three-file set completed with 67 passed; the complete contact-memory plus interest-ledger set completed with 320 passed across 15 files. Focused `py_compile`, Ruff, and `git diff --check` passed. An independent adversarial repair review reran 93 focused tests plus 25 repeated restore rehearsals and found no remaining P0/P1 blocker.

Disposable stores contain 43 Kosta and 28 Stephen canonical events with the same number of Phase D receipts, while target import manifests contain only their own subject. Restore rehearsal produced different post-apply snapshot hashes and then byte-identical restored hashes for both live-store backups. Both Poke and Guest remain `observe`; live stores contain zero Phase E import markers. No live store was applied, no maintenance ran, no Messages row changed, no gateway restarted, no sends were enabled, and no push or PR occurred.

### Phase E v3 enrichment review (2026-07-14)

Status: The underfit v2 enrichment outcome was rejected and replaced by v3 review tooling only. Nothing was applied to the live Poke or Guest stores; Gate E remains a parent review step.

- Added a strict Apple attributed-body visible-root decoder for the typed `NSAttributedString`/`NSString` payload. It rejects truncated, overlong, malformed, non-UTF-8, control-bearing, wrong-root, and preview-only archives without recursively choosing an arbitrary string. Real-shaped typedstream/keyed-archive fixtures and privacy canaries verify that hidden preview/CDN/archive strings cannot become visible text.
- Added versioned closed-world taxonomy and enrichment modules with multi-label longest-phrase matching, typed public entities, platform denial, first-person positive/negative/routine stance, question/third-party/logistics/sarcasm gates, actor attribution, batch recurrence collapse, conflict watchlisting, and authenticated target-only reply/reaction support. Topic/activity candidates require qualifying recurrence on two independent days; neutral mentions, shares, logistics, mixed polarity, and unauthenticated relations remain watchlist/exclusion evidence rather than positive interests.
- Extended schema 2 so subject reviews sign ontology/rule/derivation versions, taxonomy commitment, support and recurrence IDs, actor/polarity/stance/gates, canonical bundle commitment, derivation commitment, and complete typed projections. Apply re-derives each approved support from the current read-only scan. Signed candidate-subset artifacts bind exact candidate payload commitments and reject subject crossing or tampering.
- Removed raw private-evidence publication from v3. The final artifact contains aggregate source accounting, closed candidate/watchlist summaries, signed manifests/subsets, disposable stores, and restore evidence only; raw text, URLs, GUIDs, handles, filenames, and paths remain ephemeral in-process. Every generated directory is `0700`, every file is `0600`, and the JSON privacy scan found no URL/path/GUID shape.

The v3 decoder recovered 75,409 attributed-root standalone text events, expanding the ordinary text corpus from 1,827 to 77,236 events. On the identical legacy-visible denominator, deterministic closed-world matching rose from 116/1,827 (6.34%) to 144/1,827 (7.88%), a 1.54-point precision-proxy coverage gain. Across the expanded corpus, 5,324/77,236 standalone text events matched and 7,382 total events carried closed semantics; this is intentionally a conservative extraction rate rather than a target recall quota.

The final signed review proposes 41 candidates over 761 projection events: Kosta has 7 topics, 4 activities, and 10 typed public entities; Stephen has 6 topics, 5 activities, and 9 typed public entities. Twenty-six non-applicable groups remain aggregate-only watchlist entries (23 gated/conflicted and 3 below recurrence), one existing reviewed topic was excluded, platform entity candidates are zero, and recommendation/callback lanes remain empty without typed linkage.

Final review IDs are global `55752a9ce9217f09c966dd44c2e459c0c7ca45185c839138b4eae1215644353c`, Kosta `82a4d8b424baf72df39d824491c6f1efd21a7316f50ba062b3ba55ff35108df8`, and Stephen `219c2348756d5d6fe3e390faa22e10d4a735ab178612b811fc60492c17ec728c`. Artifacts are under `/Users/Kosta/.hermes/profiles/coding/artifacts/phase-e-canonical-review-v3-20260714-180416/`.

Focused verification completed with 85 passed, the eight-file surrounding set with 193 passed, and the complete contact-memory plus interest-ledger set with 334 passed across 16 files. Both signed subject subsets and the global manifest revalidated; disposable apply succeeded; both restore rehearsals returned byte-identical backups after producing distinct mutated hashes. No maintenance, Phase F work, gateway restart, send enablement, push, or PR occurred.
