# Plan: Move local provider catalog and routing policy into provider plugins

## Objective

Make registered `ProviderProfile` objects the authoritative source for plugin provider catalogs and alias ownership, then remove Kosta-specific VibeProxy/model-selection policy from shared core catalogs. Add narrow generic endpoint and credential-rotation hooks only where a concrete local provider consumer requires them.

## Architecture and contracts

- Extend `ProviderProfile` with explicit, deterministic metadata rather than relying on dictionary insertion order. Candidate fields: curated/fallback catalog, alias-family preference/priority, and an overrideable runtime endpoint resolver.
- Static detection and pickers must consult the registered provider registry, including user plugins, without importing user-plugin modules from core.
- Preserve explicit provider selection over automatic alias policy. A plugin must never silently hijack a provider-qualified model.
- Alias ties must fail deterministically or require explicit provider selection; they must not depend on filesystem discovery order.
- If implementing fallback notification extraction, add a generic post-success credential-rotation event carrying safe metadata only. Notification policy remains in a user plugin.
- General current-model fixes belong in bundled provider profiles/upstream or local provider overrides, not scattered conditionals in `hermes_cli/models.py`.

## Files and ownership

Core lane may modify narrowly:

- `providers/base.py` and provider registry APIs
- `hermes_cli/models.py`, model picker/detection helpers, and focused model tests
- runtime endpoint resolution only where required by a concrete provider profile
- credential-pool/recovery code only if adding the generic post-rotation event

Plugin lane may modify:

- `model-providers/vibeproxy/**`
- focused VibeProxy/provider tests
- a small notification plugin only if the generic event is implemented and fully consumed

Do not modify web tools, gateway commands, Desktop code, `scripts/local_carry_manifest.yaml`, shared de-carry docs, live credentials/config, or provider secrets.

## Implementation order

1. Add red tests for deterministic plugin-owned alias priority, explicit-provider precedence, catalog discovery, ambiguous ties, unload/reload restoration, and profile isolation.
2. Add the minimal provider metadata/registry API and convert model detection/picker paths to consume it.
3. Move VibeProxy's curated catalog and Claude-family preference to its provider plugin; remove its load-bearing static core dictionary entry/comment.
4. Where safe and scoped, relocate local newest-model/default policy into provider profiles instead of shared catalogs. Do not broaden into unrelated model refresh work.
5. Add endpoint/rotation hooks only with a concrete test-backed plugin consumer; otherwise document and leave that subpart for a follow-up rather than inventing unused infrastructure.
6. Run provider/model/auth focused suites, compile, and `git diff --check`; commit core and plugin changes separately. Stop without merge/restart/push/live config or credential changes.

## Acceptance criteria

- VibeProxy's auto-alias preference and curated picker list are owned by its plugin, not `_PROVIDER_MODELS` ordering.
- Explicit `provider/model` and explicitly configured providers always win over plugin auto preference.
- Provider plugin load order cannot change resolution results.
- Plugin disable/unload removes its policy and restores normal native-provider behavior.
- No secret values appear in logs, tests, commits, or command lines.
- Existing model/provider/auth tests plus new registry integration tests pass; worktrees are clean and committed.

## Risks

- This is billing-sensitive: an alias routed to Anthropic instead of VibeProxy can incur metered API usage. Tests must assert the provider and endpoint, not only the model string.
- Registry loading is profile-scoped while some catalogs are process-global today; fix the ownership boundary rather than caching one profile's plugin catalog globally.
- Credential rotation is security-sensitive. Emit only safe labels/ids and only after rotation plus runtime swap succeeds.
