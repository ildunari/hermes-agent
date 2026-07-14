# Poke/Guest Proactive Production Rollout Extension

Status: implementation-ready rollout plan. Phases 1–4 of [`poke-proactive-layer.md`](poke-proactive-layer.md) are present through `d560686eb`, but live delivery is still structurally impossible and neither live profile has an `agent.proactive` block.

Authorization: on 2026-07-13 Kosta explicitly authorized same-day activation of both Poke and Guest after the tests and reviews in this plan pass. This supersedes the earlier one-week dry-run waiting period; it does **not** waive any test, ownership, backup, review, P0/P1-repair, or smoke-test gate. No live profile state is changed by this planning commit.

## 1. Verified baseline and implications

The implementation currently has the right safety skeleton but is a dead production system:

- `gateway/proactive_scheduler.py` persists per-profile contacts, inbound IDs, slots, claims, actions, caps, one-strike state, 24-hour ignored outcomes, and a cross-profile ownership registry. `ProactiveConfig.from_mapping()` forces `dry_run=True`; `validate()` rejects false; `complete_claim()` can only write `dry_run` or `suppressed`.
- `gateway/proactive_fetch.py` implements bounded last30days research, a strict ≤500-character candidate, novelty/concreteness/freshness/sensitivity gates, inert-data prompt boundaries, style rejection, and suppression metrics. `DRY_RUN_ONLY=True` and `deliver_with_hard_gate()` discard the adapter and payload.
- `gateway/run.py::_proactive_scheduler_watcher()` wakes every 30 minutes, but passes `_proactive_dry_run_generate`, no gate verdict, no real compose callback, no ordinary-web fallback, and no delivery adapter. Consequently a normal production tick cannot pass the model gate or transport. Its broad exception handler can leave one broken profile obscuring the other until the next interval.
- `gateway/proactive_checkin.py::run_proactive_child_turn()` correctly creates an assistant-first child with parent lineage, stable cached system text, and no synthetic user row. Production compose must retain that invariant.
- `gateway/platforms/bluebubbles.py::send()` is the real transport. It resolves the existing chat, returns a `SendResult`, tracks outbound GUIDs to prevent echo ingestion, and marks only pre-delivery connect failures as retryable. Timeout/transport/partial-delivery results are deliberately non-retryable.
- `gateway/run.py` authenticates and routes BlueBubbles before assigning immutable contact scope. `gateway/guest_access.py::classify_bluebubbles_route()` sends owner identities to `owner_profile` and approved Stephen messages to `guest_profile`. This is the only authority production proactive routing may reuse.
- Live configuration currently says Poke owns BlueBubbles and routes guests from `~/.hermes/profiles/guest/contacts.yaml`; Guest's BlueBubbles adapter is disabled. The registry says `owner_profile: poke`, `guest_profile: guest`, and currently maps `owner_contact_id: stephen-lucier`. That owner namespace is wrong for a sender-attributed bootstrap: Kosta-authored evidence must not be stored as Stephen. The rollout must migrate the owner binding to a distinct canonical namespace such as `kosta-owner` before bootstrap or activation.
- Both live profiles already use `gpt-5.6-sol`/`openai-codex` at low effort for conversation and local Qwen3 MLX for post-turn extraction. Neither has `agent.proactive` configured.
- `gateway/contact_memory/import_contacts.py` accepts fact JSONL only, is dry-run by default, validates the whole dossier before one transactional `import_proposals()`, and is idempotent by source ID. It does not import interest events, sender identity, or an extraction-run manifest.
- The separate local contact tool at `~/.config/hermes-state/contacts/bin/hermes-contact-profile` can read the Messages DB read-only, resolve 1:1 chats, recover `text`/`attributedBody`, retain `is_from_me`, and scan all history with `--mode rebuild --limit 0`. Its current semantic path defaults to GLM/DeepSeek and labels speakers only as `Kosta` versus `contact/handle`; it must not be used unchanged. The existing Stephen profile reports 100,455 matched messages. Production bootstrap needs an in-repo, testable, provider-explicit path with canonical sender IDs.

## 2. Binding production invariants

A gate fails if any invariant is unproven.

1. **Exactly two proactive principals.** The allowlist is `(poke, kosta-owner, owner)` and `(guest, stephen-lucier, guest)`. No Mom, other approved guest, group, forwarded request, newly observed DM, or inferred identity may register, arm, compose, or send. Unknown or conflicting routes fail closed.
2. **Poke owns transport; profiles own policy and memory.** The single running Poke gateway owns the sole BlueBubbles adapter/webhook. Owner identities resolve to Poke and `kosta-owner`; Stephen resolves through the trusted registry to Guest and `stephen-lucier`. Guest never starts a second BlueBubbles adapter. Poke may execute a Guest policy tick, but all state/model/memory paths remain explicitly rooted under the Guest profile.
3. **Sender attribution is never inferred by a model.** Apple `message.is_from_me`, matched handle, chat membership, and canonical registry mapping determine the author. Kosta-authored evidence can populate only Poke/`kosta-owner`; Stephen-authored evidence can populate only Guest/`stephen-lucier`. The opposite speaker's text may be bounded context but can never become that namespace's evidence.
4. **Full-history means no silent sampling.** For Stephen's 1:1 thread, `--limit 0`, no model-message cap, decoded-or-explicit-non-text accounting, and `represented + explicit_non_text + rejected == selected source rows`. Chunk manifests record GUID/ROWID hashes, direction counts, date range, and no-drop totals, never raw text.
5. **No GLM and no Terra.** Semantic bootstrap, merge verification, gate judgment, and both adversarial reviews use `openai-codex` + `gpt-5.6-sol` at `medium`. Runtime post-turn fact/interest extraction remains local `mlx-community/Qwen3-4B-Instruct-2507-4bit`. Final friend-like proactive compose uses `openai-codex` + `gpt-5.6-sol` at `low`. No fallback may silently select GLM, Terra, DeepSeek, or another provider; failure means suppression or a failed bootstrap batch.
6. **Code decides when; models can only reduce sends.** Models cannot create/advance slots, lower caps, bypass active hours, choose a third contact, or override cancellation. Gate and compose failures suppress.
7. **At-most-one visible delivery per slot.** A unique slot/action/attempt key is durable before transport. Re-entry after a terminal ledger row never recomposes or resends. Only a definitely pre-send, explicitly retryable failure can retry. Timeout, unknown result, any returned message ID, or partial delivery is terminal `delivery_unknown`/`partial_delivery`, never retried automatically.
8. **Cancellation is checked at the last responsible moment.** Immediately before transport, claim token, inbound version, route ownership, active hours, caps, backoff, mute/disable state, and allowlist are revalidated. An inbound during fetch/compose cancels the slot and discards output.
9. **Silence remains success.** Send rate over 40% is an alarm and closes the live-send circuit. A missing model, source, session, adapter, route, digest, or health heartbeat suppresses; it never produces filler.
10. **No prompt-cache or transcript corruption.** Parent stable system text stays byte-identical; compose guidance is execution-only; generated proactive output persists only as an assistant-first child linked to the parent. No synthetic user message is written.
11. **Private data does not sprawl.** Raw iMessage text stays in read-only source DB or a mode-0700 local staging directory outside git/profile memory; logs/review artifacts contain hashes, counts, reason codes, and bounded redacted samples only. Raw last30days material is never persisted.
12. **Configuration is the reversible live fuse.** Code supports production transport only when all of `enabled: true`, `mode: live`, exact allowlisted contact, correct profile owner, and a healthy Poke BlueBubbles adapter agree. `disabled` and `observe` modes never call transport. Invalid/missing mode defaults to `disabled`, not live.

## 3. Model and execution lanes

Add explicit, non-fallback task configuration to both profile configs; do not overload the conversation default:

```yaml
model:
  default: gpt-5.6-sol
  provider: openai-codex
  reasoning_effort: low

auxiliary:
  proactive_semantic:
    provider: openai-codex
    model: gpt-5.6-sol
    reasoning_effort: medium
    fallback: false
  proactive_gate:
    provider: openai-codex
    model: gpt-5.6-sol
    reasoning_effort: medium
    fallback: false

agent:
  contact_memory:
    extractor:
      backend: qwen3-mlx
      model: mlx-community/Qwen3-4B-Instruct-2507-4bit
  proactive:
    enabled: true
    mode: observe                 # changed to live only at Gate 7
    transport_owner_profile: poke
    alarm_sink:
      configured: false              # set true only after monitored delivery is proven
      type: operator
    allowed_contacts:
      - {profile: poke, contact_id: kosta-owner, principal: owner}
      - {profile: guest, contact_id: stephen-lucier, principal: guest}
    compose_model:
      provider: openai-codex
      model: gpt-5.6-sol
      reasoning_effort: low
      fallback: false
    gate_task: proactive_gate
    # Existing hard limits remain: 09:00–21:30 local, 2 shares/week,
    # 3 total/week, 48-hour gap, 5 inbound/14d, one-strike, 30-day backoff.
```

The implementation must validate provider/model/effort at startup and report mismatches in status. It must not accept a model name alone and inherit a different ambient provider. Maintenance taxonomy proposals use `proactive_semantic`; deterministic maintenance can still run when that task is unavailable.

## 4. Exact implementation surface

### Contact bootstrap and import

- **Create `gateway/contact_memory/imessage_bootstrap.py`**: read-only Messages access; exact 1:1 chat resolution; `text` plus recoverable `attributedBody`; canonical author enum (`kosta-owner`, `stephen-lucier`); chronological chunk stream; no-drop manifest; strict semantic output schema; subject/author validator; bounded concurrency/retries; atomic checkpoints; resumability by source/chunk hash.
- **Create `scripts/bootstrap_proactive_imessage.py`**: operator CLI with required `--source-person`, two explicit target roots/contact IDs, `--prompt-only`, `--dry-run` default, `--apply`, `--resume`, and explicit `--task proactive_semantic`. It refuses any person except Stephen Lucier for this rollout and refuses target roots outside Poke/Guest unless a future generalized mode is separately reviewed.
- **Modify `gateway/contact_memory/import_contacts.py`**: retain existing fact-only interface while adding a typed transaction API capable of importing validated `FactProposal` plus `InterestEvent` batches and a run ID. CLI apply remains all-or-nothing and dry-run first.
- **Modify `gateway/contact_memory/store.py` and `schema.py` only if needed**: add an import-run/source uniqueness table and batch API; do not weaken current fact/interest constraints. Source IDs are content-stable, e.g. `imessage:<guid>:<canonical-author>:<semantic-item-hash>`.
- **Create `tests/gateway/contact_memory/test_imessage_bootstrap.py`** and extend `test_broker_importer.py`/`test_interest_ledger.py`: mixed-direction fixtures, attributed-body/non-text rows, groups/tapbacks excluded, same text from opposite senders, no-drop accounting, wrong-subject rejection, crash/resume, idempotent reapply, no cross-profile write.

Bootstrap semantics:

1. Snapshot source metadata and resolve Stephen once; show candidate handles/date/counts for operator review without text.
2. Produce chronological chunks containing canonical sender on every row. Sol-medium extracts only claims/preferences/interests evidenced by the requested subject's own authored messages; counterpart messages are context-only.
3. Run a separate Sol-medium merge/coverage pass with disjoint prompt context. It must account for every chunk candidate and preserve contradictions over time rather than flattening them.
4. Produce two dossiers from the same source scan: Kosta-authored facts/interests for Poke `kosta-owner`; Stephen-authored facts/interests for Guest `stephen-lucier`. Shared relationship facts must still have an explicit speaker and are duplicated only when independently supported for both subjects.
5. Guest facts default to `owner_only`/quarantine. Sol review may propose `guest_ok`, but only a validated `guest_reviewed: true` decision in the reviewed apply manifest grants Stephen visibility. Kosta-only/sensitive/third-party material never enters the Guest store.
6. Dry-run manifests and independent Sol-medium semantic review must pass before `--apply`. After apply, run maintenance to fold events and generate each digest, then index approved facts.

### Production orchestration and transport

- **Modify `gateway/proactive_fetch.py`**: remove `DRY_RUN_ONLY` only after replacing it with an explicit `ProactiveMode` (`disabled|observe|live`); make preparation (`fetch → deterministic gate → Sol-medium verdict → Sol-low compose`) separate from async transport. Existing inert-data, candidate-size, style, and model-veto checks remain mandatory.
- **Create `gateway/proactive_transport.py`**: an async `BlueBubblesProactiveDelivery` that accepts only an already-authenticated `ContactRoute`, resolves the live adapter from the Poke runner, verifies `route.platform == bluebubbles`, DM, exact allowlist/profile ownership, and calls `await adapter.send(chat_id, text, metadata={...})`. It returns normalized success/message ID/retryability/partial/unknown fields and never falls back to `hermes send`, plaintext, another profile adapter, or a new chat/handle during v1.
- **Modify `gateway/proactive_scheduler.py`**: stop forcing dry-run; add mode/allowlist validation, attempt count, `not_before`, terminal delivery state, transport message ID, last error class, and one unique delivery attempt per slot. Separate “prepared” from “sent”; commit `sent` only after `SendResult.success`. Bound retries to two definitely-pre-send attempts with exponential delay (for example 5 and 20 minutes), still subject to active hours and claim/cancellation checks. Add circuit states/cooldowns described below.
- **Modify `gateway/run.py`**: make `_proactive_scheduler_watcher()` an async, per-profile-isolated orchestrator. Resolve the single Poke BlueBubbles adapter on the event loop; run blocking fetch/model/store work off-loop; return to the event loop for the final send. Replace `_proactive_dry_run_generate` with provider-explicit generation: medium gate and low compose. Supply a real ordinary-web fallback. Do not let one profile exception skip the other. Record watcher heartbeat/result every pass.
- **Keep `gateway/proactive_checkin.py` assistant-first lineage** and route check-in compose through Sol-low plus the same style/final-veto/transport/attempt protocol. Check-ins do not bypass allowlist, one-strike, caps, or delivery uncertainty rules.
- **Modify `gateway/platforms/bluebubbles.py` only if necessary** to expose a non-creating “resolve existing DM” preflight and stable delivery result classification. Proactive delivery must refuse `_create_chat_for_handle`; it targets only the already authenticated chat ID captured from ingress.
- **Modify `gateway/guest_access.py` and routing tests** so `owner_contact_id: kosta-owner` is required for owner scope while Stephen remains `contact_id: stephen-lucier` on Guest. No model or bootstrap script may rewrite these bindings.

### Observability, diagnostics, and installation

- **Create `gateway/proactive_status.py`**: text-free health snapshot from profile state/ledgers: watcher heartbeat/age, mode, model-lane match, transport owner/adapter readiness, allowlist registration, recent authenticated inbound count, extraction queue health, interest/unfolded/eligible counts, digest presence/age, slots by state, lease age, attempts/retries, suppression reasons/rate, sends/outcomes, last success/error, circuit state, and cron freshness.
- **Create `scripts/proactive_status.py`**: `--profile poke|guest`, `--json`, and `--fail-on-dead`; never print contact text/candidate prose. Exit nonzero for dead-system conditions.
- **Create `scripts/install_proactive_rollout_cron.py`**: idempotently install/reconcile, per profile, the existing interest-maintenance no-agent job and a silent health watchdog job. The watchdog runs every 30 minutes and emits only on dead/alarm state. It does not perform transport; the Poke gateway watcher remains the only transport-capable scheduler.
- **Extend `scripts/install_contact_memory_maintenance_cron.py`** only if needed to select `proactive_semantic` instead of the generic `monitor` task. Installed runners remain pinned to explicit profile roots.
- **Create `tests/gateway/test_proactive_status.py` and `test_proactive_rollout_cron.py`**.

Dead-system conditions must include: no watcher heartbeat for >65 minutes while enabled; BlueBubbles ingress continues but proactive inbound version/count does not move; eligible interests exist but no slot planning attempt for >2 ticks; claimed lease older than configured lease; maintenance cron stale for >2 schedules; extraction queue permanently full/worker dead; model lane mismatch/unavailable; adapter unavailable; cross-profile ownership conflict; send-rate circuit open; repeated pre-send failure exhaustion. Every tick logs one structured summary with profile, counts, reason codes, duration, and correlation ID—never message/candidate text.

## 5. Bounded retries, cooldowns, anti-loop, and anti-sprawl controls

Keep all existing caps and add these production controls:

- **Retry budget:** maximum two retries after the first attempt, only when `SendResult.retryable` is true and `delivered_chunks == 0`; 5/20-minute backoff with deterministic jitter. Recheck active hours; spill into next active window. Model/fetch retries are at most one per stage inside one claim, then suppression.
- **Unknown-delivery rule:** timeout, process interruption around send, malformed response, partial delivery, or success without a safely persisted result becomes terminal `delivery_unknown`; no automatic retry. Operator can inspect, never “repair” by resending.
- **Per-contact circuit:** three consecutive ignored/dismissed outcomes preserve the existing 30-day backoff. One transport failure exhaustion pauses that contact 24 hours. Ownership/model/adapter invariant failures open the profile circuit until a healthy status pass plus explicit config re-enable.
- **Global circuit:** any trailing send rate >40%, >1 visible send for one slot, ownership conflict, or non-allowlisted attempt atomically changes mode to an effective disabled circuit in state (without rewriting config) and alarms. Only an operator reset command can close it.
- **One-strike and inbound cancellation:** no second proactive message until a real inbound follows an unanswered send; any newer inbound invalidates prepared content and the claim immediately.
- **No catch-up bursts:** stale slots >24 hours die; restart processes at most one due slot per contact and never sends more than one proactive message globally in a five-minute window. Missed ticks are not replayed.
- **State bounds:** retain terminal actions for audit, but prune raw prepared payloads after 30 days, keep only hashes/reason codes/message IDs; cap pending/prepared rows per contact; cap live interests at 40 and digest at 300 tokens. Never persist raw research.
- **No self-trigger loop:** outbound GUIDs and proactive child sessions are excluded from inbound extraction/scheduling. Watchdog/cron output uses local delivery only and cannot create a chat message. Cron jobs may not contain gateway lifecycle commands.

## 6. Phased implementation and gates

Each phase is one logical commit. At every code gate run touched tests, broader proactive/contact-memory tests, `py_compile`, and `git diff --check`. Do not edit live state until Phase 4.

### Phase 0 — Freeze identifiers and write fixtures

- Document exact trusted owner identities (redacted in committed artifacts), Stephen handles, current chat IDs, Poke service label, profile homes, and current DB counts in a mode-0600 local rollout manifest.
- Decide and test canonical IDs: `kosta-owner` and `stephen-lucier`.
- Build synthetic Messages fixtures and transport result fixtures; no private transcript enters git.

**Gate 0:** routing tests prove only the two principals and Poke-only transport ownership. Any ambiguity in Stephen chat/handle or owner binding blocks work.

### Phase 1 — Sender-attributed bootstrap implementation

Implement the bootstrap/import files above. Test prompt-only full-history accounting against the local Stephen source without model calls or writes. Then run a small bounded Sol-medium synthetic semantic sample.

**Gate 1:** all no-drop, attribution, privacy, resume, and idempotency tests pass; prompt-only report shows the selected full-history count and direction totals reconcile. No GLM/Terra endpoint/model appears in config, process arguments, output manifest, or fallback trace.

### Phase 2 — Live-capable protocol, still observe-only

Implement mode parsing, durable attempts, async transport adapter, final cancellation check, retry/uncertainty rules, per-profile watcher isolation, model lanes, circuits, and status diagnostics. Keep live configs absent/disabled.

**Gate 2:** unit/integration tests prove at-most-one delivery under concurrent ticks, lease expiry, restart at every transition, inbound races, partial/timeouts, adapter failure, model failure, ownership conflict, and nonallowlisted contacts. Prompt-cache and assistant-first tests remain green.

### Phase 3 — Evals and dual adversarial review

Run deterministic and model-backed evals before any live profile write:

- sender-attribution precision/recall and deliberate cross-speaker traps;
- full-history chunk/merge coverage and contradiction retention;
- Guest audience/privacy leakage cases;
- gate suppression, prompt injection, novelty, sensitivity, stale news, forced/SKIP compose;
- style blind A/B for Sol-low compose using the texture harness;
- scheduler caps, one-strike, cooldowns, active hours/DST, cancellation, retries, circuits;
- dead-system diagnostics: kill/fake-stall each worker/model/adapter/ticker/cron and require a nonzero status with a useful reason.

Commission **two independent** GPT-5.6 Sol reviewers, both at medium effort and neither sharing the implementation context or each other's report. Give each the plan, full branch diff from `d560686eb`, test/eval evidence, and read/bash-only access. Reviewer A focuses on privacy/attribution/routing/exactly-once transport; Reviewer B focuses on dead-system behavior/restart races/retries/caps/model-lane enforcement/operations. Both report P0–P3 with file/line evidence.

**Gate 3:** repair every P0/P1 from either review, rerun affected/full suites, and return the repair diff to both reviewers independently. Gate closes only when both explicitly report no remaining P0/P1. P2/P3 must be dispositioned in the rollout record, not silently ignored.

### Phase 4 — Backup, profile configuration, and bootstrap apply

From an external shell, stop short of restart and make a timestamped mode-0600 backup of:

- `~/.hermes/profiles/poke/config.yaml`, `state.db*`, `contact-memory/`, and cron store;
- `~/.hermes/profiles/guest/config.yaml`, `contacts.yaml`, `state.db*`, `contact-memory/`, and cron store;
- `~/.hermes/proactive-contact-ownership.db*`;
- Poke launchd plist/service metadata.

First change the trusted owner binding to `kosta-owner`; validate routing before importing. Run prompt-only, dry-run, semantic extraction/merge, and independent Sol-medium dossier review. Apply Kosta and Stephen dossiers transactionally to their separate roots. Run deterministic maintenance, Sol-medium taxonomy maintenance, fact indexing, and text-free self-checks.

**Gate 4:** backup restore is rehearsed into temporary profile roots; post-import source IDs/counts reconcile; Poke has no Stephen-authored evidence, Guest has no Kosta-authored evidence, and Guest visibility has no unreviewed/sensitive/third-party facts. Digests exist and are bounded. No proactive send is yet possible (`mode: observe`).

### Phase 5 — Install configuration and cron in observe mode

Install identical policy limits but profile-specific identity/principal. Poke config owns BlueBubbles; Guest config leaves BlueBubbles disabled. Reconcile maintenance and watchdog cron jobs for both profiles. Run the watcher/tick manually with fake transport and then with observe mode against real state; observe mode may fetch/gate/compose but must not call `adapter.send`.

**Gate 5:** two consecutive ticks for each profile produce fresh heartbeats and plausible status. Cron list has exactly one enabled maintenance and one watchdog job per profile. The dead-system probe detects a deliberately stale heartbeat and then clears after recovery. Observe logs show only allowlisted contacts and no raw text.

### Phase 6 — Detached Poke gateway restart and post-restart checks

The active service is Poke only. Restart from a shell outside the gateway so the command survives gateway teardown; do not start Guest separately. Discoverable commands are:

```bash
hermes --profile poke gateway status --deep --full
hermes --profile poke gateway install --force --no-start-now   # only if service definition changed
hermes --profile poke gateway restart
hermes --profile poke gateway status --deep --full
```

On macOS the expected profile-scoped launchd label is `ai.hermes.gateway-poke`; `hermes --profile poke gateway restart` is preferred because it performs the supervised/detached restart and readiness checks. If chat-originated restart is deliberately used, `/restart` selects service restart under launchd and the detached helper only when unsupervised. Never put restart in cron.

**Gate 6:** new PID, deep status healthy, exactly one Poke BlueBubbles webhook owner, no Guest gateway/adapter, both profile watchers heartbeat through the Poke process, cron healthy, ownership registry conflict-free, reactive owner and Stephen replies still route to the correct profiles, and no proactive transport occurred.

### Phase 7 — Same-day live activation and smoke tests

Kosta's explicit authorization permits changing both profiles from `observe` to `live` on the same day after Gates 0–6. Activation remains staged by contact:

1. Set Poke `kosta-owner` live while Guest remains observe. Use an operator-only smoke command that creates one specially tagged, immediate test slot but still passes allowlist, active hours, claim, cancellation, Sol-medium gate, Sol-low compose, style filter, and real BlueBubbles adapter. Kosta confirms exactly one visible message and correct Poke child lineage.
2. Verify transport message ID, `sent` action, contact ledger, suppression/send metrics, outbound echo exclusion, and one-strike. Inject an inbound and verify outcome closure/cancellation.
3. Set Guest `stephen-lucier` live. Use the same test path only when Stephen's existing DM route/session is healthy. Confirm delivery is from Poke's adapter but state/memory/model context are Guest-rooted. Never create a new chat.
4. Run a negative smoke for Mom/unknown/group and require deterministic refusal before model or transport.

**Gate 7:** Kosta visually confirms both intended live messages and no duplicate/misroute; text-free status is healthy; send rate circuit remains closed; logs contain correlation/message IDs but no private text. If Stephen is unavailable for immediate visual confirmation, Guest stays observe—same-day authorization allows activation but does not convert uncertainty into success.

### Phase 8 — Same-day watch and closeout

For the rest of the activation day, inspect status after every tick and at least hourly. Verify no catch-up bursts, no unexpected contacts, suppression reasons, cron freshness, extraction/maintenance progress, and absence of repeated transport errors. Save a redacted rollout report with commands, commit SHAs, test summaries, review dispositions, backup path/checksum, restart PID, smoke IDs, and final modes.

**Gate 8:** at least two healthy production ticks after each live activation; no P0/P1 operational finding; rollback remains available. Only then mark rollout complete.

## 7. Verification commands

Use the checkout's shared test runner/venv resolution where possible:

```bash
cd /Users/Kosta/LocalDev/.worktrees/hermes-agent/poke-proactive-layer

python -m pytest -q \
  tests/gateway/contact_memory/test_imessage_bootstrap.py \
  tests/gateway/contact_memory/test_broker_importer.py \
  tests/gateway/test_interest_ledger.py \
  tests/gateway/test_interest_maintenance.py \
  tests/gateway/test_proactive_scheduler.py \
  tests/gateway/test_proactive_gate.py \
  tests/gateway/test_proactive_initiated_turn.py \
  tests/gateway/test_proactive_status.py \
  tests/gateway/test_proactive_rollout_cron.py \
  tests/gateway/test_bluebubbles_guest_policy.py \
  tests/gateway/test_bluebubbles.py

python -m py_compile \
  gateway/contact_memory/imessage_bootstrap.py \
  gateway/contact_memory/import_contacts.py \
  gateway/proactive_checkin.py gateway/proactive_fetch.py \
  gateway/proactive_scheduler.py gateway/proactive_transport.py \
  gateway/proactive_status.py gateway/run.py \
  scripts/bootstrap_proactive_imessage.py scripts/proactive_status.py \
  scripts/install_proactive_rollout_cron.py

git diff --check
```

Operational commands, dry-run first:

```bash
# Full-history no-model accounting; writes only the protected staging manifest.
python scripts/bootstrap_proactive_imessage.py \
  --source-person "Stephen Lucier" --limit 0 --prompt-only \
  --poke-contact-id kosta-owner --guest-contact-id stephen-lucier

# Semantic dry-run: provider/model are resolved only through proactive_semantic.
python scripts/bootstrap_proactive_imessage.py \
  --source-person "Stephen Lucier" --limit 0 --dry-run \
  --task proactive_semantic \
  --poke-contact-id kosta-owner --guest-contact-id stephen-lucier

# Existing importer remains dry-run by default for generated dossiers.
python -m gateway.contact_memory.import_contacts /secure/staging/kosta.jsonl \
  --contact-id kosta-owner --root ~/.hermes/profiles/poke/contact-memory \
  --manifest /secure/staging/kosta-import-review.json
python -m gateway.contact_memory.import_contacts /secure/staging/stephen.jsonl \
  --contact-id stephen-lucier --root ~/.hermes/profiles/guest/contact-memory \
  --manifest /secure/staging/stephen-import-review.json

# Apply only after Gate 3 semantic review and backup.
python scripts/bootstrap_proactive_imessage.py ... --apply --review-manifest /secure/staging/approved.json

python scripts/install_contact_memory_maintenance_cron.py --profile poke --task proactive_semantic --dry-run
python scripts/install_contact_memory_maintenance_cron.py --profile guest --task proactive_semantic --dry-run
python scripts/install_proactive_rollout_cron.py --profile poke --dry-run
python scripts/install_proactive_rollout_cron.py --profile guest --dry-run

python scripts/proactive_status.py --profile poke --json --fail-on-dead
python scripts/proactive_status.py --profile guest --json --fail-on-dead
hermes --profile poke cron list
hermes --profile guest cron list
hermes --profile poke gateway status --deep --full
```

Ellipses in the apply example deliberately mean “reuse the reviewed exact arguments recorded in the protected manifest”; the implementation must print a copy/paste-safe fully expanded apply command after a successful dry-run.

## 8. Backup, rollback, and kill switches

Rollback is configuration-first and must not depend on broken model/transport code:

1. Set both profiles' `agent.proactive.mode: disabled` (or `enabled: false`) and run the text-free status command; the next loop must observe the fuse without sending.
2. Pause both proactive watchdog/maintenance jobs if they are implicated; never delete audit state during incident response.
3. From an external shell, run `hermes --profile poke gateway restart`, then `status --deep --full`. Do not start Guest.
4. If code rollback is needed, deploy the pre-rollout commit and restart Poke. Additive DB columns/tables may remain; old code must ignore them.
5. If state/config rollback is needed, stop Poke, restore the timestamped Poke/Guest config, `state.db` plus WAL/SHM as a consistent set, contact-memory directories, ownership DB, cron stores, and service plist; then start Poke and verify reactive routing before re-enabling anything.
6. If bootstrap attribution is wrong, disable proactive, restore the pre-bootstrap contact-memory backups rather than deleting individual rows by guesswork, then rerun corrected dry-run/review.

Immediate rollback triggers: duplicate visible send; any nonallowlisted/group send attempt; Poke/Guest cross-routing or cross-memory evidence; hidden provider fallback to GLM/Terra; raw transcript in logs; send-rate >40%; unknown delivery followed by retry; stale/dead status without alarm; reactive BlueBubbles regression. Preserve logs/DB copies before repair.

## 9. Definition of done

The rollout is complete only when all phases close, two independent Sol-medium reviews have no outstanding P0/P1, sender-attributed full history is reconciled and imported only for Kosta owner and Stephen, Poke is the sole BlueBubbles owner, Guest is policy/memory owner for Stephen, runtime extraction is local Qwen, semantic work/reviews are Sol-medium, final compose is Sol-low, cron/status diagnostics are healthy, the detached Poke restart is verified, both authorized live smoke tests produce exactly one correctly routed message, and backup/rollback evidence is recorded.
