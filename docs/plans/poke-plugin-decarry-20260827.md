# Poke/Guest Plugin De-Carry Plan

**Status:** Draft for adversarial review  
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

## 4. Target architecture

### 4.1 Generic core contracts

Add a small public module, tentatively `gateway/conversation_extensions.py`, containing immutable dataclasses/protocols only:

- `GatewayConversationExtension`: lifecycle and turn-boundary interface.
- `GatewayRuntimeFacade`: bounded host capabilities; no raw `GatewayRunner` handle.
- `GatewayRouteContext` / `GatewayRouteDirective`: normalized pre-auth source routing and trusted metadata.
- `GatewayTurnContext` / `GatewayTurnAugmentation`: per-turn user/system context, request-scoped tools, and policy scope.
- `GatewayTurnResult`: authenticated source identity, user/assistant text, durable message IDs, and delivery outcome.
- `GatewayBackgroundTask`: host-owned task registration and cancellation.
- `InitiatedTurnRequest` / result facade: generic initiated-assistant child creation.

Plugin registration should use a dedicated `PluginContext.register_gateway_conversation_extension(...)` method with tracked unload. Registration must be profile-aware and replacement-safe like platform registrations.

### 4.2 Core-owned capabilities

Core continues to own:

- session database schema and generic initiated-assistant child transaction;
- request-scoped tool binding mechanism;
- platform authorization primitives and adapter transport;
- plugin discovery/activation and capability gates;
- the generic delivery ledger if upstream behavior uses it;
- lifecycle scheduling/cancellation and health aggregation;
- generic turn composition and agent-loop execution.

These capabilities must contain no hard-coded `poke`, `guest`, contact IDs, relationship names, or proactive policy.

### 4.3 Plugin-owned Poke implementation

Create a `poke` general plugin in the user-plugin repository. It owns:

- Guest contact registry, route decisions, identity prompt policy, slash/tool restrictions, and workspace guards;
- contact-memory schema/store, broker, extraction, ingress, retrieval, interest maintenance, research workers, backfill/review utilities;
- proactive scheduler, gate/fetch/compose policy, exactly-once ownership/claims, transport policy, status, and alarms;
- conversation-texture engine plus existing per-turn texture hook;
- Poke/Guest slash and CLI/operator commands;
- Poke-specific configuration schema/defaults and profile names/contact allowlists;
- operational scripts, eval fixtures, docs, and all tests that assert plugin-owned policy.

The existing `conversation-texture` plugin may either become a thin compatibility plugin importing the Poke plugin's public texture engine, or remain independently loadable while the engine moves to a shared plugin-owned package. No core import is allowed.

## 5. Checkpoints

### Checkpoint 1 — Plugin-owned portable libraries, no runtime cutover

Purpose: establish the destination and prove copied code/tests before changing live ownership.

Plugin repository:

- Add the `poke` plugin manifest/package and explicit public subpackages.
- Move/copy texture engine, Guest policy, contact-memory, proactive libraries, operational scripts, and their policy tests.
- Replace sibling `gateway.*` imports with plugin-relative imports or small public core contracts.
- Keep registration inert by default; no background watcher or routing mutation yet.
- Add compatibility/version checks declaring the required future core extension API.
- Make `conversation-texture` consume the plugin-owned engine while preserving the current core-engine fallback until Checkpoint 3.

Core repository:

- Add no behavior-changing seam yet.
- Correct stale texture ownership documentation and split the carry feature conceptually into retired texture attachment, temporary leaf carry, and future generic seam.
- Add the migration tracker and explicit dual-run interlock requirements.

Verification:

- Plugin unit suites for texture, Guest policy, contact memory, proactive scheduler/gate/transport/status.
- Structural import test: plugin package has no private-core imports and no imports from carried `gateway.contact_memory`, `gateway.proactive_*`, or `gateway.guest_access`.
- Existing core focused suites remain green.

Stop after clean commits in both repositories. Run an independent P0/P1 review of the exact two-repository checkpoint.

### Checkpoint 2 — Generic core extension seam and dark runtime integration

Purpose: add reusable core extension points and prove parity without making them authoritative.

Core repository:

- Implement the public extension protocol and tracked plugin registration.
- Add bounded `GatewayRuntimeFacade` methods for lifecycle tasks, current profile/home, session lookup, initiated child creation, safe turn injection, prepared delivery, and health/status reporting.
- Add structured fire sites at pre-auth route, authenticated ingress, turn preparation, execution-policy scope, post-turn completion, gateway start, and gateway stop.
- Ensure extension failure semantics are explicit per phase: route/policy fail closed; optional enrichment fail open; delivery/ledger failures remain visible and retry-safe.
- Do not pass `GatewayRunner`, mutable session stores, raw SDK clients, credentials, or private callbacks into plugins.
- Preserve normal behavior when no extension is registered.

Plugin repository:

- Register the Poke extension in dark/observe mode.
- Run both existing core implementation and plugin implementation against the same frozen inputs, compare normalized decisions/context/status, and record mismatches without duplicate writes or sends.
- Add readiness probes for extension loaded, config compatible, state roots accessible, and platform ownership resolved.

Verification:

- Core contract tests for registration, unload/reload, profile isolation, sync/async callbacks, exception policy, cancellation, and no-plugin behavior.
- Differential parity tests for Guest routing/tool decisions, contact recall, ingress persistence inputs, proactive claims/gates, and initiated-child metadata.
- Full existing Guest/contact/proactive focused suite.

Stop after clean commits. Run an independent P0/P1 review focused on isolation, auth, replay, cancellation, and unsafe capability leakage.

### Checkpoint 3 — Authoritative plugin cutover and core deletion

Purpose: switch ownership and remove Kosta-specific core carry.

Plugin repository:

- Make the Poke extension authoritative when enabled and compatible.
- Move all remaining Poke/Guest commands, scripts, tests, docs, and configuration interpretation.
- Preserve a fail-closed readiness state when Poke/Guest config requires the extension but it is absent/incompatible.
- Keep legacy config parsing for one rollout window, with explicit deprecation evidence; do not rewrite live config until activation.

Core repository:

- Delete `gateway/contact_memory/`, `gateway/proactive_*`, `gateway/guest_access.py`, and `gateway/conversation_texture_v2.py` after all consumers move.
- Remove Poke-specific blocks/imports/helpers from `gateway/run.py`, `gateway/slash_access.py`, `gateway/authz_mixin.py`, `model_tools.py`, cron/config/env paths, and tests.
- Retain only generic extension invocations and generic initiated-session/storage primitives.
- Remove or migrate Poke-specific scripts/evals/docs.
- Rewrite `scripts/local_carry_manifest.yaml`, exemptions, and thinning baseline from actual residual code. Do not mark a path retired while symbols still exist.

Objective acceptance metrics:

- Zero core imports/references to `gateway.contact_memory`, `gateway.proactive_*`, `gateway.guest_access`, or `gateway.conversation_texture_v2`.
- Zero hard-coded Poke/Guest contact/profile policy in shared core.
- All deleted policy tests present and passing in the plugin repository.
- The old `gateway.poke-guest-conversation-stack` carry feature is removed; any surviving feature is generic and names its exact seam.
- `gateway/run.py` Poke/Guest/contact/proactive symbol count is zero except generic extension terminology.
- Carry report, doctor, contract, and thinning checks pass from a newly pinned baseline.

Stop after clean commits. Run an independent P0/P1 review against both repositories and the post-cutover contract.

### Checkpoint 4 — Integrated activation, final review, and landing

- Rebase/cherry-pick checkpoint commits onto fresh landing worktrees cut from current plugin `main` and core `local/studio-slim`.
- Resolve unrelated live plugin edit (`bluebubbles/adapter.py`) independently; do not overwrite or absorb it accidentally.
- Run plugin focused and broader suites, core focused and broader suites, `diff --check`, carry doctor/report/validate, thinning check, and real-config read-only compatibility probes.
- Run final independent adversarial review of exact integrated tips. Only P0/P1 findings block; one narrow closure review after repairs.
- Land plugin compatible/inert commit(s), then core seam/cutover commit(s), then explicit profile/root configuration activation.
- Use the detached safe restart helper. Verify fresh PIDs, HTTP health 200, root and profile plugin lists, Poke and Guest extension readiness, synthetic owner/Guest routing with outbound transport mocked, and a full post-response callback path.
- Verify no duplicate proactive watcher, no duplicate ingress write, no double texture, no cross-profile policy leakage, and no contact-facing smoke message.

## 6. Edge-case matrix

| Edge | Required behavior |
|---|---|
| Plugin missing on Poke/Guest | readiness fails closed; no Guest authorization or proactive send |
| Plugin missing on ordinary profile | ordinary Hermes behavior unchanged |
| Plugin callback throws during route/policy | deny/drop safely and surface health alarm |
| Optional recall/extraction throws | reply continues; error recorded; no corrupt partial state |
| Restart during proactive claim | durable claim/ledger permits safe recovery without duplicate send |
| Delivery result unknown | no blind retry; preserve uncertain state for operator review |
| Multiplexed root process | extension/config/state resolved under routed profile home per call |
| Plugin reload while tasks active | old generation cancels and cannot clear/overwrite newer registration |
| Conversation texture + proactive block | exactly one texture compilation; no duplicate markers |
| Existing sessions from pre-cutover | parent links, profile route, and origin remain readable |
| Legacy config only | parsed compatibly for rollout window; deprecation is explicit |
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
- During cutover, retain one compatibility window where the plugin can detect old core and stay inert.
- If post-cutover health fails, use the reviewed revert commits/config rollback, then the detached safe restart path.
- Never roll back by copying files between live checkouts, resetting dirty worktrees, or starting a second gateway.

## 10. Open decisions for adversarial review

These are design questions for the reviewer to resolve before implementation, not permission questions for the user:

1. Whether `GatewayConversationExtension` should be one cohesive service or several narrowly registered providers (route, turn augmentation, lifecycle, delivery).
2. Whether proactive prepared delivery belongs in the generic runtime facade or should use a new capability-gated platform action.
3. Whether legacy config remains under `agent.contact_memory` / `agent.proactive` for one release or moves immediately under `plugins.entries.poke.settings` with a read-only compatibility adapter.
4. Whether the conversation-texture plugin remains standalone or becomes a compatibility alias of the Poke plugin's texture component.
