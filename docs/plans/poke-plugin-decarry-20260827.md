# Poke/Guest Plugin De-Carry Plan

**Status:** Revised after adversarial rejection; pending closure review
**Date:** 2026-08-27  
**Core worktree:** `/Users/Kosta/LocalDev/.studio-only/hermes-worktrees/poke-plugin-decarry`  
**Plugin worktree:** `/Users/Kosta/LocalDev/.studio-only/hermes-kosta-plugin-worktrees/poke-plugin-decarry`  
**Core base:** `240839a7aca0e25d7f1e67059a96da463b9f2038` (`local/studio-slim`)  
**Plugin base:** `62422c33bb6a93a41aef7e19c89371dbb451a69b` (`main`)

## 1. Goal

Move as much Kosta-specific Poke/Guest/contact-memory/proactive behavior as safely possible from Hermes Agent core into the user-plugin repository. Core should retain only reusable, provider-neutral extension contracts and generic persistence/runtime capabilities that would be reasonable upstream features.

This is not a source-file relocation exercise. The final dependency direction must be:

```text
Hermes core -> generic gateway extension contract
Poke plugin -> implements that contract using plugin-owned policy and state
```

Core must not import `hermes_plugins.poke`, `plugins.poke`, or any other user-plugin package. The plugin must not monkeypatch or import private (`_name`) core symbols.

## 2. Current evidence and baseline

- Local branch changes 670 files relative to upstream; 91 are changed on both sides.
- Poke/Guest leaf code is about 19,873 lines across `gateway/contact_memory`, `gateway/proactive_*`, and `gateway/guest_access.py`.
- `gateway/run.py` is the dominant collision surface: approximately 3,102 locally changed lines and 321 Poke/Guest/contact/proactive references.
- Existing useful plugin surfaces: platform overrides, `pre_gateway_dispatch`, `pre_llm_call` user/system context lanes, request-scoped tools, slash/CLI commands, host-owned `ctx.llm`, background-task registration, profile-scoped plugin state, and safe gateway message injection.
- Missing surfaces: a public gateway-runtime lifecycle, structured source-routing mutation, fail-closed request policy, post-turn completion with trusted route identity, and a host-owned initiated-assistant/delivery facade for proactive work.
- Conversation texture glue is already in the plugin, but its 603-line engine remains carried in core and `gateway.proactive_fetch` imports it.
- The carry manifest still groups texture, Guest, contact memory, and proactive work into one stale `gateway.poke-guest-conversation-stack` feature.

## 3. Safety invariants

1. **Guest authorization fails closed.** Missing plugin, malformed registry, plugin exception, or ambiguous identity must not grant Guest access or tools.
2. **Profile/contact isolation survives multiplexing.** No process-global mutable Guest or contact state may leak between root, Poke, Guest, or other profiles. Request-scoped policy remains `ContextVar`-backed.
3. **Exactly-once proactive delivery remains durable.** Replay, partial commit, restart, and transport uncertainty retain the current ledger and claim semantics.
4. **No contact receives a migration smoke message.** Live verification uses mocked transport, owner/self-authorized paths, health/status inspection, and naturally occurring traffic only.
5. **Plugin absence is safe.** Core starts normally with no Poke extension. Profiles configured to require Poke must fail readiness closed rather than silently losing Guest/proactive behavior.
6. **Two-repository rollout is atomic at behavior level.** The plugin lands first in an inert/compatible state; core seams land next; config activation happens only when both compatible revisions are present. Rollback order is the reverse.
7. **Current public session semantics stay intact.** Proactive initiated children, parent links, model metadata, session routing, compression/recovery, and delivery visibility must not regress.
8. **Core owns the required-extension declaration.** A profile cannot declare the requirement only inside the plugin that may be missing. Core validates extension ID, API version, capabilities, and health before adapter connection and again before required ingress or delivery.
9. **Transport authorization and runtime routing remain separate trust domains.** The transport profile/home is immutable; extension routing is a typed proposal validated by core before entering the runtime-profile scope.
10. **Tool policy is enforced at final dispatch.** Filtered schemas and request-scoped tools are not sufficient. Every direct, deferred, bridge, and MCP dispatch passes the generic authorization capability; missing or malformed required policy denies. Core issues an immutable request-policy token after validated routing and explicitly propagates it across executor/thread boundaries.
11. **Code ownership moves without moving durable data.** Existing profile `state.db`, contact-memory SQLite trees, private queues, and root-shared ownership registry remain in place for this program.

## 4. Target architecture

### 4.1 Generic core contracts

Add a small public module, tentatively `gateway/conversation_extensions.py`, containing immutable dataclasses/protocols only:

- `GatewayConversationExtension`: one atomically registered, generation-scoped bundle containing narrow typed admission/routing, turn augmentation, tool authorization, ingress/post-turn observation, and lifecycle subinterfaces.
- `GatewayRuntimeFacade`: bounded host capabilities; no raw `GatewayRunner` handle.
- `GatewayRouteContext` / `GatewayRouteDirective`: normalized pre-auth source routing and trusted metadata.
- `GatewayTurnContext` / `GatewayTurnAugmentation`: per-turn user/system context, request-scoped tools, and policy scope.
- `GatewayTurnResult`: authenticated source identity, user/assistant text, durable message IDs, and delivery outcome.
- `GatewayBackgroundTask`: host-owned task registration and cancellation.
- `InitiatedTurnRequest` / result facade: generic initiated-assistant child creation.

Plugin registration should use a dedicated `PluginContext.register_gateway_conversation_extension(...)` method with tracked unload. Registration must be profile-aware and replacement-safe like platform registrations.

Core also owns `gateway.required_conversation_extensions`, a per-profile requirement list containing extension ID, API version, and required capabilities. Missing, incompatible, ambiguous, or unhealthy required extensions make only that profile unready and deny its ingress/sends. Ordinary profiles retain normal no-extension behavior.

The routing order is immutable: core captures the trusted adapter identity and transport profile/home; the extension returns a typed route/admission decision without mutating the live source; core validates the target against served profiles and a permitted route map; transport authorization stays bound to the original home; only then does core enter the validated runtime-profile scope.

### 4.2 Core-owned capabilities

Core continues to own:

- session database schema and generic initiated-assistant child transaction;
- request-scoped tool binding mechanism;
- platform authorization primitives and adapter transport;
- plugin discovery/activation and capability gates;
- final-dispatch authorization invocation for every tool path;
- eager startup enumeration and activation of every served profile, not lazy first-message registration;
- lifecycle scheduling/cancellation and health aggregation;
- generic turn composition and agent-loop execution;
- a capability-gated `send_authenticated_existing_dm` platform action that cannot create chats or fall back, accepts a correlation/idempotency key only after the plugin durably reserves the attempt, and returns `sent`, `definitive_failure`, or `unknown` plus a receipt when available.

These capabilities must contain no hard-coded `poke`, `guest`, contact IDs, relationship names, or proactive policy.

### 4.3 Plugin-owned Poke implementation

Create a `poke` general plugin in the user-plugin repository. It owns:

- Guest contact registry, route decisions, identity prompt policy, slash/tool restrictions, and workspace guards;
- contact-memory schema/store, broker, extraction, ingress, retrieval, interest maintenance, research workers, backfill/review utilities;
- proactive scheduler, gate/fetch/compose policy, exactly-once ownership/claims/ledger/retry decision, transport policy, status, and alarms;
- conversation-texture engine plus existing per-turn texture hook;
- Poke/Guest slash and CLI/operator commands;
- Poke-specific configuration schema/defaults and profile names/contact allowlists;
- operational scripts, eval fixtures, docs, and all tests that assert plugin-owned policy.

The existing `conversation-texture` plugin remains independently loadable. Its engine moves to a stable neutral library in the plugin repository consumed by both `conversation-texture` and Poke proactive composition. Ordinary texture users do not load Poke. Exactly one texture hook owner is permitted per profile. No core import is allowed.

## 5. Checkpoints

### Checkpoint 1 — Pure portable extraction only

Purpose: establish the destination and prove copied code/tests before changing live ownership.

Plugin repository:

- Add the `poke` plugin manifest/package and explicit public subpackages.
- Copy/move texture engine, Guest policy, contact-memory, proactive libraries, operational scripts, and their policy tests.
- Replace sibling `gateway.*` imports with plugin-relative imports or small public core contracts.
- Establish a neutral shared texture library.
- Keep all registration, watchers, live writes, initiated children, routing, and sends disabled.
- Preserve existing database paths and schemas.
- Add compatibility/version checks declaring the required future core extension API.
- Do not switch the live `conversation-texture` plugin to the new engine in this checkpoint.

Core repository:

- Add no behavior-changing seam yet.
- Correct stale texture ownership documentation and split the carry feature conceptually into retired texture attachment, temporary leaf carry, and future generic seam.
- Add the migration tracker and explicit dual-run interlock requirements.

Verification:

- Plugin unit suites for texture, Guest policy, contact memory, proactive scheduler/gate/transport/status.
- Structural import test: plugin package has no private-core imports and no imports from carried `gateway.contact_memory`, `gateway.proactive_*`, or `gateway.guest_access`.
- Existing core focused suites remain green.

Stop after clean commits in both repositories. Run an independent P0/P1 review of the exact two-repository checkpoint.

### Checkpoint 2 — Generic seams and side-effect-free dark validation

Purpose: add reusable core extension points and prove parity without making them authoritative.

Core repository:

- Implement atomic extension-bundle registration and generation-safe unload.
- Add the core-owned required-extension declaration and fail-closed profile readiness gate.
- Add the explicit transport-home/admission/runtime-route sequence.
- Add final-dispatch tool authorization across direct, deferred, bridge, and MCP paths.
- Add per-profile lifecycle activation and health aggregation; task identity includes extension ID, profile home, task key, and generation. Replacement cancels and joins the old generation before enabling the new one.
- Add bounded host capabilities for lifecycle tasks, current profile/home, session lookup, initiated child creation, safe turn injection, and health/status reporting.
- Add capability-gated `send_authenticated_existing_dm` to platform actions; do not expose prepared delivery through the runtime facade.
- Add structured fire sites at pre-auth route, authenticated ingress, turn preparation, execution-policy scope, post-turn completion, gateway start, and gateway stop.
- Ensure extension failure semantics are explicit per phase: route/policy fail closed; optional enrichment fail open; delivery/ledger failures remain visible and retry-safe.
- Do not pass `GatewayRunner`, mutable session stores, raw SDK clients, credentials, or private callbacks into plugins.
- Preserve normal behavior when no extension is registered.

Plugin repository:

- Register the Poke extension in dark/observe mode.
- Compare only pure/read-only decisions in-process. Stateful parity uses immutable SQLite snapshots or isolated shadow stores and serialized write intents. The observe plugin has no live watcher or send capability and cannot create initiated children.
- Add readiness probes for extension loaded, config compatible, state roots accessible, and platform ownership resolved.

Verification:

- Core contract tests for registration, unload/reload, profile isolation, sync/async callbacks, exception policy, cancellation, and no-plugin behavior.
- Per-dispatch-path bypass tests for direct, deferred, bridge, and MCP execution, including executor/thread hops.
- Differential parity tests for Guest routing/tool decisions and pure contact/proactive decisions. Stateful tests assert unchanged live database hashes/counters and zero outbound calls while using snapshot/shadow stores.
- Full existing Guest/contact/proactive focused suite.

Stop after clean commits. Run an independent P0/P1 review focused on isolation, auth, replay, cancellation, and unsafe capability leakage.

### Checkpoint 3 — Authoritative plugin activation with legacy fallback retained

Purpose: prove the plugin as the sole live owner while retaining a configuration-only rollback.

Plugin repository:

- Make the Poke extension authoritative when enabled, required, healthy, and compatible.
- Move all remaining Poke/Guest commands, scripts, tests, docs, and configuration interpretation.
- Make `plugins.entries.poke.settings` canonical. For one rollout window, use a read-only adapter for legacy `agent.contact_memory` / `agent.proactive`; conflicting values fail readiness closed and no config is rewritten automatically.
- Adopt the existing durable paths and schemas in place. Verify schema versions, SQLite integrity, row counts, active claims, terminal ledgers, and ownership before activation.

Core repository:

- Add a single-owner selector without deleting the legacy implementation.
- On startup, select exactly one owner for routing, ingress, extraction, proactive claims, child creation, and delivery.
- Keep the complete legacy owner inactive but available for rollback.

Activation acceptance:

- Safely restart and prove required-extension readiness, transport-home authorization, runtime routing, final tool enforcement, existing data continuity, one watcher, one ingress write, one texture compilation, and zero outbound smoke sends.
- Run bounded soak or naturally occurring traffic verification while legacy rollback remains available.
- Rollback is a config switch plus safe restart at this checkpoint.

Stop after clean commits. Run an independent P0/P1 review against both repositories and the authoritative single-owner contract.

### Checkpoint 4 — Core deletion and final de-carry

- Only after Checkpoint 3 evidence is accepted, delete `gateway/contact_memory/`, `gateway/proactive_*`, `gateway/guest_access.py`, and `gateway/conversation_texture_v2.py` and all Poke-specific blocks/imports/helpers from shared core.
- Retain only generic extension invocations, final-dispatch policy enforcement, required-extension readiness, platform action capability, and generic initiated-session/storage primitives. Do not remove `enforce_guest_tool_call` until direct, deferred, bridge, and MCP bypass tests all pass, including executor/thread propagation.
- Migrate remaining policy tests and operational tooling, then prove no consumers remain.
- Rewrite carry manifest, exemptions, residual matrix, and thinning baseline from actual residual generic seams.
- Require zero core imports/references to removed modules, zero hard-coded Poke/Guest policy, all migrated policy tests passing, and zero Poke-specific symbols in `gateway/run.py` outside generic extension terms.
- Rebase/cherry-pick onto fresh landing worktrees, preserve the unrelated live `bluebubbles/adapter.py` edit, run focused/broader suites and carry checks, and obtain final independent approval.
- Land deletion separately, safely restart, and repeat readiness, authorization, replay, uncertainty, lifecycle, post-response, and no-duplicate verification.
- After deletion, rollback requires reverting the deletion commit before disabling the plugin. Physical data relocation remains out of scope.

## 6. Edge-case matrix

| Edge | Required behavior |
|---|---|
| Plugin missing on Poke/Guest | readiness fails closed; no Guest authorization or proactive send |
| Plugin missing on ordinary profile | ordinary Hermes behavior unchanged |
| Plugin callback throws during route/policy | deny/drop safely and surface health alarm |
| Optional recall/extraction throws | reply continues; error recorded; no corrupt partial state |
| Restart during proactive claim | durable claim/ledger permits safe recovery without duplicate send |
| Delivery result unknown | no blind retry; preserve uncertain state for operator review |
| Multiplexed root process | transport home remains immutable; extension/config/state resolve only after validated admission into the routed profile |
| Plugin reload while tasks active | old generation cancels and cannot clear/overwrite newer registration |
| Conversation texture + proactive block | exactly one texture compilation; no duplicate markers |
| Existing sessions from pre-cutover | parent links, profile route, and origin remain readable |
| Legacy config only | read-only compatibility adapter; conflicting canonical/legacy values fail readiness closed |
| Dark parity | pure decisions only in-process; stateful parity uses snapshots/shadow stores and no-send transport |
| Existing durable databases | adopted in place with integrity/schema/claim checks; no physical migration |
| Dirty live plugin checkout | landing preserves unrelated edit and verifies exact target commits |

## 7. Documentation and tracking contract

Update at every checkpoint, not only at the end:

- `docs/local/POKE_PLUGIN_DECARRY_TRACKER.md`: current checkpoint, commits, tests, review report, residual carry metrics, blockers.
- This plan: decisions and scope changes after adversarial review.
- `scripts/local_carry_manifest.yaml` and exemptions: exact ownership only.
- `docs/local/SLIM_EXIT_RESIDUAL_MATRIX_2026-07-29.md`: residual category and update-conflict effect.
- Plugin `README.md`, `docs/plugin-hygiene.md`, and Poke plugin README: activation, contracts, rollback, tests.
- Existing Poke/contact/proactive plans: mark superseded sections and link to current ownership; do not silently rewrite historical completion reports.
- `hey.md`: only active coordination; remove this lane's note when landed.

## 8. Review prompts and evidence

Each adversarial reviewer receives:

- exact core and plugin base/tip SHAs;
- complete two-repository diff and checkpoint contract;
- focused test outputs and real-config read-only probes;
- explicit instruction to construct failures for auth, cross-profile isolation, restart/replay, partial persistence, cancellation, duplicate delivery, plugin absence, and activation order;
- read-only constraint and required `CURRENT_FINAL_DIFF_APPROVED` / `REJECTED` verdict.

## 9. Rollback

- Before authoritative cutover, disable the Poke plugin entry; existing core implementation remains owner.
- During activation, retain the complete legacy owner behind a single-owner selector; rollback is a config switch plus detached safe restart.
- After the deletion checkpoint, revert the deletion commit before disabling the plugin.
- Never roll back by copying files between live checkouts, resetting dirty worktrees, or starting a second gateway.

## 10. Decisions resolved by adversarial review

1. Register one atomic, generation-scoped extension bundle with narrow typed subinterfaces; do not permit partial policy-generation replacement.
2. Keep ledger/retry ownership in Poke and add a narrow capability-gated authenticated-existing-DM platform action; no prepared-delivery runtime facade.
3. Make plugin settings canonical immediately, with one read-only legacy adapter and fail-closed conflict handling. Required-extension declaration is core-owned.
4. Keep conversation texture independently loadable and move its engine to a neutral shared plugin-repository library.
