# Poke Plugin De-Carry Tracker

**Program:** Move Poke/Guest/contact-memory/proactive policy from Hermes core to the user-plugin repository.
**Plan:** `docs/plans/poke-plugin-decarry-20260827.md`
**Started:** 2026-08-27
**Closed:** 2026-08-29

## Final state

- Phase: **COMPLETE**. Checkpoint 4 is landed, live-restarted, soaked, verified, and cleanup is complete.
- Closure review: **CLOSURE_APPROVED**. The independent narrow review before landing found no P0/P1 after the BlueBubbles load-order repair.
- Live runtime changed: yes — gateway was safely restarted after plugin `e3103ad` and is healthy. No smoke message was sent and no profile database was mutated by verification.
- Residual core carry: **generic seams only** — conversation-extension registration/invocation/readiness, exactly-one ownership resolution, final-dispatch authorization, authenticated existing-DM platform action, and initiated-session/storage primitives. There are no targeted Poke/Guest/contact/proactive runtime leaves in core.

## Landed tips

| Repository | Live branch | Tip / required commits |
|---|---|---|
| Core | `local/studio-slim` | live tip at closure start `6eb1f58d7b`; Poke deletion equivalence includes `0a8c7d277be5`; docs closure commit follows this tracker update |
| User plugins | `main` | `e3103ad2b6e` (`fix(bluebubbles): remove Poke plugin load-order dependency`), with `46b7e236e6` (`Fix Poke profile home resolution under gateway multiplex`) and `a0f116c331` present |

## Checkpoints

| Checkpoint | Core commit | Plugin commit | Tests | Review | State |
|---|---|---|---|---|---|
| 0. Plan | `e063360820`, `a0e701869d`, `906aca5b98` | n/a | evidence audit + diff checks | approved | complete |
| 1. Portable plugin libraries | `9182fb07f6`, `f30eafc74f` | `66219a6`, `d27abc6` | 411 plugin + 188 core focused; carry gates pass | closure approved | complete |
| 2. Generic seams + dark parity | `23de9414be`, `50df8641f9`, `dc5d21bc58` | `96026ea` | 438 plugin + 293 closure/integration + 206 focused core; carry gates pass; contact_memory 47 baseline-identical BlueBubbles failures classified | closure approved | complete |
| 3. Authoritative activation, legacy fallback retained | `f16961a5ff`, `a02e1325e7`, `81c590e99d` | `8d36131`, `fea92e8` | 518 plugin; 233 focused core; 234 Guest/proactive/contact passes; carry gates pass | closure approved after P0/P1 repair | complete |
| 4. Core deletion and final de-carry | `e5fdf94cf7`, `7bc6a50aa7`, `35798e8081`, `28e12493ca`, landed equivalence through `0a8c7d277b` | `cf8af2d`, `e4853a5`, `ccd99d26`, `c216833b41`, `a0f116c`, `46b7e236`, `e3103ad` | final focused rerun: 30 core + 175 plugin passed; prior reviewed closure: 108 BlueBubbles, 67 activation/cron, 668 plugin, 177 core + 2 skipped, 51 Poke policy/import | **CLOSURE_APPROVED** | **complete** |

## Final closure evidence — 2026-08-29

### Runtime restart, health, and startup log

- Fresh gateway PID/start time: `15497`, `Sat Aug 29 10:37:31 2026` EDT, launched from `/Users/Kosta/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main gateway run --replace`.
- Health checks after restart: `http://127.0.0.1:8787/health` -> `200`; `http://100.69.228.58:8642/health` -> `200`.
- Fresh startup lines show BlueBubbles loaded/registered: `Connecting to bluebubbles...`, `hermes_plugins.bluebubbles.adapter: [bluebubbles] connected`, webhook listening/registered, and `✓ bluebubbles connected`.
- Fresh-startup scan from `2026-08-29 10:37:36` through bounded soak found no `Failed to load plugin 'bluebubbles'`, no `No module named 'poke'`, and no startup `ERROR`, `Traceback`, `ownership_conflict`, `missing_capability`, or `unready`.

### Authoritative Guest/Poke activation and lifecycle

- Guest activated authoritative: `Poke activating authoritative` with `compatible`, `settings_coherent`, `enabled`, `required`, and `healthy` all `True`; `preflight_ok=True`.
- Poke activated authoritative with the same all-true checks and `preflight_ok=True`.
- Exactly one proactive watcher generation per profile started: guest generation `1`; poke generation `2`.
- Duplicate lifecycle starts were skipped: `Poke extension start already applied for guest (generation 1); skipping duplicate` and the same for poke generation `2`.

### Ownership domains

Both `guest` and `poke` resolved all seven domains to `extension:poke`:

- `routing`, `ingress`, `extraction`, `turn_policy`;
- `initiated_claims`, `child_creation`, `delivery`.

Fresh log evidence records `Conversation ownership for profile guest` with every domain owner `extension`, `extension_id='poke'`, generation `1`, and `Conversation ownership for profile poke` with every domain owner `extension`, `extension_id='poke'`, generation `2`.

### Zero-outbound validator probe

In-process no-send probe against the current core/plugin imported real `hermes_cli`, loaded/registerered BlueBubbles in isolation with no `poke` module preloaded, and confirmed:

- registration includes callable `cron_delivery_validator_fn`;
- internal maintenance/watchdog/bootstrap/dry-run/probe delivery targeting BlueBubbles is rejected;
- an ordinary reminder targeting BlueBubbles is allowed;
- no outbound send was invoked and no profile database mutation occurred.

Probe output: `{"bluebubbles_registered":"bluebubbles","validator_present":true,"poke_preloaded_before":[],"poke_modules_after":[],"internal_maintenance_to_bluebubbles":"rejected","ordinary_reminder_to_bluebubbles":"allowed","outbound_send_invoked":false,"profile_db_mutation":false}`.

### Focused tests and static checks

- Core clean interpreter: `python -m pytest tests/gateway/test_poke_authoritative_activation.py tests/cron/test_platform_delivery_validation.py -q` -> **30 passed**.
- Plugin clean interpreter with real core pre-import, run from a clean detached plugin worktree at `e3103ad`: `tests/plugins/test_bluebubbles_plugin.py`, `tests/poke_plugin/test_authoritative_activation.py`, `tests/poke_plugin/test_proactive_rollout_cron.py` -> **175 passed, 1 warning**.
- Live plugin checkout intentionally still has the unrelated preserved `bluebubbles/adapter.py` attachment-target edit; running the BlueBubbles file directly in that dirty checkout produces the expected unrelated one-test mismatch against the old attachment-target assertion, so the final focused count above is from the clean `e3103ad` tree.
- `git diff --check` passed in both core and plugins.

### Bounded soak outcome

- Bounded post-restart soak covered the startup window through activation and readiness; health stayed `200/200` and the gateway remained on fresh PID `15497`.
- No post-restart Poke import/load-order warning appeared; the old `08:29/08:30` `No module named 'poke'` lines are pre-`e3103ad` and were not counted as fresh evidence.
- Startup warnings observed were unrelated expected platform warnings (Telegram fallback discovery and API-server network-accessible warning), not Poke de-carry readiness or ownership failures.

## Defects found and fixed during landing

- Initial profile-home/lifecycle deployment defect: the gateway multiplex activation resolved the Poke profile home incorrectly, so required-extension ownership could be evaluated against the wrong home. Fixed in plugin `46b7e236e6` by resolving Poke profile home under gateway multiplex correctly.
- Root BlueBubbles load-order defect: BlueBubbles had a module-load dependency on separately scoped bare `poke` packages; live startup produced `Failed to load plugin 'bluebubbles': No module named 'poke'`. Fixed in plugin `e3103ad2b6e` by removing that load-order dependency.
- The earlier CP4 review P1s were closed before landing: the provider-neutral `cron_delivery_validator_fn` seam rejects internal operational jobs before live or standalone sends, and migrated benchmark tests import plugin-owned operations.

## Historical CP4 comparison retained

The fixed 8-shard comparison used the same 713 surviving files on pinned baseline `cd8547ee7d8de3017b46fab4cdd44e17381100be` and deletion core `28e12493cac5d53f63aa611dfa8a295598ef638a`:

| | pinned baseline | deletion core |
|---|---:|---:|
| Fixed files | 713 | 713 |
| Shard return codes | `1,1,1,1,1,0,1,1` | `1,1,1,1,0,0,1,0` |
| Every shard rc < 2 | yes | yes |
| Passed / failed / skipped / xfailed | 6725 / 31 / 32 / 1 | 6723 / 20 / 32 / 1 |
| Exact failed IDs introduced | n/a | **0** |

All 38 deleted baseline test files were mapped to migrated plugin or surviving generic-core coverage in `/tmp/cp4/deleted_baseline_test_coverage_map.json` (`all_entries_mapped: true`).

## Cleanup

- Removed with `git worktree remove` after clean-status checks: core `poke-plugin-decarry`, `poke-plugin-decarry-landing`, `cp4-baseline`; plugin `poke-plugin-decarry`, `poke-plugin-decarry-landing`.
- Deleted after `git cherry` proved all commits equivalent/already live: core `migration/poke-plugin-decarry-20260827`, `landing/poke-plugin-decarry-20260828`; plugin branches with the same names.
- Live branches were not deleted: core `local/studio-slim`, plugin `main`.
