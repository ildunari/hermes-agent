# Plan: Move local Desktop enhancements into a runtime Desktop plugin

## Objective

Add generic Desktop SDK seams that let a runtime plugin own Kosta's chat-width modes, Markdown-table layout mode, Local Enhancements settings UI, and PDF/DOCX side-panel preview behavior. Preserve security boundaries for local files and remote Desktop connections.

## Architecture and contracts

- Add only concrete SDK contributions consumed immediately by the new plugin: a settings-section area, plugin-scoped persistent state, scoped style contribution, and an attachment previewer contribution.
- Runtime plugins must not receive arbitrary Node/Electron or filesystem access. Previewers operate through a host capability that validates attachment origin, path/URL scheme, size, and allowed MIME/extension before returning a safe preview source.
- Plugin styles must be scoped and removable on disable/unload. Do not allow unrestricted persistent mutation of the app's stylesheet.
- Plugin settings state must be namespaced, versionable, and survive relaunch without colliding with another plugin.
- Existing Desktop profile routing, plugin home, compaction, sticky-turn, and theme correctness remain core and out of scope.

## Files and ownership

Core lane may modify narrowly:

- Desktop SDK/contribution registry and host capability modules
- Settings composition mount
- attachment rendering/preview dispatch boundary
- Electron preload/main only when needed for a generic validated preview capability
- focused SDK/security/lifecycle tests

Plugin lane should create a runtime Desktop plugin under the user-plugin repo's canonical Desktop plugin layout and own:

- chat width state/options/styles
- Markdown table state/options/styles
- Local Enhancements settings section
- PDF/DOCX preview registration and UI
- plugin-focused tests/build setup

Do not modify backend providers, gateway commands, web tools, `scripts/local_carry_manifest.yaml`, shared de-carry docs, live Desktop install, user settings, or profile data.

## Implementation order

1. Add red SDK tests for settings contribution lifecycle, namespaced persisted state, style cleanup, previewer selection, untrusted path rejection, and plugin unload.
2. Implement the four narrow SDK seams with typed contracts and capability checks.
3. Build the user Desktop plugin and move the four local enhancement behaviors into it.
4. Delete the corresponding local stores, settings components, CSS, and direct PDF/DOCX feature wiring from core while retaining generic SDK/host support.
5. Run Desktop unit tests, plugin tests, typecheck, production build, and `git diff --check` in both repos. Use fixture files only; do not open private documents or mutate the installed live app.
6. Commit core and plugin changes separately. Stop without merging, packaging/installing, restarting, pushing, or modifying live Desktop configuration.

## Acceptance criteria

- With the plugin enabled, all four behaviors match current functionality and persisted settings survive a simulated relaunch.
- With it disabled/unloaded, contributions/styles disappear cleanly and core Desktop remains functional.
- Preview tests cover PDF, DOCX, unsupported type, oversized input, remote URL, traversal, symlink, and stale file cases.
- No plugin has raw `fs`, Electron IPC, shell, or unrestricted CSS authority.
- Desktop test/typecheck/build and plugin tests pass; both worktrees are clean and committed.

## Risks

- Runtime Desktop plugins are renderer code; filesystem and IPC exposure is a security boundary, not a convenience API.
- Current document preview touches several shell/rendering paths. Introduce one generic preview contribution rather than reproducing those private imports in the plugin.
- Appearance features currently have low merge pressure. Do not expand this lane into unrelated visual redesign or theme work.
