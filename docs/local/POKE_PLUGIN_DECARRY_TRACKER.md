# Poke Plugin De-Carry Tracker

**Program:** Move Poke/Guest/contact-memory/proactive policy from Hermes core to the user-plugin repository.  
**Plan:** `docs/plans/poke-plugin-decarry-20260827.md`  
**Started:** 2026-08-27

## Current state

- Phase: Checkpoint 2 approved and complete; Checkpoint 3 implementation starting.
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
| 1. Portable plugin libraries | `9182fb07f6`, `f30eafc74f` | `66219a6`, `d27abc6` | 411 plugin + 188 core focused; carry gates pass | closure approved | complete |
| 2. Generic seams + dark parity | `23de9414be`, `50df8641f9`, `dc5d21bc58` | `96026ea` | 438 plugin + 293 closure/integration + 206 focused core; carry gates pass; contact_memory 47 pre-existing BlueBubbles failures (baseline-identical, classified) | closure approved | complete |
| 3. Authoritative activation, legacy fallback retained | pending | pending | pending | pending | not started |
| 4. Core deletion and final de-carry | pending | pending | pending | pending | not started |

## Evidence log

### 2026-08-28 — Checkpoint 2 closure approved

- Final narrow review verdict: `CHECKPOINT_APPROVED`; all original P0/P1 findings are closed.
- Reviewer reran the 37-test production-wiring suite and verified canonical home-scoped readiness at both admission gates, including the named single-profile case.
- Report: `/Users/Kosta/.hermes/profiles/coding/cache/delegation/subagent-summary-0-20260828_162727_728712.txt`.

### 2026-08-28 — Checkpoint 2 closure review residual fixed

- Narrow closure review verified nine of ten original findings closed, then rejected on one residual P1: a named single-profile gateway stored readiness by profile name while an unstamped `SessionSource.profile=None` looked up `default`, allowing early control paths to bypass an unready `coding` profile.
- Readiness is now keyed exclusively by canonical `hermes_home_key(profile_home)`, matching extension registration and request-policy scope. Both early admission and the deep defence-in-depth gate resolve the source to that same home scope.
- Added the exact regression shape (`active profile=coding`, `source.profile=None`, unsatisfied requirement); the full closure set passes 293 tests and all carry gates.
- Review record: `/Users/Kosta/.hermes/profiles/coding/cache/delegation/subagent-summary-0-20260828_161805_223252.txt`.

### 2026-08-28 — Checkpoint 2 first review rejected, P0/P1 closure implemented

- Independent review verdict: `CHECKPOINT_REJECTED`. Full report:
  `/Users/Kosta/.hermes/profiles/coding/cache/delegation/subagent-summary-0-20260828_152935_283754.txt`.
- The reviewer confirmed the contracts, registry generation safety, facade
  boundedness, DM anti-duplicate bias, and dark-Poke capability restriction all
  hold. The rejection was about **wiring**: several required fire sites existed
  as tested modules with no production caller, and one guard was dead code.

**P0 findings, all closed:**

- **P0-1 — ambiguous tool-authorization owner escaped as an unhandled
  exception.** `turn_policy_scope` was a `@contextmanager`, so its body — and
  therefore the ambiguity check — only ran at `__enter__`. `run.py` guarded the
  *call*, making its `except` unreachable and turning an ambiguous authorizer
  into a per-message crash instead of a refusal. Fixed by making
  `turn_policy_scope` a plain function that resolves the owner eagerly at call
  time and returns an already-bound inner context manager. The agent turn moved
  into `GatewayRunner._run_agent_turn_with_policy`, so the refusal is reachable
  through the real wiring and is now tested there (agent never invoked, clean
  `None` returned).
- **P0-2 — `notify_gateway_start` / `notify_gateway_stop` had no callers.**
  Added `fire_gateway_start` / `fire_gateway_stop` to the runtime module and
  wired them: start fires per served profile during eager activation (before the
  gateway is marked running); stop fires in `GatewayRunner.stop()` alongside
  generation-scoped lifecycle-task cancellation. Previously a gateway shutdown
  left extension watchers running, because `on_stop` only fired on plugin
  *unload*.
- **P0-3 — no eager startup enumeration/activation; readiness was soft.** Added
  `_activate_conversation_extensions_for_served_profiles()`, which runs after
  plugin discovery and before adapters serve. It enumerates every served
  profile, fires the start site, and records a hard per-profile ready/unready
  verdict. Requirement evaluation now reads the profile's config from its
  **home** (`_load_gateway_config_from_home`) rather than re-resolving by name.
  The readiness probe reports `unready` — not `degraded` — for an unsatisfied
  hard requirement, and `collect_runtime_readiness` ranks `unready` above
  `degraded` so it cannot be masked. An unreadable config stays `degraded`: we
  cannot establish that a requirement even exists, and a YAML syntax error must
  not take an ordinary no-requirement gateway out of service.

**P1 findings, all closed:**

- **P1-1** — turn augmentation now has a production call site:
  `_collect_extension_turn_context()` is called in the real turn path and its
  output rides the API-only current-user-message lane (never the cached system
  prefix), so per-conversation prompt caching stays byte-stable. Fails open.
- **P1-2** — `create_initiated_child`, `inject_turn`, and
  `send_authenticated_existing_dm` are wired into `GatewayHostOperations`.
  Initiated children resolve an *existing* session and refuse otherwise;
  injection reuses the authorization-checked plugin injection path with
  `allow_gateway_control` off; the DM op resolves the transport itself and
  verifies an existing authorized DM. The facade still exposes no runner,
  store, client, adapter, or credential, and undeclared capabilities still
  deny — both re-asserted in tests. No-create/no-fallback preserved.
- **P1-3** — post-turn observation reports the validated
  `decision.runtime_profile` via `_extension_runtime_profile()`, falling back to
  the transport profile only when no decision exists.
- **P1-4** — the hard admission gate moved above the ~16 early returns in
  `_handle_message`, so slash-command dispatch, pause handling, confirm
  resolution, and busy-slash dispatch are all covered for a profile with an
  unsatisfied requirement. It is a dict lookup on the startup verdict, not a
  config read.
- **P1-5** — `_served_profile_names()` now unions the config-declared set from
  `_multiplex_profile_homes` with live adapter profiles, so a legitimate route
  is no longer rejected as `route_not_served` because that profile's adapter
  failed to connect.
- **P1-6** — `permitted_conversation_routes` is a real `GatewayConfig` field
  with a fail-closed normalizer, top-level/nested parity, `to_dict` round-trip,
  and tests that exercise both the permitted and refused branches.
- **P1-7** — evidence corrected; see the verification classification below.

**Verification (actually run, honest classification):**

- Conversation-extension + parity + readiness suites: **214 passed, 0 failed**
  (`test_conversation_extensions`, `..._dispatch`, `..._lifecycle`,
  `..._registration`, the new `..._run_wiring`, `test_authenticated_dm`,
  `test_poke_plugin_parity`, `test_readiness`).
- New `tests/gateway/test_conversation_extension_run_wiring.py`: **36 tests**
  driving the real `GatewayRunner` methods, written failing-first. All 36 failed
  before the fixes.
- Standalone plugin suite: **438 passed**.
- Focused core regression set (config, api_server, profile resolution,
  multiplex, startup, ingress dispatch): **206 passed, 0 failed**.
- Carry gates: local carry contract **PASS** (149 connected surfaces, 38
  documented features); carry registry **PASS** (368/368 managed paths).
- **`tests/gateway/contact_memory` is NOT green and this checkpoint does not
  claim it is.** It reports **47 failures / 292 passed**. These are
  **pre-existing baseline failures in the BlueBubbles platform override**
  (`BlueBubblesAdapter` has no `_normalize_ingress_record`), not regressions.
  Verified by diffing the failing-test *set*, not just the count, against base
  `e94c5c899c` in a clean detached worktree: the two sets are **identical** —
  zero new failures, zero fixed. The previous "full existing Guest/contact/
  proactive focused suite green" claim was false and has been withdrawn.
- **Whole-suite regression check.** `tests/gateway` (the full directory) was run
  on both this branch and a clean detached worktree at base `e94c5c899c`. Both
  report **63 failures**, and the failing-test *sets* are **identical** — zero
  new failures, zero fixed. Pre-existing failure families outside
  contact_memory: `test_usage_command` (3), `test_status_owner_guard` (3),
  `test_cron_active_work_drain` (3), `test_scale_to_zero` (2), and one each in
  `test_telegram_thread_fallback`, `test_systemd_notify`,
  `test_session_model_reset`, `test_bluebubbles`, `test_background_command`,
  `contact_memory/test_link_research_worker`. None are attributable to this
  range and none are claimed as green.
- One genuine regression was introduced during this closure and fixed:
  extracting the agent turn into `_run_agent_turn_with_policy` broke the
  `canonical_event_ids` AST contract test, which asserted exactly one
  `_handle_message_with_agent` call inside `_handle_message`. The test was
  updated to span the extracted pair and still pins the invariant (exactly one
  dispatch across the seam, no stray second dispatch, ids reach the handler).

### 2026-08-28 — Checkpoint 2 implementation

- Added a generic, profile-scoped conversation-extension runtime with immutable contracts, core-owned requirements/readiness, two-phase generation publication, stale-safe unload, and bounded host capabilities.
- Wired authenticated ingress/admission, whole-turn request-policy scope, final tool authorization, and post-turn observation into production gateway dispatch. Direct, deferred bridge, MCP, and inline executor paths are covered.
- Added authenticated-existing-DM tri-state delivery (`sent`, `definitive_failure`, `unknown`) with no chat creation and no fallback transport.
- Poke registers only dark capabilities: tool decision observation, ingress/post-turn observation, and health. It cannot route, start lifecycle tasks, send, create initiated children, or write durable state.
- Frozen cross-repository corpus: 54/54 copied-vs-legacy Guest route/tool and proactive policy comparisons pass with no skips. Standalone plugin suite: 438 passed. CP2/core suite: 175 passed.
- Atomic replacement race tests prove the outgoing generation remains visible until replacement start succeeds; failed replacement start preserves the prior generation.
- **Correction (2026-08-28):** this entry originally claimed the full existing Guest/contact/proactive focused suite was green. That was false — `tests/gateway/contact_memory` had 47 pre-existing BlueBubbles-override failures at the time and still does. See the closure entry above for the verified baseline-identical classification. The wiring gaps this entry implied were delivered are itemized as P0-1..P0-3 and P1-1..P1-7 there.

## Residual owner matrix after Checkpoint 2

| Surface | Current authority | Plugin state | Next checkpoint |
|---|---|---|---|
| Guest route/admission and identity/context | Core legacy owner | copied; dark comparison only | Checkpoint 3 activation with fallback |
| Final Guest tool decision | Core legacy guard remains authoritative | dark observer; generic final-dispatch seam is live | Checkpoint 3 activation with fallback |
| Contact-memory ingress and durable stores | Core and BlueBubbles platform override | portable libraries copied; data stays in place | Checkpoint 3 adapter/facade activation |
| Proactive watcher, scheduler, initiated children, delivery | Core legacy owner | copied libraries; no dark runtime capability | Checkpoint 3 activation; Checkpoint 4 deletion |
| Delivery ledger and initiated-session persistence | Generic core infrastructure | consumed only through bounded contracts | Remains core |
| Conversation-texture `pre_llm_call` attachment | Plugin authoritative | core engine still consumed by proactive leaves | Engine deletion waits for remaining consumers |

### 2026-08-27 — Checkpoint 1 closure approved

- Narrow independent Claude Opus closure review: `CHECKPOINT_APPROVED`.
- Reviewer independently loaded the root plus all 46 submodules through the production `hermes_plugins.poke` loader shape with bare `poke` unavailable.
- Reviewer executed all six operator files directly from `/tmp` with isolated import behavior and confirmed 6/6 exit successfully for `--help`.
- Reviewer confirmed all six original P0/P1 findings closed, no forbidden core imports, no hooks, no registration side effects, and no live state/config changes.
- Full review record: `/Users/Kosta/.hermes/profiles/coding/cache/delegation/subagent-summary-0-20260827_234515_638898.txt`.

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

- Resolved: carry manifest now separates retired texture attachment, generic extension/runtime infrastructure, and temporary policy leaves.
- Resolved: plugin restart documentation now matches the detached safe-restart policy; VibeProxy restarts still require explicit approval.
- Existing historical Poke plans do not distinguish current core owner, plugin owner, and retired surfaces.

## Activation guard

No plugin configuration, live plugin checkout, profile config, gateway process, contact database, or outbound transport may be changed before the integrated checkpoint is approved. Live activation must use the detached safe restart route and must not message an uninvolved contact.
