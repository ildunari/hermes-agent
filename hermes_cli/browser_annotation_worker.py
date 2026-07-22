"""Dedicated, persistence-isolated execution for browser annotation turns.

This module deliberately has no dependency on ``tui_gateway.server`` and never
registers an annotation agent in the generic ``_sessions`` map.  The durable
lineage repository is the queue and authority; an injected agent factory is the
only bridge into existing AI-session execution machinery.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Protocol

from hermes_cli.browser_annotation_lineage import (
    AnnotationLineageRepository,
    ClaimedAnnotationTurnInput,
)

_CONTEXT_CODEC_VERSION = 1


class AnnotationAgent(Protocol):
    pre_provider_dispatch_callback: Callable[[dict[str, object]], None] | None
    _persist_disabled: bool
    model: str
    tools: list[dict[str, object]]
    _api_max_retries: int
    _fallback_chain: list[object]
    _fallback_index: int
    _credential_pool: object | None
    _annotation_provider_rounds_completed: int
    _disable_streaming: bool
    _annotation_canonical_request_messages: list[dict[str, object]]
    _annotation_canonical_request_tools: list[dict[str, object]]
    _annotation_wire_request_snapshot: dict[str, object]
    _annotation_provider_response_callback: Callable[[dict[str, object]], None]
    _annotation_tool_round_callback: Callable[
        [dict[str, object], list[dict[str, object]]], None
    ]

    def run_conversation(
        self,
        user_message: str,
        *,
        system_message: str | None = None,
        conversation_history: list[dict[str, object]] | None = None,
        task_id: str | None = None,
    ) -> dict[str, object]: ...

    def interrupt(self, message: str | None = None) -> None: ...


AgentFactory = Callable[[ClaimedAnnotationTurnInput], AnnotationAgent]
EvidenceProvider = Callable[[str, str], dict[str, object]]


@dataclass(frozen=True)
class AnnotationWorkerResult:
    claimed: bool
    turn_id: str | None = None
    status: str | None = None
    assistant_message_id: int | None = None
    error_code: str | None = None


def _canonical_json(value: dict[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _provider_text(value: object) -> str:
    """Normalize provider text blocks while ignoring transport cache metadata."""

    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        block_type = value.get("type")
        if block_type not in (None, "text", "input_text", "output_text"):
            raise RuntimeError("annotation provider request contains non-text content")
        for key in ("text", "content"):
            if key in value:
                return _provider_text(value[key])
        raise RuntimeError("annotation provider request contains unknown content")
    if isinstance(value, list):
        return "".join(_provider_text(item) for item in value)
    raise RuntimeError("annotation provider request contains unknown content")


def _without_transport_metadata(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _without_transport_metadata(item)
            for key, item in value.items()
            if key != "cache_control"
        }
    if isinstance(value, list):
        return [_without_transport_metadata(item) for item in value]
    return value


def _normalized_provider_messages(request: dict[str, object]) -> list[dict[str, str]]:
    raw_messages = request.get("messages")
    if not isinstance(raw_messages, list):
        raw_messages = request.get("input")
    if not isinstance(raw_messages, list):
        raise RuntimeError("annotation provider request lacks messages")
    normalized: list[dict[str, str]] = []
    for message in raw_messages:
        if not isinstance(message, dict):
            raise RuntimeError("annotation provider request has malformed messages")
        role = message.get("role")
        if not isinstance(role, str):
            item_type = message.get("type")
            if item_type == "function_call":
                role = "assistant"
            elif item_type == "function_call_output":
                role = "tool"
            else:
                raise RuntimeError("annotation provider request has malformed messages")
            content = _canonical_json(_without_transport_metadata(message))
        else:
            content = _provider_text(message.get("content"))
        normalized_message: dict[str, str] = {"role": role, "content": content}
        for field in ("tool_calls", "tool_call_id", "name"):
            if field in message:
                normalized_message[field] = _canonical_json(
                    _without_transport_metadata(message[field])
                )
        normalized.append(normalized_message)
    external_system = request.get("system", request.get("instructions"))
    if external_system is not None:
        normalized.insert(0, {"role": "system", "content": _provider_text(external_system)})
    return normalized


def _canonical_tool_arguments(value: object) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError("annotation tool arguments are not valid JSON") from exc
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_annotation_messages(
    messages: object,
) -> list[dict[str, object]]:
    """Reduce trusted core messages to the fields governing tool authority."""

    if not isinstance(messages, list):
        raise RuntimeError("annotation canonical request lacks messages")
    canonical: list[dict[str, object]] = []
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("role"), str):
            raise RuntimeError("annotation canonical request has malformed messages")
        role = str(message["role"])
        item: dict[str, object] = {
            "role": role,
            "content": _provider_text(message.get("content", "")),
        }
        if "tool_calls" in message:
            raw_calls = message["tool_calls"]
            if not isinstance(raw_calls, list):
                raise RuntimeError("annotation canonical tool calls are malformed")
            calls: list[dict[str, object]] = []
            for raw_call in raw_calls:
                if not isinstance(raw_call, dict):
                    raise RuntimeError("annotation canonical tool call is malformed")
                function = raw_call.get("function")
                if not isinstance(function, dict):
                    raise RuntimeError("annotation canonical tool function is malformed")
                call_id = raw_call.get("id")
                name = function.get("name")
                if not isinstance(call_id, str) or not isinstance(name, str):
                    raise RuntimeError("annotation canonical tool identity is malformed")
                calls.append(
                    {
                        "id": call_id,
                        "name": name,
                        "arguments": _canonical_tool_arguments(
                            function.get("arguments", {})
                        ),
                    }
                )
            item["tool_calls"] = calls
        if role == "tool":
            call_id = message.get("tool_call_id")
            name = message.get("name")
            if not isinstance(call_id, str) or not isinstance(name, str):
                raise RuntimeError("annotation canonical tool result identity is malformed")
            item["tool_call_id"] = call_id
            item["name"] = name
        canonical.append(item)
    return canonical


def _user_envelope(
    claimed: ClaimedAnnotationTurnInput,
    *,
    body: str,
    anchor_revision_id: str,
    capture_digest: str | None,
    reply_to_message_id: int | None,
    context_digest: str,
    anchor_stale_at_submit: bool,
    turn_sequence: int,
    evidence: dict[str, object],
) -> str:
    """Byte-stable immutable context passed as one provider user message."""

    return _canonical_json(
        {
            "anchorRevisionId": anchor_revision_id,
            "anchorStaleAtSubmit": bool(anchor_stale_at_submit),
            "annotationId": claimed.turn.annotation_id,
            "annotationKind": evidence["annotationKind"],
            "body": body,
            "captureDigest": capture_digest,
            "contextDigest": context_digest,
            "documentTitle": evidence.get("documentTitle"),
            "documentUrl": evidence["documentUrl"],
            "evidenceTrust": "untrusted_page_evidence",
            "replyToMessageId": reply_to_message_id,
            "schemaVersion": _CONTEXT_CODEC_VERSION,
            "selectedText": evidence.get("selectedText"),
            "sourceMessageId": claimed.source_message_id,
            "sourceSessionLineageId": claimed.source_session_lineage_id,
            "threadGeneration": claimed.turn.thread_generation,
            "turnSequence": turn_sequence,
        }
    )


def project_provider_input(
    claimed: ClaimedAnnotationTurnInput,
    evidence_provider: EvidenceProvider,
) -> tuple[list[dict[str, object]], str, str]:
    """Project frozen completed pairs and the exact current-turn envelope.

    Returns ``(history, current_user_message, projection_digest)``.  The digest
    covers the frozen system/history/current projection before the agent builds
    its transport request.  At the exact provider boundary the worker separately
    validates that projection and stores a digest of the final request context.
    """

    def evidence_for(anchor_revision_id: str) -> dict[str, object]:
        evidence = evidence_provider(claimed.turn.annotation_id, anchor_revision_id)
        if not isinstance(evidence.get("annotationKind"), str) or not evidence["annotationKind"]:
            raise ValueError("annotation evidence lacks annotationKind")
        if not isinstance(evidence.get("documentUrl"), str) or not evidence["documentUrl"]:
            raise ValueError("annotation evidence lacks documentUrl")
        return evidence

    history: list[dict[str, object]] = []
    for predecessor in claimed.completed_history:
        predecessor_revision_id = str(predecessor["anchor_revision_id"])
        history.append(
            {
                "role": "user",
                "content": _user_envelope(
                    claimed,
                    body=str(predecessor["user_content"]),
                    anchor_revision_id=predecessor_revision_id,
                    capture_digest=(
                        str(predecessor["capture_digest"])
                        if predecessor["capture_digest"] is not None
                        else None
                    ),
                    reply_to_message_id=(
                        int(predecessor["reply_to_message_id"])
                        if predecessor["reply_to_message_id"] is not None
                        else None
                    ),
                    context_digest=str(predecessor["context_digest"]),
                    anchor_stale_at_submit=bool(
                        predecessor["anchor_stale_at_submit"]
                    ),
                    turn_sequence=int(predecessor["turn_sequence"]),
                    evidence=evidence_for(predecessor_revision_id),
                ),
            }
        )
        history.append(
            {
                "role": "assistant",
                "content": str(predecessor["assistant_content"]),
            }
        )

    current = _user_envelope(
        claimed,
        body=claimed.body,
        anchor_revision_id=claimed.anchor_revision_id,
        capture_digest=claimed.capture_digest,
        reply_to_message_id=claimed.reply_to_message_id,
        context_digest=claimed.context_digest,
        anchor_stale_at_submit=claimed.anchor_stale_at_submit,
        turn_sequence=claimed.turn.turn_sequence,
        evidence=evidence_for(claimed.anchor_revision_id),
    )
    projection_bytes = _canonical_json(
        {
            "current": current,
            "history": history,
            "system": claimed.system_prompt,
        }
    ).encode("utf-8")
    return history, current, hashlib.sha256(projection_bytes).hexdigest()


class AnnotationTurnWorker:
    """Claim and execute exact FIFO annotation turns outside generic sessions."""

    def __init__(
        self,
        repository: AnnotationLineageRepository,
        agent_factory: AgentFactory,
        evidence_provider: EvidenceProvider,
        *,
        worker_id: str | None = None,
        lease_seconds: float = 60.0,
        heartbeat_seconds: float = 15.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if heartbeat_seconds <= 0 or heartbeat_seconds >= lease_seconds:
            raise ValueError("heartbeat_seconds must be positive and shorter than the lease")
        self.repository = repository
        self.agent_factory = agent_factory
        self.evidence_provider = evidence_provider
        self.worker_id = worker_id or f"annotation-worker:{uuid.uuid4().hex}"
        self.lease_seconds = float(lease_seconds)
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.clock = clock

    def run_next(
        self,
        annotation_id: str,
        thread_generation: int,
        *,
        cancellation_event: object | None = None,
    ) -> AnnotationWorkerResult:
        claimed = self.repository.claim_next_turn(
            annotation_id=annotation_id,
            thread_generation=thread_generation,
            lease_owner=self.worker_id,
            lease_seconds=self.lease_seconds,
            now=self.clock(),
        )
        if claimed is None:
            return AnnotationWorkerResult(claimed=False)

        stop_heartbeat = threading.Event()
        heartbeat_error: list[BaseException] = []
        agent_ref: list[AnnotationAgent] = []

        def cancellation_is_set() -> bool:
            if cancellation_event is None:
                return False
            exact = getattr(cancellation_event, "is_turn_cancelled", None)
            if callable(exact):
                return bool(exact(claimed.turn_id))
            generic = getattr(cancellation_event, "is_set", None)
            return bool(generic()) if callable(generic) else False

        def heartbeat() -> None:
            while not stop_heartbeat.wait(self.heartbeat_seconds):
                try:
                    self.repository.renew_turn_lease(
                        claimed.turn_id,
                        lease_owner=self.worker_id,
                        attempt=claimed.attempt,
                        lease_seconds=self.lease_seconds,
                        now=self.clock(),
                    )
                except BaseException as exc:
                    heartbeat_error.append(exc)
                    if agent_ref:
                        try:
                            agent_ref[0].interrupt("annotation lease lost")
                        except Exception:
                            pass
                    return

        heartbeat_thread = threading.Thread(
            target=heartbeat,
            name=f"annotation-heartbeat-{claimed.turn_id}",
            daemon=True,
        )
        heartbeat_thread.start()

        def watch_cancellation() -> None:
            if cancellation_event is None:
                return
            exact_wait = getattr(cancellation_event, "wait_for_turn", None)
            generic_wait = getattr(cancellation_event, "wait", None)
            while not stop_heartbeat.is_set():
                if callable(exact_wait):
                    cancelled = bool(exact_wait(claimed.turn_id, timeout=0.1))
                elif callable(generic_wait):
                    cancelled = bool(generic_wait(timeout=0.1))
                else:
                    return
                if cancelled:
                    break
            if stop_heartbeat.is_set() or not cancellation_is_set():
                return
            if agent_ref:
                try:
                    agent_ref[0].interrupt("annotation turn cancelled")
                except Exception:
                    pass

        cancellation_thread = threading.Thread(
            target=watch_cancellation,
            name=f"annotation-cancellation-{claimed.turn_id}",
            daemon=True,
        )
        cancellation_thread.start()

        try:
            turn_input = self.repository.claimed_turn_input(
                claimed.turn_id,
                lease_owner=self.worker_id,
                attempt=claimed.attempt,
                now=self.clock(),
            )
            history, current, _projection_digest = project_provider_input(
                turn_input, self.evidence_provider
            )
            agent = self.agent_factory(turn_input)
            agent_ref.append(agent)
            if cancellation_is_set():
                raise RuntimeError("annotation turn cancelled")
            expected_tools = turn_input.model_config.get("tools")
            if not isinstance(expected_tools, list):
                raise RuntimeError("annotation runtime snapshot lacks frozen tools")
            if getattr(agent, "model", None) != turn_input.model:
                raise RuntimeError("annotation runtime model does not match snapshot")
            if _without_transport_metadata(getattr(agent, "tools", None)) != expected_tools:
                raise RuntimeError("annotation runtime tools do not match snapshot")
            # Annotation messages are already persisted exactly once by the
            # lineage repository.  Never let the generic agent persistence path
            # duplicate or mutate that transcript.
            agent._persist_disabled = True
            agent._session_db = None
            agent._session_json_enabled = False
            agent.save_trajectories = False
            agent._memory_manager = None
            agent._memory_nudge_interval = 0
            agent._skill_nudge_interval = 0
            agent._skip_mcp_refresh = True
            agent.compression_enabled = False
            agent.codex_app_server_auto_compaction = False
            agent.per_turn_user_context = ""
            agent._annotation_isolated = True
            # A dispatched annotation attempt is outcome-uncertain until its
            # response arrives. Generic retries, credential rotation, or
            # provider fallback could duplicate that side effect, so this
            # dedicated lane permits one network attempt per unique context.
            agent._api_max_retries = 1
            agent._fallback_chain = []
            agent._fallback_index = 0
            agent._credential_pool = None
            agent._annotation_provider_rounds_completed = 0
            agent._disable_streaming = True
            if getattr(agent, "api_mode", None) == "codex_app_server":
                raise RuntimeError("codex app-server annotation turns are unsupported")
        except Exception:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=self.heartbeat_seconds + 1.0)
            cancellation_thread.join(timeout=1.0)
            try:
                self.repository.fail_turn(
                    claimed.turn_id,
                    lease_owner=self.worker_id,
                    attempt=claimed.attempt,
                    error_code="runtime_snapshot_invalid",
                    failed_at=self.clock(),
                )
            except RuntimeError:
                pass
            durable = self.repository.turn(claimed.turn_id)
            durable_status = str(durable.get("status")) if durable else "failed"
            durable_error = (
                durable.get("error_code") if durable else "runtime_snapshot_invalid"
            )
            return AnnotationWorkerResult(
                claimed=True,
                turn_id=claimed.turn_id,
                status=durable_status,
                error_code=str(durable_error) if durable_error else None,
            )

        dispatched = False
        dispatched_request_digests: set[str] = set()
        provider_context_digest: str | None = None
        dispatch_lock = threading.Lock()
        expected_canonical_messages = _canonical_annotation_messages(
            [
                {"role": "system", "content": turn_input.system_prompt},
                *copy.deepcopy(history),
                {"role": "user", "content": current},
            ]
        )
        pending_provider_response: list[dict[str, object]] = []
        agent._annotation_canonical_request_messages = copy.deepcopy(
            expected_canonical_messages
        )
        agent._annotation_canonical_request_tools = copy.deepcopy(expected_tools)

        def on_provider_response(message: dict[str, object]) -> None:
            with dispatch_lock:
                if pending_provider_response:
                    raise RuntimeError(
                        "annotation provider response preceded tool settlement"
                    )
                pending_provider_response.extend(
                    _canonical_annotation_messages([copy.deepcopy(message)])
                )

        def on_tool_round(
            assistant_message: dict[str, object],
            tool_messages: list[dict[str, object]],
        ) -> None:
            with dispatch_lock:
                if len(pending_provider_response) != 1:
                    raise RuntimeError("annotation tool round lacks provider response")
                canonical_assistant = _canonical_annotation_messages(
                    [assistant_message]
                )[0]
                canonical_tools = _canonical_annotation_messages(tool_messages)
                if canonical_assistant != pending_provider_response[0]:
                    raise RuntimeError("annotation provider tool call was mutated")
                if not canonical_tools or any(
                    message.get("role") != "tool" for message in canonical_tools
                ):
                    raise RuntimeError("annotation tool result ledger is malformed")
                expected_canonical_messages.append(
                    pending_provider_response.pop()
                )
                expected_canonical_messages.extend(canonical_tools)

        def before_provider_dispatch(request: dict[str, object]) -> None:
            nonlocal dispatched, provider_context_digest
            with dispatch_lock:
                if cancellation_is_set():
                    raise RuntimeError("annotation turn cancelled")
                if heartbeat_error:
                    raise RuntimeError("annotation lease lost") from heartbeat_error[0]
                canonical_messages = getattr(
                    agent, "_annotation_canonical_request_messages", None
                )
                canonical_tools = getattr(
                    agent, "_annotation_canonical_request_tools", None
                )
                if _canonical_annotation_messages(
                    canonical_messages
                ) != expected_canonical_messages:
                    raise RuntimeError("annotation provider context was mutated")
                if _without_transport_metadata(canonical_tools) != expected_tools:
                    raise RuntimeError("annotation provider tools were mutated")
                wire_snapshot = getattr(
                    agent, "_annotation_wire_request_snapshot", None
                )
                if wire_snapshot is not None:
                    if request != wire_snapshot:
                        raise RuntimeError("annotation provider wire context was mutated")
                elif (
                    not isinstance(request.get("messages"), list)
                    or _canonical_annotation_messages(
                        request.get("messages")
                    )
                    != expected_canonical_messages
                ):
                    # Test-double/backward-compatible Chat fallback. Production
                    # AIAgent always supplies the exact native wire snapshot.
                    raise RuntimeError("annotation provider wire context was mutated")
                if not dispatched:
                    wire_tools = request.get("tools")
                    if isinstance(wire_tools, list) and all(
                        isinstance(tool, dict) and "function" in tool
                        for tool in wire_tools
                    ):
                        if _without_transport_metadata(wire_tools) != expected_tools:
                            raise RuntimeError("annotation provider wire tools were mutated")
                if dispatched:
                    completed_rounds = int(
                        getattr(agent, "_annotation_provider_rounds_completed", 0)
                    )
                    if completed_rounds < len(dispatched_request_digests):
                        raise RuntimeError(
                            "annotation provider request preceded its response"
                        )
                    if len(canonical_messages) <= len(history) + 2:
                        raise RuntimeError("annotation provider request replay refused")
                request_model = request.get("model")
                if request_model is not None and request_model != turn_input.model:
                    raise RuntimeError("annotation provider model was mutated")
                provider_context = {
                    key: request.get(key)
                    for key in (
                        "messages",
                        "input",
                        "system",
                        "instructions",
                        "model",
                        "tools",
                    )
                    if key in request
                }
                provider_context_digest = hashlib.sha256(
                    _canonical_json(provider_context).encode("utf-8")
                ).hexdigest()
                if provider_context_digest in dispatched_request_digests:
                    raise RuntimeError("annotation provider request replay refused")
                dispatched_request_digests.add(provider_context_digest)
                if dispatched:
                    return
                self.repository.mark_dispatched(
                    claimed.turn_id,
                    lease_owner=self.worker_id,
                    attempt=claimed.attempt,
                    now=self.clock(),
                )
                dispatched = True

        agent.pre_provider_dispatch_callback = before_provider_dispatch
        agent._annotation_provider_response_callback = on_provider_response
        agent._annotation_tool_round_callback = on_tool_round
        try:
            result = agent.run_conversation(
                current,
                system_message=turn_input.system_prompt,
                conversation_history=history,
                task_id=claimed.turn_id,
            )
            if heartbeat_error:
                raise RuntimeError("annotation lease lost") from heartbeat_error[0]
            if not dispatched:
                raise RuntimeError("annotation provider was not dispatched")
            if provider_context_digest is None:
                raise RuntimeError("annotation provider context was not frozen")
            if (
                bool(result.get("failed"))
                or result.get("error")
                or result.get("completed") is not True
            ):
                raise RuntimeError("annotation agent failed")
            assistant_body = str(result.get("final_response") or "").strip()
            if not assistant_body:
                self.repository.fail_turn(
                    claimed.turn_id,
                    lease_owner=self.worker_id,
                    attempt=claimed.attempt,
                    error_code="empty_response",
                    failed_at=self.clock(),
                )
                return AnnotationWorkerResult(
                    claimed=True,
                    turn_id=claimed.turn_id,
                    status="failed",
                    error_code="empty_response",
                )
            assistant_message_id = self.repository.complete_turn(
                turn_id=claimed.turn_id,
                lease_owner=self.worker_id,
                attempt=claimed.attempt,
                assistant_body=assistant_body,
                context_digest=provider_context_digest,
                actor_id="hermes:browser-annotation",
                completed_at=self.clock(),
            )
            return AnnotationWorkerResult(
                claimed=True,
                turn_id=claimed.turn_id,
                status="completed",
                assistant_message_id=assistant_message_id,
            )
        except Exception:
            # The exact failure transition may itself be fenced out after lease
            # loss/recovery.  Never overwrite a newer attempt's authority.
            try:
                self.repository.fail_turn(
                    claimed.turn_id,
                    lease_owner=self.worker_id,
                    attempt=claimed.attempt,
                    error_code="agent_error",
                    failed_at=self.clock(),
                )
            except RuntimeError:
                pass
            durable = self.repository.turn(claimed.turn_id)
            durable_status = str(durable.get("status")) if durable else "failed"
            durable_error = durable.get("error_code") if durable else "agent_error"
            return AnnotationWorkerResult(
                claimed=True,
                turn_id=claimed.turn_id,
                status=durable_status,
                error_code=str(durable_error) if durable_error else None,
            )
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=self.heartbeat_seconds + 1.0)
            cancellation_thread.join(timeout=1.0)
