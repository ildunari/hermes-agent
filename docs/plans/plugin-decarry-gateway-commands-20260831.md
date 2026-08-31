# Plan: De-carry Kosta gateway session commands

## Objective

Move Kosta-specific gateway commands (`/cwd`, `/repo`, `/thread`, `/threads`, `/newthread`, `/personality_session`, `/tts`, and `/bgnotify`) out of `gateway/slash_commands.py` into a user plugin. Add the smallest generic command-invocation context required for plugins to perform session-aware, platform-capability-aware work without reaching into adapter internals.

## Architecture and contracts

- Extend plugin slash-command registration from raw `fn(raw_args)` to a backward-compatible invocation context. Existing plugins that accept only raw arguments must continue to work unchanged.
- Context should expose safe values/capabilities, not the gateway object: raw args, event/source identity, profile, session key/id, adapter capabilities, reply helpers, and narrow session metadata operations needed by the migrated commands.
- Session mutations must remain atomic through the existing async session store. Do not expose raw dictionaries or process-global mutable state.
- Telegram topic creation/rename remains capability-based. Non-Telegram adapters must degrade with the same messages and no accidental transport call.
- Keep universal commands and dispatch in core: `/new`, `/reset`, `/model`, command discovery/help, and generic reset/session semantics.

## Files and ownership

Core lane may modify only generic command plumbing and removal sites, principally:

- `hermes_cli/plugins.py`
- `gateway/run.py` and/or a focused new gateway command-context module
- `gateway/slash_commands.py`
- `gateway/session.py` only for a generic narrow session metadata API
- focused command/plugin tests

Plugin lane should create one focused plugin directory (for example `gateway_session_commands/**`) and its tests.

Do not edit provider/model code, web tools, Desktop code, `scripts/local_carry_manifest.yaml`, shared de-carry docs, live config, profile/session databases, or gateway runtime state.

## Implementation order

1. Write red compatibility tests for legacy raw-argument handlers and new context-aware handlers in CLI and gateway dispatch.
2. Introduce a typed immutable `CommandInvocationContext` and narrowly scoped session/platform capability facade.
3. Move the eight Kosta commands and their helper functions into the plugin without importing private gateway mixin internals.
4. Delete product-specific command bodies/state from `gateway/slash_commands.py`; retain only generic dispatch and universal commands.
5. Test DM, group/topic, missing capability, session reset, cwd validation, personality persistence, TTS failure, and background notification paths with outbound transports mocked.
6. Run focused gateway/plugin suites, compile, and `git diff --check`; commit core and plugin work separately. Stop without merge/restart/push/live config changes.

## Acceptance criteria

- The eight named commands have no implementation or product state left in core.
- A plugin can register context-aware commands while old `fn(raw_args)` plugins remain compatible.
- Context cannot expose secrets, arbitrary adapter mutation, or another profile's session state.
- Commands preserve current visible behavior, thread/session bindings, cwd validation, and fail-closed platform handling.
- Disabling the plugin removes only these commands; core command handling remains healthy.
- Focused tests pass in both repositories; worktrees end clean with attributable commits.

## Risks

- Gateway multiplex serves multiple profiles in one process: command state and registration must remain profile-scoped.
- Topic/session creation can create external side effects. Tests must mock them; do not send or rename a real Telegram topic.
- Avoid a giant context object. Every exposed method must have a concrete migrated consumer and a security test.
