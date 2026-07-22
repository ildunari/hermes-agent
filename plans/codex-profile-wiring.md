# Codex profile wiring — plan

Goal: a Hermes profile named `codex` that runs the codex_app_server runtime against the
OpenCodex home (`~/.codex-opencodex`) via the `opencodex` wrapper binary, with
launch-scoped instruction injection and truthful model forwarding. No persisted edits to
any Codex config.toml; no cross-profile token/auth sharing.

## Invariants (MUST hold)

- I1: Zero writes to `~/.codex/`, `~/.codex-opencodex/`, or any OpenCodex-owned file at
  runtime. All customization goes through `-c` config_overrides / spawn env on the
  app-server subprocess (launch-scoped).
- I2: No auth.json / tokens / session DBs / memories copied between profiles. The new
  profile gets its own empty state; auth resolves through the normal per-profile pool.
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
```

- Load via the same config accessor the agent already uses (hermes_cli.config.load_config
  is profile-aware through HERMES_HOME). Validate types defensively; ignore garbage.
- Pass codex_bin / codex_home / codex_config_overrides into CodexAppServerSession (the
  session already accepts all three — currently nothing populates them).
- developer_instructions_file: read the file at session spawn (not import time); if
  readable, append `developer_instructions=<contents>` as a `-c` override. TOML-quote the
  value correctly (codex -c parses `key=value` where value is TOML; use a triple-quoted or
  properly escaped TOML string — check how codex parses -c overrides and match it. If
  plain TOML string escaping is fragile for multi-line text, write the contents verbatim
  and escape backslashes+quotes; add a unit test with newlines/quotes).
- forward_model: when true, `thread/start` params include `model` (and `modelProvider`
  when the configured model id carries a `provider/model` slash form, split it). Model
  comes from the agent's effective model (agent.model or model.default). The thread/start
  response echoes accepted model/provider — log it and store on the session object as
  `accepted_model` / `accepted_provider` for display truthfulness (see C3).

### C2. hermes-tools bridge allowlist (launch-scoped)
`agent/transports/hermes_tools_mcp_server.py`: support env var
`HERMES_TOOLS_EXPOSE` (comma-separated tool names). When set and non-empty, expose only
the intersection with EXPOSED_TOOLS. Unset → current full list (I3). The codex profile
will set this env var in its profile config (agent.environment or gateway env) — do NOT
hardcode profile names in the server.

### C3. Truthful model display for codex_app_server sessions
Where the codex_app_server turn result is recorded (`_record_codex_app_server_usage` in
agent/codex_runtime.py), if the session has `accepted_model`, surface a mismatch: when
accepted_model != agent.model, log at INFO and set `agent.last_executed_model` attr (new,
display-only; no schema change). Banner/UI wiring beyond the attr is out of scope here —
keep the change minimal and observable.

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
