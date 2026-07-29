# Slim Exit Upstream-Overlap Migration Plan

**Goal:** Remove Hermes carry now supplied by upstream, move Kosta-specific surfaces into the user-plugin repository, and retain only behavior proven to remain distinct.

**Architecture:** Develop core reductions in an isolated branch rebased through current `origin/main`, and develop user-owned behavior in `~/.hermes/plugins` on a separate feature branch. Keep the live checkout untouched until the durable update service has merged and validated the same pinned upstream; then cherry-pick only the reviewed post-merge reduction commits, rerun the owning tests, fast-forward deliberately, and activate with the detached safe restart path.

**Pinned starting point:** `local/studio-slim` at `7621d035e32c69d7a7535608662bb496636f0232`; implementation must repin upstream immediately before the development merge and record that SHA in the resulting merge commit.

**Primary artifacts:**

- Core worktree: `/Users/Kosta/LocalDev/.studio-only/hermes-worktrees/slim-exit-upstream-overlap`
- Core branch: `migration/slim-exit-upstream-overlap-20260729`
- Plugin repo: `/Users/Kosta/.hermes/plugins`
- Audit inputs: `/Users/Kosta/.hermes/profiles/coding/tmp/slim-exit-audit/{SLIM_EXIT_MAP_2026-07-28.md,UPSTREAM_OVERLAP_2026-07-29.md}`

---

## Task 1: Establish a recoverable baseline

1. Retire the terminal failed update-service run `20260728T215216Z-f29d0e57cf22` and verify its integration worktree is gone.
2. Record ownership in live `hey.md`; do not absorb the Desktop agent's DOCX smoke artifact or theme branches.
3. Create this isolated core branch from the current live tip and commit this plan before code changes.
4. Create a separate plugin feature branch/worktree before editing `~/.hermes/plugins`; never leave plugin source loose or uncommitted.
5. Run `scripts/setup_update_service.py check`, verify rerere/zdiff3/shared rr-cache, and do not begin live update activation until the live tree is clean.

## Task 2: Merge current upstream in the development worktree

1. Fetch and pin one immutable `origin/main` SHA.
2. Merge that SHA in the isolated core worktree, using shared rerere and resolving conflicts there only.
3. Validate every resolution against both parents, especially Desktop preview routes, TTS/voice, API Server, tool batching, cron, `hermes_state.py`, fallback routing, and restart/status files.
4. Run carry sentinel and the focused tests selected by `scripts/local_carry_manifest.yaml` before making reduction commits. This merge is development evidence only; the update service still owns the later live update transaction.

## Task 3: Retire duplicated TTS/voice carry

**Core paths to inspect or modify:**

- `tools/tts_tool.py`
- `tools/tts_text_formatter.py`
- `tools/voice_mode.py`
- `hermes_cli/web_server.py`
- `apps/desktop/src/lib/voice-playback.ts`
- `tests/test_tts_spoken_formatter.py`
- `tests/tools/test_tts_command_providers.py`
- `tests/tools/test_voice_mode_gate.py`
- `tests/gateway/test_tts_command.py`
- `tests/gateway/test_voice_command.py`
- `scripts/local_carry_manifest.yaml`

Steps:

1. Diff each carried behavior against pinned upstream; classify it as equivalent, complementary, or still missing.
2. For equivalent behavior, restore the upstream implementation and remove duplicate carried helpers/tests.
3. Preserve only behaviorally distinct requirements with a focused regression test; do not retain code merely because symbols differ.
4. Update the carry manifest using the two-step retirement rule: empty needles with a RETIRED note in the path-changing commit, then delete retired entries after the path leaves `changed_paths`.
5. Run the focused Python and Desktop tests plus a real configured TTS preprocessing/provider smoke test without exposing secrets.

## Task 4: Extract CodexBar into a dashboard plugin

**Plugin files to create:**

- `codexbar_usage/plugin.yaml`
- `codexbar_usage/dashboard/manifest.json`
- `codexbar_usage/dashboard/plugin_api.py`
- `codexbar_usage/dashboard/dist/index.js`
- `codexbar_usage/dashboard/dist/style.css`
- `tests/plugins/test_codexbar_usage_plugin.py`

**Core/plugin files to reduce:**

- `web/src/App.tsx`
- `web/src/lib/api.ts`
- `gateway/platforms/api_server.py`
- `tests/gateway/test_api_server_subscription_usage.py`
- `api_server_override/adapter.py`
- `api_server_override/README.md`
- `tests/plugins/test_api_server_plugin.py`

Steps:

1. Move CodexBar CLI discovery, enabled-provider parsing, subprocess timeout/redaction, and payload normalization into the dashboard plugin backend under authenticated `/api/plugins/codexbar-usage/` routes.
2. Recreate the provider-usage UI as a dashboard plugin tab using the public plugin SDK and authenticated fetch helper; do not patch core WebUI files.
3. Add malformed config, missing CLI, timeout, non-JSON, partial-provider, and redaction tests.
4. Remove the duplicated core/API-override CodexBar routes only after plugin tests prove parity.
5. Enable the plugin in the appropriate root/profile `plugins.enabled` lists only after source and tests are committed.

## Task 5: Thin the API Server override

**Primary files:**

- `api_server_override/adapter.py`
- `api_server_override/claude_sessions.py`
- `api_server_override/README.md`
- `tests/plugins/test_api_server_plugin.py`
- `gateway/platforms/api_server.py`
- `gateway/claude_sessions.py`
- API Server carry tests and manifest entries

Steps:

1. Compare every override method against pinned upstream and generate an explicit residual-method inventory.
2. Replace the copied 3,656-line adapter with a subclass that calls `super()` and extends `_http_route_table()` only for residual Mini App, Claude-session telemetry, command/process/background-task, or other proven Kosta-specific routes.
3. Drop copied upstream run/model-routing/session/auth/drain behavior and module-global sweeps now provided by core.
4. Preserve Mini App path traversal protection, Telegram initData verification, profile-aware Mini App location, and all still-required endpoints with direct tests.
5. Enable the override only after route parity is intentionally redefined as `upstream routes + documented residual routes`, then remove corresponding core carry and tests.

## Task 6: Make message-cards self-contained

**Plugin files:**

- `message_cards/renderer.py`
- `message_cards/validate.py`
- `message_cards/models.py`
- `message_cards/tool.py`
- `message_cards/__init__.py`
- message-card plugin tests

**Core files to reduce:**

- `gateway/rich_cards/**`
- `tools/send_message_tool.py`
- rich-card carry tests and manifest entries

Steps:

1. Move renderer, validation, models, fallback formatting, templates/assets, and output-path ownership into the plugin.
2. Change plugin imports so no `gateway.rich_cards` module is required.
3. Use the upstream standalone platform sender path for explicit delivery where available; preserve text fallback and ordered segment behavior only when a test demonstrates a remaining gap.
4. Test render success, repair behavior, invalid specs, output existence, Telegram/Discord/BlueBubbles hints, and standalone delivery.
5. Remove core renderer/tool carry and retire its manifest entries after plugin activation is proven.

## Task 7: Thin residual high-collision carry

**Packages:** mixed-tool batching, cron, runtime status/restart, session state, and fallback routing.

Steps for each package:

1. Use `git diff <pinned-upstream>..HEAD`, commit provenance, and manifest consumers/tests to form a behavior matrix.
2. Delete code/tests now covered upstream; preserve only residual policy with a failing-before/passing-after behavior test.
3. Prefer config, skills, or plugins for Kosta-specific policy. Keep core changes only for agent-loop, process ownership, authorization, or missing generic seams.
4. Run each package's focused suite after its own commit so failures are attributable.
5. Recompute collision-density and thinning scores; the final branch must reduce both changed paths and shared hot-file carry unless a documented correctness blocker prevents it.

## Task 8: Integrated review, landing, and activation

1. Run carry sentinel, all tests named by modified manifest features, full Python validation when dependency/config or resolver gates require it, and owning Desktop/WebUI/plugin suites.
2. Run one independent adversarial integrated-diff review because this migration touches auth, routing, process lifecycle, and broad refactoring. Fix P0/P1 findings and perform at most one narrow closure review.
3. Commit and push the clean plugin branch first; verify live plugin directory and profile wiring.
4. Wait for the live checkout to become clean, then run the durable update service against the pinned/current upstream. Resolve only in its integration worktree and require its validation receipts.
5. Apply the reviewed post-merge reduction commits to a fresh worktree from the updated live tip, rerun their owning tests, and fast-forward `local/studio-slim` deliberately.
6. Enqueue the detached safe Hermes restart, then verify fresh PIDs, HTTP 200 health, authenticated WebUI turn, plugin identity, API/Mini App routes, dashboard plugin load, TTS, message-card delivery, cron/status/session behavior, and fallback lifecycle.
7. Remove resolved `hey.md` entries, delete merged migration branches/worktrees, and report exact live commits plus any remaining slim-exit blockers.

## Completion criteria

- No duplicated TTS implementation remains without a documented behavioral difference.
- CodexBar and message-card ownership is entirely in the user-plugin repo.
- API Server override is a thin extension of current upstream, not a copied adapter.
- Mixed-tool, cron, status/state, and fallback carry contain only test-proven residual behavior.
- Both repositories are clean and pushed; `local/studio-slim` contains the reviewed changes.
- The detached activation returns every required surface healthy and a real authenticated turn succeeds.
