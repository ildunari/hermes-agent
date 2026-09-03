"""Temporary model tools whose authority exists only for one agent request.

Bindings are held in a :mod:`contextvars` value rather than on the cached
agent.  Overlapping executor threads can therefore use the same cached agent
without observing or restoring each other's capabilities.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Sequence


@dataclass(frozen=True)
class RequestScopedTool:
    """One request-local schema and its service-owned handler."""

    schema: Mapping[str, Any]
    handler: Callable[[dict[str, Any]], Any]
    on_success: Callable[[Sequence[Any]], Any] | None = None

    @property
    def name(self) -> str:
        name = self.schema.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("request-scoped tool schema requires a name")
        return name


@dataclass
class RequestScopedBinding:
    agent_id: int
    schemas: tuple[dict[str, Any], ...]
    handlers: dict[str, Callable[[dict[str, Any]], Any]]
    success_callbacks: dict[str, Callable[[Sequence[Any]], Any]]
    usage: dict[str, list[Any]] = field(default_factory=dict)
    _committed: bool = False

    def commit_success(self) -> None:
        """Commit callback usage only after the enclosing turn succeeds."""
        if self._committed:
            return
        self._committed = True
        for name, values in self.usage.items():
            callback = self.success_callbacks.get(name)
            if callback is not None and values:
                try:
                    callback(tuple(values))
                except Exception:
                    # Usage accounting is advisory and must not fail a completed turn.
                    continue


_bindings: ContextVar[tuple[RequestScopedBinding, ...]] = ContextVar(
    "hermes_request_scoped_tool_bindings", default=()
)


def _binding_for(agent: Any) -> RequestScopedBinding | None:
    agent_id = id(agent)
    for binding in reversed(_bindings.get()):
        if binding.agent_id == agent_id:
            return binding
    return None


def get_request_scoped_handler(agent: Any, name: str) -> Callable[[dict[str, Any]], Any] | None:
    binding = _binding_for(agent)
    if binding is None:
        return None
    handler = binding.handlers.get(name)
    return handler if callable(handler) else None


def get_effective_tools(agent: Any) -> list[dict[str, Any]]:
    """Return base plus current-request schemas without mutating the agent."""
    tools = list(getattr(agent, "tools", None) or [])
    binding = _binding_for(agent)
    if binding is not None:
        tools.extend(binding.schemas)
    return tools


def get_effective_tool_names(agent: Any) -> set[str]:
    names: set[str] = set(getattr(agent, "valid_tool_names", None) or ())
    binding = _binding_for(agent)
    if binding is not None:
        names.update(binding.handlers)
    return names


def record_request_scoped_usage(agent: Any, name: str, value: Any) -> None:
    """Stage evidence returned by a request tool for post-success commit."""
    binding = _binding_for(agent)
    if binding is not None and name in binding.handlers:
        binding.usage.setdefault(name, []).append(value)


def record_current_request_scoped_usage(name: str, value: Any) -> None:
    """Stage usage from inside a closed-over handler."""
    for binding in reversed(_bindings.get()):
        if name in binding.handlers:
            binding.usage.setdefault(name, []).append(value)
            return


@contextmanager
def bind_request_scoped_tools(
    agent: Any,
    tools: Sequence[RequestScopedTool],
) -> Iterator[RequestScopedBinding]:
    """Expose *tools* only in this execution context.

    The cached agent is never changed. Context variables are thread-local and
    task-local, so an interrupted request may unwind concurrently with its
    replacement without leaking authority or corrupting a shared snapshot.
    """
    base_names: set[str] = set(getattr(agent, "valid_tool_names", None) or ())
    parent = _binding_for(agent)
    if parent is not None:
        base_names.update(parent.handlers)

    schemas: list[dict[str, Any]] = []
    handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}
    callbacks: dict[str, Callable[[Sequence[Any]], Any]] = {}
    for tool in tools:
        name = tool.name
        if name in base_names or name in handlers:
            raise ValueError(f"request-scoped tool conflicts with existing tool: {name}")
        schemas.append({"type": "function", "function": dict(tool.schema)})
        handlers[name] = tool.handler
        if tool.on_success is not None:
            callbacks[name] = tool.on_success

    binding = RequestScopedBinding(id(agent), tuple(schemas), handlers, callbacks)
    token = _bindings.set((*_bindings.get(), binding))
    try:
        yield binding
    finally:
        _bindings.reset(token)
