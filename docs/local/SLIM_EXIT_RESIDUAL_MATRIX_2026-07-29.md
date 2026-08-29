# Slim-exit residual overlap matrix — 2026-07-29

Baseline: current `origin/main` after the 2026-07-29 upstream merge. The rule for this pass was to remove only behavior that current upstream demonstrably owns; correctness-sensitive policy remains carry when its lifecycle or consumer is absent upstream.

| Package | Upstream-owned behavior now present | Residual local behavior | Disposition |
| --- | --- | --- | --- |
| Mixed-tool batching | `271a9d8ec` segments mixed batches; `9a21d0e3f` canonicalizes conflict paths; `e12626b34` handles null arguments. | The local grouped executor is integrated with Relay/Hermes dispatch middleware, one outer budget/steer lifecycle, policy preflight, timeout abandonment, incremental persistence, and local terminal accounting (`agent/tool_executor.py`, `run_agent.py`). | Retain for this cutover. Replacing it requires rebasing the managed dispatch pipeline onto upstream's segmented executor as one tested unit; a mechanical revert conflicts inside dispatch ordering and is not a safe thinning edit. |
| Cron | Upstream now owns atomic heartbeat writes, catch-up counters, wedged one-shot diagnostics, generic delivery resolution, and active cron visibility. | Probe-generation compare-and-set/ACK proof; profile-pinned subprocess and Hermes-home overrides; mem0 harvest exception; cron-to-gateway active-agent persistence; generic fail-closed cross-profile operational delivery. | Product-specific cron installers and runners are plugin-owned. Retain only the generic delivery-profile and scheduler correctness seams absent upstream. |
| Runtime status and restart | Upstream owns the base PID/runtime-status schema, runtime lock checks, and ordinary lifecycle/status helpers. | Cross-process `flock` around read/merge/write; owner-only identity stamping; callable active-agent recount; independent active-agent freshness; multiplex profile status files; profile-scoped PID validation; detached safe restart/drain/update-service contracts. | Retain. These are process-ownership and safe-drain correctness guarantees, not duplicate presentation code; current upstream does not serialize cross-process status writers or provide the detached restart contract. |
| Session state | Upstream contains the read-connection split (`6623ee9bb`, `f228e145b`), external-content FTS/trigram and CJK work, WAL/repair hardening, and recent session-filter APIs. Those upstream changes are already merged into the local implementation. | External LCM discovery/load metadata, contact/gateway routing metadata, session activity lifecycle, compact legacy reads, local hygiene/export behavior, and a stricter cap on per-thread read connections. | Retain. The remaining delta carries schemas and data-integrity behavior with live consumers; wholesale restoration to upstream would delete required columns/index lifecycle and is not a safe overlap reduction. |
| Fallback routing | Upstream owns per-turn fallback and primary credential-pool restoration (`c7619773e`, plus the existing fallback chain). | `agent/runtime_routing.py` and its route events do not exist upstream; Desktop/TUI/API visibility, one-shot ownership, init-fallback baseline, failed-switch reconciliation, and model-pill state all consume them. | Retain. Primary restoration is now upstream-backed at the transport level, but removing local lifecycle state would make the active route invisible or false; thin later around a generic upstream runtime-route event seam. |

## Additional overlap removed in this pass

- Message-card rendering and validation moved wholly to the `message-cards` user plugin. Core `gateway/rich_cards/**`, the core Node renderer, automatic markdown/fence conversion, and ordered rich-segment delivery carry were deleted; only normal `MEDIA:` auto-append remains in core.
- API Server carry was reduced to plugin-owned residual routes on top of upstream core. Core Mini App, Claude-session, run/background-task, and CodexBar dashboard residuals were removed.
- WebUI hidden-source carry checks were updated to the current structured filter implementation, and the computer-use ended-session revival feature was removed from the carry registry because the file now matches upstream.
- A whitespace-only Buzz adapter drift was removed. The remaining TTS delivery-metadata sanitizer is explicitly registered as test-backed carry rather than left unowned.

## Verification evidence

- Message-card plugin: 6 tests passed; API Server/plugin residual suite: 36 tests passed.
- Core media/send-message suite: 256 tests passed; stream consumer: 143 tests passed; platform/cron affected suite: 602 tests passed after the media-display compatibility correction.
- Cron jobs/scheduler/shutdown suite: 392 tests passed after upstream recovery convergence.
- TTS normalization/preparation suite: 20 tests passed.
- Carry registry: 463/463 managed paths covered, 20 features, 33 legacy surfaces.

## Conversation-policy de-carry — Checkpoint 4 residual ownership (2026-08-28)

Checkpoint 4 physically deletes the product-policy leaves. There is no in-core
behavioral fallback after this commit; disabling a required extension requires
reverting the deletion commit first. Durable profile data remains in place and
was not opened or migrated by this change.

| Surface | Residual category | Current owner | Update-conflict effect |
| --- | --- | --- | --- |
| `gateway/conversation_extensions.py`, runtime, readiness | generic extension seam | core | Atomic registration, invocation, required-extension readiness, and bounded lifecycle facade. No product policy. |
| `gateway/conversation_ownership.py` | generic exactly-one selector | core | Optional extension domains default to an inert core verdict; extension requests require exactly one healthy claimant and conflicts fail closed. |
| `gateway/run.py` | generic invocation and route application | core | Startup activation, validated route application, turn augmentation, lifecycle fire sites, and post-turn observation only. No product writer, watcher, texture engine, or route classifier remains. |
| `model_tools.py` | generic final-dispatch authorization | core | One request-scoped gate covers direct, deferred bridge, MCP, inline, and executor paths. The product-specific second guard is deleted. |
| `gateway/authenticated_dm.py`, `gateway/delivery_ledger.py`, `hermes_state.py` | generic platform action and initiated-session/storage primitives | core | Tri-state existing-DM send, durable delivery recovery, and assistant-first initiated children remain reusable infrastructure. |
| `agent/turn_context.py` + conversation loop | generic pre-LLM system-context lane | core | Standalone texture plugins inject at API-call time; no engine or policy is carried in core. |
| Product routing, identity policy, filesystem tool, per-turn recall, ingress/extraction, texture, and background watchers | plugin behavior | user-plugin repository | No core collision surface; tests and runtime libraries move with the owner. |

The deterministic post-deletion thinning baseline at core
`28e12493cac5d53f63aa611dfa8a295598ef638a` contains 402 actual residual
hotspots with weighted score 132601. Two independent generations were
byte-identical. No hotspot path matches the deleted Poke/Guest/contact-memory,
proactive runtime, texture-engine, Guest-workspace, or Guest-cron surfaces.

