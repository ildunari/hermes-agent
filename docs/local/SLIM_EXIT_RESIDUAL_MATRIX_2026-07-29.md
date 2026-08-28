# Slim-exit residual overlap matrix — 2026-07-29

Baseline: current `origin/main` after the 2026-07-29 upstream merge. The rule for this pass was to remove only behavior that current upstream demonstrably owns; correctness-sensitive policy remains carry when its lifecycle or consumer is absent upstream.

| Package | Upstream-owned behavior now present | Residual local behavior | Disposition |
| --- | --- | --- | --- |
| Mixed-tool batching | `271a9d8ec` segments mixed batches; `9a21d0e3f` canonicalizes conflict paths; `e12626b34` handles null arguments. | The local grouped executor is integrated with Relay/Hermes dispatch middleware, one outer budget/steer lifecycle, policy preflight, timeout abandonment, incremental persistence, and local terminal accounting (`agent/tool_executor.py`, `run_agent.py`). | Retain for this cutover. Replacing it requires rebasing the managed dispatch pipeline onto upstream's segmented executor as one tested unit; a mechanical revert conflicts inside dispatch ordering and is not a safe thinning edit. |
| Cron | Upstream now owns atomic heartbeat writes, catch-up counters, and wedged one-shot diagnostics in `cron/jobs.py`; all were restored here. Upstream also owns generic delivery resolution and active cron visibility. | Probe-generation compare-and-set/ACK proof; profile-pinned subprocess and Hermes-home overrides; mem0 harvest exception; cron-to-gateway active-agent persistence; internal BlueBubbles delivery prohibition; guest/proactive runners. | Removed the duplicated/reverted `cron/jobs.py` recovery implementation, leaving only the 41-line probe-generation delta there. Retain scheduler residuals because upstream has no probe binding, delivery-profile, mem0-harvest, or guest/proactive policy equivalent. |
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

## Poke/Guest de-carry — Checkpoint 3 residual ownership (2026-08-28)

Checkpoint 3 changed *ownership*, not file location. The legacy in-core
implementation is still present and is still the default owner; a new generic
selector (`gateway/conversation_ownership.py`) picks exactly one owner per
domain per profile, so rollback is a configuration switch plus a safe restart
rather than a revert. Deletion is Checkpoint 4.

| Surface | Residual category | Current owner | Update-conflict effect |
| --- | --- | --- | --- |
| `gateway/conversation_ownership.py` | new generic core seam | core (provider-neutral) | New file; no upstream counterpart, so no merge conflict. Contains no product policy — a test asserts no `poke`/`guest` identifier appears in it. |
| `gateway/run.py` ownership gates | generic core seam wired at legacy call sites | core | Small additive guards at four sites (ingress, extraction, proactive watcher, startup activation). Conflict surface is narrow but sits inside the already-hot `run.py` region. |
| `model_tools.py` legacy tool guard | temporary policy leaf, now gated | core legacy owner, stands down under a bound policy token | One added conditional around the pre-existing guard. Removed entirely in Checkpoint 4. |
| `gateway/guest_access.py`, `gateway/proactive_*.py`, `gateway/contact_memory/**`, `gateway/conversation_texture_v2.py` | temporary policy leaves | core legacy owner, retained for rollback | Unchanged from Checkpoint 2. Full carry cost persists until Checkpoint 4 deletion; this is the deliberate price of a config-only rollback. |
| Poke policy/settings/preflight/authoritative extension | plugin-owned | user-plugin repository | Zero core conflict surface. |
| Durable databases (`state.db`, `contact-memory/**`, ownership registry) | adopted in place | unchanged on disk | No migration, no copy, no schema change. Preflight opens `mode=ro` only. |

Net effect on the thinning baseline: Checkpoint 3 *adds* a small generic core
seam and does not yet remove any leaf. The carry reduction lands in Checkpoint
4, when the gated legacy leaves are deleted and only the generic seams remain.

