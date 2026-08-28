# Poke Plugin De-Carry Tracker

**Program:** Move Poke/Guest/contact-memory/proactive policy from Hermes core to the user-plugin repository.  
**Plan:** `docs/plans/poke-plugin-decarry-20260827.md`  
**Started:** 2026-08-27

## Current state

- Phase: Checkpoint 1 — first review rejected; P0/P1 fixes complete; closure review pending.
- Live runtime changed: no.
- Core worktree: `/Users/Kosta/LocalDev/.studio-only/hermes-worktrees/poke-plugin-decarry`.
- Plugin worktree: `/Users/Kosta/LocalDev/.studio-only/hermes-kosta-plugin-worktrees/poke-plugin-decarry`.
- Active writer: parent Coding agent only; reconnaissance agents were read-only.

## Baseline

| Metric | Value |
|---|---:|
| Core changed files vs upstream | 670 |
| Files changed by both local and upstream | 91 |
| Current dry-run merge conflicts | 13 |
| Carry-managed paths | 364 |
| Named-feature mapped paths | 150 |
| Exempted/grandfathered paths | 214 |
| Thinning hotspots | 401 |
| Poke/Guest/contact/proactive leaf LOC | ~19,873 |
| Local changes in `gateway/run.py` | ~3,102 lines |

## Checkpoints

| Checkpoint | Core commit | Plugin commit | Tests | Independent review | State |
|---|---|---|---|---|---|
| 0. Plan | `e063360820`, `a0e701869d`, `906aca5b98` | n/a | evidence audit + diff checks | approved | complete |
| 1. Portable plugin libraries | pending | `66219a6`, `d27abc6` | 411 plugin + 188 core focused; carry gates pass | first review rejected; closure pending | review pending |
| 2. Generic seams + dark parity | pending | pending | pending | pending | not started |
| 3. Authoritative activation, legacy fallback retained | pending | pending | pending | pending | not started |
| 4. Core deletion and final de-carry | pending | pending | pending | pending | not started |

## Evidence log

### 2026-08-27 — Checkpoint 1 first review and P0/P1 closure

- Independent Claude Opus review: `CHECKPOINT_REJECTED`.
- P0 fixed: all internal imports are package-relative; a clean subprocess now imports the root plus all 46 submodules as `hermes_plugins.poke` with the repository root absent from `sys.path`.
- P0 fixed: all six operator modules support direct `--help` execution; tests no longer rely on pytest making bare `poke` importable.
- P1 fixed: Guest policy defaults to multiplex-safe env isolation, while request-scoped Guest context remains authoritative; the enforcement path is covered.
- P1 fixed: portability rejects `agent`, `cron`, `gateway`, `hermes_cli`, `hermes_state`, `model_tools`, `poke`, `scripts`, and `tools`; diagnostics and profile operations use explicit injected services/roots.
- P1 fixed: missing model/cron host services return deterministic unavailable states and are tested rather than silently importing core.
- P1 fixed: carry registry split into retired texture attachment, generic runtime infrastructure, temporary policy leaves, and the existing future-facing pre-LLM seam. Contact-memory and all proactive leaves are explicitly owned.
- Closure evidence: 411 plugin tests, 188 core focused tests, compile, direct-CLI checks, diff checks, carry 364/364, doctor, and 145-surface contract all pass.

### 2026-08-27 — Checkpoint 1 implementation

- Plugin commit: `66219a6` (`feat: stage inert Poke policy libraries in plugin`).
- Added an inert `poke` plugin with no hooks and a no-op `register`; no live profile/config/runtime changed.
- Extracted neutral texture, Guest policy, contact-memory, proactive, and offline operation libraries. Host adapters remain in core; `lane_b` was intentionally not copied.
- Portability gate proves zero `agent`, `gateway`, or `tools` imports under `poke/`; compile gate passes.
- Verification: 408 plugin tests passed; 188 existing core texture/Guest/proactive tests passed; carry registry 364/364, carry doctor, and 144-surface carry contract passed.
- Durable databases and profile state were not copied, opened, or migrated.

### 2026-08-27 — plan approved

- Final closure verdict: `PLAN_APPROVED`; no P0/P1 findings remain.
- Closure review verified the checkpoint order, immutable final-dispatch policy token, thread/executor propagation, per-path bypass gates, eager profile activation, and reserve-before-send idempotency contract.
- Review transcript: `/Users/Kosta/.hermes/profiles/coding/cache/delegation/live/deleg_a972c741/task-0.log`.

### 2026-08-27 — adversarial plan rejection

- Verdict: `PLAN_REJECTED`; full report at `/Users/Kosta/.hermes/profiles/coding/cache/delegation/subagent-summary-0-20260827_224604_319414.txt`.
- Corrected seven blockers: core-owned required-extension gate; immutable transport-home authorization sequence; final-dispatch Guest policy; side-effect-free dark mode; plugin-owned ledger with narrow platform action; activation before deletion; in-place durable data continuity; generation-safe per-profile lifecycle.
- Resolved extension shape, delivery surface, config ownership, and standalone conversation-texture ownership.
- No implementation or live runtime changes occurred before the plan correction.

### 2026-08-27 — baseline audit

- Confirmed existing plugin surfaces: platform override registry; hook registration; `pre_gateway_dispatch`; `pre_llm_call` user/system context; request-scoped tools; host LLM facade; tracked background tasks; slash/CLI commands; profile-scoped plugin state; message injection.
- Confirmed missing clean surfaces: gateway runtime lifecycle, structured source routing mutation, fail-closed execution policy, trusted post-turn completion, initiated-assistant/prepared-delivery facade.
- Confirmed conversation-texture plugin still imports the 603-line carried core engine.
- Confirmed `gateway/run.py` remains the dominant coupling surface.
- Created isolated core and plugin worktrees; live Poke/Guest code and config were not modified.
- Independent reconnaissance agents were read-only. One verified 60 synchronous focused tests; async tests were not accepted as evidence because that scout's venv lacked `pytest-asyncio`.

## Documentation debt discovered

- `scripts/local_carry_manifest.yaml` still groups retired texture attachment with the remaining Poke/Guest stack.
- Plugin `docs/plugin-hygiene.md` says restarts require fresh approval; current repository policy pre-authorizes the detached safe-restart mechanism.
- Existing historical Poke plans do not distinguish current core owner, plugin owner, and retired surfaces.

## Activation guard

No plugin configuration, live plugin checkout, profile config, gateway process, contact database, or outbound transport may be changed before the integrated checkpoint is approved. Live activation must use the detached safe restart route and must not message an uninvolved contact.
