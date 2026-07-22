"""Stable external element references for the in-app browser adapter.

``agent-browser`` intentionally rebuilds its private ref map on every snapshot.
That is safe for its CLI, but it means an old ``@e1`` may silently address a
new node after the next snapshot.  This module keeps that recycled namespace
behind the adapter and exposes a task-local, process-monotonic namespace.

The registry never returns backend node or frame identifiers.  They are used
only as trusted reconciliation keys between successful snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import secrets
import threading
from typing import Any, Callable, Hashable, Mapping

_REF_RE = re.compile(r"^@?e([1-9][0-9]*)$")
_SNAPSHOT_REF_RE = re.compile(
    r"(?P<prefix>(?<=\[)ref=|(?<=, )ref=)(?P<ref>e[1-9][0-9]*)\b"
)
_BACKEND_ID_KEYS = ("backend_node_id", "backendNodeId", "backendDOMNodeId")
_FRAME_ID_KEYS = ("frame_id", "frameId")


class InAppRefError(ValueError):
    """A stable, content-free adapter failure."""


@dataclass
class _ExternalRef:
    identity: tuple[str, str]
    internal: str | None


class StableExternalRefRegistry:
    """One tab-generation ref registry with a task-lifetime allocator."""

    def __init__(self, allocate: Callable[[], str]) -> None:
        self._allocate = allocate
        self._identity_to_external: dict[tuple[str, str], str] = {}
        self._external: dict[str, _ExternalRef] = {}
        self._snapshot_epoch = 0

    @staticmethod
    def _identity(metadata: Any, *, epoch: int, internal: str) -> tuple[str, str]:
        if not isinstance(metadata, Mapping):
            raise InAppRefError("STALE_REF: snapshot reference identity is unavailable")

        backend_id: Any = None
        for key in _BACKEND_ID_KEYS:
            if key in metadata:
                backend_id = metadata[key]
                break
        if backend_id is None:
            # agent-browser 0.26.0 exposes only role/name in its public ref map.
            # Those values cannot prove node continuity: a replacement "Save"
            # button is indistinguishable from its predecessor. Give every such
            # ref a fresh snapshot-scoped identity. This is deliberately less
            # sticky, but guarantees that another snapshot can never retarget an
            # old external ref. Newer incumbents that expose backendNodeId take
            # the strong reconciliation path below without changing schemas.
            return f"weak-snapshot-{epoch}", internal
        if isinstance(backend_id, bool) or not isinstance(backend_id, (str, int)):
            raise InAppRefError("STALE_REF: snapshot reference identity is unavailable")
        backend_text = str(backend_id)
        if not backend_text or len(backend_text) > 256:
            raise InAppRefError("STALE_REF: snapshot reference identity is unavailable")

        frame_id: Any = "main"
        for key in _FRAME_ID_KEYS:
            if key in metadata:
                frame_id = metadata[key]
                break
        if frame_id is None:
            frame_id = "main"
        if not isinstance(frame_id, str) or not frame_id or len(frame_id) > 256:
            raise InAppRefError("STALE_REF: snapshot frame identity is unavailable")
        return frame_id, backend_text

    @staticmethod
    def _internal_ref(value: Any) -> str:
        if not isinstance(value, str):
            raise InAppRefError("STALE_REF: snapshot reference is malformed")
        match = _REF_RE.fullmatch(value)
        if not match:
            raise InAppRefError("STALE_REF: snapshot reference is malformed")
        return f"e{match.group(1)}"

    def reconcile(self, snapshot: str, refs: Mapping[str, Any]) -> tuple[str, int]:
        """Reconcile one successful snapshot and rewrite its visible ref tokens."""

        if not isinstance(snapshot, str) or not isinstance(refs, Mapping):
            raise InAppRefError("STALE_REF: snapshot reference map is unavailable")

        self._snapshot_epoch += 1
        current: dict[tuple[str, str], str] = {}
        internal_to_external: dict[str, str] = {}
        for raw_internal, metadata in refs.items():
            internal = self._internal_ref(raw_internal)
            identity = self._identity(metadata, epoch=self._snapshot_epoch, internal=internal)
            if identity in current or internal in internal_to_external:
                raise InAppRefError("STALE_REF: snapshot reference identity is ambiguous")
            current[identity] = internal

            external = self._identity_to_external.get(identity)
            if external is None:
                external = self._allocate()
                self._identity_to_external[identity] = external
                self._external[external] = _ExternalRef(identity=identity, internal=internal)
            else:
                self._external[external].internal = internal
            internal_to_external[internal] = external

        # A disappeared node is a tombstone.  Its external number remains in
        # ``_external`` forever for this registry and can never name a successor.
        for identity, external in tuple(self._identity_to_external.items()):
            if identity not in current:
                self._identity_to_external.pop(identity, None)
                self._external[external].internal = None

        def replace(match: re.Match[str]) -> str:
            external = internal_to_external.get(match.group("ref"))
            if external is None:
                # A visible actionable token with no matching trusted metadata
                # would bypass the adapter.  Fail the entire snapshot instead.
                raise InAppRefError("STALE_REF: snapshot contains an unmapped reference")
            return f"ref={external}"

        return _SNAPSHOT_REF_RE.sub(replace, snapshot), len(current)

    def translate(self, external_ref: str) -> str:
        normalized = self._internal_ref(external_ref)
        entry = self._external.get(normalized)
        if entry is None or entry.internal is None:
            raise InAppRefError(f"STALE_REF: {normalized} is stale or unknown")
        return f"@{entry.internal}"

    def invalidate(self) -> None:
        self._identity_to_external.clear()
        for entry in self._external.values():
            entry.internal = None


_lock = threading.RLock()
_registries: dict[Hashable, StableExternalRefRegistry] = {}
_next_by_task: dict[str, int] = {}
# External refs must not recycle when a new gateway process reconstructs an
# otherwise identical task/tab generation. Keep the existing ``e<number>``
# contract, but place each process in a random 96-bit numeric range. The low
# 64 bits remain available for the task-local monotonic counter.
_ref_incarnation_floor = secrets.randbits(96) << 64


def _new_registry(task_id: str) -> StableExternalRefRegistry:
    def allocate() -> str:
        next_value = _next_by_task.get(task_id, 0) + 1
        _next_by_task[task_id] = next_value
        return f"e{_ref_incarnation_floor + next_value}"

    return StableExternalRefRegistry(allocate)


def reconcile_snapshot(
    *, task_id: str, scope: Hashable, snapshot: str, refs: Mapping[str, Any]
) -> tuple[str, int]:
    with _lock:
        registry = _registries.get(scope)
        if registry is None:
            registry = _new_registry(task_id)
            _registries[scope] = registry
        return registry.reconcile(snapshot, refs)


def translate_ref(*, scope: Hashable, external_ref: str) -> str:
    with _lock:
        registry = _registries.get(scope)
        if registry is None:
            normalized = external_ref if str(external_ref).startswith("@") else f"@{external_ref}"
            raise InAppRefError(f"STALE_REF: {normalized.removeprefix('@')} is stale or unknown")
        return registry.translate(external_ref)


def invalidate_scope(scope: Hashable) -> None:
    with _lock:
        registry = _registries.get(scope)
        if registry is not None:
            registry.invalidate()


def retire_task(task_id: str) -> None:
    """Tombstone live task refs without resetting its process-lifetime counter."""

    with _lock:
        for scope, registry in tuple(_registries.items()):
            if isinstance(scope, tuple) and scope and scope[0] == task_id:
                registry.invalidate()
                _registries.pop(scope, None)


def _reset_for_tests(*, incarnation_floor: int = 0) -> None:
    global _ref_incarnation_floor
    with _lock:
        _registries.clear()
        _next_by_task.clear()
        _ref_incarnation_floor = incarnation_floor


__all__ = [
    "InAppRefError",
    "StableExternalRefRegistry",
    "invalidate_scope",
    "reconcile_snapshot",
    "retire_task",
    "translate_ref",
]
