# Codex profile wiring — plan

Goal: a Hermes profile named `codex` that runs the codex_app_server runtime against the
OpenCodex home (`~/.codex-opencodex`) via the `opencodex` wrapper binary, with
launch-scoped instruction injection and truthful model forwarding. No persisted edits to
any Codex config.toml and no Hermes-owned writes into Codex homes. The OpenCodex
subprocess intentionally reuses the live state in `~/.codex-opencodex`.

## Invariants (MUST hold)

- I1: Hermes never persists edits to a Codex `config.toml` and never writes directly
  into Codex or OpenCodex homes. The spawned app-server subprocess owns runtime state in
  the configured `codex_home`; choosing the OpenCodex home intentionally uses and shares
  its live state.
- I2: Hermes does not copy auth.json, tokens, session DBs, or memories between homes.
  Authentication and runtime state resolve normally inside the configured Codex home.
- I3: Default runtime behavior unchanged for every other profile: all new config keys are
  optional, absent keys preserve today's behavior byte-for-byte (prompt-cache safety).
- I4: The codex_app_server session must keep working with stock `codex` + `~/.codex`
  exactly as today when the new keys are unset.

## Changes (in this worktree)

### C1. Plumb model.codex_app_server config into the session spawn
`agent/codex_runtime.py` (`run_codex_app_server_turn`), where `CodexAppServerSession` is
constructed: read an optional dict from Hermes config:

```yaml
model:
  openai_runtime: codex_app_server
  codex_app_server:
    codex_bin: /Users/Kosta/.local/bin/opencodex   # optional; default "codex"
    codex_home: /Users/Kosta/.codex-opencodex      # optional; default env/~/.codex
    developer_instructions_file: <abs path>        # optional; injected via -c
    config_overrides: ["key=value", ...]           # optional; forwarded as -c pairs
    forward_model: true                            # optional; default false
    model_provider: openai                         # optional; requires forward_model
```

- Load via the same config accessor the agent already uses (hermes_cli.config.load_config
  is profile-aware through HERMES_HOME). Validate types defensively; ignore garbage.
- Pass codex_bin / codex_home / codex_config_overrides into CodexAppServerSession (the
  session already accepts all three — currently nothing populates them).
- developer_instructions_file: read the file at session spawn (not import time); if
  readable, append `developer_instructions=<contents>` as a `-c` override. Encode the
  contents with `json.dumps(..., ensure_ascii=False)`, which is a TOML-compatible basic
  string representation and preserves Unicode while escaping newlines, quotes, and
  backslashes.
- forward_model: when true, `thread/start` receives the full effective requested model id
  verbatim as `model` (agent.model or model.default); slashes are never split. Forward
  `model.codex_app_server.model_provider` as `modelProvider` only when it is an explicit,
  non-empty string. Otherwise omit `modelProvider`.

### C2. hermes-tools bridge allowlist (launch-scoped)
`agent/transports/hermes_tools_mcp_server.py`: support env var
`HERMES_TOOLS_EXPOSE` (comma-separated tool names). Unset exposes the full
`EXPOSED_TOOLS` list. Present but empty (including whitespace or commas only) exposes no
tools. Non-empty values use exact, case-sensitive matching, expose only the allowlisted
intersection, and warn for unknown names. The codex profile sets this env var in its
profile config (agent.environment or gateway env) — do NOT hardcode profile names in the
server.

### C3. Truthful model display for codex_app_server sessions
`TurnResult` carries the session's accepted model/provider immediately after
`ensure_started()` succeeds, so truth survives session retirement. Where the result is
recorded (`_record_codex_app_server_usage` in agent/codex_runtime.py), read acceptance
from the turn rather than `agent._codex_session`. Only when `forward_model: true`, set
`agent.last_executed_model` on every turn that has an accepted model, including a match;
log mismatches at INFO. With absent/disabled forwarding, do not mutate the attribute.
Banner/UI wiring beyond the attr is out of scope.

Known limitation: pricing and persisted usage attribution still use the requested Hermes
model id, not the app-server's accepted model id.

### C4. Runtime gate stays provider-scoped
No change to `_maybe_apply_codex_app_server_runtime` semantics (openai/openai-codex
only). The codex profile keeps `model.provider: openai-codex`.

### C5. Tests
- Unit: config plumbing (codex_bin/codex_home/config_overrides reach the session ctor;
  absent config → defaults). Use the existing test fixtures in
  tests/agent/transports/test_codex_app_server_session.py style.
- Unit: developer_instructions_file → -c override present + correctly escaped
  (newlines, quotes, backslashes).
- Unit: HERMES_TOOLS_EXPOSE filtering (set/unset/garbage names).
- Unit: forward_model → thread/start params contain model; response echo captured.
- Run: `python -m pytest tests/agent/transports/ tests/run_agent/test_codex_app_server_integration.py -x -q`
  plus any new test files.

## Out of scope (deliberately)

- Hermes model-picker listing OpenCodex catalog models (separate change).
- Mid-thread model switch UI (`thread/set_model` etc.) — first land spawn-time truth.
- Creating the profile itself (done operationally with `hermes profile create`, not code).
- sessions MCP changes (already landed live, outside this repo).
