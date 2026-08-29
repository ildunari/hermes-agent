# Poke/Guest Plugin De-Carry Plan

**Status:** **COMPLETE** as of 2026-08-29. Checkpoint 4 runtime deletion is landed on the live core branch, the compatible plugin migration is landed on plugin `main`, the gateway was restarted after the final BlueBubbles load-order fix, live startup/ownership/readiness was verified, the no-send validator probe passed, focused tests passed, and migration worktrees/branches were cleaned up.
**Date:** 2026-08-27
**Core live branch:** `/Users/Kosta/.hermes/hermes-agent` on `local/studio-slim`.
**Plugin live branch:** `/Users/Kosta/.hermes/plugins` on `main`.
**Tracker:** `docs/local/POKE_PLUGIN_DECARRY_TRACKER.md`.

## 1. Goal

Move Kosta-specific Poke/Guest/contact-memory/proactive behavior from Hermes Agent core into the user-plugin repository. Core now retains only reusable, provider-neutral extension contracts and generic persistence/runtime capabilities that are reasonable upstream features.

Final dependency direction:

```text
Hermes core -> generic gateway extension contract
Poke plugin -> implements that contract using plugin-owned policy and state
```

Core does not import `hermes_plugins.poke`, `plugins.poke`, or any other user-plugin package. The plugin does not monkeypatch or import private core policy leaves.

## 2. Final architecture

### 2.1 Generic core contracts retained

Core owns the provider-neutral gateway-extension surface:

- immutable extension registration, generation-scoped lifecycle, unload/reload safety, and profile-scoped readiness;
- typed route/admission directives, turn augmentation, request-scoped executable tools, final-dispatch authorization, and post-turn observation;
- exactly-one conversation ownership across the seven domains: `routing`, `ingress`, `extraction`, `turn_policy`, `initiated_claims`, `child_creation`, and `delivery`;
- bounded host capabilities for lifecycle tasks, safe current-profile/home resolution, initiated child creation, message injection, and authenticated existing-DM delivery.

Core still owns session storage, transport adapters, plugin discovery, readiness aggregation, generic initiated-session persistence, and the capability-gated authenticated existing-DM platform action. These are generic seams, not Poke product policy.

### 2.2 Plugin-owned Poke implementation

The user plugin repository owns:

- Guest contact registry, BlueBubbles route decisions, identity/context prompt policy, final tool policy, and guest filesystem/tool restrictions;
- contact-memory schema/store, ingress persistence, extraction, interest maintenance, Lane A recall, Lane B retrieval, research workers, and operator tooling;
- proactive scheduler, claim/reserve/finish ledger policy, lifecycle watcher, child creation, delivery policy, status, alarms, and cron tooling;
- shared conversation texture engine consumed from the plugin repository;
- canonical Poke settings, profile-home resolution under multiplex, and read-only compatibility with legacy config keys for the rollout window.

BlueBubbles also registers as a plugin platform without requiring bare `poke` to be importable, and registers a `cron_delivery_validator_fn` when the current core supports the seam.

## 3. Safety invariants — final result

1. Guest authorization fails closed on missing/incompatible/ambiguous extension, malformed registry, callback exception, or unresolved identity.
2. Profile/contact isolation survives multiplexing: transport home remains immutable, runtime profile changes only after validated route directives, and Poke/Guest state resolves by canonical profile home.
3. Exactly-once proactive delivery remains durable: claim/reserve/finish and unknown-delivery semantics are plugin-owned through generic host operations.
4. No contact received a migration smoke message: final live verification used health/status logs and an in-process no-send probe only.
5. Plugin absence is safe: ordinary profiles run unchanged; profiles requiring Poke fail readiness closed instead of silently losing Guest/proactive behavior.
6. After CP4 deletion, rollback requires reverting the deletion commit before disabling Poke; a config-only rollback is no longer sufficient because the core policy leaves are intentionally gone.

## 4. Checkpoints — final disposition

| Checkpoint | Result |
|---|---|
| 0. Plan | Approved after adversarial review corrected seven blockers. |
| 1. Portable plugin libraries | Complete; plugin code imported portably and core focused tests/carry gates passed. |
| 2. Generic seams + dark validation | Complete; registration/readiness/final-dispatch/lifecycle seams landed and closure review approved after wiring repairs. |
| 3. Authoritative plugin activation | Complete; authoritative Poke became functional for all claimed domains and closure review approved after P0/P1 repair. |
| 4. Core deletion + final de-carry | **Complete**; targeted runtime leaves deleted from core, plugin migration landed, load-order defects fixed, safe restart/soak/tests/probe/cleanup verified. |

## 5. Checkpoint 4 final truth

Checkpoint 4 physically deleted the targeted core leaves: `gateway/contact_memory/`, `gateway/proactive_*`, `gateway/guest_access.py`, `gateway/conversation_texture_v2.py`, `tools/guest_workspace_tools.py`, superseded Poke-specific core tests, and Guest cron operator carry. Core retains only generic seams: extension registration/invocation/readiness, exactly-one ownership, final-dispatch auth, authenticated existing-DM action, and initiated-session/storage primitives.

The first CP4 attempt found real blockers and did not delete through them: no plugin owner for the turn lane, executable `request_tools` were being discarded, and `guest_fs` had no replacement. Those blockers were fixed before deletion and are now regression-covered.

The final review then found two reachable P1s: internal maintenance cron output could reach BlueBubbles when `delivery_profile` was absent, and one migrated test still imported a deleted core benchmark module. The landed closure added a provider-neutral `PlatformEntry.cron_delivery_validator_fn` seam, made BlueBubbles own the internal-job classifier, and repointed the benchmark test to plugin-owned code.

Two deployment defects were fixed after landing:

- plugin `46b7e236e6` fixed Poke profile-home resolution under gateway multiplex;
- plugin `e3103ad2b6e` fixed root BlueBubbles load order by removing the module-load dependency on separately scoped bare `poke` packages.

## 6. Final evidence

- Live gateway PID/start time after the post-`e3103ad` restart: `15497`, `Sat Aug 29 10:37:31 2026` EDT.
- Health: `http://127.0.0.1:8787/health` -> `200`; `http://100.69.228.58:8642/health` -> `200`.
- Fresh startup logs show BlueBubbles connected/registered without `No module named 'poke'`; no startup `ERROR`, `Traceback`, `ownership_conflict`, `missing_capability`, or `unready` appeared in the fresh window.
- Guest and Poke each activated authoritative, each started exactly one proactive watcher generation, duplicate lifecycle starts were skipped, and all seven ownership domains resolved to `extension:poke`.

Focused test evidence at closure:

- Core: `tests/gateway/test_poke_authoritative_activation.py tests/cron/test_platform_delivery_validation.py` -> **30 passed**.
- Plugin clean `e3103ad` tree with real core pre-import: `tests/plugins/test_bluebubbles_plugin.py tests/poke_plugin/test_authoritative_activation.py tests/poke_plugin/test_proactive_rollout_cron.py` -> **175 passed, 1 warning**.
- Prior reviewed landing evidence remains: **108 BlueBubbles**, **67 activation/cron**, **668 plugin**, **177 core + 2 skipped**, **51 Poke policy/import**, fixed 8-shard comparison introduced **0** failed node IDs.
- `git diff --check` passed in both repositories.

No-send validator probe:

- loaded/registered BlueBubbles in-process with real `hermes_cli` imported first and no `poke` module preloaded;
- confirmed callable `cron_delivery_validator_fn` registration;
- rejected an internal maintenance job targeting BlueBubbles;
- allowed an ordinary reminder targeting BlueBubbles;
- invoked no send path and mutated no profile database.

## 7. Residual owner matrix

| Surface | Final owner |
|---|---|
| Guest route/admission and identity/context | Poke plugin through `routing` / `admission_policy`; core validates typed route directives. |
| Final Guest tool decision | Poke plugin through `turn_policy`; core enforces at final dispatch. |
| Contact-memory ingress/extraction | Poke plugin through `ingress` and `extraction`. |
| Lane A recall, interest digest, Lane B retrieval | Poke plugin through `turn_policy` augmentation. |
| Proactive claims / child creation / delivery | Poke plugin through `initiated_claims`, `child_creation`, and `delivery`; one watcher per generation. |
| Delivery ledger + initiated-session persistence | Generic core infrastructure consumed through bounded contracts. |
| BlueBubbles internal cron delivery validation | BlueBubbles plugin via provider-neutral `cron_delivery_validator_fn`. |

## 8. Edge-case matrix

| Edge | Final behavior |
|---|---|
| Plugin missing on Poke/Guest | readiness fails closed; no Guest authorization or proactive send. |
| Plugin missing on ordinary profile | ordinary Hermes behavior unchanged. |
| Plugin callback throws during route/policy | deny/drop safely and surface health/readiness failure. |
| Optional recall/extraction throws | reply continues; error recorded; no corrupt partial state. |
| Restart during proactive claim | durable claim/ledger permits safe recovery without duplicate send. |
| Delivery result unknown | no blind retry; preserve uncertain state for operator review. |
| Multiplexed root process | transport home remains immutable; extension/config/state resolve after validated admission into the routed profile. |
| Plugin reload while tasks active | old generation cancels and cannot clear/overwrite newer registration. |
| Conversation texture + proactive block | exactly one texture compilation; no duplicate markers. |
| Existing durable databases | adopted in place with integrity/schema/claim checks; no physical migration. |
| Dirty live plugin checkout | unrelated `bluebubbles/adapter.py` attachment-target edit remains uncommitted and preserved exactly. |

## 9. Documentation and tracking closure

The tracker is the authoritative closure record. Historical checkpoint notes are preserved there only where they remain useful evidence; obsolete checkpoint-gate language has been replaced with final truth.

`hey.md` no longer carries the Poke de-carry coordination line. Migration worktrees and migration/landing branches are removed only after clean-worktree and `git cherry` equivalence checks; live branches are never deletion targets.

## 10. Rollback

After CP4, rollback requires reverting the core deletion range before disabling the Poke plugin. Do not roll back by copying files between live checkouts, resetting dirty worktrees, or starting a second gateway.
