# Runtime fallback visibility implementation plan

**Goal prompt:** Make Hermes Desktop and Hermes WebUI/Hermex truthfully show the selected session model separately from the model/provider that is executing (or executed) the current turn after automatic fallback, without persisting fallback as user intent, breaking explicit model switches, changing fallback behavior, invalidating prompt caches, or touching the offline MacBook.

## Contract

Use the existing `AIAgent.event_callback(name, payload)` seam. Emit `runtime:route` with this additive payload:

```json
{
  "schema_version": 1,
  "state": "started|fallback_activated|primary_restored|finished",
  "selected": {"model": "...", "provider": "..."},
  "runtime": {"model": "...", "provider": "..."},
  "fallback": {"active": true, "reason": "rate_limit|billing|authentication|provider_unavailable|timeout|upstream_error|context_limit|non_retryable_error|unknown", "chain_index": 0}
}
```

The core owns selected/runtime truth. Selected identity comes from `_primary_runtime` when present, otherwise the current runtime. Runtime identity comes from the live agent. No credential, base URL, account label, or raw exception may enter the payload.

## State ownership

- Hermes Agent core owns live routing truth and emits lifecycle changes through the existing event callback.
- TUI/compute host and Gateway Runs API translate/preserve the event; they do not infer fallback from prose.
- Desktop stores routing transiently per runtime session. Existing persisted `$currentModel` and `$currentProvider` continue to mean selected intent.
- WebUI journals routing events with the run, keeps active routing per stream/session in browser presentation state, and attaches only the settled routing summary to the completed assistant message/session payload. `Session.model` remains selected intent.

## Implementation slices

1. Core helper and tests: structured payload builder/emitter; turn start, successful fallback activation, restoration/cooldown continuation, and turn finish.
2. TUI/Desktop: add routing to `session.info` and live events, preserve through compute-host metadata mirror, add per-session transient state, and render quiet Selected/Running/Last response labels in the model pill and picker. Older backends degrade to current behavior.
3. Gateway Runs API: bridge `runtime:route` into `runtime.routing` SSE events and settled run status/result metadata.
4. WebUI direct path: pass an event callback into `AIAgent`, journal/emit `runtime_routing`, consume it without changing `S.session.model`, and retain the settled summary on the completed assistant message for reload.
5. WebUI gateway path: translate Gateway `runtime.routing` to the identical local event/persistence contract.
6. Verification: focused tests first, then neighboring/full suites, typecheck/lint, desktop and responsive browser evidence, independent P0-P3 review, live-branch landing, safe Studio restart, and real smoke tests. Do not deploy or copy a Desktop build to the offline MacBook.

## Required invariants

- Automatic fallback never invokes selected-model setters or rewrites session/config/local-storage model choice.
- Explicit `/model --session`, one-shot model overrides, and picker selection remain authoritative selected intent.
- Background-session events cannot update the foreground session's routing display.
- Replay/reconnect is idempotent; terminal routing metadata belongs to the run/message that used it.
- A failed fallback candidate is never presented as active; activation is emitted only after runtime swap succeeds.
- Missing new fields/events from an older backend produce no fallback inference and preserve existing UI behavior.
- Prompt messages, tool schemas, cache identity, retry counts, credential pools, and fallback restoration semantics remain unchanged.
