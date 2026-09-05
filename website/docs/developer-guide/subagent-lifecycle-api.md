---
title: Public Subagent Lifecycle API
sidebar_label: Subagent lifecycle API
---

# Public Subagent Lifecycle API

Plugins can launch and supervise fresh Hermes child sessions without importing
`tools.delegate_tool`, gateway internals, TUI state, or `AIAgent` fields.
The service resolves its parent from the current agent turn, so it works in
CLI, gateway, non-interactive, and kanban-worker sessions. Launching outside an
active agent turn fails closed with `No active Hermes parent session`.

```python
from agent.subagent_lifecycle import SubagentLaunchRequest

def launch_review(ctx):
    # Call from a plugin tool or hook while an agent turn is active.
    service = ctx.subagent_lifecycle
    handle = service.launch(SubagentLaunchRequest(
        goal="Review this change for regressions.",
        context="Only inspect the supplied repository.",
        role="leaf",
        correlation_id="review-42",
        allowed_toolsets=("file",),
    ))
    # Persist handle.to_dict() if desired.
    if service.wait(handle, timeout_seconds=2).timed_out:
        return handle.to_dict()
    return service.result(handle)
```

`SubagentHandle` is serializable and carries a versioned, opaque capability.
Pass it back to `status`, `wait`, `cancel`, `result`, or `reconnect`; malformed
or forged handles return `UNKNOWN`/`UNKNOWN_HANDLE` and cannot access a child.

The stable states are `PENDING`, `STARTING`, `RUNNING`, `SUCCEEDED`, `FAILED`,
`INTERRUPTED`, `CANCEL_REQUESTED`, `CANCELLED`, and `UNKNOWN`.

`cancel(handle, reason=...)` is cooperative: it asks the child agent to
interrupt at its next safe boundary and returns `CANCEL_REQUESTED`; it never
claims completion until `wait` or `result` observes a terminal state. Terminal
results are immutable, idempotent, bounded to 32k characters, omit transcripts
and hidden reasoning, and include a stable result hash.

This API is lifecycle-managed asynchronous execution. Child construction and
completion use the same host-owned path as `delegate_task`, including parent
tool-resolution restoration, memory notification, serialized `subagent_stop`
hooks, resource cleanup, and child-cost rollup. It does not change the
synchronous `delegate_task` tool, batch delegation, or its gateway/TUI display.
The initial implementation retains metadata and terminal results in-process for
one hour.
After a process restart, `reconnect` returns `RECONNECT_UNAVAILABLE` and never
starts a replacement child. Running Python threads also cannot survive process
exit; callers must treat those handles as interrupted by process exit.

Requests are fail-closed: goal/context/metadata sizes are capped, unknown or
parent-broadening toolsets are rejected, and per-tool blocks, working-directory
overrides, and per-launch timeouts are explicitly rejected until Hermes can
support them without weakening isolation. Use `allowed_toolsets` to narrow a
child; Hermes's existing unsafe-tool block remains enforced.

## Detached authenticated sessions

The additional detached API does not require an active parent turn. A trusted
gateway host reconstructs a fresh parent inside a context manager; a plugin
receives only an opaque binding through the registered facade:

```python
from gateway.conversation_extensions import SubagentSessionRequest
from agent.subagent_lifecycle import SubagentLaunchRequest

binding = facade.bind_subagent_session(SubagentSessionRequest(
    session_key="saved-routing-key", parent_session_id="saved-session-id",
    runtime_profile="target-profile", scope_id="authenticated-contact-scope",
    principal="authenticated-principal",
))
handle = ctx.subagent_lifecycle.detached_launch(binding, SubagentLaunchRequest(
    goal="Complete the approved work", job_id="persisted-job-id",
    deadline_at=approved_at_epoch + 1800,
    model="chosen-model", provider="chosen-provider", reasoning_effort="high",
    blocked_tools=("send_message",),
))
status = ctx.subagent_lifecycle.detached_status(binding, handle)
accepted = ctx.subagent_lifecycle.detached_steer(binding, handle, text="Updated context")
cancel = ctx.subagent_lifecycle.detached_cancel(binding, handle, reason="No longer relevant")
result = ctx.subagent_lifecycle.detached_result(binding, handle)
```

The extension declares `detached_subagents`. The host installs
`GatewayHostOperations.bind_subagent_session(request) -> SubagentSessionBinding`.
The callback validates **all** request assertions against the existing session's
saved authenticated origin, including runtime profile and contact/principal.
Multiplex cross-profile resolution is allowed only after that check; there is
no fallback recipient or inferred contact authority.

Host registration uses these public types from
`agent.subagent_lifecycle_detached`:

```python
identity = SubagentSessionIdentity(profile_home, session_id, scope_id, principal)
binding = lifecycle.bind_session(
    identity,
    parent_factory=factory,
    notification_target=SilentNotificationTarget(identity, on_result),
)
```

`factory: Callable[[], ContextManager[AIAgent]]` is trusted host code, not a
plugin/model-provided factory. It revalidates saved authority on every launch,
enters the target profile's secret/runtime scope and `request_policy_scope`,
yields a fresh parent with the exact saved session ID and authorized toolsets,
and closes that parent on exit. The full child run happens inside that scope.
Core checks profile home and policy-home equality before constructing a child.
Do not capture a live parent or depend on the old turn's ContextVars/weakrefs.
`on_result: Callable[[SubagentResult], None]` runs on the worker thread; it must
enqueue internally and must not send to a contact. Core disables progress relay
and asynchronous terminal notifications only in the detached worker's context.

`deadline_at` is a finite UTC epoch timestamp, at most 30 minutes away. Every
worker sharing a contact/job ID must supply the same absolute deadline. Ten
unfinished workers are admitted per profile/contact/principal across jobs;
replacement workers do not reset the job clock. Nested `delegate_task` is
blocked so it cannot evade this accounting. Additional `blocked_tools` are
enforced at ordinary and inline dispatch, including Tool Search redispatch.
`timeout_seconds` remains unsupported: detached callers use the job deadline.

Provider overrides go through the existing delegate/runtime credential resolvers.
An explicit base URL must resolve to that exact endpoint with its own credential;
core will not send a parent's OAuth credential to another endpoint. Runtime
request overrides are passed into child construction, and per-launch reasoning
wins over delegation defaults. Existing active-parent `launch()` also accepts
the new provider/base URL/reasoning fields.

Cancellation and deadlines invoke the agent's hard interrupt (model I/O and tool
thread signals) plus task-owned process-registry cleanup for background shells.
A cancellation request is not a claim that a Python thread has stopped. An
uncooperative tool can remain `CANCEL_REQUESTED`, occupying its slot until it
returns; subsequent dispatch is denied. Arbitrary unmanaged processes that a
tool escapes from the existing process/terminal ownership mechanisms cannot be
proven killed by this API. Host verification must cover its selected backends.
No automatic respawn occurs after restart: bindings are invalid and handles are
unknown. Reconstruct a new binding only for new work after scheduler recovery.

`AuthenticatedDmRequest.attachments` accepts up to ten absolute local paths
(maximum 4096 characters each). Text is still required. The host must verify
retained-artifact ownership/readability and perform media transport; this field
does not authorize a new recipient or publish files.
