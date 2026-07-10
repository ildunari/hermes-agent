# Prompt caching and Claude OAuth stability

Hermes treats the provider prompt cache as a per-conversation invariant. The
system prompt, tool schemas, and persisted history must remain byte-stable for
the life of a conversation except when context compression intentionally
rebuilds the prefix.

This document describes Hermes's cache strategy, the July 2026 comparison with
Oh My Pi (OMP) 16.4.0, and the improvements retained from that audit.

## Current request layout

`agent/prompt_caching.py` decorates a request-local deep copy. Canonical session
history is never mutated.

Hermes allocates at most four Anthropic cache breakpoints:

1. the system prompt;
2. the last three non-system messages that can legally carry a marker; or
3. on OpenAI-wire Claude routes with tools, the stable tool schema plus at most
   three message markers.

The carrier check avoids spending a breakpoint on empty assistant tool-call
messages or malformed/unsupported content parts. Native Anthropic tool-result
markers are relocated by the Anthropic adapter. OpenAI-wire routes receive
markers inside content parts because top-level markers on `role: tool` are not
portable.

Provider policy lives in
`agent/agent_runtime_helpers.py::anthropic_prompt_cache_policy`. It distinguishes
native Anthropic layout from OpenAI-wire envelopes and explicitly covers native
Anthropic, OpenRouter/Nous, CLIProxy (`vibeproxy`) Claude aliases, MiniMax,
Kimi/Moonshot, Qwen/Alibaba, and MoA aggregators. Unknown strict OpenAI-wire
providers stay disabled rather than receiving speculative fields.

## TTL policies

Configure `prompt_caching.cache_ttl` in `config.yaml`:

```yaml
prompt_caching:
  cache_ttl: mixed
```

Supported policies:

- `5m`: system, tools, and rolling messages use the five-minute tier.
- `1h`: every explicit breakpoint uses the one-hour tier.
- `mixed` (default for native Anthropic and local CLIProxy Claude): stable
  system and tool-schema breakpoints use one hour; rolling message breakpoints
  use five minutes.

`mixed` borrows OMP's useful split-retention idea without copying its provider
fingerprinting. It reduces repeated writes of the large stable prefix after
short pauses while avoiding the higher one-hour write multiplier on every new
conversation-tail breakpoint. Routes whose one-hour support is not guaranteed
(OpenRouter, MiniMax, GLM, Qwen, Kimi, and other third-party endpoints) safely
downgrade the default to `5m`; an explicit `1h` setting remains an advanced
user override.

Anthropic requires longer-lived breakpoints to precede shorter-lived ones.
Hermes naturally satisfies that order: tools and system precede messages on the
wire. Tests pin the marker values and four-breakpoint ceiling.

## Cache invalidation diagnostics

Hermes does not infer a miss merely because configuration or MCP state changed.
`detect_cache_invalidation` reports only an observed warm-to-cold transition:

- the previous request read at least 2,048 cached tokens;
- the current request reads zero cached tokens;
- the current request writes a replacement prefix; and
- input plus cache-write volume is at least 2,048 tokens.

The conversation loop records this as a warning in `agent.log`; verbose
interactive sessions also show a short cache-rebuilt notice. The telemetry is
observation-only and is never included in prompt construction.

## Claude OAuth refresh serialization

Claude OAuth refresh tokens rotate on successful use. Atomic credential-file
replacement protects JSON integrity but does not prevent two Hermes processes
from concurrently POSTing the same single-use refresh token.

`agent/anthropic_adapter.py::_claude_oauth_refresh_lock` now serializes the full
transaction across Hermes threads and processes, including the credential-pool
path:

1. acquire `~/.claude/.credentials.refresh.lock` with a bounded wait;
2. re-read live Claude credentials after acquiring the lock;
3. adopt a token another process already refreshed, when available;
4. otherwise perform one refresh POST; and
5. atomically write the rotated credential pair before releasing the lock.

Claude Code does not participate in Hermes's lock. If Claude Code wins the
remaining race during Hermes's POST, Hermes re-reads the credentials once and
adopts the valid replacement rather than reporting a false auth failure or
immediately consuming the newly rotated token.

If Claude's canonical credential JSON is malformed, Hermes repairs it while
persisting the rotated chain. If replacing the canonical file fails after the
provider has already consumed the single-use refresh token, Hermes atomically
writes `~/.claude/.credentials.hermes-recovery.json` at mode `0600`. All Hermes
profiles consider that file during credential reconciliation, preferring the
freshest valid chain. A later successful canonical write removes the recovery
file. This prevents a transient local write problem from forcing a new login.

The lock uses `flock` on POSIX and `msvcrt.locking` on Windows. The lock file is
opened without following symlinks where the OS supports it, validated as a
regular file owned by the current user, and forced to mode `0600` on POSIX.
Both the thread wait and file-lock wait are bounded. Forked children reset the
in-process lock so they cannot inherit a permanently owned mutex. Tests run two
concurrent refreshers, verify that the single-use token is posted exactly once,
exercise bounded thread waits, and cover the Claude-Code-won-the-race fallback.

## OMP ideas retained

The OMP 16.4.0 source audit informed these changes:

- explicit global budgeting of Anthropic's four cache breakpoints;
- stable-prefix/rolling-tail TTL separation;
- usage-based invalidation detection instead of configuration guesses;
- deterministic, request-local prompt mutations;
- re-reading credentials inside a serialized refresh transaction;
- keeping stable system guidance out of mid-history synthetic messages; and
- preserving cache-read and cache-write accounting as distinct quantities.

Hermes already had stronger endpoint-aware marker placement, legal-carrier
checks, canonical-history immutability, deterministic fallback tool-call IDs,
and provider-normalized cache accounting. Those implementations were kept.

## GPT, Codex, and Grok routes

Hermes does not apply Anthropic `cache_control` markers to GPT or Grok. Those
providers use automatic prefix caching plus routing hints:

- OpenAI/Codex Responses and xAI Responses use a content-addressed
  `prompt_cache_key` derived from stable instructions and sorted tool schemas.
- Codex subscription app-server caching remains owned by Codex; Hermes preserves
  the app-server thread instead of injecting unsupported request fields.
- xAI OAuth also receives `x-grok-conv-id`; OpenRouter Grok uses documented
  sticky `session_id` routing. Grok caching is automatic.
- Responses usage reads `input_tokens_details.cached_tokens` and the current
  GPT-5.6 `cache_write_tokens` field, with legacy `cache_creation_tokens` kept
  only as a compatibility fallback.

Provider-specific retention or breakpoint fields are not sent through generic
OpenAI-compatible proxies without a verified compatibility contract. This
avoids turning a cache optimization into request failures on subscription
routes.

## OMP behavior intentionally not copied

The audit rejected mechanisms that are brittle, private, or mismatched with
Hermes:

- Claude Code fingerprint impersonation and hard-coded private beta ordering;
- billing-header attestation/body patching;
- fake Claude Agent SDK system instructions;
- blanket tool-name prefix mutations;
- token-prefix-only auth classification;
- OMP's fixed 8K warm-tail pruning threshold and 90-minute cold boundary; and
- automatic cache modes that would replace Hermes's more robust explicit
  breakpoints in tool-heavy conversations.

Hermes continues to identify and route providers explicitly, mutates only
request-local copies, and keeps caching disabled when an endpoint's wire
contract is uncertain.

## Verification

Focused tests:

```bash
pytest -q \
  tests/agent/test_prompt_caching.py \
  tests/agent/test_anthropic_keychain.py \
  tests/run_agent/test_anthropic_prompt_cache_policy.py \
  tests/run_agent/test_run_agent.py \
  -k 'prompt_caching_vibeproxy or prompt_cache_policy or prompt_caching or refresh'
```

The important invariants are:

- no more than four cache breakpoints reach Anthropic;
- mixed TTL keeps stable markers at one hour and rolling markers at five
  minutes;
- canonical tools and history remain unchanged;
- unsupported endpoints receive no markers;
- normal warm turns do not emit invalidation warnings; and
- concurrent OAuth refreshers perform one network refresh.
