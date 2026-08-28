# Poke Plugin De-Carry Tracker

**Program:** Move Poke/Guest/contact-memory/proactive policy from Hermes core to the user-plugin repository.  
**Plan:** `docs/plans/poke-plugin-decarry-20260827.md`  
**Started:** 2026-08-27

## Current state

- Phase: Checkpoint 3 implemented in isolated worktrees; independent review pending.
- Live runtime changed: no. No live config, profile, database, gateway process, or transport was touched.
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
| 3. Authoritative activation, legacy fallback retained | `f16961a5ff` (code) + docs commit + Review-3 repair commit | `8d36131` + Review-3 repair commit | see "Review 3 repair" below | Review 3 **rejected**; repair complete, re-review pending | repaired, re-review pending |
| 4. Core deletion and final de-carry | pending | pending | pending | pending | not started |

## Evidence log

### 2026-08-28 — Review 3 repair (Checkpoint 3 rejected, then repaired)

Independent Review 3 **rejected** Checkpoint 3 with three P0s and one P1. All
four are closed. The repair did not narrow any claim or amend any acceptance
criterion — the criteria stand as written and the implementation was completed
to meet them.

**P0-1 — Rollback silently disabled the Guest tool policy (security bypass).**

The legacy guard stood down on *token presence*. `turn_policy_scope` binds a
token whenever any registered bundle declares `tool_authorization`, and the
dark/rolled-back Poke bundle declared it while its `authorize_tool` returned an
unconditional allow. On the documented rollback path ("disable the setting +
safe restart") a guest therefore got unsandboxed `terminal` / `execute_code`.
The review's repro was reproduced here before fixing: the malicious
`terminal {"command": "cat ~/.hermes/.env", "workdir": "/"}` call returned the
real file contents through `handle_function_call`.

Two independent fixes, either of which closes the hole:

- `model_tools._legacy_tool_policy_owner_active` now keys off the **ownership
  verdict** (`is_extension_owned(scope, ROUTING)`), not token presence. Legacy,
  unowned, no-plan, and any lookup error all keep the legacy guard.
- The dark bundle no longer declares `tool_authorization` and supplies no
  `authorize_tool` callback, so it cannot bind a policy token or be asked the
  question. Shadow parity decisions are still computed via
  `observe_shadow_decision`, which answers no dispatch gate.

Evidence: `tests/gateway/test_tool_guard_rollback_bypass.py` (11 tests) drives
the shipped dark bundle through the production dispatcher on the direct,
deferred/bridge, inline-recursive, and executor/thread-hop paths with both
`terminal` and `execute_code`, and asserts a denial on each.

**P0-2 — Authoritative mode was a zero-owner outage.**

Activation resolved all six domains to `extension` and gated every legacy site
off while the plugin performed no equivalent work: `observe_ingress` and
`observe_turn_result` were counters and `on_start` spawned nothing. Contact
memory would stop recording and proactive delivery would stop, silently.

The plugin now implements every domain it claims, in `poke/owners.py`, through
bounded host operations only:

| Domain | Implementation | Durable effect |
|---|---|---|
| routing | `RoutingOwner` runs the real `classify_bluebubbles_route` | denies unapproved senders; returns principal/subject/context prefix/scope metadata |
| ingress | `IngressOwner` calls `persist_live_communication_ingress` | one committed `communication_event` row per batch; replays deduplicate |
| extraction | `ExtractionOwner.submit_extraction` | one extraction job per authenticated turn |
| texture | `ExtractionOwner.compile_texture` | exactly one compile per `(session, turn)` |
| claims/child/delivery | `ProactiveOwner` | one host-owned watcher per generation driving `claim_due` / `reserve_delivery` / `finish_delivery` |

Core changes that make this reachable: the extension seam was **relocated
before** the legacy BlueBubbles routing block (it previously ran after, so an
extension owner could not classify); `GatewayRouteContext` now carries the
adapter's frozen `ingress_records`; `GatewayRouteDirective` / `GatewayRouteDecision`
carry the identity classification; `_apply_extension_route_decision` applies the
validated directive; the legacy routing and ingress sites are gated on the
ownership verdict; and a `run_blocking` host op keeps plugin SQLite work off
the gateway loop.

Trust boundaries preserved: the transport profile/home never move, `principal`
is accepted only as `owner`/`guest`, only the two `_hermes_contact_*` metadata
keys are applied, and a `/command` is never prefixed.

Evidence: `tests/gateway/test_poke_functional_ownership.py` (27 tests) proves
**positively** — one persisted ingress write in a temp production-shaped
snapshot, replay dedupe, exactly one watcher (and its generation-scoped
cancellation), a real claim-path tick, one texture compile per turn, the
initiated-child path through the host op, the authenticated existing-DM
tri-state with `UNKNOWN` never retried, runtime guest routing/policy
establishment, and zero outbound transport across the whole sequence. A
dedicated `test_activation_is_not_a_zero_owner_state` asserts the durable-effect
counters are non-zero — the assertion whose absence let a zero-owner state pass.

**P0-3 — The read-only preflight was vacuous against the real schemas.**

Every table and path constant was fictional (`proactive_slot_claims`,
`proactive_sends`, `contacts`, `contact-memory/*.db`), so all six checks failed
open on real data — including the two duplicate-send gates. `row_counts`
recorded `{}`.

`poke/preflight.py` was rewritten against production:

| Check | Production shape |
|---|---|
| `active_claims` | `proactive_slot WHERE status='claimed' AND claim_token IS NOT NULL` |
| `terminal_ledger` | `proactive_delivery.state NOT IN (sent, suppressed, delivery_unknown, partial_delivery, failed)` |
| `ownership` | `proactive_transport_owner` lease + `proactive_contact_owner` conflicts + `proactive_global_circuit`, at the root-shared `proactive-contact-ownership.db` |
| contact memory | `contact-memory/contacts/<sha>.sqlite3` with `schema_meta`/`fact`/`communication_event`/`interest`, schema version validated |

A missing expected table is now a **failure**, not `ok=True`, and an empty
report is not `ok`. Connections remain `mode=ro` and refuse to create a missing
file.

Evidence: `tests/poke_plugin/test_preflight_production_schema.py` (38 tests).
Crucially, `test_fixture_schema_matches_real_profile` and
`test_contact_memory_fixture_matches_real_store` compare the fixtures against
the **real** `~/.hermes/profiles/guest` column-for-column (both executed, not
skipped), and `test_preflight_over_copied_live_snapshot_is_read_only` runs the
whole preflight over a copy of the live profile and asserts source and copy
hashes are unchanged. That breaks the self-referential trap the review
identified.

**P1-1 — `_legacy_owns` failed open to legacy on lookup error.**

`gateway/run.py` now returns `False` (refuse) on any lookup error whenever a
plan is installed, matching the `scope is None` branch and the module's
documented fail-closed contract. `True` remains only for an empty registry —
no plan anywhere, so nothing was activated and behavior is unchanged for CLI,
tests, and any process that never ran gateway activation.

**Tests updated rather than worked around.** Two pre-existing core tests and
several plugin tests encoded the rejected behaviors (token-presence stand-down,
"dark always allows", "start spawns no watcher", "route proposes no profile
change", the invented preflight schema). They were rewritten to the corrected
contract; none were deleted or skipped.

**Docs.** The invalid "two authoritative surfaces are intentionally quiescent"
note was **removed** from the plan, not amended — it described an outage as a
scope decision.

### 2026-08-28 — Checkpoint 3 implementation

**What changed, in one line each.**

Core gained a generic, provider-neutral single-owner selector
(`gateway/conversation_ownership.py`) and gated its four legacy owner sites on
it. The plugin gained an authoritative extension, a canonical settings resolver
with a read-only legacy adapter, and a strictly read-only durable preflight.
The complete legacy implementation was **not** removed — it is still the
default owner, so rollback is a configuration switch plus a safe restart.

**Core: the selector.**

- Six generic domains (`routing`, `ingress`, `extraction`, `proactive_claims`,
  `child_creation`, `delivery`), each mapped to the extension capability a
  claimant must declare. No `poke`, `guest`, contact, or product identifier
  appears in the module; a test asserts this against the file's source.
- Default is legacy for every domain, so an ordinary profile is unaffected and
  the current Poke/Guest behavior is unchanged until an operator opts in.
- Exactly one owner, or nobody. Zero claimants and two claimants both resolve
  to `UNOWNED`, as do an unhealthy owner, a raising health probe, an unknown
  owner value, and a typo'd domain key.
- **`UNOWNED` is not a fallback to legacy.** `_legacy_owns()` is true only for
  a *proven* legacy verdict, so an ambiguous plan refuses both owners. Treating
  "not extension-owned" as "run legacy" is the specific bug that produces two
  live owners, and therefore a duplicate ingress write or a duplicate send.
- Coupled domain groups (`ingress`+`extraction`;
  `proactive_claims`+`child_creation`+`delivery`) may not be split across
  owners. A split plan is a conflict, not a configuration.
- Ownership is resolved **once at startup** and installed in a process
  registry. The hot path is a dict read, and a mid-turn plugin unload cannot
  move ownership between two halves of the same turn.

**Core: production wiring (each has an AST test proving the call site exists).**

- `_handle_communication_ingress` consults the gate and delegates to an
  extracted `_classify_and_persist_legacy_ingress`; the legacy write is skipped
  when the domain is extension-owned or unowned.
- The post-turn extraction submit is gated on `_legacy_extraction_permitted`.
- `_proactive_scheduler_watcher` skips a profile unless the legacy owner holds
  all three coupled proactive domains.
- `_activate_conversation_extensions_for_profile` installs the plan before the
  profile serves traffic; a conflicted plan makes the profile **unready**
  (`ownership_conflict:…`) rather than starting with two owners.
- `model_tools.handle_function_call` runs the legacy Guest guard only when no
  request-policy token is bound. Under a bound token the extension owns the
  decision and the generic final-dispatch seam enforces it, so exactly one
  guard decides — verified by a test that asserts the legacy guard is never
  called while an owner denies.

**Plugin: activation contract.**

- Authoritative only when **enabled** (canonical setting or core ownership
  config), **required** (core-owned `required_conversation_extensions`),
  **healthy** (preflight passes), and **compatible** (API version + no settings
  conflict). Short of all four it registers the Checkpoint 2 dark bundle.
- `plugins.entries.poke.settings` is canonical; `agent.contact_memory` /
  `agent.proactive` are read-only fallbacks for one window. Identical values in
  both places are fine; **differing values fail readiness closed** rather than
  being merged or silently preferred. No config is ever written.
- Preflight opens SQLite `mode=ro` — a test asserts a write attempt raises —
  and checks integrity, schema, row counts, no in-flight claim, terminal
  ledger, and uncontested ownership. A test asserts every durable file is
  byte-identical after a full preflight, and that no database is created.

**Deliberately quiescent surfaces.** The authoritative bundle declares
`lifecycle` and `admission_policy` (that is what lets core name a single
owner) but `on_start` spawns no watcher and `authorize_route` proposes no
runtime-profile change. Starting a watcher beside the legacy one core still
contains is the duplicate-claim edge; route mutation has no accepted parity
evidence. `turn_policy` is deliberately **not** declared, so the standalone
conversation-texture plugin remains the single texture owner.

**Verification (actually run; classification is honest).**

- New core selector suites: `test_conversation_ownership.py` **33 passed**,
  `test_conversation_ownership_wiring.py` **24 passed**. Both written
  failing-first — the wiring suite failed 14/16 before implementation.
- Isolated activation harness `test_poke_authoritative_activation.py`:
  **25 passed**. Loads real core + real plugin in one process against
  temp-directory state; asserts single ownership, legacy retention, fail-closed
  conflicts (two claimants, split plan, unhealthy owner, in-flight claim,
  settings conflict), one watcher / one ingress writer / one texture owner,
  byte-identical durable files, **zero sends**, restart and reload stability,
  and rollback.
- CP2 + CP3 combined seam set (11 files): **297 passed, 0 failed**.
- Legacy owner suites (guest access, BlueBubbles guest policy, proactive
  scheduler/gate/status/transport/initiated-turn/watcher): **197 passed**.
- Focused core regression set (config, profile resolution/routing, multiplex
  lifecycle/authz/phase0, runner startup failures, pre-gateway dispatch, own
  policy startup gate): **122 passed**.
- Standalone plugin suite `tests/poke_plugin`: **478 passed** (438 at CP2 + 40
  new).
- Carry gates: registry validate **PASS** (368/368 managed paths, 39 features,
  52 legacy surfaces); doctor **PASS**; local carry contract **PASS** (153
  connected surfaces, 39 documented features). A real gate catch during this
  work — a primary-owner collision on `gateway/run.py` — was fixed by scoping
  the new feature's `owner_paths` to its own file.

**Not green, and not claimed to be.**

- **Whole-suite regression check by exact node-ID set.** `tests/gateway` was
  run bare on this branch and on a clean detached worktree at base
  `85a22d4bc2`. Branch: **70 failed / 7462 passed / 33 skipped / 1 xfailed**.
  Base: **70 failed / 7380 passed / 33 skipped / 1 xfailed**. The failing
  **node-ID sets are identical** — 70 shared, **zero branch-only, zero
  base-only** — independently reparsed by the parent agent. The +82 passing
  delta is exactly the tests this checkpoint adds (33 ownership + 24 wiring +
  25 activation harness).
  - Evidence files: `/tmp/cp3_gw_branch.txt`, `/tmp/cp3_gw_base.txt`.
  - **Caveat on evidence hygiene:** those two runs were captured through a
    wrapper ending in `echo`/`tail`, so the recorded shell exit status was `0`
    and does **not** represent pytest's status. The verdict above comes from
    the summary lines and the node-ID set diff, never from that exit code.
- Failing families on both sides: `contact_memory/test_live_communication_ingress`
  (46), `test_usage_command` (3), `test_status_owner_guard` (3),
  `test_discord_send` (3), `test_cron_active_work_drain` (3),
  `test_scale_to_zero` (2), and one each in `test_telegram_thread_fallback`,
  `test_telegram_polling_health_confirmation`, `test_teams_dotenv_isolation`,
  `test_systemd_notify`, `test_session_store_prune`, `test_session_model_reset`,
  `test_send_multiple_images`, `test_bluebubbles`, `test_background_command`,
  and `contact_memory/test_link_research_worker`.
- `test_usage_command` was re-run **bare** on both sides (no pipe, real exit
  code): 3 failed / 3 passed on branch *and* on base, same three node IDs. The
  assertions expect a usage-rendering format the current renderer no longer
  emits (`"30,000"`, `"API calls: 10"`, `"Context breakdown"`). This checkpoint
  touches no usage, rendering, or context-breakdown code.
- `tests/gateway/contact_memory` run in isolation: **47 failed / 292 passed**,
  also verified baseline-identical by failing-test set (zero new, zero fixed).
  These are the pre-existing BlueBubbles platform-override failures documented
  at Checkpoint 2. (The whole-directory run attributes 46 of them to
  `test_live_communication_ingress` plus one in `test_link_research_worker`.)
- `carry.py verify` reports 15 failing features (desktop ×12, runtime
  fallback-routing, update futureproof-system, webui session-defaults). The
  failing-feature set is **identical to base**; none are attributable to this
  range and none are claimed as green.

**Explicitly NOT claimed.** No live activation evidence exists. The gateway was
not restarted, no live config or profile was modified, no live database was
opened, and no bounded soak or naturally-occurring-traffic verification was
run. All evidence above is isolated in-process evidence against temp state.
The live safe restart and soak remain outstanding work for this checkpoint.

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

## Residual owner matrix after Checkpoint 3

Ownership below is what the *selector* assigns when a profile opts in. With no
configuration — every ordinary profile, and Poke/Guest today — every row reads
"Core legacy owner", unchanged.

| Surface | Authority when activated | Authority by default / after rollback | Next checkpoint |
|---|---|---|---|
| Guest route/admission | Plugin (`admission_policy`), admits without route mutation | Core legacy owner | CP4: route mutation after core deletion |
| Final Guest tool decision | Plugin, enforced through the generic final-dispatch seam; legacy guard stands down under a bound token | Core legacy guard | CP4: delete `enforce_guest_tool_call` |
| Contact-memory ingress | Plugin owns the domain; core's legacy writer is gated off | Core + BlueBubbles override | CP4: delete legacy writer |
| Post-turn extraction | Plugin (`post_turn_observer`); legacy submit gated off | Core legacy owner | CP4 |
| Proactive claims / child creation / delivery | Plugin owns the coupled group; legacy watcher skips the profile. **No plugin watcher starts in CP3.** | Core legacy watcher | CP4: plugin watcher activates as legacy is deleted |
| Delivery ledger + initiated-session persistence | Generic core infrastructure, consumed through bounded contracts | same | Remains core |
| Conversation-texture `pre_llm_call` | Standalone texture plugin (Poke declares no `turn_policy`) | same | Engine deletion waits for remaining core consumers |
| Durable databases | Adopted in place; read-only preflight only | same files, untouched | Physical relocation out of scope |

### Residual owner matrix after Checkpoint 2

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
