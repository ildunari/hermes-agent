# Plan: De-carry the unified web wrapper into `local_tools`

## Objective

Remove Kosta-specific unified `web` dispatch, CurlMD loading, and local fast-extraction policy from Hermes core. Keep generic search/extract providers, SSRF checks, caching, rescue behavior, provider-native modes, and legacy core tools in core. The user plugin must override the built-in `web` tool through the existing explicit trust gate.

## Architecture and contracts

- The live plugin source is `/Users/Kosta/.hermes/plugins`; this lane receives an isolated plugin worktree.
- Register `web` from `local_tools` with `ctx.register_tool(..., override=True)` and preserve the current schema, async behavior, availability checks, result-size contract, and error messages.
- The operator trust boundary remains `plugins.entries.<plugin>.allow_tool_override: true`; do not weaken it or auto-grant overrides.
- Remove all core imports of `hermes_plugins.local_tools` / `plugins.local_tools` and all local-plugin fallback loading from `tools/web_tools.py`.
- Keep `web_search`, `web_extract`, and their generic implementation registered in core. Only the Kosta-specific combined wrapper and plugin-owned CurlMD/fast-path policy move.
- If the fast extractor is generally reusable infrastructure, expose one narrow provider/hook interface in core; do not leave a direct core-to-user-plugin import.

## Files and ownership

Core lane may modify only the narrow web extension surface and tests, principally:

- `tools/web_tools.py`
- `tools/web_fast_extract.py` only if required to delete or genericize the local policy
- focused `tests/tools/**` and plugin-loader tests needed for override lifecycle

Plugin lane may modify:

- `local_tools/**`
- focused plugin tests for `local_tools` / web override
- plugin manifest metadata for the existing local-tools plugin

Do not edit `scripts/local_carry_manifest.yaml`, shared de-carry docs, live config, profile data, gateway state, or unrelated tools.

## Implementation order

1. Add red plugin tests proving an enabled/trusted local-tools plugin replaces only `web`, preserves the exact schema/dispatch behavior, and restores the built-in registration when unloaded.
2. Move the unified dispatcher, CurlMD action, and local fast-extract orchestration into `local_tools` with no core imports back into the plugin.
3. Delete the plugin-loader/fallback branches and user-specific schema/dispatch code from core.
4. Preserve generic provider extraction behavior and direct legacy tool registrations.
5. Run focused core and plugin suites, then compile and `git diff --check` both repositories.
6. Commit core and plugin changes separately with focused messages. Stop; do not merge, restart, push, or edit live config.

## Acceptance criteria

- No core source imports `hermes_plugins.local_tools`, `plugins.local_tools`, or `curlmd_tool` from the user plugin.
- Trusted enabled plugin supplies `web`; disabled/untrusted plugin leaves the built-in tool intact and fails closed.
- Search, fetch, answer, summary, JSON, links, and CurlMD actions retain their current contracts.
- SSRF/secret URL checks happen before any cache/provider/plugin network fetch.
- Core web tests and focused plugin tests pass; new override lifecycle tests execute and pass.
- Both worktrees end clean with one or more attributable commits and an exact test report.

## Risks

- Tool registration is profile-scoped and replacement-aware; test two profiles/scopes so one plugin instance cannot leak globally.
- Unload/reload must restore the previous tool registration rather than leaving the wrapper absent.
- Do not duplicate core extraction internals into the plugin; the plugin should call stable public helpers or a narrow new seam.
