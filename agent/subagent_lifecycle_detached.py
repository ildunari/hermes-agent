"""Host-bound detached delegation. Tokens and workers deliberately do not survive restart."""
from __future__ import annotations

import contextvars
import dataclasses
import math
import secrets
import threading
import time
from typing import Callable, ContextManager, Any

from agent import subagent_lifecycle as api


@dataclasses.dataclass(frozen=True)
class SubagentSessionIdentity:
    profile_home: str
    session_id: str
    scope_id: str
    principal: str

    def __post_init__(self):
        if any(not isinstance(v, str) or not v.strip() for v in dataclasses.astuple(self)):
            raise api.SubagentLifecycleError("All session identity fields are required.")
        from hermes_constants import hermes_home_key
        object.__setattr__(self, "profile_home", hermes_home_key(self.profile_home))


@dataclasses.dataclass(frozen=True)
class SubagentSessionBinding:
    identity: SubagentSessionIdentity
    authority_token: str = dataclasses.field(repr=False)


@dataclasses.dataclass(frozen=True)
class SilentNotificationTarget:
    """Internal result sink; never an adapter or a chat delivery callback."""
    identity: SubagentSessionIdentity
    on_result: Callable[[api.SubagentResult], None]


@dataclasses.dataclass
class _Binding:
    public: SubagentSessionBinding
    factory: Callable[[], ContextManager[Any]]
    target: SilentNotificationTarget


class DetachedLifecycle:
    def __init__(self):
        self.lock = threading.RLock()
        self.bindings = {}
        self.owners = {}
        self.jobs = {}
        self.tool_limits = {}

    def bind(self, identity, factory, target):
        if not isinstance(identity, SubagentSessionIdentity) or not callable(factory):
            raise api.SubagentLifecycleError("A trusted identity and context-manager factory are required.")
        if not isinstance(target, SilentNotificationTarget) or target.identity != identity or not callable(target.on_result):
            raise api.SubagentLifecycleError("A matching silent notification target is required.")
        public = SubagentSessionBinding(identity, secrets.token_urlsafe(32))
        with self.lock:
            self.bindings[public.authority_token] = _Binding(public, factory, target)
        return public

    def binding(self, value):
        if not isinstance(value, SubagentSessionBinding):
            raise api.SubagentLifecycleError("Invalid session authority.")
        with self.lock:
            stored = self.bindings.get(value.authority_token)
        if stored is None or stored.public != value:
            raise api.SubagentLifecycleError("Unknown or altered session authority.")
        return stored

    def launch(self, binding, request):
        bound = self.binding(binding)
        if not isinstance(request, api.SubagentLaunchRequest):
            raise api.SubagentLifecycleError("Expected SubagentLaunchRequest.")
        now = time.time()
        deadline = request.deadline_at
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or not now < deadline <= now + 1800:
            raise api.SubagentLifecycleError("deadline_at must be a future UTC epoch within 30 minutes.")
        if not isinstance(request.job_id, str) or not request.job_id.strip():
            raise api.SubagentLifecycleError("job_id is required.")
        if request.parent_session_id not in (None, binding.identity.session_id):
            raise api.SubagentLifecycleError("Parent session mismatch.")
        if request.timeout_seconds is not None:
            raise api.SubagentLifecycleError("Detached launches use the absolute job deadline, not timeout_seconds.")
        if not isinstance(request.blocked_tools, tuple) or any(not isinstance(n, str) or not n for n in request.blocked_tools):
            raise api.SubagentLifecycleError("blocked_tools must be a tuple of tool names.")
        for field in ("model", "provider", "base_url", "reasoning_effort"):
            value = getattr(request, field)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise api.SubagentLifecycleError(f"{field} must be a nonempty string.")
        if request.reasoning_effort is not None:
            from hermes_constants import parse_reasoning_effort
            if parse_reasoning_effort(request.reasoning_effort) is None:
                raise api.SubagentLifecycleError("Unknown reasoning_effort.")
        # Run ordinary validation now, and again against actual parent permissions.
        validated = dataclasses.replace(request, blocked_tools=())
        api.SubagentLifecycleService._validate_request(validated, None)
        owner = binding.identity
        contact = (owner.profile_home, owner.scope_id, owner.principal)
        job = (contact, request.job_id)
        with self.lock, api._REGISTRY.lock:
            api.SubagentLifecycleService._cleanup_locked()
            expired = set(self.owners) - set(api._REGISTRY.records)
            for sid in expired:
                self.owners.pop(sid, None)
                self.tool_limits.pop(sid, None)
            self.jobs = {key: end for key, end in self.jobs.items() if end + 3600 > now}
            existing = self.jobs.get(job)
            if existing is not None and existing != deadline:
                raise api.SubagentLifecycleError("A job's absolute deadline cannot change.")
            active = sum(1 for sid, identity in self.owners.items()
                         if (identity.profile_home, identity.scope_id, identity.principal) == contact
                         and sid in api._REGISTRY.records and api._REGISTRY.records[sid].result is None)
            if active >= 10:
                raise api.SubagentLifecycleError("Contact already has ten active workers.")
            sid = "detached-" + secrets.token_hex(12)
            handle = api.SubagentHandle(api.PUBLIC_CONTRACT_VERSION, sid, owner.session_id,
                request.correlation_id, now, request.provider, request.model, "leaf", 1,
                api.SubagentLifecycleService._capability(sid, owner.session_id, now))
            record = api._Record(handle, api.SubagentState.PENDING, now)
            api._REGISTRY.records[sid] = record
            self.owners[sid] = owner
            self.jobs[job] = deadline
            # Nested delegation would evade this service's per-contact accounting.
            blocked = frozenset(request.blocked_tools) | {"delegate_task"}
            self.tool_limits[sid] = (blocked, time.monotonic() + deadline - now, record)
        # One thread per admitted worker: no shared eight-worker queue silently reduces
        # the per-contact allowance. The fresh Context cannot retain the old turn.
        thread = threading.Thread(target=lambda: contextvars.Context().run(self.run, bound, request, record), daemon=True)
        thread.start()
        return handle

    def service(self, binding, handle):
        self.binding(binding)
        if self.owners.get(getattr(handle, "subagent_id", None)) != binding.identity:
            raise api.SubagentLifecycleError("Worker is not owned by this session principal.")
        from types import SimpleNamespace
        return api.SubagentLifecycleService(lambda: SimpleNamespace(session_id=binding.identity.session_id))

    def operation(self, binding, handle, name):
        return getattr(self.service(binding, handle), name)(handle)

    def cancel(self, binding, handle, reason):
        service = self.service(binding, handle)
        if not isinstance(reason, str) or not reason.strip():
            raise api.SubagentLifecycleError("A cancellation reason is required.")
        record = service._record(handle)
        result = service.cancel(handle, reason=reason)
        # Hard interrupt aborts model I/O and signals foreground/concurrent tool tids.
        # Background shells have independent process groups and need explicit cleanup.
        if service._record(handle) is not None and not result.already_terminal:
            from tools.process_registry import process_registry
            process_registry.kill_all(handle.subagent_id, source="detached lifecycle cancellation")
        if record is not None and record.agent is None and record.result is None:
            return dataclasses.replace(result, accepted=True, unsupported=False)
        return result

    def steer(self, binding, handle, text):
        service = self.service(binding, handle)
        record = service._record(handle)
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise api.SubagentLifecycleError("Steering text must contain 1..16000 characters.")
        with api._REGISTRY.lock:
            if record is None or record.result is not None or record.state == api.SubagentState.CANCEL_REQUESTED:
                return False
            return bool(record.agent and record.agent.steer(text))

    def tool_denial(self, task_id, name):
        with self.lock:
            limit = self.tool_limits.get(task_id)
        if limit is None:
            return None
        blocked, deadline, record = limit
        if name in blocked:
            return "Tool blocked by detached launch policy."
        if time.monotonic() >= deadline or record.state == api.SubagentState.CANCEL_REQUESTED or record.result is not None:
            return "Detached job has ended; further tool execution is denied."
        return None

    def run(self, bound, request, record):
        timer = threading.Timer(max(0, request.deadline_at - time.time()),
                                lambda: self.cancel(bound.public, record.handle, "Absolute job deadline reached"))
        timer.daemon = True
        timer.start()
        try:
            with bound.factory() as parent:
                if api._session_id_of(parent) != bound.public.identity.session_id:
                    raise api.SubagentLifecycleError("Reconstructed parent session mismatch.")
                from hermes_constants import hermes_home_key
                from gateway.conversation_extensions import current_request_policy
                policy = current_request_policy()
                if hermes_home_key() != bound.public.identity.profile_home:
                    raise api.SubagentLifecycleError("Reconstructed profile scope mismatch.")
                if policy is None or hermes_home_key(policy.profile_home) != bound.public.identity.profile_home:
                    raise api.SubagentLifecycleError("Reconstructed tool authorization policy is missing or mismatched.")
                api.SubagentLifecycleService._validate_request(dataclasses.replace(request, blocked_tools=()), parent)
                from gateway.session_context import declare_stateless_channel
                declare_stateless_channel()
                parent.tool_progress_callback = None
                parent._delegate_spinner = None
                parent._print_fn = None
                if record.state == api.SubagentState.CANCEL_REQUESTED or time.time() >= request.deadline_at:
                    raise api.SubagentLifecycleError("Job expired before child construction.")
                child = build_child(parent, request)
                child._subagent_id = record.handle.subagent_id
                child._lifecycle_owns_deadline = True
                with api._REGISTRY.lock:
                    record.agent = child
                if record.state == api.SubagentState.CANCEL_REQUESTED or time.time() >= request.deadline_at:
                    self.cancel(bound.public, record.handle, "Job cancelled during construction")
                    child.close()
                    raise api.SubagentLifecycleError("Job cancelled before execution.")
                service = self.service(bound.public, record.handle)
                service._run(record, request.goal, parent)
        except Exception as exc:
            with api._REGISTRY.lock:
                record.state = api.SubagentState.CANCELLED if record.state == api.SubagentState.CANCEL_REQUESTED else api.SubagentState.FAILED
                record.completed_at = record.updated_at = time.time()
                record.result = api.SubagentResult(record.handle, record.state, True,
                    completed_at=record.completed_at, error_classification=type(exc).__name__, error_message=str(exc)[:1000])
                record.agent = None
        finally:
            timer.cancel()
            from tools.process_registry import process_registry
            process_registry.kill_all(record.handle.subagent_id, source="detached lifecycle cleanup")
        bound.target.on_result(record.result)


def build_child(parent, request):
    from tools.delegate_tool import _build_child_preserving_parent_tools, DEFAULT_MAX_ITERATIONS
    from tools.delegate_tool_config import _resolve_delegation_credentials
    cfg = {k: getattr(request, k) for k in ("model", "provider", "base_url") if getattr(request, k) is not None}
    creds = _resolve_delegation_credentials(cfg, parent)
    if request.base_url:
        # Never borrow the parent's OAuth/API credential for a different endpoint.
        from hermes_cli.runtime_provider import resolve_runtime_provider
        runtime = resolve_runtime_provider(requested=request.provider or "custom",
            explicit_base_url=request.base_url, target_model=request.model)
        if str(runtime.get("base_url", "")).rstrip("/") != request.base_url.rstrip("/"):
            raise api.SubagentLifecycleError("Provider did not resolve the requested endpoint.")
        for key in ("provider", "base_url", "api_key", "api_mode", "request_overrides"):
            creds[key] = runtime.get(key)
        if not creds["api_key"]:
            raise api.SubagentLifecycleError("Explicit endpoint requires its own resolved credential.")
    from hermes_constants import parse_reasoning_effort
    reasoning = parse_reasoning_effort(request.reasoning_effort) if request.reasoning_effort is not None else None
    if request.reasoning_effort is not None and reasoning is None:
        raise api.SubagentLifecycleError("Unknown reasoning_effort.")
    # Explicit per-launch reasoning must win over profile delegation defaults.
    child = _build_child_preserving_parent_tools(task_index=0, goal=request.goal, context=request.context,
        toolsets=list(request.allowed_toolsets) if request.allowed_toolsets else None,
        model=creds["model"], max_iterations=DEFAULT_MAX_ITERATIONS, task_count=1, parent_agent=parent,
        override_provider=creds["provider"], override_base_url=creds["base_url"],
        override_api_key=creds["api_key"], override_api_mode=creds["api_mode"],
        override_request_overrides=creds["request_overrides"], override_max_tokens=creds["max_output_tokens"],
        override_reasoning_config=reasoning)
    return child


manager = DetachedLifecycle()
