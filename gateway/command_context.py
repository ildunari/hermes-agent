"""Gateway host for the plugin command invocation context.

This module is the only bridge from the public command facade to GatewayRunner.
Plugins receive immutable snapshots and narrow operations, never the runner,
adapter, session store, or raw MessageEvent.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import os
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Optional

from gateway.config import Platform
from gateway.session import SessionEntry, SessionSource
from hermes_cli.command_context import (
    CommandActionResult,
    CommandCapabilityError,
    CommandInvocationContext,
    CommandSession,
    CommandSource,
    _CommandServices,
)


@dataclasses.dataclass(frozen=True)
class GatewayPluginCommandDispatch:
    """Host-side outcome of one plugin slash-command dispatch.

    ``continue_as_message`` is deliberately explicit. A context-aware command
    may rewrite the authenticated event through the narrow ``message.rewrite``
    capability and return no immediate response (for example, a command that
    expands into a skill invocation). Treating that ``None`` like an ordinary
    handled command drops the rewritten turn before it ever reaches the agent.
    """

    matched: bool
    response: Optional[str] = None
    continue_as_message: bool = False


def _profile_name(source: SessionSource) -> str:
    stamped = str(getattr(source, "profile", "") or "").strip()
    if stamped:
        return stamped
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name() or "default"
    except Exception:
        return "default"


def _platform_name(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _safe_source(event: Any) -> CommandSource:
    source = event.source
    return CommandSource(
        platform=_platform_name(source.platform),
        user_id=str(source.user_id or ""),
        chat_id=str(source.chat_id or ""),
        chat_type=str(source.chat_type or ""),
        profile=_profile_name(source),
        thread_id=str(source.thread_id) if source.thread_id is not None else None,
        parent_chat_id=(
            str(source.parent_chat_id)
            if getattr(source, "parent_chat_id", None) is not None
            else None
        ),
        guild_id=(
            str(source.guild_id)
            if getattr(source, "guild_id", None) is not None
            else None
        ),
        user_name=getattr(source, "user_name", None),
        chat_name=getattr(source, "chat_name", None),
        chat_topic=getattr(source, "chat_topic", None),
        message_id=str(event.message_id) if event.message_id is not None else None,
    )


def _effective_cwd(runner: Any, entry: Optional[SessionEntry]) -> str:
    resolver = getattr(runner, "_session_cwd_for_entry", None)
    if callable(resolver):
        return str(resolver(entry))
    if entry is not None and entry.cwd_override:
        return str(entry.cwd_override)
    raw = os.getenv("TERMINAL_CWD") or str(Path.home())
    return os.path.abspath(os.path.expanduser(raw))


def _session_snapshot(
    runner: Any,
    entry: Optional[SessionEntry],
    *,
    fallback_source: SessionSource,
) -> Optional[CommandSession]:
    if entry is None:
        return None
    origin = entry.origin or fallback_source
    personality = entry.personality_override
    return CommandSession(
        session_key=str(entry.session_key),
        session_id=str(entry.session_id),
        platform=_platform_name(entry.platform or origin.platform),
        chat_id=str(origin.chat_id or ""),
        chat_type=str(entry.chat_type or origin.chat_type or ""),
        profile=_profile_name(origin),
        cwd_override=str(entry.cwd_override) if entry.cwd_override else None,
        effective_cwd=_effective_cwd(runner, entry),
        personality_override=(
            MappingProxyType(
                {str(key): str(value) for key, value in personality.items()}
            )
            if isinstance(personality, dict)
            else None
        ),
        thread_id=str(origin.thread_id) if origin.thread_id is not None else None,
        topic=origin.chat_topic,
        display_name=entry.display_name,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


def _adapter_method(adapter: Any, name: str) -> Any:
    """Return a real declared adapter method without MagicMock auto-members."""

    if adapter is None:
        return None
    try:
        inspect.getattr_static(adapter, name)
    except AttributeError:
        return None
    method = getattr(adapter, name, None)
    return method if callable(method) else None


async def _set_session_title(
    runner: Any,
    session_id: str,
    source: SessionSource,
    title: str,
) -> str:
    session_db = getattr(runner, "_session_db", None)
    if session_db is None or not session_id:
        return ""
    try:
        await session_db.create_session(
            session_id=session_id,
            source=_platform_name(source.platform) or "gateway",
            user_id=source.user_id,
        )
    except Exception:
        pass
    try:
        updated = await session_db.set_session_title(session_id, title)
        return "" if updated else "Hermes session title could not be persisted."
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return str(exc)


async def _normalize_thread_title(runner: Any, raw_title: str) -> str:
    from hermes_state import SessionDB

    title = SessionDB.sanitize_title(raw_title)
    if not title:
        raise ValueError("Topic name cannot be empty.")
    session_db = getattr(runner, "_session_db", None)
    if session_db is not None:
        try:
            title = await session_db.get_next_title_in_lineage(title)
        except Exception:
            pass
    return title[:100].strip()


async def build_gateway_command_context(
    runner: Any,
    event: Any,
    command: str,
    raw_args: str,
    *,
    registration: Optional[object] = None,
) -> CommandInvocationContext:
    """Build one source-scoped command context for an authenticated event."""

    source: SessionSource = event.source
    profile = _profile_name(source)
    entry = await runner.async_session_store.get_or_create_session(source)

    def entry_belongs_to_source(candidate: Optional[SessionEntry]) -> bool:
        if candidate is None:
            return False
        origin = candidate.origin
        if origin is None:
            return True  # Legacy entries did not persist an origin snapshot.
        return (
            origin.platform == source.platform
            and str(origin.chat_id) == str(source.chat_id)
            and _profile_name(origin) == profile
            and (
                str(origin.thread_id) if origin.thread_id is not None else None
            )
            == (str(source.thread_id) if source.thread_id is not None else None)
        )

    if not entry_belongs_to_source(entry):
        raise PermissionError(
            "Gateway command session resolution crossed a source/profile boundary"
        )

    def registration_is_active() -> bool:
        if registration is None:
            # Runtime dispatch always supplies a token. Keeping the unbound
            # path preserves the additive builder API used by host embedders.
            return True
        try:
            from hermes_cli.plugins import _get_plugin_command_registration

            return _get_plugin_command_registration(command) is registration
        except Exception:
            return False

    initial_snapshot = _session_snapshot(runner, entry, fallback_source=source)
    session_key = str(entry.session_key)
    adapter = runner._adapter_for_source(source)

    async def list_sessions() -> tuple[CommandSession, ...]:
        entries = await runner.async_session_store.list_sessions()
        snapshots: list[CommandSession] = []
        for candidate in entries:
            origin = candidate.origin
            if origin is None:
                continue
            if origin.platform != source.platform or str(origin.chat_id) != str(source.chat_id):
                continue
            # Defence in depth for multiplexed stores: even if a caller hands
            # this facade a mixed store, another profile's entry stays hidden.
            if _profile_name(origin) != profile:
                continue
            snapshot = _session_snapshot(runner, candidate, fallback_source=source)
            if snapshot is not None:
                snapshots.append(snapshot)
        snapshots.sort(
            key=lambda item: (
                item.updated_at or item.created_at or datetime.min
            ),
            reverse=True,
        )
        return tuple(snapshots)

    async def resolve_cwd(raw_path: str) -> tuple[Optional[str], Optional[str]]:
        token = str(raw_path or "").strip()
        if (token.startswith('"') and token.endswith('"')) or (
            token.startswith("'") and token.endswith("'")
        ):
            token = token[1:-1].strip()
        if not token:
            return None, "Path is empty."
        current = await runner.async_session_store.get_session(session_key)
        if not entry_belongs_to_source(current):
            return None, "Current session is no longer available for this source."
        base_dir = Path(_effective_cwd(runner, current))
        expanded = Path(os.path.expandvars(os.path.expanduser(token)))
        if expanded.is_absolute():
            candidates = [expanded]
        else:
            candidates = [base_dir / expanded]
            explicit = token.startswith(("./", "../")) or token in {".", ".."}
            if not explicit:
                home_candidate = Path.home() / expanded
                if home_candidate not in candidates:
                    candidates.append(home_candidate)
        first_missing: Optional[Path] = None
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except Exception:
                resolved = candidate
            try:
                from tools.terminal_tool import _validate_workdir

                validation_error = _validate_workdir(str(resolved))
                if validation_error:
                    return None, validation_error
            except Exception:
                pass
            if not resolved.exists():
                first_missing = first_missing or resolved
                continue
            if not resolved.is_dir():
                return None, f"Not a directory: {resolved}"
            return str(resolved), None
        return None, f"Directory not found: {first_missing or candidates[0]}"

    async def set_cwd(cwd: Optional[str]) -> Optional[CommandSession]:
        current = await runner.async_session_store.get_session(session_key)
        if not entry_belongs_to_source(current):
            raise PermissionError("Session cwd write crossed a source/profile boundary")
        if not registration_is_active():
            raise CommandCapabilityError(
                "Plugin command authorization was revoked before the cwd write"
            )
        updated = await runner.async_session_store.set_session_cwd(session_key, cwd)
        return _session_snapshot(runner, updated, fallback_source=source)

    async def reset_session(preserve: bool) -> str:
        return str(
            await runner._handle_reset_command(
                event,
                preserve_session_config=preserve,
            )
        )

    async def set_personality(
        personality: Optional[dict[str, str]],
    ) -> Optional[CommandSession]:
        current = await runner.async_session_store.get_session(session_key)
        if not entry_belongs_to_source(current):
            raise PermissionError(
                "Session personality write crossed a source/profile boundary"
            )
        if not registration_is_active():
            raise CommandCapabilityError(
                "Plugin command authorization was revoked before the personality write"
            )
        normalized = dict(personality) if personality else None
        updated = await runner.async_session_store.set_session_personality_override(
            session_key, normalized
        )
        overrides = runner.__dict__.setdefault("_session_personality_overrides", {})
        if normalized is None:
            overrides.pop(session_key, None)
        else:
            overrides[session_key] = dict(normalized)
        cache = runner.__dict__.setdefault("_session_entry_cache", {})
        if updated is not None:
            cache[session_key] = updated
        runner._evict_cached_agent(session_key)
        return _session_snapshot(runner, updated, fallback_source=source)

    async def create_thread(
        name: str,
        cwd: Optional[str],
        welcome: str,
    ) -> CommandActionResult:
        if (
            source.platform != Platform.TELEGRAM
            or str(source.chat_type).lower() == "channel"
        ):
            return CommandActionResult(
                ok=False,
                error_code="capability_unavailable",
                message="Telegram topic creation is unavailable for this source.",
            )
        create_topic = _adapter_method(adapter, "create_topic")
        if create_topic is None:
            return CommandActionResult(
                ok=False,
                error_code="capability_unavailable",
                message="Telegram topic creation is not available on this gateway right now.",
            )
        try:
            title = await _normalize_thread_title(runner, name)
        except ValueError as exc:
            return CommandActionResult(ok=False, error_code="invalid_title", message=str(exc))
        if not registration_is_active():
            return CommandActionResult(
                ok=False,
                error_code="authorization_revoked",
                message="The plugin command was unloaded before topic creation.",
            )
        try:
            thread_id = await create_topic(
                chat_id=int(source.chat_id), name=title, persist=True
            )
        except Exception as exc:
            return CommandActionResult(
                ok=False,
                error_code="platform_error",
                message=f"Failed to create Telegram topic: {exc}",
            )
        if not thread_id:
            last_error = str(
                getattr(adapter, "last_topic_create_error", "")
                or getattr(adapter, "_last_topic_create_error", "")
                or ""
            )
            error_code = (
                "forum_create_forbidden"
                if "forum_create_forbidden" in last_error.lower()
                else "create_failed"
            )
            return CommandActionResult(ok=False, error_code=error_code)

        new_source = dataclasses.replace(
            source,
            thread_id=str(thread_id),
            chat_topic=title,
        )
        new_entry = await runner.async_session_store.get_or_create_session(
            new_source, force_new=True
        )
        new_key = runner._session_key_for_source(new_source)
        runner.__dict__.setdefault("_session_model_overrides", {}).pop(new_key, None)
        if cwd:
            new_entry = await runner.async_session_store.set_session_cwd(new_key, cwd)
        try:
            await asyncio.to_thread(
                runner._record_telegram_topic_binding, new_source, new_entry
            )
        except Exception:
            pass
        title_warning = await _set_session_title(
            runner, new_entry.session_id, new_source, title
        )
        if welcome:
            send = _adapter_method(adapter, "send")
            if send is not None:
                try:
                    warning_note = (
                        f"\n\n⚠️ {title_warning}" if title_warning else ""
                    )
                    await send(
                        source.chat_id,
                        welcome.format(title=title, warning=warning_note),
                        metadata={
                            "thread_id": str(thread_id),
                            "chat_type": source.chat_type,
                        },
                    )
                except Exception:
                    pass
        return CommandActionResult(
            ok=True,
            message=title_warning,
            thread_id=str(thread_id),
            session=_session_snapshot(runner, new_entry, fallback_source=new_source),
        )

    async def rename_thread(name: str) -> CommandActionResult:
        title = str(name or "").strip()
        current = await runner.async_session_store.get_or_create_session(source)
        if not entry_belongs_to_source(current):
            return CommandActionResult(
                ok=False,
                error_code="authorization_failed",
                message="Current session no longer belongs to this source.",
            )
        if not registration_is_active():
            return CommandActionResult(
                ok=False,
                error_code="authorization_revoked",
                message="The plugin command was unloaded before session rename.",
            )
        warning = await _set_session_title(runner, current.session_id, source, title)
        rename_topic = (
            _adapter_method(adapter, "rename_topic")
            if source.platform == Platform.TELEGRAM
            else None
        )
        if rename_topic is not None and source.thread_id:
            if not registration_is_active():
                return CommandActionResult(
                    ok=False,
                    error_code="authorization_revoked",
                    message="The plugin command was unloaded before topic rename.",
                )
            try:
                await rename_topic(
                    chat_id=int(source.chat_id),
                    thread_id=int(source.thread_id),
                    name=title,
                )
                return CommandActionResult(ok=True, message=warning)
            except Exception as exc:
                return CommandActionResult(
                    ok=False,
                    error_code="platform_error",
                    message=f"Hermes session renamed, but Telegram topic rename failed: {exc}",
                )
        if warning:
            return CommandActionResult(
                ok=False, error_code="session_title_failed", message=warning
            )
        return CommandActionResult(
            ok=True,
            error_code="platform_unavailable",
            message="Telegram topic rename is not available on this adapter yet.",
        )

    async def prompt_for_text(
        prompt: str,
        cancel_message: str,
    ) -> CommandActionResult:
        send = _adapter_method(adapter, "send")
        if send is None:
            return CommandActionResult(
                ok=False,
                error_code="capability_unavailable",
                message="Interactive follow-up prompts are unavailable on this adapter.",
            )
        metadata_builder = getattr(runner, "_thread_metadata_for_source", None)
        metadata = (
            metadata_builder(source, reply_to_message_id=event.message_id)
            if callable(metadata_builder)
            else None
        )
        try:
            send_result = await send(source.chat_id, prompt, metadata=metadata)
        except Exception as exc:
            return CommandActionResult(
                ok=False, error_code="platform_error", message=str(exc)
            )
        if getattr(send_result, "success", True) is False:
            return CommandActionResult(
                ok=False,
                error_code="platform_error",
                message=str(
                    getattr(send_result, "error", "")
                    or "Prompt could not be delivered."
                ),
            )
        if not registration_is_active():
            return CommandActionResult(
                ok=False,
                error_code="authorization_revoked",
                message="The plugin command was unloaded while prompting.",
            )
        pending = runner.__dict__.setdefault("_pending_plugin_command_followups", {})
        pending[session_key] = {
            "command": command,
            "profile": profile,
            "registration": registration,
            "cancel_message": str(
                cancel_message or "Cancelled command follow-up."
            ),
        }
        return CommandActionResult(ok=True)

    def rewrite_input(text: str) -> None:
        event.text = str(text)

    async def send_voice(text: str) -> CommandActionResult:
        try:
            await runner._send_voice_reply(event, text)
            return CommandActionResult(ok=True)
        except Exception as exc:
            return CommandActionResult(
                ok=False, error_code="platform_error", message=str(exc)
            )

    capabilities = {
        "session.list",
        "session.cwd.resolve",
        "session.cwd.write",
        "session.reset",
        "session.personality.write",
        "thread.rename",
        "message.rewrite",
    }
    # Topic creation is a Telegram transport capability, not a convention
    # inferred solely from a method name. A plugin platform may expose a
    # same-named method with different semantics; never grant that external
    # effect accidentally.
    if (
        source.platform == Platform.TELEGRAM
        and str(source.chat_type).lower() != "channel"
        and _adapter_method(adapter, "create_topic") is not None
    ):
        capabilities.add("thread.create")
    if _adapter_method(adapter, "send") is not None:
        capabilities.add("followup.prompt")
    if callable(getattr(runner, "_send_voice_reply", None)):
        capabilities.add("voice.reply")

    return CommandInvocationContext(
        raw_args=str(raw_args or ""),
        surface="gateway",
        command=str(command or ""),
        profile=profile,
        source=_safe_source(event),
        session=initial_snapshot,
        capabilities=frozenset(capabilities),
        _services=_CommandServices(
            list_sessions=list_sessions,
            resolve_cwd=resolve_cwd,
            set_cwd=set_cwd,
            reset_session=reset_session,
            set_personality=set_personality,
            create_thread=create_thread,
            rename_thread=rename_thread,
            prompt_for_text=prompt_for_text,
            rewrite_input=rewrite_input,
            send_voice=send_voice,
            authorize=registration_is_active,
        ),
    )


async def dispatch_gateway_plugin_command(
    runner: Any,
    event: Any,
    command: str,
) -> GatewayPluginCommandDispatch:
    """Dispatch one plugin command while preserving rewrite fall-through.

    Handler presence is checked again after the context build's awaits. If the
    plugin was disabled/unloaded in that window, the command is reported as
    unmatched rather than swallowing the user's message as a handled ``None``.
    """

    from hermes_cli.plugins import (
        _get_plugin_command_registration,
        invoke_plugin_command,
    )

    plugin_name = str(command or "").replace("_", "-")
    if not plugin_name:
        return GatewayPluginCommandDispatch(matched=False)
    registration = _get_plugin_command_registration(plugin_name)
    if registration is None:
        return GatewayPluginCommandDispatch(matched=False)

    raw_args = event.get_command_args().strip()
    original_text = str(event.text or "")
    invocation = await build_gateway_command_context(
        runner,
        event,
        plugin_name,
        raw_args,
        registration=registration,
    )

    if _get_plugin_command_registration(plugin_name) is not registration:
        return GatewayPluginCommandDispatch(matched=False)

    result = invoke_plugin_command(
        plugin_name,
        raw_args,
        context=invocation,
        registration=registration,
    )
    if inspect.isawaitable(result):
        result = await result

    rewritten = str(event.text or "") != original_text
    if rewritten and not result:
        return GatewayPluginCommandDispatch(
            matched=True,
            continue_as_message=True,
        )
    return GatewayPluginCommandDispatch(
        matched=True,
        response=str(result) if result else None,
    )


async def dispatch_pending_plugin_command_followup(
    runner: Any,
    event: Any,
    session_key: str,
) -> Optional[str | bool]:
    """Reinvoke a plugin command for one requested free-text follow-up.

    ``False`` means no follow-up was consumed.  A different slash command
    cancels the stale prompt and continues normal dispatch.
    """

    pending_map = runner.__dict__.get("_pending_plugin_command_followups", {})
    pending = pending_map.get(session_key)
    if not pending:
        return False
    if str(pending.get("profile") or "") != _profile_name(event.source):
        pending_map.pop(session_key, None)
        return False
    raw = str(event.text or "").strip()
    command_token = event.get_command()
    if command_token in {"cancel", "deny", "stop"} or raw.lower() in {
        "cancel",
        "nevermind",
        "never mind",
    }:
        pending_map.pop(session_key, None)
        return str(
            pending.get("cancel_message") or "Cancelled command follow-up."
        )
    if command_token:
        pending_map.pop(session_key, None)
        return False

    command = str(pending.get("command") or "")
    if not command:
        pending_map.pop(session_key, None)
        return False
    from hermes_cli.plugins import (
        _get_plugin_command_registration,
        invoke_plugin_command,
    )

    # A disable/unload or replacement between prompt and reply must not hand
    # the free text to a different plugin generation or eat it as model input.
    registration = pending.get("registration")
    current_registration = _get_plugin_command_registration(command)
    if current_registration is None or (
        registration is not None and current_registration is not registration
    ):
        pending_map.pop(session_key, None)
        return False

    pending_map.pop(session_key, None)
    context = await build_gateway_command_context(
        runner,
        event,
        command,
        raw,
        registration=current_registration,
    )
    result = invoke_plugin_command(
        command,
        raw,
        context=context,
        registration=current_registration,
    )
    if inspect.isawaitable(result):
        result = await result
    return str(result) if result else None
