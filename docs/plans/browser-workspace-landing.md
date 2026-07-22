# Browser Workspace Landing Plan (2026-07-22)

Goal: extract the in-app browser from `plan/in-app-browser-wayfinder-20260716` into
current `local/studio-slim` as a profile-independent, app-global Desktop feature:
chat left, embedded tabbed browser right (split pane), agent + human co-control.
NOT a separate HermesBrowser app; NOT profile-scoped.

## Facts (measured)
- Base: 056580d38; live +1301 commits, branch +342.
- 119 new code files (85 apps/desktop, 11 hermes_cli, 3 tools, 19 tests, 1 nix) — zero
  name collisions with live. Ported verbatim in Phase 1.
- Merge surface: 19 desktop files modified on both sides + 13 backend files
  (agent/*, hermes_cli/web_server.py, config, doctor, setup, tools_config, tools/browser_tool.py).
  These are re-implemented as thin shims against live code, not diff-applied.

## Phases
1. Mechanical port (orchestrator): `git checkout <branch> -- <119 files>`; commit.
2. Parallel shims (delegate_task, 3 lanes):
   A. Electron main-process wiring (main.ts, preload.ts, package.json, after-pack,
      bundle scripts) — gpt-5.6-sol (careful backend correctness).
   B. Renderer split-pane UI: mount browser workspace pane in app-shell chat-right,
      settings entries, i18n keys, gateway-event + session-action hooks —
      claude-opus-4-8 (UI/layout judgment).
   C. Backend bridge: agent tool exposure (tools/browser_tool.py merge,
      tools_config, web_server routes, config/doctor/setup registration) —
      gpt-5.6-sol.
3. Build + tests: desktop typecheck/build, browser unit suites, security probes
   (browser-guest-security tests), backend pytest for browser modules.
4. Adversarial review: claude-opus-4-8, P0–P3, security-focused (CDP allowlist,
   IPC surface, remote-auth) + product review of split-pane UX.
5. Fix findings, re-verify, land on local/studio-slim, safe restart, smoke test.

## Deliberate scope cuts
- Drop: separate HermesBrowser packaging, browser-client HERMES_HOME isolation,
  remote two-host release matrix, .orch/.planr/docs bulk (71 docs files — port only
  user-facing browser.md if trivially clean).
- Profile independence: no profile gating; browser state lives app-global under
  HERMES_HOME/browser.
