"""Typed, immutable invocation context for plugin slash commands.

The context is deliberately a narrow capability facade.  It carries immutable
identity/session snapshots and host-owned operations; it never exposes a live
GatewayRunner, platform adapter, session store, or config mapping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import replace
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, Optional


class CommandCapabilityError(RuntimeError):
    """Raised when a command asks for a capability unavailable on its surface."""


@dataclass(frozen=True)
class CommandSource:
    """Safe immutable identity copied from a gateway message source."""

    platform: str
    user_id: str
    chat_id: str
    chat_type: str
    profile: str
    thread_id: Optional[str] = None
    parent_chat_id: Optional[str] = None
    guild_id: Optional[str] = None
    user_name: Optional[str] = None
    chat_name: Optional[str] = None
    chat_topic: Optional[str] = None
    message_id: Optional[str] = None


@dataclass(frozen=True)
class CommandSession:
    """Safe immutable snapshot of one gateway routing entry."""

    session_key: str
    session_id: str
    platform: str
    chat_id: str
    chat_type: str
    profile: str
    cwd_override: Optional[str]
    effective_cwd: str
    personality_override: Optional[Mapping[str, str]]
    thread_id: Optional[str] = None
    topic: Optional[str] = None
    display_name: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class CommandActionResult:
    """Normalized result from a capability-backed platform/session action."""

    ok: bool
    message: str = ""
    error_code: str = ""
    thread_id: Optional[str] = None
    session: Optional[CommandSession] = None


@dataclass(frozen=True)
class _CommandServices:
    """Host-owned callables backing the public facade.

    Callables, rather than a gateway object, are captured so the context has no
    adapter/runner escape hatch.  Every operation is scoped to the invocation's
    already-authenticated source.
    """

    list_sessions: Optional[Callable[[], Awaitable[tuple[CommandSession, ...]]]] = None
    resolve_cwd: Optional[Callable[[str], Awaitable[tuple[Optional[str], Optional[str]]]]] = None
    set_cwd: Optional[Callable[[Optional[str]], Awaitable[Optional[CommandSession]]]] = None
    reset_session: Optional[Callable[[bool], Awaitable[str]]] = None
    set_personality: Optional[
        Callable[[Optional[dict[str, str]]], Awaitable[Optional[CommandSession]]]
    ] = None
    create_thread: Optional[
        Callable[[str, Optional[str], str], Awaitable[CommandActionResult]]
    ] = None
    rename_thread: Optional[Callable[[str], Awaitable[CommandActionResult]]] = None
    prompt_for_text: Optional[
        Callable[[str, str], Awaitable[CommandActionResult]]
    ] = None
    rewrite_input: Optional[Callable[[str], None]] = None
    send_voice: Optional[Callable[[str], Awaitable[CommandActionResult]]] = None
    authorize: Optional[Callable[[], bool]] = None


@dataclass(frozen=True)
class CommandInvocationContext:
    """Invocation envelope passed to context-aware plugin command handlers.

    Plugins opt into this shape with ``ctx.register_command(..., context=True)``.
    Legacy handlers continue to receive their raw argument string.
    """

    raw_args: str
    surface: str
    command: str
    profile: str
    source: Optional[CommandSource] = None
    session: Optional[CommandSession] = None
    capabilities: frozenset[str] = frozenset()
    _services: _CommandServices = field(
        default_factory=_CommandServices,
        repr=False,
        compare=False,
    )

    def supports(self, capability: str) -> bool:
        """Return whether the host granted a named invocation capability."""

        return capability in self.capabilities

    def with_args(
        self, raw_args: str, *, command: Optional[str] = None
    ) -> "CommandInvocationContext":
        """Return the same source-scoped facade with different parsed args."""

        return replace(
            self,
            raw_args=str(raw_args or ""),
            command=self.command if command is None else str(command),
        )

    def _require(self, capability: str, operation: Any) -> Any:
        if capability not in self.capabilities or operation is None:
            raise CommandCapabilityError(
                f"Command capability {capability!r} is unavailable on {self.surface}"
            )
        authorize = self._services.authorize
        if authorize is not None and not authorize():
            raise CommandCapabilityError(
                f"Command capability {capability!r} is no longer authorized "
                "because its plugin command was unloaded or replaced"
            )
        return operation

    async def list_sessions(self) -> tuple[CommandSession, ...]:
        operation = self._require("session.list", self._services.list_sessions)
        return await operation()

    async def resolve_cwd(
        self, raw_path: str
    ) -> tuple[Optional[str], Optional[str]]:
        operation = self._require("session.cwd.resolve", self._services.resolve_cwd)
        return await operation(raw_path)

    async def set_cwd(self, cwd: Optional[str]) -> Optional[CommandSession]:
        operation = self._require("session.cwd.write", self._services.set_cwd)
        return await operation(cwd)

    async def reset_session(self, *, preserve_session_config: bool = False) -> str:
        operation = self._require("session.reset", self._services.reset_session)
        return await operation(preserve_session_config)

    async def set_personality(
        self, personality: Optional[dict[str, str]]
    ) -> Optional[CommandSession]:
        operation = self._require(
            "session.personality.write", self._services.set_personality
        )
        return await operation(personality)

    async def create_thread(
        self,
        name: str,
        *,
        cwd: Optional[str] = None,
        welcome: str = "",
    ) -> CommandActionResult:
        operation = self._require("thread.create", self._services.create_thread)
        return await operation(name, cwd, welcome)

    async def rename_thread(self, name: str) -> CommandActionResult:
        operation = self._require("thread.rename", self._services.rename_thread)
        return await operation(name)

    async def prompt_for_text(
        self,
        prompt: str,
        *,
        cancel_message: str = "Cancelled command follow-up.",
    ) -> CommandActionResult:
        operation = self._require("followup.prompt", self._services.prompt_for_text)
        return await operation(prompt, cancel_message)

    def rewrite_input(self, text: str) -> None:
        operation = self._require("message.rewrite", self._services.rewrite_input)
        operation(text)

    async def send_voice(self, text: str) -> CommandActionResult:
        operation = self._require("voice.reply", self._services.send_voice)
        return await operation(text)


def cli_command_context(raw_args: str, command: str) -> CommandInvocationContext:
    """Build the capability-free context used by CLI/TUI command dispatch."""

    try:
        from hermes_cli.profiles import get_active_profile_name

        profile = get_active_profile_name() or "default"
    except Exception:
        profile = "default"
    return CommandInvocationContext(
        raw_args=str(raw_args or ""),
        surface="cli",
        command=str(command or ""),
        profile=profile,
    )
