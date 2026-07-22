"""Dedicated authenticated-RPC service for browser annotation threads.

The service is intentionally independent of ``tui_gateway.server``.  Durable
lineage state lives in the active profile's ``state.db`` and execution is
woken only through :class:`AnnotationWorkerRegistry`; generic chat sessions,
``prompt.submit``, source-chat fallback, and mutable turn-context injection are
not part of this path.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Callable

from hermes_cli.browser_annotation_bundle import AnnotationBundleCoordinator
from hermes_cli.browser_annotation_lineage import (
    AcceptedAnnotationMessage,
    AnnotationLineageRepository,
    ClaimedAnnotationTurnInput,
)
from hermes_cli.browser_annotation_worker import AnnotationTurnWorker
from hermes_cli.browser_annotation_worker_registry import AnnotationWorkerRegistry
from hermes_cli.browser_annotations_db import AnnotationRepository
from hermes_cli.browser_annotations_models import AnchorRevision, AnnotationRecordV1


class AnnotationRpcError(RuntimeError):
    """Stable public failure category without request or provider prose."""

    def __init__(self, code: str, *, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = int(status_code)


def _canonical_digest(value: dict[str, object]) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _string_list(snapshot: dict[str, object], key: str) -> list[str] | None:
    value = snapshot.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise RuntimeError(f"annotation runtime snapshot has invalid {key}")
    return [item.strip() for item in value]


def production_annotation_agent_factory(
    profile_home: Path,
) -> Callable[[ClaimedAnnotationTurnInput], object]:
    """Build an isolated AIAgent from one immutable lineage runtime snapshot.

    Credentials are resolved from the active profile at execution time, while
    model, endpoint mode, system bytes, tool filters, and request policy remain
    pinned to the lineage snapshot.  There is deliberately no fallback chain.
    """

    home = Path(profile_home)

    def create(claimed: ClaimedAnnotationTurnInput):
        snapshot = claimed.model_config
        expected_tools = snapshot.get("tools")
        if not isinstance(expected_tools, list):
            raise RuntimeError("annotation runtime snapshot lacks frozen tools")
        requested = snapshot.get("provider")
        if requested is not None and (
            not isinstance(requested, str) or not requested.strip()
        ):
            raise RuntimeError("annotation runtime snapshot has invalid provider")
        base_url = snapshot.get("base_url")
        if base_url is not None and (
            not isinstance(base_url, str) or not base_url.strip()
        ):
            raise RuntimeError("annotation runtime snapshot has invalid base_url")

        from hermes_cli.runtime_provider import resolve_runtime_provider

        resolve_kwargs: dict[str, object] = {
            "requested": requested.strip() if isinstance(requested, str) else None,
            "target_model": claimed.model,
        }
        if isinstance(base_url, str):
            resolve_kwargs["explicit_base_url"] = base_url
        runtime = resolve_runtime_provider(**resolve_kwargs)

        api_mode = snapshot.get("api_mode") or runtime.get("api_mode")
        if api_mode is not None and not isinstance(api_mode, str):
            raise RuntimeError("annotation runtime snapshot has invalid api_mode")
        reasoning = snapshot.get("reasoning_config")
        if reasoning is not None and not isinstance(reasoning, dict):
            raise RuntimeError("annotation runtime snapshot has invalid reasoning_config")
        service_tier = snapshot.get("service_tier")
        if service_tier is not None and not isinstance(service_tier, str):
            raise RuntimeError("annotation runtime snapshot has invalid service_tier")
        request_overrides = snapshot.get("request_overrides")
        if request_overrides is not None and not isinstance(request_overrides, dict):
            raise RuntimeError("annotation runtime snapshot has invalid request_overrides")
        max_iterations = snapshot.get("max_iterations", 90)
        if (
            isinstance(max_iterations, bool)
            or not isinstance(max_iterations, int)
            or max_iterations < 1
            or max_iterations > 1000
        ):
            raise RuntimeError("annotation runtime snapshot has invalid max_iterations")

        from run_agent import AIAgent

        agent = AIAgent(
            model=claimed.model,
            max_iterations=max_iterations,
            provider=runtime.get("provider"),
            base_url=base_url or runtime.get("base_url"),
            api_key=runtime.get("api_key"),
            api_mode=api_mode,
            acp_command=runtime.get("command"),
            acp_args=runtime.get("args"),
            credential_pool=None,
            quiet_mode=True,
            verbose_logging=False,
            reasoning_config=reasoning,
            service_tier=service_tier,
            request_overrides=request_overrides,
            enabled_toolsets=_string_list(snapshot, "enabled_toolsets"),
            disabled_toolsets=_string_list(snapshot, "disabled_toolsets"),
            platform="desktop",
            session_id=claimed.annotation_lineage_root_id,
            session_db=None,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=None,
            save_trajectories=False,
            checkpoints_enabled=False,
            pass_session_id=False,
        )
        # The root stores the already-built, byte-stable prompt.  Supplying it as
        # mutable per-turn context would rebuild the prefix and violate D-021.
        agent._cached_system_prompt = claimed.system_prompt
        agent.session_cwd = claimed.cwd
        if claimed.cwd:
            from tools.terminal_tool import register_task_env_overrides

            register_task_env_overrides(claimed.turn.turn_id, {"cwd": claimed.cwd})
        return agent

    # Retain the bound home for diagnostics/tests without reading another profile.
    create.profile_home = home  # type: ignore[attr-defined]
    return create


def production_annotation_evidence_provider(
    repository: AnnotationRepository,
) -> Callable[[str, str], dict[str, object]]:
    """Resolve only one immutable same-profile anchor revision."""

    def provide(annotation_id: str, anchor_revision_id: str) -> dict[str, object]:
        return repository.immutable_evidence(annotation_id, anchor_revision_id)

    return provide


class _ProfileScopedAnnotationWorker:
    """Install active-profile secrets for the complete provider/tool turn."""

    def __init__(self, worker: AnnotationTurnWorker, profile_home: Path) -> None:
        self.worker = worker
        self.profile_home = Path(profile_home)

    def run_next(
        self,
        annotation_id: str,
        thread_generation: int,
        *,
        cancellation_event: object | None = None,
    ):
        from agent.secret_scope import (
            build_profile_secret_scope,
            reset_secret_scope,
            set_secret_scope,
        )

        token = set_secret_scope(build_profile_secret_scope(self.profile_home))
        result = None
        try:
            result = self.worker.run_next(
                annotation_id,
                thread_generation,
                cancellation_event=cancellation_event,
            )
            return result
        finally:
            reset_secret_scope(token)
            turn_id = getattr(result, "turn_id", None)
            if turn_id:
                try:
                    from tools.terminal_tool import clear_task_env_overrides

                    clear_task_env_overrides(turn_id)
                except Exception:
                    pass


def production_annotation_registry(
    *,
    profile_id: str,
    profile_home: Path,
    lineage_repository: AnnotationLineageRepository | None = None,
    annotation_repository: AnnotationRepository | None = None,
) -> AnnotationWorkerRegistry:
    """Create the one dedicated worker registry for an active profile process."""

    home = Path(profile_home)
    lineage = lineage_repository or AnnotationLineageRepository(
        profile_id=profile_id, profile_home=home
    )
    annotations = annotation_repository or AnnotationRepository(
        profile_id=profile_id, profile_home=home
    )
    agent_factory = production_annotation_agent_factory(home)
    evidence_provider = production_annotation_evidence_provider(annotations)

    def worker_factory() -> _ProfileScopedAnnotationWorker:
        return _ProfileScopedAnnotationWorker(
            AnnotationTurnWorker(lineage, agent_factory, evidence_provider), home
        )

    return AnnotationWorkerRegistry(lineage, worker_factory)


def production_annotation_snapshot_factory(
    *, profile_home: Path, lineage: AnnotationLineageRepository
) -> Callable[[str | None, int | None], dict[str, object]]:
    """Freeze a source root or active-profile default without generic session APIs."""

    home = Path(profile_home)

    def snapshot(source_id: str | None, source_message_id: int | None) -> dict[str, object]:
        source = (
            lineage.source_runtime_snapshot(source_id, source_message_id)
            if source_id is not None
            else None
        )
        from agent.secret_scope import (
            build_profile_secret_scope,
            reset_secret_scope,
            set_secret_scope,
        )
        from hermes_cli.config import load_config
        from hermes_cli.models import get_preferred_silent_default_model
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from run_agent import AIAgent

        cfg = load_config() or {}
        configured_model = cfg.get("model")
        if isinstance(configured_model, dict):
            default_model = str(configured_model.get("default") or "").strip()
            configured_provider = str(configured_model.get("provider") or "").strip() or None
        else:
            default_model = str(configured_model or "").strip()
            configured_provider = None
        model = str(source.get("model") if source else default_model).strip()
        if not model:
            model = get_preferred_silent_default_model()
        model_config = dict(source.get("model_config") or {}) if source else {}
        requested = model_config.get("provider") or configured_provider
        token = set_secret_scope(build_profile_secret_scope(home))
        try:
            runtime = resolve_runtime_provider(
                requested=str(requested).strip() if requested else None,
                target_model=model,
            )
            agent = AIAgent(
                model=model,
                provider=runtime.get("provider"),
                base_url=model_config.get("base_url") or runtime.get("base_url"),
                api_key=runtime.get("api_key"),
                api_mode=model_config.get("api_mode") or runtime.get("api_mode"),
                acp_command=runtime.get("command"),
                acp_args=runtime.get("args"),
                credential_pool=None,
                quiet_mode=True,
                verbose_logging=False,
                reasoning_config=model_config.get("reasoning_config"),
                service_tier=model_config.get("service_tier"),
                enabled_toolsets=_string_list(model_config, "enabled_toolsets"),
                disabled_toolsets=_string_list(model_config, "disabled_toolsets"),
                platform="desktop",
                session_id=f"annotation-snapshot:{uuid.uuid4().hex}",
                session_db=None,
                skip_memory=True,
                fallback_model=None,
                save_trajectories=False,
                checkpoints_enabled=False,
                pass_session_id=False,
            )
            system_prompt = (
                str(source["system_prompt"])
                if source is not None
                else (agent._cached_system_prompt or agent._build_system_prompt(None))
            )
            model_config.update({
                "provider": runtime.get("provider"),
                "base_url": model_config.get("base_url") or runtime.get("base_url"),
                "api_mode": model_config.get("api_mode") or runtime.get("api_mode"),
                "tools": agent.tools or [],
                "max_iterations": int(getattr(agent, "max_iterations", 90)),
            })
        finally:
            reset_secret_scope(token)
        return {
            "model": model,
            "model_config": model_config,
            "system_prompt": system_prompt,
            "cwd": source.get("cwd") if source else None,
        }

    return snapshot


class AnnotationRpcFacade:
    """Narrow active-profile facade used by authenticated HTTP annotation RPCs."""

    def __init__(
        self,
        *,
        profile_id: str,
        annotation_repository: AnnotationRepository,
        lineage_repository: AnnotationLineageRepository,
        worker_registry: AnnotationWorkerRegistry,
        bundle_coordinator: AnnotationBundleCoordinator | None = None,
    ) -> None:
        if (
            annotation_repository.profile_id != profile_id
            or lineage_repository.profile_id != profile_id
        ):
            raise PermissionError("annotation RPC repositories do not match active profile")
        if worker_registry.repository is not lineage_repository:
            raise ValueError("annotation RPC registry must own the dedicated lineage repository")
        self.profile_id = profile_id
        self.annotations = annotation_repository
        self.lineage = lineage_repository
        self.registry = worker_registry
        self.bundle = bundle_coordinator

    def create_annotation(
        self,
        record: AnnotationRecordV1,
        first_revision: AnchorRevision,
        *,
        source_message_id: int | None = None,
    ) -> AnnotationRecordV1:
        if self.bundle is None:
            raise AnnotationRpcError("backend_offline", status_code=503)
        return self.bundle.create(
            record, first_revision, source_message_id=source_message_id
        )

    def export_bundle(
        self, annotation_id: str, *, include_screenshot_bytes: bool = False
    ) -> str:
        if self.bundle is None:
            raise AnnotationRpcError("backend_offline", status_code=503)
        return self.bundle.export(
            annotation_id, include_screenshot_bytes=include_screenshot_bytes
        )

    def delete_bundle(self, annotation_id: str) -> dict[str, object]:
        if self.bundle is None:
            raise AnnotationRpcError("backend_offline", status_code=503)
        return self.bundle.delete(annotation_id)

    def submit_message(
        self,
        *,
        annotation_id: str,
        thread_generation: int,
        body: str,
        intent: str,
        anchor_revision_id: str,
        reply_to_message_id: int | None,
        client_request_id: str,
        actor_id: str,
        anchor_stale_at_submit: bool,
    ) -> dict[str, object]:
        thread_generation = self._thread_generation(thread_generation)
        if not isinstance(anchor_stale_at_submit, bool):
            raise AnnotationRpcError("invalid_request")
        if isinstance(reply_to_message_id, bool) or (
            reply_to_message_id is not None
            and (not isinstance(reply_to_message_id, int) or reply_to_message_id < 1)
        ):
            raise AnnotationRpcError("reply_target_invalid", status_code=409)
        if intent not in {"comment_only", "ask_agent"}:
            raise AnnotationRpcError("invalid_intent")
        try:
            evidence = self.annotations.immutable_evidence(
                annotation_id, anchor_revision_id
            )
        except KeyError as exc:
            code = (
                "annotation_missing"
                if self.annotations.get(annotation_id) is None
                else "revision_conflict"
            )
            raise AnnotationRpcError(code, status_code=404 if code == "annotation_missing" else 409) from exc

        capture_digest = str(evidence["captureDigest"])
        context_digest = _canonical_digest(
            {
                "anchorRevisionId": anchor_revision_id,
                "anchorStaleAtSubmit": bool(anchor_stale_at_submit),
                "annotationId": annotation_id,
                "captureDigest": capture_digest,
                "contextCodecVersion": 1,
                "replyToMessageId": reply_to_message_id,
                "threadGeneration": int(thread_generation),
            }
        )
        try:
            accepted = self.lineage.submit_human_message(
                annotation_id=annotation_id,
                thread_generation=thread_generation,
                body=body,
                intent=intent,  # type: ignore[arg-type]
                anchor_revision_id=anchor_revision_id,
                capture_digest=capture_digest,
                reply_to_message_id=reply_to_message_id,
                context_digest=context_digest,
                client_request_id=client_request_id,
                actor_id=actor_id,
                turn_id=(f"annotation-turn:{uuid.uuid4().hex}" if intent == "ask_agent" else None),
                anchor_stale_at_submit=anchor_stale_at_submit,
            )
        except KeyError as exc:
            raise AnnotationRpcError("thread_orphaned", status_code=404) from exc
        except ValueError as exc:
            if "reply target" in str(exc):
                raise AnnotationRpcError("reply_target_invalid", status_code=409) from exc
            raise AnnotationRpcError("invalid_request") from exc
        except RuntimeError as exc:
            message = str(exc)
            if "idempotency collision" in message:
                raise AnnotationRpcError("duplicate_request", status_code=409) from exc
            if "orphaned" in message:
                raise AnnotationRpcError("thread_orphaned", status_code=409) from exc
            if "read_only" in message or "deleting" in message or "deleted" in message:
                raise AnnotationRpcError("thread_read_only", status_code=409) from exc
            raise

        if intent == "ask_agent":
            self.registry.notify(annotation_id, thread_generation)
        return self._accepted_projection(accepted, intent=intent)

    def thread(self, *, annotation_id: str, thread_generation: int) -> dict[str, object]:
        thread_generation = self._thread_generation(thread_generation)
        if self.annotations.get(annotation_id) is None:
            raise AnnotationRpcError("annotation_missing", status_code=404)
        try:
            return self.lineage.thread_projection(annotation_id, thread_generation)
        except KeyError as exc:
            raise AnnotationRpcError("thread_orphaned", status_code=404) from exc
        except RuntimeError as exc:
            if "orphaned" in str(exc):
                raise AnnotationRpcError("thread_orphaned", status_code=409) from exc
            raise

    def turn(
        self, *, annotation_id: str, thread_generation: int, turn_id: str
    ) -> dict[str, object]:
        thread_generation = self._thread_generation(thread_generation)
        turn = self._require_turn(annotation_id, thread_generation, turn_id)
        return self._turn_projection(turn)

    def cancel(
        self, *, annotation_id: str, thread_generation: int, turn_id: str
    ) -> dict[str, object]:
        thread_generation = self._thread_generation(thread_generation)
        self._require_turn(annotation_id, thread_generation, turn_id)
        status = self.registry.cancel(turn_id)
        turn = self._require_turn(annotation_id, thread_generation, turn_id)
        projection = self._turn_projection(turn)
        projection["outcome"] = "turn_cancelled" if status == "cancelled" else projection["outcome"]
        return projection

    def retry(
        self, *, annotation_id: str, thread_generation: int, turn_id: str
    ) -> dict[str, object]:
        thread_generation = self._thread_generation(thread_generation)
        self._require_turn(annotation_id, thread_generation, turn_id)
        try:
            self.registry.retry(turn_id)
        except RuntimeError as exc:
            if "outcome_unknown" in str(exc):
                raise AnnotationRpcError("outcome_unknown", status_code=409) from exc
            if "only failed" in str(exc):
                current = self._require_turn(annotation_id, thread_generation, turn_id)
                code = {
                    "cancelled": "turn_cancelled",
                    "completed": "completed",
                    "queued": "queued",
                    "running": "running",
                }.get(str(current["status"]), "turn_failed")
                raise AnnotationRpcError(code, status_code=409) from exc
            if "read_only" in str(exc) or "orphaned" in str(exc):
                raise AnnotationRpcError("thread_read_only", status_code=409) from exc
            raise
        return self.turn(
            annotation_id=annotation_id,
            thread_generation=thread_generation,
            turn_id=turn_id,
        )

    @staticmethod
    def _accepted_projection(
        accepted: AcceptedAnnotationMessage, *, intent: str
    ) -> dict[str, object]:
        return {
            "messageId": accepted.message_id,
            "threadSequence": accepted.thread_sequence,
            "turnId": accepted.turn_id,
            "turnSequence": accepted.turn_sequence,
            "duplicate": accepted.duplicate,
            "intent": intent,
            "outcome": (
                "duplicate_request"
                if accepted.duplicate
                else ("queued" if intent == "ask_agent" else "comment_only")
            ),
        }

    def _require_turn(
        self, annotation_id: str, thread_generation: int, turn_id: str
    ) -> dict[str, object]:
        try:
            turn = self.lineage.turn(turn_id)
        except KeyError as exc:
            raise AnnotationRpcError("thread_orphaned", status_code=404) from exc
        if (
            turn is None
            or turn.get("annotation_id") != annotation_id
            or int(turn.get("thread_generation", 0)) != int(thread_generation)
        ):
            raise AnnotationRpcError("turn_failed", status_code=404)
        return turn

    @staticmethod
    def _thread_generation(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise AnnotationRpcError("invalid_request")
        return value

    @staticmethod
    def _turn_projection(turn: dict[str, object]) -> dict[str, object]:
        status = str(turn["status"])
        dispatch = str(turn.get("dispatch_state") or "")
        outcome = {
            "queued": "queued",
            "running": "running",
            "completed": "completed",
            "cancelled": "turn_cancelled",
            "failed": "outcome_unknown" if dispatch == "outcome_unknown" else "turn_failed",
        }.get(status, "turn_failed")
        return {
            "turnId": turn["turn_id"],
            "annotationId": turn["annotation_id"],
            "threadGeneration": turn["thread_generation"],
            "turnSequence": turn["turn_sequence"],
            "triggerMessageId": turn["trigger_message_id"],
            "status": status,
            "attempt": turn["attempt"],
            "assistantMessageId": turn.get("assistant_message_id"),
            "errorCode": turn.get("error_code"),
            "outcome": outcome,
        }
