# 2026-07-23 smart-update overhaul — incident ledger and follow-ups

Every issue hit while rebuilding the update pipeline and running the first
service-routed update (runs 1–6, `~/.hermes/update-service/runs/`). Fixed
items name their commit; open items are the work queue.

## Fixed today (commits on local/studio-slim)

| # | Issue | Fix |
|---|---|---|
| 1 | Live checkout left mid-merge by session `ec8970978b22`; reviewer output ended the root turn | merge aborted, resolutions preserved via rerere; service is now the only merge path (`update-smart` 0.7.0) |
| 2 | `NEEDS_RESOLUTION` was terminal — no resume path for parked conflicts | parked/resume loop, `resume` verb (`888b963`) |
| 3 | `resume` crashed on the zero-byte released lease file (review P0) | tolerant `current_lease` + `lease_is_live` (`32ddd93`) |
| 4 | Abort of a parked run was a no-op that blocked future updates | pre-activation finalize-to-ABORTED (`32ddd93`) |
| 5 | Worker death mid-drain could double-enqueue the restart helper | persisted enqueue marker (`32ddd93`) |
| 6 | Binary / delete-modify conflicts silently auto-staged | `conflict_is_resolved` parks them (`32ddd93`) |
| 7 | Gateway listeners 8644/8647 never verified after restart | `LISTENER_ONLY_PORTS` (`32ddd93`) |
| 8 | Every Desktop-changing update notarized (plist injected `APPLE_NOTARY_PROFILE`) | env removed; release flows set it explicitly (`32ddd93`) |
| 9 | Unreachable travel MacBook failed the whole run | recorded deferral (`56534ed`) |
| 10 | Worktree symlinked stale node_modules; upstream's new `cross-env` broke all UI carry tests (run 1) | `npm ci` on manifest change (`8c13c89`) |
| 11 | Resume hit "phase regression: NEEDS_RESOLUTION -> PREFLIGHT" (run 4) | phase-preserving `record()` (`6b2a1aa`) |
| 12 | Silent 900s restart poll, no drain visibility (July 22 pattern) | `wait_for_surfaces` + `drain-audit.jsonl` + blocked-port naming (`888b963`) |
| 13 | Poisoned rerere resolution: sessions INSERT had 17 placeholders for 16 columns — 78 test failures (run 5) | rr-cache postimage repaired in place |
| 14 | Poisoned rerere resolution: tui provider tests took upstream's `anthropic` expectations over the local vibeproxy alias-routing carry (run 5) | rr-cache postimage repaired; local expectations restored |
| 15 | Carry revival heuristic hijacked upstream's window-metadata / "0 elements" results | narrowed to blank-payload-only (`c09e3cc`) |
| 16 | Upstream's linux-only gnome-shell test and <0.15s timing test fail on this Mac by construction | named conftest skips (`c09e3cc`, `f2e00b4`) |
| 17 | Local-only `browser-settings.tsx` depended on a locally-added export in upstream `primitives.tsx` | inlined; carry no longer touches the upstream file (`f2e00b4`) |
| 18 | Activation fast-forwarded the live checkout but left its node_modules/.venv on old lockfiles; deployed carry-verify failed and restarted services paired new code with the old venv (run 6) | live dependency refresh (npm ci + pinned uv sync) at activation (`cc33f4f`); live env synced manually this run |
| 19 | `gateway_state.json` recorded a dead pid (145); helper's PID cross-check refused the graceful gateway restart, stalling the update 20 min | manual SIGUSR1 restart healed the status file; writer bug + helper fallback remain open (follow-up 1) |
| 20 | Relaunched gateway raced its dying predecessor for 8642 (`Errno 48`), gave up permanently, served without its API platform | second graceful restart bound cleanly; bind-retry remains open (follow-up 2) |

## Fixed 2026-07-23 (second wave — speed + restart reliability)

| # | Issue | Fix |
|---|---|---|
| 21 | Carry-verify ran up to 24 cold serial subprocess groups (`-j 1` pytest, cold vitest/electron per feature) — ~9m | batched union runs, at most three commands, per-feature fallback for attribution (`carry.py`) |
| 22 | Deployed carry-verify re-ran all behavior tests against the live service-occupied checkout (~7m, flaky — failed run 6) | `--probes-only` mode: needles + consumers + runtime probes only |
| 23 | Desktop `dist:mac` build serialized after validation (~5m) | overlapped: build starts as background child at VERIFIED, BUILT reaps; killed on validation failure/abort |
| 24 | No escalation path from scoped to full validation | `full_validation_required`: resolver used / >5 conflicts / core-dir conflicts / dep manifests / harness files → `UPDATE_VALIDATION_FULL=1` |
| 25 | Curated step 7 (desktop typecheck+UI) ran on backend-only diffs; `dependency_manifests_changed` matched any nested package.json | `UPDATE_CHANGED_DESKTOP` env gate; manifest check restricted to six root paths |
| 26 | Live `npm ci` and `uv sync` at activation ran serially | concurrent children, either failure kills the sibling and fails the phase |
| 27 | Helper verdict invisible: service polled 2h while helper had already failed (open item 3) | `--completion-marker` wired into enqueue; wait probe consumes outcome JSON and fails immediately with the helper's message |
| 28 | Abort during a running subprocess recorded FAILED (open item 4) | abort marker wins over CalledProcessError in `wait_owned_child`; pre-activation generic failures with pending abort transition ABORTED |
| 29 | `gateway_state.json` poisoned by foreign writer (pid 145 + feishu entry) and never self-heals; helper refused graceful restart, 20-min stall (open item 1) | helper falls back to fresh `state/gateway.heartbeat` then launchd-pid-owns-8642; `write_runtime_status` ownership guard (lock handle / gateway.pid / live-identity check) stops foreign identity stamps; watchdog re-stamps identity every ~30s so clobbers self-heal |
| 30 | api_server gave up permanently on transient `EADDRINUSE` during restart handoff (open item 2) | 60s bounded bind retry (0.5s→5s backoff, runner/site rebuilt per attempt) before the existing non-retryable fatal |

## Open follow-ups

1. **Identify the pid-145/feishu foreign writer.** The ownership guard makes
   it harmless, but the source (worktree validation env? container?) that
   stamped a feishu-enabled record at 12:56:58Z is still unidentified.
2. **3 pre-existing failures in `tests/hermes_cli/test_update_gateway_restart.py`**
   (`test_update_system_service_restart_failure_shows_error`,
   `test_reset_failed_also_runs_before_retry_restart`,
   `test_final_failure_message_tells_user_to_reset_failed`) — reproduce on
   clean 51250daff; systemd restart-messaging drift from upstream. Triage
   separately.
3. **Rerere hygiene rule.** Never record resolutions (`git rerere`) from an
   interrupted session's staged-but-unvalidated state without marking them
   suspect — both run-5 poisons came from exactly that (Phase 0 recovery of
   `ec8970978b22`). Recovery procedure: record, then treat the first
   subsequent validated merge as the trust gate.
6. **Evaluate upstream's borrowed-provider routing config** — it may express
   the vibeproxy short-alias routing natively and let that code carry retire
   (tests at `tests/test_tui_gateway_server.py:2478,2497` guard it).
7. **Carry-weight ratchet vs upstream churn.** ~~The pre-push baseline inflates
   as origin/main moves (37,950 → 261,618 with zero local change), blocking
   pushes until after a merge.~~ FIXED 2026-07-23: `thinning.py check` now
   scores against the baseline's recorded `upstream_sha` (not the live ref),
   making the score deterministic between baseline refreshes, and accepts a
   `--tolerance-pct` band (default 1%) for weighted-score growth. New hotspot
   paths still hard-fail. Regression tests in
   `tests/scripts/test_thinning.py`.
8. **WebUI: 58 pre-existing test failures** (identical pre/post merge) —
   recorded in `~/.hermes/hermes-webui/.known-failing-tests-20260723.txt`;
   triage separately.
9. **Speed.** This first service-routed update took ~5h wall clock, dominated
   by serial failure discovery (env bug run 1, poisons runs 5). Those causes
   are now fixed at the root; a normal future run should be one pass
   (~45–60 min: merge ~5m, validation ~25m, Desktop build ~10m, restart +
   verify ~10m). Keep it that way by: running the full validation suite in
   the worktree *before* `request start` when the merge is conflict-heavy;
   reading `drain-audit.jsonl` at the first sign of a slow RESTARTED phase;
   and never re-fixing in a doomed run — abort, fix pre-merge, rerun.
