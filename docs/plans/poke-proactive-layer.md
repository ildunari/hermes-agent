# Implementation Plan: Poke Proactive Layer (Interest Ledger + Proactive Sends)

Status: PLAN — approved for handoff to implementation lane. Do not restart the live gateway without Kosta's approval at any point.
Scope: `~/.hermes/hermes-agent` (branch work in a separate worktree off `local/studio-slim`; the live gateway runs from this checkout) + profile config for `poke` and `guest`.
Author: Claude Fable 5 (coding profile), 2026-07-13. Research inputs: production-proactive-agent survey (Poke/Meta/Replika/C.AI/Pi/OpenClaw heartbeat) and interest-modeling literature survey (half-life decay, Letta sleep-time compute, Zep bitemporal, bandit exploration). Key numbers cited inline.

## Overview

Turn the Poke/Guest iMessage agent from purely reactive into selectively proactive: an offline per-contact interest ledger learned from conversation, a code-owned scheduler that decides WHEN a proactive send may happen, a background content-fetch delegate (last30days + web) that produces a concrete candidate, a silence-default gate that kills most candidates, and delivery through the existing texture-v2 persona pipeline so proactive sends read like a friend texting, not a report.

Non-goals: no fine-tuning, no changes to the reactive reply path's latency, no new model tools in the core toolset, no cross-contact data flow. The ledger and all fetch machinery stay OUT of the conversation session context; only a ≤300-token digest and per-send ephemeral blocks ever cross into model context.

## Design principles (binding — reviewers should reject violations)

1. **Silence is the default and a success.** Asymmetry rule, verbatim in the gate prompt: "a skipped ping is a small miss; a bad ping gets you muted." Most fired scheduler slots MUST end in no message. Instrument this: if >40% of fired slots result in a send, the gate is broken.
2. **Code decides WHEN, model decides WORDS, texture constrains them.** Same contract as the ported check-in state machine. No LLM ever chooses send timing.
3. **Hot path untouched.** The reactive reply path gains zero new blocking calls. Interest extraction rides the existing async post-turn extractor. Fetch/gate/maintenance run in cron/delegate contexts.
4. **Prompt cache is sacred.** Nothing here may mutate a live session's stable system prompt. Digest injection follows the same ephemeral-suffix mechanism as `conversation_texture` (`_with_conversation_texture` in `gateway/run.py` keeps cache prompt and execution prompt separate — reuse that exact pattern).
5. **Per-contact isolation.** All ledger state lives in the existing per-contact SQLite under `profiles/<profile>/contact-memory/contacts/<hash>.sqlite3`. No shared interest state. Guest and Poke isolation semantics identical to facts.
6. **Never emotionally needy.** No "I miss you", no guilt, no FOMO hooks, no apology-for-silence openers (HBS manipulation-audit finding: effective short-term, corrosive long-term; also the kit's check-in module already bans tone-apologies). Structural ban in the gate + delivery prompts, and a deterministic post-check regex for the worst offenders.
7. **Extend, don't duplicate.** Reuse: `recall_event`/`callback_event` cooldown tables, `LaneAGate` session bookkeeping patterns, the kit's `proactive_checkin.py` state machine (extracted at `/tmp/poke-phase2-kit-review/poke-phase2-kit/runtime/proactive_checkin.py` — re-extract from `~/.hermes/desktop-attachments/poke-phase2-kit.zip` if gone), the cron scheduler's `no_agent` script jobs, and `delegate_task` for fetch.

## Current architecture (verified 2026-07-13)

- `gateway/contact_memory/schema.py` — bitemporal `fact` table (trust/confidence, audience, mention_policy), `recommendation` lifecycle table, `recall_event` + `callback_event` cooldown tables, `entity` registry. SCHEMA_VERSION = 2.
- `gateway/contact_memory/extractor.py` + `runtime.py` — async post-turn extraction via local Qwen3-4B MLX worker (bounded queue, 1 worker, retries). This is where interest-event emission hooks in.
- `gateway/contact_memory/gating.py` — `LaneAGate`: EMA topic centroid, time-gap segmentation, per-session cooldowns. Pattern to copy for scheduler state hygiene (TTL + LRU bounded dicts).
- `gateway/contact_memory/lane_b.py` — request-scoped `contact_memory_search` tool.
- `gateway/conversation_texture_v2.py` — `compile_turn_guidance(...)` per-turn ephemeral block; response classes; deterministic hash-seeded sampling. Proactive delivery must flow through this.
- `gateway/run.py` — `_compile_conversation_texture_prompt` + `_with_conversation_texture` (cache-safe ephemeral injection); contact-memory extraction submission.
- `gateway/platforms/bluebubbles.py` — `send()` with split bubbles + typing cadence; guest routing via `guest_contacts_file: /Users/Kosta/.hermes/profiles/guest/contacts.yaml`; `guest_routing_enabled: true` on the poke profile.
- `cron/scheduler.py` — profile-owned cron ticker with `no_agent` script jobs and LLM jobs. Poke profile has a live ticker (`profiles/poke/cron/`).
- Profiles: `poke` (Sol low, texture v2 on, contact_memory lanes A+B+extraction on) and `guest` (same model family; verify its `contact_memory`/texture blocks during Phase 0 — config is 1629 lines and must be diffed against poke's, not assumed).

## Data model & schema changes

Bump `SCHEMA_VERSION` to 3 with an idempotent migration (CREATE TABLE IF NOT EXISTS — the existing schema already uses this style; add version write to `schema_meta`).

### New tables (per-contact SQLite, alongside `fact`)

```sql
CREATE TABLE IF NOT EXISTS interest_event (
  event_id TEXT PRIMARY KEY,
  topic_text TEXT NOT NULL,              -- free text from extractor, e.g. "sports cars"
  signal_type TEXT NOT NULL CHECK(signal_type IN (
    'spontaneous_raise','enthusiasm','long_reply','engaged_mention',
    'neutral_ack','dismissive','explicit_negative','proactive_engaged','proactive_ignored')),
  valence TEXT NOT NULL CHECK(valence IN ('positive','negative','neutral')),
  source_id TEXT NOT NULL,               -- message/session pointer, same convention as fact.source_id
  created_at REAL NOT NULL,
  folded_at REAL                          -- NULL until a maintenance run consumes it
);
CREATE INDEX IF NOT EXISTS interest_event_unfolded ON interest_event(folded_at, created_at);

CREATE TABLE IF NOT EXISTS interest (
  interest_id TEXT PRIMARY KEY,
  topic TEXT NOT NULL,                   -- canonical label after merge, e.g. "cars"
  parent_id TEXT,                        -- NULL for top-level genre; one level of nesting only
  raw_score REAL NOT NULL DEFAULT 0,
  last_evidence_at REAL NOT NULL,
  evidence_count INTEGER NOT NULL DEFAULT 0,
  valence TEXT NOT NULL DEFAULT 'positive' CHECK(valence IN ('positive','negative','neutral')),
  half_life_days REAL NOT NULL DEFAULT 90,   -- assigned by maintenance LLM: 14 transient / 90 hobby / 365 identity
  state TEXT NOT NULL CHECK(state IN ('candidate','active','retired')) DEFAULT 'candidate',
  ts_alpha REAL NOT NULL DEFAULT 1,      -- Thompson-sampling Beta params for proactive engagement
  ts_beta REAL NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  retired_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS interest_topic_live ON interest(topic) WHERE retired_at IS NULL;

CREATE TABLE IF NOT EXISTS proactive_send (
  send_id TEXT PRIMARY KEY,
  interest_id TEXT,                      -- NULL for pure check-ins
  kind TEXT NOT NULL CHECK(kind IN ('interest_share','checkin','exploration')),
  candidate_json TEXT NOT NULL,          -- the gated payload {topic, concrete_item, why_now, link}
  gate_decision TEXT NOT NULL CHECK(gate_decision IN ('sent','suppressed')),
  gate_reason TEXT NOT NULL,
  sent_at REAL,
  outcome TEXT CHECK(outcome IN ('engaged','acknowledged','ignored','dismissed', NULL)),
  outcome_at REAL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS proactive_send_recent ON proactive_send(sent_at, gate_decision);
```

Effective score is ALWAYS computed at read time: `raw_score * 2^(-(now - last_evidence_at)/86400/half_life_days)`. No decay cron exists. Store nothing derived.

### Interest digest (file, not table)

Maintenance writes `profiles/<profile>/contact-memory/digests/<contact_hash>.md`, ≤300 tokens, structured: 3-6 active interests with one-line flavor each ("cars — especially dark green/military green paint, sports builds"), explicit disinterests ("do not bring up: X"), and last-2-proactive-sends summary. This file is the ONLY ledger artifact that ever enters model context.

## Signal weights (initial values; tune in Phase 5 eval)

| Signal | Δraw_score | Notes |
|---|---|---|
| spontaneous_raise | +1.0 | contact introduces topic unprompted |
| enthusiasm | +0.8 | exclamations, follow-up questions, "omg", caps |
| long_reply | +0.6 | reply > 2× that contact's rolling median length (per-contact normalization is mandatory — verbosity baselines differ wildly) |
| engaged_mention | +0.4 | topic raised by agent, contact engages |
| neutral_ack | +0.1 | |
| proactive_engaged | +1.2 and ts_alpha+=1 | reply >5 words w/ topic continuation to a proactive send |
| proactive_ignored | −0.3 and ts_beta+=1 | no reply within 24h |
| dismissive | −0.8 and ts_beta+=1 | "meh", one-word reply, immediate subject change |
| explicit_negative | −2.0, valence=negative | "i don't care about X" — permanent block, never expires, never re-suggested |

Promotion: candidate→active at effective score ≥1.5 **and** ≥2 evidence events on separate days (kills one-off pollution). Retire: effective <0.2 AND no evidence in 2× half-life. Proactive eligibility: active, positive valence, effective ≥2.0.

## Component 1 — Interest event emission (extractor extension)

File: `gateway/contact_memory/extractor.py` (+ the Qwen3 worker prompt).
The post-turn extraction prompt gains one additional output array: `interest_events: [{topic, signal_type, valence}]` alongside fact proposals. Store writes them to `interest_event`. Zero new inference calls — same batch, same worker, same queue.

Pitfalls to enforce in review:
- The extractor must NOT emit interest events for the agent's own messages, only the contact's.
- Topic strings must be lowercase noun phrases ≤4 words; the extractor prompt shows 3 examples. Garbage-in here poisons the taxonomy.
- Extraction failure must remain non-fatal exactly as today (advisory pipeline).
- `long_reply` is computed deterministically in Python (rolling median per contact, persisted in `schema_meta` or computed from recent events), NOT by the LLM.

## Component 2 — Maintenance job (the ledger brain, offline)

New file: `gateway/contact_memory/interest_maintenance.py` + a profile cron job (LLM job, cheap model — gemini-3.6-flash-high via vibeproxy or the local Qwen worker; NOT the main Sol lane). Trigger: per contact, ≥20 unfolded events OR 7 days since last run, whichever first. Letta sleep-time pattern.

Steps per run (single batched LLM call per contact, deterministic pre/post processing around it):
1. Deterministic: fold unfolded `interest_event` rows into raw_score deltas per topic string; mark `folded_at`.
2. LLM (strict JSON out, schema-validated): given current interest rows + new topic strings → merge map (embedding similarity >0.85 via existing embeddinggemma worker as a pre-filter, LLM confirms), split directives (node with ≥3 distinct sub-themes → children, e.g. cars → sports cars / paint colors), half_life_days class per topic {14, 90, 365}, and the ≤300-token digest text.
3. Deterministic: apply merges (score transfer at 0.5 discount), splits, promotions, retirements; write digest file atomically (tmp+rename).

Pitfalls:
- LLM output is a PROPOSAL; code validates every referenced interest_id exists, rejects merges across valence polarity, caps taxonomy at 2 levels and 40 live topics per contact (prune lowest effective score beyond cap).
- Never let the maintenance model see raw message text — events only. Privacy boundary: it sees topic strings and signal types, nothing else.
- Idempotency: rerunning after a crash must not double-fold (folded_at guard).
- Digest regeneration invalidates NO cache: it is only read at session start / proactive compose, never mid-conversation. If a session is live when the digest changes, the old digest stays for that session's lifetime (accept staleness; do not hot-swap).

## Component 3 — Scheduler (code decides WHEN)

New file: `gateway/proactive_scheduler.py`, driven by the existing profile cron ticker (a `no_agent`-style tick every 30 min is fine; the tick itself is pure Python — no LLM). Port `/tmp/poke-phase2-kit-review/.../proactive_checkin.py` first and extend it; its state machine (plan → armed → fired/cancelled, quiet hours push-to-morning with jitter, cancel-on-user-message, stale plans die silently) is the skeleton.

Hard rules (all deterministic, all config-surfaced under `agent.proactive` in profile config):
- **Active hours** 09:00–21:30 contact-local (config; default inside Kosta's stated 8am–10pm), quiet-hour spillover pushed to next morning with ±45 min jitter.
- **Eligibility**: contact sent ≥5 messages in trailing 14 days (Meta's confirmed gate). New contacts get nothing until they cross it.
- **Frequency cap**: ≤2 interest-share sends per contact per week, ≤3 proactive messages of any kind (shares + check-ins) per week, minimum 48h between proactive sends. Jittered slot sampling, never fixed times — no "every Tuesday 10am" smell.
- **One-strike**: a proactive send that gets no reply is NEVER followed by another unanswered proactive send; require an inbound contact message before the next slot arms.
- **Backoff**: 3 consecutive `dismissive`/`ignored` outcomes → proactive lane disabled for that contact for 30 days (config), and the maintenance digest notes it.
- **Cancel-on-inbound**: any user message cancels pending armed slots (kit behavior, keep it).
- **Topic pick**: Thompson sampling over eligible interests using ts_alpha/ts_beta; with probability ε = max(0.1, 0.5 × 0.97^total_sends) pick an exploration candidate instead — a SIBLING topic adjacent to a high-score interest (same parent, or parent's other children), never negative valence, and exploration capped at 1 in 4 sends.
- Serious-mode suppression: if the last conversation ended in serious register (texture v2 already computes this), interest-share sends are blocked for 72h; only the check-in lane (already designed in the kit: one check-in 3-6h later) may fire.

State: per-contact rows in the profile-level `state.db` (scheduler is gateway-process-adjacent; do NOT put scheduler state in the contact sqlite, which the broker owns). TTL/LRU bounded like `LaneAGate`.

## Component 4 — Fetch + gate (background delegate, decide ≠ write)

When a slot fires with a chosen topic, the cron job (LLM job on a cheap-but-competent model — claude-sonnet-5 or gemini-3.1-pro via vibeproxy; NOT the live Poke session) runs a two-step, fully isolated from any conversation context:

1. **Fetch**: run last30days (skill already installed; it needs its env keys — verify with its doctor in Phase 0) scoped to the topic, plus ordinary web search fallback if last30days sources are thin for that topic. Produce ONE candidate: `{topic, concrete_item, why_now, source_url, freshness_ts, optional_image_url}`. "Concrete" means a named, dated thing (album dropped Friday, spec sheet published, trailer released) — not vibes.
2. **Gate** (same call or second cheap call, strict JSON verdict): checks, in order — novelty (query the contact's fact store + `proactive_send.candidate_json` history for prior discussion of this exact item; already-discussed = suppress), concreteness, interest fit (effective score, valence), staleness (item older than 10 days = suppress for transient topics), sensitivity (topic intersects health/relationship/money facts = suppress), and the final verbatim test: "Would this contact be genuinely glad their phone buzzed for this? If it is a maybe, the answer is no." Log every suppression with reason to `proactive_send` (gate_decision='suppressed').

Pitfall: the fetch output can be large; it lives and dies in the delegate context. Only the ≤500-char candidate JSON crosses into the send pipeline. Never attach last30days raw output anywhere persistent.

## Component 5 — Delivery (persona writes the WORDS)

A gated candidate is delivered by injecting a purpose-built ephemeral block into the contact's Poke/Guest session and letting the normal agent turn produce the bubbles over the existing BlueBubbles send path (split bubbles + typing cadence apply automatically):

```
<proactive_send private="true">
You saw this and thought of them: {concrete_item} ({source_url}).
Share it the way a friend texts a link they just saw. 1-2 bubbles max.
Do not open with an apology, a greeting ritual, or "thought of you".
Do not summarize like a report. Hot take or one-liner is fine. Attaching the link/image is fine.
If including it would feel forced right now, output exactly SKIP_PROACTIVE and nothing else.
</proactive_send>
```

The model retains a final veto (SKIP_PROACTIVE → record suppressed, reason='model_veto'). Texture v2 wraps this turn with `register=casual`, forced `response_class` ∈ {reaction, plain}, craft ineligible — proactive sends must be structurally boring-capable.

Mechanics to verify during implementation (reviewer: demand evidence): how the gateway initiates an agent turn with no inbound message for a given session (the check-in kit assumed this exists; confirm the actual entry point in `gateway/run.py`/session machinery, likely the same path cron delivery uses for `deliver='platform:chat_id'`). If no clean entry point exists, building one is in-scope for Phase 3 and must preserve session lineage + role alternation (no synthetic user messages — use the ephemeral/system-side mechanism).

**Outcome tracking**: the next inbound message from the contact within 24h is classified (deterministic first: reply length, topic-word overlap; extractor confirms valence async) → `proactive_send.outcome` + a `proactive_engaged`/`proactive_ignored` interest event. This closes the bandit loop.

**Reactive enrichment** (the "dark green car" case) is NOT a new component: the digest (injected once per session start as part of contact context) plus existing Lane A/B retrieval already give the model what it needs to append "also they have a sick dark green" — verify with an eval scenario, don't build machinery.

## Poke + Guest applicability

Both profiles get identical code; enablement is per-profile config:

```yaml
agent:
  proactive:
    enabled: true
    transport: bluebubbles           # iMessage is the primary lane per contact
    active_hours: {start: "09:00", end: "21:30"}
    weekly_interest_cap: 2
    weekly_total_cap: 3
    min_gap_hours: 48
    eligibility_min_messages_14d: 5
    backoff_after_dismissals: 3
    backoff_days: 30
    exploration_floor: 0.10
    fetch_model: {provider: vibeproxy, model: claude-sonnet-5}
    maintenance_model: {provider: vibeproxy, model: gemini-3.6-flash-high}
```

Guest specifics: guest routing already maps senders → contacts via `guest_contacts_file`; the scheduler must resolve the SAME contact hash the memory broker uses so ledger and sends line up (single source of truth: reuse the broker's contact-id derivation, do not re-derive). Group chats: proactive sends are DM-only in v1 — never initiate into a group. Guest-profile audience rules apply unchanged: candidates may only reference `guest_ok`/`public` facts; the gate runs with the same `RetrievalScope` the guest lane uses. Rollout order: Poke first (Kosta's own threads), Guest only after Gate C.

## Edge cases

| Edge case | Handling |
|---|---|
| Contact messages while a slot is armed/composing | Cancel slot; the fetch result may be reused reactively within 24h if topical, else discarded |
| Two profiles (poke + guest) both proactive toward the same contact | Must not happen: a contact is owned by exactly one profile's routing; assert at scheduler init from guest_contacts + poke routing, refuse to arm on conflict |
| last30days keys missing / sources down | Fetch job degrades to web search; if that's thin, suppress (reason='no_material'), never send filler |
| Gateway restart with armed slots | Slots persist in state.db with absolute fire times; stale (>24h past) slots die silently on load (kit behavior) |
| Contact in a different timezone | Per-contact timezone field in contacts.yaml, default profile timezone; active hours computed contact-local |
| Sensitive adjacency (interest "cars" but recent fact "totaled the car last week") | Gate sensitivity check queries recent facts for negative-event adjacency to the topic; suppress on hit |
| The model ignores SKIP semantics and writes an apology-opener anyway | Deterministic post-filter on the outbound text: banned-opener regex (apology/greeting-ritual/"thought of you"/"miss you") → drop send, log 'style_reject'; never regex-rewrite |
| Duplicate item across weeks (same album news resurfaces) | Novelty check hashes concrete_item against prior candidate_json rows (sent AND suppressed) |
| DB migration on live profile | Migration is CREATE-IF-NOT-EXISTS additive only; run on broker open as today; no destructive change to fact tables |
| Extractor emits junk topics ("the", "stuff") | Stopword/length validation in store write; maintenance merge pass is second defense; cap of 40 live topics is the backstop |

## File-by-file changes

| File | Action | Changes |
|---|---|---|
| `gateway/contact_memory/schema.py` | Modify | SCHEMA_VERSION=3; add `interest_event`, `interest`, `proactive_send` tables + dataclasses |
| `gateway/contact_memory/store.py` | Modify | Write/read APIs for events, interests (lazy-decay read), proactive_send log; rolling median helper |
| `gateway/contact_memory/extractor.py` (+ worker prompt) | Modify | Emit `interest_events` array; validation; store write |
| `gateway/contact_memory/interest_maintenance.py` | Create | Fold/merge/split/retire + digest writer; CLI entry for cron |
| `gateway/proactive_checkin.py` | Create (port) | Kit state machine, adapted to gateway session/state.db |
| `gateway/proactive_scheduler.py` | Create | Slots, caps, eligibility, backoff, bandit topic pick, contact-hash resolution |
| `gateway/proactive_fetch.py` | Create | Fetch+gate job body (invoked by cron LLM job / delegate); candidate + verdict schemas |
| `gateway/run.py` | Modify | Digest injection at session context assembly (ephemeral, cache-safe); proactive-turn entry point; outcome classification hook on inbound |
| `gateway/conversation_texture_v2.py` | Modify | Accept forced response_class for proactive turns (small, additive) |
| `gateway/platforms/bluebubbles.py` | Probably none | send() reused as-is; verify image/link attach path |
| `tests/gateway/test_interest_ledger.py` | Create | Decay math, promotion/retire thresholds, valence blocks, median normalization, migration idempotency |
| `tests/gateway/test_proactive_scheduler.py` | Create | Caps, one-strike, backoff, quiet hours + jitter bounds, cancel-on-inbound, restart persistence, conflict refusal |
| `tests/gateway/test_proactive_gate.py` | Create | Novelty/sensitivity/staleness suppressions; banned-opener post-filter; SKIP_PROACTIVE handling |
| `tests/gateway/test_interest_maintenance.py` | Create | Fold idempotency, merge validation, taxonomy caps, digest size bound |
| profile configs (`poke`, `guest`) | Modify | `agent.proactive` block; guest AFTER Gate C only |

## Implementation order & adversarial review gates

Per Kosta's standard flow: each gate is an adversarial diff-based review using independent GPT-5.6 Sol subagents at medium reasoning effort by default, unless Kosta explicitly requests another model. Use distinct implementation and review contexts so the implementing subagent never reviews its own work. Reviews report P0–P3 findings, and P0/P1 must be fixed before the phase closes. Reviewers get the diff file, this plan, and read/bash-only tools. Every phase ends with `pytest -q` on touched test files + `py_compile` on touched modules + `git diff --check`, evidence pasted into the phase report.

**Phase 0 — Preflight (no code)**: diff guest vs poke config for contact_memory/texture parity; confirm last30days doctor passes with available keys; locate the exact no-inbound-turn entry point in gateway internals; re-extract kit if /tmp is gone. Deliverable: short findings note appended to this file. Gate A0: Kosta ack (cheap, catches wrong assumptions before code).

**Phase 1 — Ledger foundation**: schema v3 + store APIs + extractor emission + tests. Useful standalone (data starts accumulating immediately). **Gate A**: adversarial review focus — migration safety on live contact DBs, extractor prompt regression risk (fact extraction quality must not degrade; run the existing contact-memory evals in `profiles/poke/contact-memory/evals/` before/after), privacy of event content.

**Phase 2 — Maintenance + digest**: maintenance module + cron job + digest injection into session context. **Gate B**: review focus — LLM-proposal validation completeness, digest token bound, cache-safety of injection (prove the stable prompt is byte-identical across turns), taxonomy cap behavior, what happens when the maintenance model returns garbage JSON.

**Phase 3 — Scheduler + check-in port**: port kit check-in, build scheduler, entry point for agent-initiated turns, outcome tracking. NO interest-share sends yet — check-ins only, dry-run mode for shares (slots fire, candidates logged, nothing sent). **Gate C**: review focus — role-alternation/lineage safety of the initiated turn, cap/backoff correctness under restart, the one-strike invariant, timezone math, dual-profile conflict assertion. Plus 1 week of live dry-run logs reviewed by Kosta before enabling sends.

**Phase 4 — Fetch + gate + delivery**: last30days delegate, gate, ephemeral delivery block, banned-opener post-filter, texture integration. Enable on Poke for Kosta's own contacts only. **Gate D**: review focus — suppression-rate instrumentation (expect >60% suppressed), candidate JSON size bound, prompt-injection surface (fetched web content flows into a compose prompt — the candidate must be data, never instructions; gate output schema-validated), the HBS neediness bans present in both gate and delivery prompts.

**Phase 5 — Eval + Guest rollout**: eval scenarios (proactive believability blind A/B using the established texture eval harness + judge-hygiene protocol from the phase-2 kit: different-family judges, length normalization, Kosta's blind labels as promotion gate); reactive-enrichment scenario (dark-green-car case); tune signal weights from real outcome data. Enable guest profile. **Gate E**: full-system adversarial review + Kosta sign-off on real send transcripts.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Over-eager agent (the classic failure) | Structural: silence-default gate, hard caps, one-strike, backoff, model veto, suppression-rate alarm. Not prompt-begged. |
| Context bloat / cache invalidation | Ledger+fetch never enter session; digest ≤300 tokens, session-lifetime stable; ephemeral blocks use the existing texture suffix mechanism |
| Sounding pre-programmed | Jittered slots, bandit topic choice, persona writes fresh words, forced-boring response classes, novelty hash |
| Extractor prompt change degrades fact extraction | Before/after run of existing contact-memory evals is a Gate A requirement |
| Fetched-content prompt injection | Candidate is schema-validated data JSON; compose prompt treats it as inert; no URLs auto-fetched in the session |
| Cheap maintenance model produces garbage | All LLM output is proposal + deterministic validation; garbage JSON = skip run, alert, no state change |
| Guest privacy leak via proactive content | Gate runs under guest RetrievalScope; guest_ok/public facts only; DM-only; Guest enabled last |
| Live gateway disruption | All work in a separate worktree; no restart without Kosta's explicit approval; migrations additive-only |

## Open questions for the implementer (answer in Phase 0, don't guess)

- [ ] Exact mechanism for agent-initiated turns (cron delivery path vs a new session entry) — inspect, document, choose with evidence.
- [ ] Where digest injection best lives: contact-context assembly vs texture suffix. Must be the cache-safe side.
- [ ] Whether guest profile's contact_memory block matches poke's (diff the configs).
- [ ] last30days key availability on the Studio for the topics that matter (music/culture will lean X + Reddit + YouTube).

## Phase 0 findings (2026-07-13)

- Poke and Guest have matching enabled contact-memory extraction/Lane A/Lane B and conversation-texture-v2 blocks. Poke owns BlueBubbles ingress and routes approved guest DMs into the Guest profile; proactive identity must reuse the routed `decision.contact_id` / authenticated `session_contact_id`, then `opaque_contact_filename()`, rather than re-deriving an address hash.
- There is no clean existing no-inbound agent-turn entry point. Cron LLM jobs run isolated sessions and raw-deliver their output; cron mirroring writes a synthetic `user` role. Phase 3 therefore needs a dedicated proactive child-session path whose durable first generated turn is `assistant`, linked by `parent_session_id`, rather than mutating the live session or injecting a fake user message.
- The cache-safe digest lane is the API-only per-turn user context used by contact-memory recall. A digest must be snapshotted per session so maintenance updates do not hot-swap it mid-session. The short proactive compose directive can reuse `_with_conversation_texture`'s execution-only system suffix.
- Profile scheduler state belongs in an additive table in the explicit profile `state.db`; do not rely on module-import-time `DEFAULT_DB_PATH` and do not use one large `state_meta` blob. Cron ticks can overlap, so claims must be short, transactional, and idempotent.
- The check-in kit was present and inspected at `/tmp/poke-phase2-kit-review/poke-phase2-kit/runtime/proactive_checkin.py` (139 lines). Its state-machine logic is suitable as a starting point, but its host-local timezone math must be replaced with configured contact-local `zoneinfo` handling.
- `last30days doctor --json` passed with Reddit, YouTube, TikTok, Instagram, GitHub, HN, Digg, arXiv, Techmeme, Polymarket, and Exa web search available. X is unavailable through last30days on this profile, so fetch must tolerate that source being absent and use ordinary web/other-source fallback.
- All work is isolated in `/Users/Kosta/LocalDev/.worktrees/hermes-agent/poke-proactive-layer` on `feat/poke-proactive-layer`. The live gateway checkout and runtime were not modified or restarted.

## Goal prompt (self-contained, for the implementation lane)

> Implement the plan at `docs/plans/poke-proactive-layer.md` in this repo, phase by phase, in a worktree off `local/studio-slim`. Never restart the live gateway; never modify `~/.hermes/profiles/*` configs except where a phase explicitly says so. For each phase: implement, run the phase's tests plus `py_compile` and `git diff --check`, write a phase report with real command output, produce a full diff file, then STOP and request the adversarial review gate (independent GPT-5.6 Sol subagents at medium reasoning effort by default; use another model only when Kosta explicitly requests it). Fix all P0/P1 findings with re-review before proceeding. Phase 3 ends in dry-run mode and requires Kosta's explicit approval of a week of dry-run logs before Phase 4 enables real sends. Do not invent results; paste actual tool output. The binding design principles in the plan override any convenience shortcut; if reality contradicts the plan (an entry point doesn't exist, a schema conflicts), stop and report rather than improvising around it.
