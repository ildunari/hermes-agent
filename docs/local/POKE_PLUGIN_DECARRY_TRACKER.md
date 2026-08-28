# Poke Plugin De-Carry Tracker

**Program:** Move Poke/Guest/contact-memory/proactive policy from Hermes core to the user-plugin repository.  
**Plan:** `docs/plans/poke-plugin-decarry-20260827.md`  
**Started:** 2026-08-27

## Current state

- Phase: plan correction after adversarial rejection; closure review pending.
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
| 0. Plan | pending | n/a | evidence audit | pending | in progress |
| 1. Portable plugin libraries | pending | pending | pending | pending | not started |
| 2. Generic seams + dark parity | pending | pending | pending | pending | not started |
| 3. Authoritative cutover + deletion | pending | pending | pending | pending | not started |
| 4. Integrated landing/activation | pending | pending | pending | pending | not started |

## Evidence log

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
