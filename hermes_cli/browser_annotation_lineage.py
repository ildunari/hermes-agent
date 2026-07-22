"""Body-free routing ledger for dedicated browser-annotation conversations.

The ledger lives in the active profile's ``state.db`` beside ``sessions`` and
``messages``. Message bodies are inserted exactly once in ``messages`` in the
same SQLite transaction as their typed context and turn rows. The annotation
metadata/capture repository remains separate and is referenced only by opaque
identifiers and digests.

This module deliberately does not run an agent. It provides the durable queue
and lease boundary consumed by the dedicated annotation RPC/worker.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

from hermes_cli.sqlite_util import write_txn
from hermes_constants import get_hermes_home

SCHEMA_VERSION = 3
ANNOTATION_SESSION_SOURCE = "browser_annotation"

RouteState = Literal["active", "read_only", "orphaned", "deleting", "deleted"]
MessageIntent = Literal["comment_only", "ask_agent", "agent_reply"]
Participation = Literal["excluded", "pending", "committed", "failed", "cancelled"]
TurnStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
DispatchState = Literal["not_dispatched", "dispatched", "outcome_unknown"]
_STABLE_TURN_ERROR_CODES = frozenset(
    {
        "agent_error",
        "context_corrupt",
        "empty_response",
        "interrupted",
        "outcome_unknown",
        "provider_unavailable",
        "runtime_snapshot_invalid",
    }
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS annotation_lineage_schema (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    schema_version INTEGER NOT NULL,
    profile_id TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS annotation_thread_route (
    annotation_id TEXT NOT NULL,
    thread_generation INTEGER NOT NULL CHECK(thread_generation >= 1),
    profile_id TEXT NOT NULL,
    annotation_lineage_root_id TEXT NOT NULL UNIQUE REFERENCES sessions(id),
    source_session_lineage_id TEXT,
    source_message_id INTEGER,
    state TEXT NOT NULL CHECK(state IN
        ('active','read_only','orphaned','deleting','deleted')),
    next_thread_sequence INTEGER NOT NULL CHECK(next_thread_sequence >= 1),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(annotation_id, thread_generation)
);

CREATE TABLE IF NOT EXISTS annotation_message_context (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id),
    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
    annotation_id TEXT NOT NULL,
    thread_generation INTEGER NOT NULL,
    thread_sequence INTEGER NOT NULL CHECK(thread_sequence >= 1),
    anchor_revision_id TEXT NOT NULL,
    capture_digest TEXT,
    reply_to_message_id INTEGER REFERENCES annotation_message_context(message_id),
    intent TEXT NOT NULL CHECK(intent IN ('comment_only','ask_agent','agent_reply')),
    model_participation TEXT NOT NULL CHECK(model_participation IN
        ('excluded','pending','committed','failed','cancelled')),
    triggering_message_id INTEGER REFERENCES annotation_message_context(message_id),
    context_codec_version INTEGER NOT NULL CHECK(context_codec_version = 1),
    context_digest TEXT NOT NULL,
    anchor_stale_at_submit INTEGER NOT NULL CHECK(anchor_stale_at_submit IN (0,1)),
    idempotency_digest TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY(annotation_id, thread_generation)
        REFERENCES annotation_thread_route(annotation_id, thread_generation),
    UNIQUE(annotation_id, thread_generation, thread_sequence),
    UNIQUE(annotation_id, thread_generation, actor_id, client_request_id)
);

CREATE TABLE IF NOT EXISTS annotation_turn_route (
    turn_id TEXT PRIMARY KEY,
    annotation_id TEXT NOT NULL,
    thread_generation INTEGER NOT NULL,
    turn_sequence INTEGER NOT NULL CHECK(turn_sequence >= 1),
    trigger_message_id INTEGER NOT NULL UNIQUE
        REFERENCES annotation_message_context(message_id),
    status TEXT NOT NULL CHECK(status IN
        ('queued','running','completed','failed','cancelled')),
    dispatch_state TEXT NOT NULL CHECK(dispatch_state IN
        ('not_dispatched','dispatched','outcome_unknown')),
    attempt INTEGER NOT NULL CHECK(attempt >= 0),
    lease_owner TEXT,
    lease_expires_at REAL,
    assistant_message_id INTEGER UNIQUE
        REFERENCES annotation_message_context(message_id),
    error_code TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    FOREIGN KEY(annotation_id, thread_generation)
        REFERENCES annotation_thread_route(annotation_id, thread_generation),
    UNIQUE(annotation_id, thread_generation, turn_sequence)
);

CREATE UNIQUE INDEX IF NOT EXISTS annotation_one_running_turn
    ON annotation_turn_route(annotation_id, thread_generation)
    WHERE status = 'running';
CREATE INDEX IF NOT EXISTS annotation_turn_fifo
    ON annotation_turn_route(annotation_id, thread_generation, status, turn_sequence);
"""

_BODY_LIKE_COLUMN_FRAGMENTS = (
    "body",
    "content",
    "prompt",
    "response",
    "excerpt",
    "reasoning",
    "tool_output",
    "partial_delta",
    "screenshot_bytes",
)


def annotation_state_db_path(profile_home: Path | None = None) -> Path:
    home = Path(profile_home) if profile_home is not None else get_hermes_home()
    return home / "state.db"


def _require_text(name: str, value: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{name} must not be empty")
    return result


def _require_time(value: float | None) -> float:
    result = time.time() if value is None else float(value)
    if not math.isfinite(result):
        raise ValueError("timestamp must be finite")
    return result


def _require_digest(name: str, value: str | None, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    digest = _require_text(name, value or "")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return digest


def _idempotency_digest(
    *,
    body: str,
    intent: MessageIntent,
    context_digest: str,
    reply_to_message_id: int | None,
    anchor_stale_at_submit: bool,
) -> str:
    encoded = json.dumps(
        {
            "body": body,
            "intent": intent,
            "contextDigest": context_digest,
            "replyToMessageId": reply_to_message_id,
            "anchorStaleAtSubmit": bool(anchor_stale_at_submit),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class AcceptedAnnotationMessage:
    message_id: int
    thread_sequence: int
    turn_id: str | None
    turn_sequence: int | None
    duplicate: bool


@dataclass(frozen=True)
class ClaimedAnnotationTurn:
    turn_id: str
    annotation_id: str
    thread_generation: int
    turn_sequence: int
    trigger_message_id: int
    attempt: int
    lease_owner: str
    lease_expires_at: float


@dataclass(frozen=True)
class ClaimedAnnotationTurnInput:
    """Immutable execution input for one turn held by an exact worker lease."""

    turn: ClaimedAnnotationTurn
    annotation_lineage_root_id: str
    source_session_lineage_id: str | None
    source_message_id: int | None
    system_prompt: str
    model: str
    model_config: dict[str, object]
    cwd: str | None
    body: str
    anchor_revision_id: str
    capture_digest: str | None
    reply_to_message_id: int | None
    context_digest: str
    anchor_stale_at_submit: bool
    completed_history: tuple[dict[str, object], ...]


class AnnotationLineageRepository:
    """Transactional annotation lineage queue bound to one profile state DB."""

    def __init__(
        self,
        *,
        profile_id: str,
        profile_home: Path | None = None,
        state_db_path: Path | None = None,
        private_workspace: bool = False,
    ) -> None:
        self.profile_id = _require_text("profile_id", profile_id)
        if state_db_path is not None and profile_home is not None:
            raise ValueError("pass profile_home or state_db_path, not both")
        self.db_path = (
            Path(state_db_path)
            if state_db_path is not None
            else annotation_state_db_path(profile_home)
        )
        self.private_workspace = bool(private_workspace)

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if self.private_workspace:
            raise PermissionError("private browser workspaces cannot persist annotation threads")
        if not self.db_path.exists():
            raise FileNotFoundError("profile state.db must be initialized before annotation lineage")
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            from hermes_state import apply_wal_with_fallback

            apply_wal_with_fallback(conn, db_label="annotation lineage")
            self._install_schema(conn)
            yield conn
        finally:
            conn.close()

    def _install_schema(self, conn: sqlite3.Connection) -> None:
        required = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('sessions','messages')"
            ).fetchall()
        }
        if required != {"sessions", "messages"}:
            raise RuntimeError("profile state.db lacks sessions/messages schema")
        # ``sqlite3.executescript`` owns its transaction boundary and commits any
        # transaction already in progress. Run idempotent DDL first, then bind the
        # profile identity under the normal writer fence.
        conn.executescript(_SCHEMA)
        with write_txn(conn):
            identity = conn.execute(
                "SELECT schema_version, profile_id FROM annotation_lineage_schema WHERE singleton=1"
            ).fetchone()
            if identity is None:
                conn.execute(
                    "INSERT INTO annotation_lineage_schema(singleton,schema_version,profile_id) VALUES(1,?,?)",
                    (SCHEMA_VERSION, self.profile_id),
                )
            elif identity["profile_id"] != self.profile_id:
                raise PermissionError("annotation lineage repository belongs to another profile")
            elif int(identity["schema_version"]) in {1, 2}:
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(annotation_message_context)")
                }
                if "anchor_stale_at_submit" not in columns:
                    conn.execute(
                        """ALTER TABLE annotation_message_context
                           ADD COLUMN anchor_stale_at_submit INTEGER NOT NULL DEFAULT 0
                           CHECK(anchor_stale_at_submit IN (0,1))"""
                    )
                legacy_rows = conn.execute(
                    """SELECT c.message_id,c.intent,c.context_digest,
                              c.reply_to_message_id,c.anchor_stale_at_submit,m.content
                       FROM annotation_message_context c
                       JOIN messages m ON m.id=c.message_id"""
                ).fetchall()
                for legacy in legacy_rows:
                    conn.execute(
                        """UPDATE annotation_message_context SET idempotency_digest=?
                           WHERE message_id=?""",
                        (
                            _idempotency_digest(
                                body=legacy["content"],
                                intent=legacy["intent"],
                                context_digest=legacy["context_digest"],
                                reply_to_message_id=legacy["reply_to_message_id"],
                                anchor_stale_at_submit=bool(
                                    legacy["anchor_stale_at_submit"]
                                ),
                            ),
                            legacy["message_id"],
                        ),
                    )
                conn.execute(
                    "UPDATE annotation_lineage_schema SET schema_version=? WHERE singleton=1",
                    (SCHEMA_VERSION,),
                )
            elif int(identity["schema_version"]) != SCHEMA_VERSION:
                raise RuntimeError("unsupported annotation lineage schema version")
        self._validate_body_free_schema(conn)

    @staticmethod
    def _validate_body_free_schema(conn: sqlite3.Connection) -> None:
        for table in (
            "annotation_thread_route",
            "annotation_message_context",
            "annotation_turn_route",
        ):
            columns = [row[1].lower() for row in conn.execute(f"PRAGMA table_info({table})")]
            forbidden = [
                column
                for column in columns
                if any(fragment in column for fragment in _BODY_LIKE_COLUMN_FRAGMENTS)
            ]
            if forbidden:
                raise RuntimeError(f"annotation ledger contains body-like columns: {forbidden}")

    def create_lineage(
        self,
        *,
        annotation_id: str,
        annotation_lineage_root_id: str,
        thread_generation: int = 1,
        source_session_lineage_id: str | None = None,
        source_message_id: int | None = None,
        model: str | None = None,
        model_config: dict[str, object] | None = None,
        system_prompt: str | None = None,
        cwd: str | None = None,
        created_at: float | None = None,
    ) -> None:
        annotation_id = _require_text("annotation_id", annotation_id)
        root_id = _require_text("annotation_lineage_root_id", annotation_lineage_root_id)
        if thread_generation < 1:
            raise ValueError("thread_generation must be positive")
        when = _require_time(created_at)
        if source_session_lineage_id is not None:
            source_session_lineage_id = _require_text(
                "source_session_lineage_id", source_session_lineage_id
            )
            if source_session_lineage_id == root_id:
                raise ValueError("annotation lineage must differ from source lineage")
        clean_config = dict(model_config or {})
        for key in tuple(clean_config):
            if key.startswith(("_branched_", "_delegate_", "_proactive_")):
                clean_config.pop(key, None)
        clean_config["_session_kind"] = "browser_annotation"
        encoded_config = json.dumps(clean_config, sort_keys=True, separators=(",", ":"))

        with self.connect() as conn, write_txn(conn):
            if conn.execute("SELECT 1 FROM sessions WHERE id=?", (root_id,)).fetchone():
                raise ValueError("annotation lineage root id already exists")
            if source_session_lineage_id is not None:
                source = conn.execute(
                    "SELECT id,parent_session_id FROM sessions WHERE id=?",
                    (source_session_lineage_id,),
                ).fetchone()
                if source is None or source["parent_session_id"] is not None:
                    raise ValueError("source lineage must name an existing session root")
                if source_message_id is not None:
                    source_message = conn.execute(
                        "SELECT session_id FROM messages WHERE id=?", (source_message_id,)
                    ).fetchone()
                    if source_message is None or not self._session_descends_from(
                        conn, source_message["session_id"], source_session_lineage_id
                    ):
                        raise ValueError("source message does not belong to source lineage")
            elif source_message_id is not None:
                raise ValueError("source_message_id requires source_session_lineage_id")

            conn.execute(
                """INSERT INTO sessions(
                       id,source,model,model_config,system_prompt,parent_session_id,
                       started_at,last_active,cwd,archived
                   ) VALUES(?,?,?,?,?,NULL,?,?,?,1)""",
                (
                    root_id,
                    ANNOTATION_SESSION_SOURCE,
                    model,
                    encoded_config,
                    system_prompt,
                    when,
                    when,
                    cwd,
                ),
            )
            conn.execute(
                """INSERT INTO annotation_thread_route(
                       annotation_id,thread_generation,profile_id,
                       annotation_lineage_root_id,source_session_lineage_id,
                       source_message_id,state,next_thread_sequence,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,'active',1,?,?)""",
                (
                    annotation_id,
                    thread_generation,
                    self.profile_id,
                    root_id,
                    source_session_lineage_id,
                    source_message_id,
                    when,
                    when,
                ),
            )

    def lineage_matches(
        self,
        *,
        annotation_id: str,
        thread_generation: int,
        annotation_lineage_root_id: str,
    ) -> bool:
        """Return whether an exact dedicated route/root pair already committed."""

        with self.connect() as conn:
            row = conn.execute(
                """SELECT r.annotation_lineage_root_id,s.source
                   FROM annotation_thread_route r
                   JOIN sessions s ON s.id=r.annotation_lineage_root_id
                   WHERE r.annotation_id=? AND r.thread_generation=? AND r.profile_id=?""",
                (annotation_id, int(thread_generation), self.profile_id),
            ).fetchone()
        return bool(
            row is not None
            and row["annotation_lineage_root_id"] == annotation_lineage_root_id
            and row["source"] == ANNOTATION_SESSION_SOURCE
        )

    def source_runtime_snapshot(
        self, source_session_lineage_id: str, source_message_id: int | None = None
    ) -> dict[str, object]:
        """Copy only immutable runtime/provenance fields from one profile-local root."""

        source_id = _require_text("source_session_lineage_id", source_session_lineage_id)
        with self.connect() as conn:
            row = conn.execute(
                """SELECT id,parent_session_id,source,model,model_config,system_prompt,cwd
                   FROM sessions WHERE id=?""",
                (source_id,),
            ).fetchone()
            if (
                row is None
                or row["parent_session_id"] is not None
                or row["source"] == ANNOTATION_SESSION_SOURCE
            ):
                raise ValueError("source lineage must name an ordinary session root")
            if source_message_id is not None:
                message = conn.execute(
                    "SELECT session_id FROM messages WHERE id=?", (int(source_message_id),)
                ).fetchone()
                if message is None or not self._session_descends_from(
                    conn, message["session_id"], source_id
                ):
                    raise ValueError("source message does not belong to source lineage")
        if not isinstance(row["model"], str) or not row["model"].strip():
            raise RuntimeError("source session has no resolved model snapshot")
        if not isinstance(row["system_prompt"], str) or not row["system_prompt"]:
            raise RuntimeError("source session has no stable system prompt snapshot")
        try:
            model_config = json.loads(row["model_config"] or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("source session runtime snapshot is malformed") from exc
        if not isinstance(model_config, dict):
            raise RuntimeError("source session runtime snapshot is malformed")
        for key in tuple(model_config):
            if key.startswith(("_branched_", "_delegate_", "_proactive_")):
                model_config.pop(key, None)
        return {
            "model": row["model"].strip(),
            "model_config": model_config,
            "system_prompt": row["system_prompt"],
            "cwd": row["cwd"],
        }

    def begin_bundle_delete(
        self, annotation_id: str, *, changed_at: float | None = None
    ) -> tuple[str, ...]:
        """Freeze all generations read-only and durably cancel pending work."""

        annotation_id = _require_text("annotation_id", annotation_id)
        when = _require_time(changed_at)
        with self.connect() as conn, write_txn(conn):
            routes = conn.execute(
                """SELECT thread_generation,state FROM annotation_thread_route
                   WHERE annotation_id=? AND profile_id=? ORDER BY thread_generation""",
                (annotation_id, self.profile_id),
            ).fetchall()
            if not routes:
                return ()
            if any(
                row["state"] not in {"active", "read_only", "orphaned", "deleting"}
                for row in routes
            ):
                raise RuntimeError("annotation thread cannot enter bundle deletion")
            turn_rows = conn.execute(
                """SELECT turn_id FROM annotation_turn_route
                   WHERE annotation_id=? AND status IN ('queued','running')
                   ORDER BY thread_generation,turn_sequence""",
                (annotation_id,),
            ).fetchall()
            conn.execute(
                """UPDATE annotation_thread_route SET state='deleting',updated_at=?
                   WHERE annotation_id=? AND profile_id=? AND state!='deleting'""",
                (when, annotation_id, self.profile_id),
            )
            conn.execute(
                """UPDATE annotation_turn_route
                   SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL,
                       error_code='interrupted',finished_at=?
                   WHERE annotation_id=? AND status IN ('queued','running')""",
                (when, annotation_id),
            )
            conn.execute(
                """UPDATE annotation_message_context SET model_participation='cancelled'
                   WHERE annotation_id=? AND model_participation='pending'""",
                (annotation_id,),
            )
        return tuple(str(row["turn_id"]) for row in turn_rows)

    def deleting_annotations(self) -> tuple[str, ...]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT annotation_id FROM annotation_thread_route
                   WHERE profile_id=? AND state='deleting' ORDER BY annotation_id""",
                (self.profile_id,),
            ).fetchall()
        return tuple(str(row["annotation_id"]) for row in rows)

    def delete_bundle_lineage(self, annotation_id: str) -> bool:
        """Remove only the dedicated roots, descendants, ledgers, and message bodies."""

        annotation_id = _require_text("annotation_id", annotation_id)
        with self.connect() as conn, write_txn(conn):
            routes = conn.execute(
                """SELECT annotation_lineage_root_id,state FROM annotation_thread_route
                   WHERE annotation_id=? AND profile_id=?""",
                (annotation_id, self.profile_id),
            ).fetchall()
            if not routes:
                return False
            if any(row["state"] != "deleting" for row in routes):
                raise RuntimeError("annotation thread is not deleting")
            roots = [str(row["annotation_lineage_root_id"]) for row in routes]
            session_ids: set[str] = set(roots)
            frontier = list(roots)
            while frontier:
                placeholders = ",".join("?" for _ in frontier)
                children = [
                    str(row["id"])
                    for row in conn.execute(
                        f"SELECT id FROM sessions WHERE parent_session_id IN ({placeholders})",
                        frontier,
                    ).fetchall()
                    if str(row["id"]) not in session_ids
                ]
                session_ids.update(children)
                frontier = children
            conn.execute(
                "DELETE FROM annotation_turn_route WHERE annotation_id=?", (annotation_id,)
            )
            conn.execute(
                "DELETE FROM annotation_message_context WHERE annotation_id=?",
                (annotation_id,),
            )
            conn.execute(
                "DELETE FROM annotation_thread_route WHERE annotation_id=? AND profile_id=?",
                (annotation_id, self.profile_id),
            )
            if session_ids:
                all_ids = list(session_ids)
                all_placeholders = ",".join("?" for _ in all_ids)
                conn.execute(
                    f"DELETE FROM messages WHERE session_id IN ({all_placeholders})", all_ids
                )
                remaining = list(all_ids)
                while remaining:
                    placeholders = ",".join("?" for _ in remaining)
                    removed = conn.execute(
                        f"""DELETE FROM sessions WHERE id IN ({placeholders})
                            AND NOT EXISTS (SELECT 1 FROM sessions child
                                            WHERE child.parent_session_id=sessions.id)""",
                        remaining,
                    ).rowcount
                    if removed == 0:
                        raise RuntimeError(
                            "annotation lineage contains an undeletable session cycle"
                        )
                    remaining = [
                        str(row["id"])
                        for row in conn.execute(
                            f"SELECT id FROM sessions WHERE id IN ({all_placeholders})",
                            all_ids,
                        ).fetchall()
                    ]
            return True

    def frozen_export_snapshot(
        self, conn: sqlite3.Connection, annotation_id: str
    ) -> dict[str, object]:
        """Project all dedicated generations while the caller holds a writer fence."""

        routes = conn.execute(
            """SELECT * FROM annotation_thread_route
               WHERE annotation_id=? AND profile_id=? ORDER BY thread_generation""",
            (annotation_id, self.profile_id),
        ).fetchall()
        if not routes:
            raise KeyError("annotation thread is unavailable in the active profile")
        generations: list[dict[str, object]] = []
        for route in routes:
            root_id = str(route["annotation_lineage_root_id"])
            root = conn.execute(
                """SELECT id,source,model,model_config,system_prompt,started_at,cwd
                   FROM sessions WHERE id=?""",
                (root_id,),
            ).fetchone()
            if root is None or root["source"] != ANNOTATION_SESSION_SOURCE:
                raise RuntimeError("annotation thread is orphaned")
            contexts = conn.execute(
                """SELECT * FROM annotation_message_context
                   WHERE annotation_id=? AND thread_generation=?
                   ORDER BY thread_sequence,message_id""",
                (annotation_id, route["thread_generation"]),
            ).fetchall()
            turns = conn.execute(
                """SELECT * FROM annotation_turn_route
                   WHERE annotation_id=? AND thread_generation=? ORDER BY turn_sequence""",
                (annotation_id, route["thread_generation"]),
            ).fetchall()
            message_ids = [int(row["message_id"]) for row in contexts]
            messages_by_id: dict[int, sqlite3.Row] = {}
            if message_ids:
                placeholders = ",".join("?" for _ in message_ids)
                messages_by_id = {
                    int(row["id"]): row
                    for row in conn.execute(
                        f"SELECT id,role,content,timestamp FROM messages WHERE id IN ({placeholders})",
                        message_ids,
                    ).fetchall()
                }
            if set(messages_by_id) != set(message_ids):
                raise RuntimeError("annotation history linkage is corrupt")
            turn_by_trigger = {int(row["trigger_message_id"]): row for row in turns}
            consumed: set[int] = set()
            ordered_ids: list[int] = []
            for context in contexts:
                message_id = int(context["message_id"])
                if message_id in consumed:
                    continue
                if context["intent"] == "agent_reply":
                    ordered_ids.append(message_id)
                    continue
                ordered_ids.append(message_id)
                turn = turn_by_trigger.get(message_id)
                if (
                    turn is not None
                    and turn["status"] == "completed"
                    and turn["assistant_message_id"] is not None
                ):
                    assistant_id = int(turn["assistant_message_id"])
                    if assistant_id not in messages_by_id:
                        raise RuntimeError("annotation history linkage is corrupt")
                    ordered_ids.append(assistant_id)
                    consumed.add(assistant_id)
            generations.append({
                "route": {
                    "annotationId": route["annotation_id"],
                    "threadGeneration": route["thread_generation"],
                    "annotationLineageRootId": root_id,
                    "sourceSessionLineageId": route["source_session_lineage_id"],
                    "sourceMessageId": route["source_message_id"],
                    "state": route["state"],
                    "nextThreadSequence": route["next_thread_sequence"],
                    "createdAt": route["created_at"],
                    "updatedAt": route["updated_at"],
                },
                "contexts": [
                    {
                        "messageId": row["message_id"],
                        "schemaVersion": row["schema_version"],
                        "threadSequence": row["thread_sequence"],
                        "anchorRevisionId": row["anchor_revision_id"],
                        "captureDigest": row["capture_digest"],
                        "replyToMessageId": row["reply_to_message_id"],
                        "intent": row["intent"],
                        "modelParticipation": row["model_participation"],
                        "triggeringMessageId": row["triggering_message_id"],
                        "contextCodecVersion": row["context_codec_version"],
                        "contextDigest": row["context_digest"],
                        "anchorStaleAtSubmit": bool(row["anchor_stale_at_submit"]),
                        "clientRequestId": row["client_request_id"],
                        "actorId": row["actor_id"],
                        "createdAt": row["created_at"],
                    }
                    for row in contexts
                ],
                "turns": [
                    {
                        "turnId": row["turn_id"],
                        "turnSequence": row["turn_sequence"],
                        "triggerMessageId": row["trigger_message_id"],
                        "status": row["status"],
                        "dispatchState": row["dispatch_state"],
                        "attempt": row["attempt"],
                        "leaseOwner": row["lease_owner"],
                        "leaseExpiresAt": row["lease_expires_at"],
                        "assistantMessageId": row["assistant_message_id"],
                        "errorCode": row["error_code"],
                        "createdAt": row["created_at"],
                        "startedAt": row["started_at"],
                        "finishedAt": row["finished_at"],
                    }
                    for row in turns
                ],
                "sessionProjection": {
                    "root": {
                        "sessionLineageId": root_id,
                        "model": root["model"],
                        "modelConfig": json.loads(root["model_config"] or "{}"),
                        "systemPrompt": root["system_prompt"],
                        "cwd": root["cwd"],
                        "startedAt": root["started_at"],
                    },
                    "orderedMessageIds": ordered_ids,
                    "messages": [
                        {
                            "messageId": message_id,
                            "role": messages_by_id[message_id]["role"],
                            "content": messages_by_id[message_id]["content"],
                            "timestamp": messages_by_id[message_id]["timestamp"],
                        }
                        for message_id in ordered_ids
                    ],
                },
            })
        return {"threadGenerations": generations}

    def submit_human_message(
        self,
        *,
        annotation_id: str,
        thread_generation: int,
        body: str,
        intent: Literal["comment_only", "ask_agent"],
        anchor_revision_id: str,
        capture_digest: str | None,
        reply_to_message_id: int | None,
        context_digest: str,
        client_request_id: str,
        actor_id: str,
        turn_id: str | None = None,
        anchor_stale_at_submit: bool = False,
        created_at: float | None = None,
    ) -> AcceptedAnnotationMessage:
        if intent not in {"comment_only", "ask_agent"}:
            raise ValueError("human annotation intent must be comment_only or ask_agent")
        body = _require_text("body", body)
        annotation_id = _require_text("annotation_id", annotation_id)
        anchor_revision_id = _require_text("anchor_revision_id", anchor_revision_id)
        context_digest = _require_digest("context_digest", context_digest)  # type: ignore[assignment]
        capture_digest = _require_digest("capture_digest", capture_digest, nullable=True)
        client_request_id = _require_text("client_request_id", client_request_id)
        actor_id = _require_text("actor_id", actor_id)
        when = _require_time(created_at)
        if intent == "ask_agent":
            turn_id = _require_text("turn_id", turn_id or "")
        elif turn_id is not None:
            raise ValueError("comment_only cannot create an agent turn")
        idem_digest = _idempotency_digest(
            body=body,
            intent=intent,
            context_digest=context_digest,
            reply_to_message_id=reply_to_message_id,
            anchor_stale_at_submit=anchor_stale_at_submit,
        )

        with self.connect() as conn, write_txn(conn):
            route = self._require_route(conn, annotation_id, thread_generation)
            existing = conn.execute(
                """SELECT c.message_id,c.thread_sequence,c.idempotency_digest,
                          t.turn_id,t.turn_sequence
                   FROM annotation_message_context c
                   LEFT JOIN annotation_turn_route t ON t.trigger_message_id=c.message_id
                   WHERE c.annotation_id=? AND c.thread_generation=?
                     AND c.actor_id=? AND c.client_request_id=?""",
                (annotation_id, thread_generation, actor_id, client_request_id),
            ).fetchone()
            if existing is not None:
                if existing["idempotency_digest"] != idem_digest:
                    raise RuntimeError("annotation request idempotency collision")
                return AcceptedAnnotationMessage(
                    message_id=existing["message_id"],
                    thread_sequence=existing["thread_sequence"],
                    turn_id=existing["turn_id"],
                    turn_sequence=existing["turn_sequence"],
                    duplicate=True,
                )
            if route["state"] != "active":
                raise RuntimeError(f"annotation thread is {route['state']}")
            if reply_to_message_id is not None:
                reply = conn.execute(
                    """SELECT 1 FROM annotation_message_context
                       WHERE message_id=? AND annotation_id=? AND thread_generation=?""",
                    (reply_to_message_id, annotation_id, thread_generation),
                ).fetchone()
                if reply is None:
                    raise ValueError("reply target is not in this annotation thread")
            tip = self._compression_tip(conn, route["annotation_lineage_root_id"])
            sequence = int(route["next_thread_sequence"])
            message_id = self._insert_message(conn, tip, "user", body, when)
            participation: Participation = "excluded" if intent == "comment_only" else "pending"
            conn.execute(
                """INSERT INTO annotation_message_context(
                       message_id,schema_version,annotation_id,thread_generation,
                       thread_sequence,anchor_revision_id,capture_digest,
                       reply_to_message_id,intent,model_participation,
                       triggering_message_id,context_codec_version,context_digest,
                       anchor_stale_at_submit,idempotency_digest,client_request_id,
                       actor_id,created_at
                   ) VALUES(?,1,?,?,?,?,?,?,?, ?,NULL,1,?,?,?,?,?,?)""",
                (
                    message_id,
                    annotation_id,
                    thread_generation,
                    sequence,
                    anchor_revision_id,
                    capture_digest,
                    reply_to_message_id,
                    intent,
                    participation,
                    context_digest,
                    int(anchor_stale_at_submit),
                    idem_digest,
                    client_request_id,
                    actor_id,
                    when,
                ),
            )
            conn.execute(
                """UPDATE annotation_thread_route
                   SET next_thread_sequence=?,updated_at=?
                   WHERE annotation_id=? AND thread_generation=? AND profile_id=?""",
                (sequence + 1, when, annotation_id, thread_generation, self.profile_id),
            )
            turn_sequence = None
            if intent == "ask_agent":
                turn_sequence = int(
                    conn.execute(
                        """SELECT COALESCE(MAX(turn_sequence),0)+1
                           FROM annotation_turn_route
                           WHERE annotation_id=? AND thread_generation=?""",
                        (annotation_id, thread_generation),
                    ).fetchone()[0]
                )
                conn.execute(
                    """INSERT INTO annotation_turn_route(
                           turn_id,annotation_id,thread_generation,turn_sequence,
                           trigger_message_id,status,dispatch_state,attempt,
                           lease_owner,lease_expires_at,assistant_message_id,error_code,
                           created_at,started_at,finished_at
                       ) VALUES(?,?,?,?,?,'queued','not_dispatched',0,NULL,NULL,NULL,NULL,?,NULL,NULL)""",
                    (
                        turn_id,
                        annotation_id,
                        thread_generation,
                        turn_sequence,
                        message_id,
                        when,
                    ),
                )
            return AcceptedAnnotationMessage(
                message_id=message_id,
                thread_sequence=sequence,
                turn_id=turn_id,
                turn_sequence=turn_sequence,
                duplicate=False,
            )

    def claim_next_turn(
        self,
        *,
        annotation_id: str,
        thread_generation: int,
        lease_owner: str,
        lease_seconds: float,
        now: float | None = None,
    ) -> ClaimedAnnotationTurn | None:
        lease_owner = _require_text("lease_owner", lease_owner)
        when = _require_time(now)
        lease_seconds = float(lease_seconds)
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be finite and positive")
        with self.connect() as conn, write_txn(conn):
            self._require_active_route(conn, annotation_id, thread_generation)
            if conn.execute(
                """SELECT 1 FROM annotation_turn_route
                   WHERE annotation_id=? AND thread_generation=? AND status='running'""",
                (annotation_id, thread_generation),
            ).fetchone():
                return None
            row = conn.execute(
                """SELECT * FROM annotation_turn_route
                   WHERE annotation_id=? AND thread_generation=? AND status='queued'
                   ORDER BY turn_sequence LIMIT 1""",
                (annotation_id, thread_generation),
            ).fetchone()
            if row is None:
                return None
            expires = when + lease_seconds
            changed = conn.execute(
                """UPDATE annotation_turn_route
                   SET status='running',lease_owner=?,lease_expires_at=?,started_at=?,error_code=NULL
                   WHERE turn_id=? AND status='queued'""",
                (lease_owner, expires, when, row["turn_id"]),
            )
            if changed.rowcount != 1:
                return None
            return ClaimedAnnotationTurn(
                turn_id=row["turn_id"],
                annotation_id=row["annotation_id"],
                thread_generation=row["thread_generation"],
                turn_sequence=row["turn_sequence"],
                trigger_message_id=row["trigger_message_id"],
                attempt=row["attempt"],
                lease_owner=lease_owner,
                lease_expires_at=expires,
            )

    def mark_dispatched(
        self,
        turn_id: str,
        *,
        lease_owner: str,
        attempt: int,
        now: float | None = None,
    ) -> None:
        when = _require_time(now)
        with self.connect() as conn, write_txn(conn):
            changed = conn.execute(
                """UPDATE annotation_turn_route SET dispatch_state='dispatched'
                   WHERE turn_id=? AND status='running' AND lease_owner=? AND attempt=?
                     AND lease_expires_at > ? AND dispatch_state='not_dispatched'
                     AND EXISTS (
                         SELECT 1 FROM annotation_thread_route r
                         JOIN sessions s ON s.id=r.annotation_lineage_root_id
                         WHERE r.annotation_id=annotation_turn_route.annotation_id
                           AND r.thread_generation=annotation_turn_route.thread_generation
                           AND r.profile_id=? AND r.state='active' AND s.source=?
                     )""",
                (
                    _require_text("turn_id", turn_id),
                    _require_text("lease_owner", lease_owner),
                    int(attempt),
                    when,
                    self.profile_id,
                    ANNOTATION_SESSION_SOURCE,
                ),
            )
            if changed.rowcount != 1:
                raise RuntimeError("turn is not held in a pre-dispatch state by this lease")

    def recover_expired_turns(self, *, now: float | None = None) -> dict[str, int]:
        when = _require_time(now)
        with self.connect() as conn, write_txn(conn):
            requeued = conn.execute(
                """UPDATE annotation_turn_route
                   SET status='queued',attempt=attempt+1,lease_owner=NULL,
                       lease_expires_at=NULL,started_at=NULL,error_code='lease_expired_before_dispatch'
                   WHERE status='running' AND dispatch_state='not_dispatched'
                     AND lease_expires_at <= ?""",
                (when,),
            ).rowcount
            uncertain = conn.execute(
                """UPDATE annotation_turn_route
                   SET status='failed',dispatch_state='outcome_unknown',lease_owner=NULL,
                       lease_expires_at=NULL,finished_at=?,error_code='outcome_unknown'
                   WHERE status='running' AND dispatch_state='dispatched'
                     AND lease_expires_at <= ?""",
                (when, when),
            ).rowcount
            conn.execute(
                """UPDATE annotation_message_context SET model_participation='failed'
                   WHERE model_participation='pending'
                     AND message_id IN (
                         SELECT trigger_message_id FROM annotation_turn_route
                         WHERE status='failed' AND dispatch_state='outcome_unknown'
                     )"""
            )
            return {"requeued": requeued, "outcome_unknown": uncertain}

    def complete_turn(
        self,
        *,
        turn_id: str,
        lease_owner: str,
        attempt: int,
        assistant_body: str,
        context_digest: str,
        actor_id: str,
        completed_at: float | None = None,
    ) -> int:
        turn_id = _require_text("turn_id", turn_id)
        lease_owner = _require_text("lease_owner", lease_owner)
        assistant_body = _require_text("assistant_body", assistant_body)
        context_digest = _require_digest("context_digest", context_digest)  # type: ignore[assignment]
        actor_id = _require_text("actor_id", actor_id)
        when = _require_time(completed_at)
        with self.connect() as conn, write_txn(conn):
            turn = conn.execute(
                "SELECT * FROM annotation_turn_route WHERE turn_id=?", (turn_id,)
            ).fetchone()
            if turn is None:
                raise KeyError("unknown annotation turn")
            if turn["status"] == "completed" and turn["assistant_message_id"] is not None:
                if int(turn["attempt"]) != int(attempt):
                    raise RuntimeError("turn completion belongs to another attempt")
                return int(turn["assistant_message_id"])
            if (
                turn["status"] != "running"
                or turn["lease_owner"] != lease_owner
                or int(turn["attempt"]) != int(attempt)
                or turn["dispatch_state"] != "dispatched"
                or turn["lease_expires_at"] is None
                or float(turn["lease_expires_at"]) <= when
            ):
                raise RuntimeError("turn is not held by this lease")
            route = self._require_active_route(
                conn, turn["annotation_id"], turn["thread_generation"]
            )
            trigger = conn.execute(
                "SELECT * FROM annotation_message_context WHERE message_id=?",
                (turn["trigger_message_id"],),
            ).fetchone()
            if trigger is None:
                raise RuntimeError("annotation turn trigger context is missing")
            sequence = int(route["next_thread_sequence"])
            tip = self._compression_tip(conn, route["annotation_lineage_root_id"])
            message_id = self._insert_message(conn, tip, "assistant", assistant_body, when)
            idem_digest = _idempotency_digest(
                body=assistant_body,
                intent="agent_reply",
                context_digest=context_digest,
                reply_to_message_id=turn["trigger_message_id"],
                anchor_stale_at_submit=bool(trigger["anchor_stale_at_submit"]),
            )
            conn.execute(
                """INSERT INTO annotation_message_context(
                       message_id,schema_version,annotation_id,thread_generation,
                       thread_sequence,anchor_revision_id,capture_digest,
                       reply_to_message_id,intent,model_participation,
                       triggering_message_id,context_codec_version,context_digest,
                       anchor_stale_at_submit,idempotency_digest,client_request_id,
                       actor_id,created_at
                   ) VALUES(?,1,?,?,?,?,?,?,'agent_reply','committed',?,1,?,?,?,?,?,?)""",
                (
                    message_id,
                    turn["annotation_id"],
                    turn["thread_generation"],
                    sequence,
                    trigger["anchor_revision_id"],
                    trigger["capture_digest"],
                    turn["trigger_message_id"],
                    turn["trigger_message_id"],
                    context_digest,
                    int(bool(trigger["anchor_stale_at_submit"])),
                    idem_digest,
                    f"agent-reply:{turn_id}",
                    actor_id,
                    when,
                ),
            )
            conn.execute(
                """UPDATE annotation_message_context SET model_participation='committed'
                   WHERE message_id=? AND model_participation='pending'""",
                (turn["trigger_message_id"],),
            )
            conn.execute(
                """UPDATE annotation_turn_route
                   SET status='completed',assistant_message_id=?,lease_owner=NULL,
                       lease_expires_at=NULL,error_code=NULL,finished_at=?
                   WHERE turn_id=?""",
                (message_id, when, turn_id),
            )
            conn.execute(
                """UPDATE annotation_thread_route
                   SET next_thread_sequence=?,updated_at=?
                   WHERE annotation_id=? AND thread_generation=? AND profile_id=?""",
                (
                    sequence + 1,
                    when,
                    turn["annotation_id"],
                    turn["thread_generation"],
                    self.profile_id,
                ),
            )
            return message_id

    def provider_history(self, annotation_id: str, thread_generation: int) -> list[dict[str, object]]:
        """Project completed pairs only, preserving strict user/assistant alternation."""
        with self.connect() as conn:
            self._require_route(conn, annotation_id, thread_generation)
            rows = conn.execute(
                """SELECT t.turn_sequence,u.message_id AS user_id,um.content AS user_content,
                          a.message_id AS assistant_id,am.content AS assistant_content
                   FROM annotation_turn_route t
                   JOIN annotation_message_context u ON u.message_id=t.trigger_message_id
                   JOIN messages um ON um.id=u.message_id
                   JOIN annotation_message_context a ON a.message_id=t.assistant_message_id
                   JOIN messages am ON am.id=a.message_id
                   WHERE t.annotation_id=? AND t.thread_generation=?
                     AND t.status='completed'
                   ORDER BY t.turn_sequence""",
                (annotation_id, thread_generation),
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            result.append({"role": "user", "content": row["user_content"], "message_id": row["user_id"]})
            result.append({"role": "assistant", "content": row["assistant_content"], "message_id": row["assistant_id"]})
        return result

    def thread_projection(
        self, annotation_id: str, thread_generation: int
    ) -> dict[str, object]:
        """Return the dedicated UI projection in authoritative thread order."""

        with self.connect() as conn:
            route = self._require_route(conn, annotation_id, thread_generation)
            rows = conn.execute(
                """SELECT c.*,m.role,m.content,
                          t.turn_id,t.turn_sequence,t.status,t.dispatch_state,t.attempt,
                          t.trigger_message_id AS turn_trigger_message_id,
                          t.assistant_message_id,t.error_code
                   FROM annotation_message_context c
                   JOIN messages m ON m.id=c.message_id
                   LEFT JOIN annotation_turn_route t
                     ON t.trigger_message_id=c.message_id
                     OR t.assistant_message_id=c.message_id
                   WHERE c.annotation_id=? AND c.thread_generation=?
                   ORDER BY c.thread_sequence,c.message_id""",
                (annotation_id, thread_generation),
            ).fetchall()
            source_id = route["source_session_lineage_id"]
            source_available = bool(
                source_id
                and conn.execute(
                    "SELECT 1 FROM sessions WHERE id=?", (source_id,)
                ).fetchone()
            )
            current_tip = self._compression_tip(
                conn, route["annotation_lineage_root_id"]
            )

        def item(row: sqlite3.Row) -> dict[str, object]:
            projected: dict[str, object] = {
                "messageId": row["message_id"],
                "threadSequence": row["thread_sequence"],
                "role": row["role"],
                "content": row["content"],
                "intent": row["intent"],
                "modelParticipation": row["model_participation"],
                "anchorRevisionId": row["anchor_revision_id"],
                "captureDigest": row["capture_digest"],
                "replyToMessageId": row["reply_to_message_id"],
                "triggeringMessageId": row["triggering_message_id"],
                "contextDigest": row["context_digest"],
                "anchorStaleAtSubmit": bool(row["anchor_stale_at_submit"]),
                "actorId": row["actor_id"],
                "createdAt": row["created_at"],
            }
            if row["turn_id"] is not None:
                projected["turn"] = {
                    "turnId": row["turn_id"],
                    "turnSequence": row["turn_sequence"],
                    "status": row["status"],
                    "attempt": row["attempt"],
                    "triggerMessageId": row["turn_trigger_message_id"],
                    "assistantMessageId": row["assistant_message_id"],
                    "errorCode": row["error_code"],
                }
            return projected

        by_id = {int(row["message_id"]): row for row in rows}
        consumed_assistants: set[int] = set()
        projected_messages: list[dict[str, object]] = []
        for row in rows:
            message_id = int(row["message_id"])
            if message_id in consumed_assistants:
                continue
            # Completed turns display as one strict user/assistant pair even if a
            # concurrent comment received an earlier raw insertion sequence.
            if row["intent"] == "agent_reply":
                projected_messages.append(item(row))
                continue
            projected_messages.append(item(row))
            assistant_id = row["assistant_message_id"]
            if row["status"] == "completed" and assistant_id is not None:
                assistant = by_id.get(int(assistant_id))
                if assistant is None:
                    raise RuntimeError("annotation history linkage is corrupt")
                projected_messages.append(item(assistant))
                consumed_assistants.add(int(assistant_id))

        return {
            "annotationId": annotation_id,
            "threadGeneration": int(thread_generation),
            "annotationLineageRootId": route["annotation_lineage_root_id"],
            "currentSessionId": current_tip,
            "sourceSessionLineageId": source_id,
            "sourceMessageId": route["source_message_id"],
            "sourceAvailable": source_available,
            "state": route["state"],
            "orderedMessageIds": [
                message["messageId"] for message in projected_messages
            ],
            "messages": projected_messages,
        }

    def turn(self, turn_id: str) -> dict[str, object] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM annotation_turn_route WHERE turn_id=?",
                (_require_text("turn_id", turn_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    def cancel_turn(self, turn_id: str, *, cancelled_at: float | None = None) -> str:
        """Cancel queued/running work without deleting its accepted user message.

        Completion wins once durable.  A running worker immediately loses its
        lease authority; any later provider response is therefore fenced out by
        ``complete_turn``.  The caller is responsible for signalling the local
        worker as a best-effort latency optimization.
        """

        turn_id = _require_text("turn_id", turn_id)
        when = _require_time(cancelled_at)
        with self.connect() as conn, write_txn(conn):
            turn = conn.execute(
                "SELECT status,trigger_message_id FROM annotation_turn_route WHERE turn_id=?",
                (turn_id,),
            ).fetchone()
            if turn is None:
                raise KeyError("unknown annotation turn")
            status = str(turn["status"])
            if status in {"completed", "failed", "cancelled"}:
                return status
            changed = conn.execute(
                """UPDATE annotation_turn_route
                   SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL,
                       error_code='interrupted',finished_at=?
                   WHERE turn_id=? AND status IN ('queued','running')""",
                (when, turn_id),
            )
            if changed.rowcount != 1:
                raise RuntimeError("annotation turn changed during cancellation")
            conn.execute(
                """UPDATE annotation_message_context SET model_participation='cancelled'
                   WHERE message_id=? AND model_participation='pending'""",
                (turn["trigger_message_id"],),
            )
            return "cancelled"

    def retry_turn(self, turn_id: str, *, retried_at: float | None = None) -> int:
        """Requeue one safely retryable failed attempt without duplicating the body."""

        turn_id = _require_text("turn_id", turn_id)
        when = _require_time(retried_at)
        with self.connect() as conn, write_txn(conn):
            turn = conn.execute(
                "SELECT * FROM annotation_turn_route WHERE turn_id=?", (turn_id,)
            ).fetchone()
            if turn is None:
                raise KeyError("unknown annotation turn")
            self._require_active_route(
                conn, turn["annotation_id"], int(turn["thread_generation"])
            )
            if turn["status"] != "failed":
                raise RuntimeError("only failed annotation turns may be retried")
            if turn["dispatch_state"] == "outcome_unknown":
                raise RuntimeError("outcome_unknown must be reconciled before retry")
            attempt = int(turn["attempt"]) + 1
            changed = conn.execute(
                """UPDATE annotation_turn_route
                   SET status='queued',dispatch_state='not_dispatched',attempt=?,
                       lease_owner=NULL,lease_expires_at=NULL,error_code=NULL,
                       started_at=NULL,finished_at=NULL
                   WHERE turn_id=? AND status='failed' AND dispatch_state!='outcome_unknown'""",
                (attempt, turn_id),
            )
            if changed.rowcount != 1:
                raise RuntimeError("annotation turn changed during retry")
            conn.execute(
                """UPDATE annotation_message_context SET model_participation='pending'
                   WHERE message_id=? AND model_participation='failed'""",
                (turn["trigger_message_id"],),
            )
            conn.execute(
                """UPDATE annotation_thread_route SET updated_at=?
                   WHERE annotation_id=? AND thread_generation=? AND profile_id=?""",
                (when, turn["annotation_id"], turn["thread_generation"], self.profile_id),
            )
            return attempt

    def mark_orphaned(self, annotation_id: str, thread_generation: int) -> None:
        """Durably fail closed a corrupt/missing dedicated lineage."""

        with self.connect() as conn, write_txn(conn):
            route = conn.execute(
                """SELECT state FROM annotation_thread_route
                   WHERE annotation_id=? AND thread_generation=? AND profile_id=?""",
                (annotation_id, thread_generation, self.profile_id),
            ).fetchone()
            if route is None:
                raise KeyError("annotation thread is unavailable in the active profile")
            if route["state"] in {"deleting", "deleted"}:
                raise RuntimeError(f"annotation thread is {route['state']}")
            conn.execute(
                """UPDATE annotation_thread_route SET state='orphaned',updated_at=?
                   WHERE annotation_id=? AND thread_generation=? AND profile_id=?""",
                (time.time(), annotation_id, thread_generation, self.profile_id),
            )
            conn.execute(
                """UPDATE annotation_turn_route
                   SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL,
                       error_code='context_corrupt',finished_at=?
                   WHERE annotation_id=? AND thread_generation=?
                     AND status IN ('queued','running')""",
                (time.time(), annotation_id, thread_generation),
            )
            conn.execute(
                """UPDATE annotation_message_context SET model_participation='cancelled'
                   WHERE annotation_id=? AND thread_generation=?
                     AND model_participation='pending'""",
                (annotation_id, thread_generation),
            )

    def claimed_turn_input(
        self,
        turn_id: str,
        *,
        lease_owner: str,
        attempt: int,
        now: float | None = None,
    ) -> ClaimedAnnotationTurnInput:
        """Read one exact leased turn plus only its completed predecessor pairs."""

        turn_id = _require_text("turn_id", turn_id)
        lease_owner = _require_text("lease_owner", lease_owner)
        when = _require_time(now)
        with self.connect() as conn:
            row = conn.execute(
                """SELECT t.*,r.annotation_lineage_root_id,r.source_session_lineage_id,
                          r.source_message_id,s.system_prompt,s.model,
                          s.model_config,s.cwd,c.anchor_revision_id,c.capture_digest,
                          c.reply_to_message_id,c.context_digest,c.anchor_stale_at_submit,
                          m.content AS body,m.session_id AS body_session_id,m.role AS body_role
                   FROM annotation_turn_route t
                   JOIN annotation_thread_route r
                     ON r.annotation_id=t.annotation_id
                    AND r.thread_generation=t.thread_generation
                    AND r.profile_id=?
                   JOIN sessions s ON s.id=r.annotation_lineage_root_id
                   JOIN annotation_message_context c ON c.message_id=t.trigger_message_id
                    AND c.annotation_id=t.annotation_id
                    AND c.thread_generation=t.thread_generation
                    AND c.intent='ask_agent'
                   JOIN messages m ON m.id=c.message_id
                   WHERE t.turn_id=? AND t.status='running' AND t.lease_owner=?
                     AND t.attempt=? AND t.lease_expires_at > ?
                     AND m.role='user' AND r.state='active'
                     AND s.source=?""",
                (
                    self.profile_id,
                    turn_id,
                    lease_owner,
                    int(attempt),
                    when,
                    ANNOTATION_SESSION_SOURCE,
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("annotation turn is not held by this lease")
            if not isinstance(row["system_prompt"], str) or not row["system_prompt"]:
                raise RuntimeError("annotation runtime snapshot lacks a stable system prompt")
            if not isinstance(row["model"], str) or not row["model"]:
                raise RuntimeError("annotation runtime snapshot lacks a model")
            try:
                model_config = json.loads(row["model_config"] or "{}")
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError("annotation runtime snapshot is malformed") from exc
            if not isinstance(model_config, dict):
                raise RuntimeError("annotation runtime snapshot is malformed")
            if not self._session_descends_from(
                conn, row["body_session_id"], row["annotation_lineage_root_id"]
            ):
                raise RuntimeError("annotation turn linkage is corrupt")
            history_rows = conn.execute(
                """SELECT t.turn_sequence,um.content AS user_content,
                          u.anchor_revision_id,u.capture_digest,u.reply_to_message_id,
                          u.context_digest,u.anchor_stale_at_submit,
                          am.content AS assistant_content,
                          a.context_digest AS assistant_context_digest,
                          um.session_id AS user_session_id,am.session_id AS assistant_session_id
                   FROM annotation_turn_route t
                   JOIN annotation_message_context u ON u.message_id=t.trigger_message_id
                    AND u.annotation_id=t.annotation_id
                    AND u.thread_generation=t.thread_generation
                    AND u.intent='ask_agent'
                   JOIN messages um ON um.id=u.message_id AND um.role='user'
                   JOIN annotation_message_context a ON a.message_id=t.assistant_message_id
                    AND a.annotation_id=t.annotation_id
                    AND a.thread_generation=t.thread_generation
                    AND a.intent='agent_reply'
                    AND a.triggering_message_id=t.trigger_message_id
                   JOIN messages am ON am.id=a.message_id AND am.role='assistant'
                   WHERE t.annotation_id=? AND t.thread_generation=?
                     AND t.status='completed' AND t.turn_sequence < ?
                   ORDER BY t.turn_sequence""",
                (row["annotation_id"], row["thread_generation"], row["turn_sequence"]),
            ).fetchall()
            expected_history_count = conn.execute(
                """SELECT count(*) FROM annotation_turn_route
                   WHERE annotation_id=? AND thread_generation=?
                     AND status='completed' AND turn_sequence < ?""",
                (row["annotation_id"], row["thread_generation"], row["turn_sequence"]),
            ).fetchone()[0]
            if len(history_rows) != expected_history_count:
                raise RuntimeError("annotation history linkage is corrupt")
            if any(
                not self._session_descends_from(
                    conn, item[session_column], row["annotation_lineage_root_id"]
                )
                for item in history_rows
                for session_column in ("user_session_id", "assistant_session_id")
            ):
                raise RuntimeError("annotation history linkage is corrupt")
        history = tuple(
            {
                "turn_sequence": item["turn_sequence"],
                "user_content": item["user_content"],
                "anchor_revision_id": item["anchor_revision_id"],
                "capture_digest": item["capture_digest"],
                "reply_to_message_id": item["reply_to_message_id"],
                "context_digest": item["context_digest"],
                "anchor_stale_at_submit": bool(item["anchor_stale_at_submit"]),
                "assistant_content": item["assistant_content"],
                "assistant_context_digest": item["assistant_context_digest"],
            }
            for item in history_rows
        )
        claimed = ClaimedAnnotationTurn(
            turn_id=row["turn_id"],
            annotation_id=row["annotation_id"],
            thread_generation=row["thread_generation"],
            turn_sequence=row["turn_sequence"],
            trigger_message_id=row["trigger_message_id"],
            attempt=row["attempt"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
        )
        return ClaimedAnnotationTurnInput(
            turn=claimed,
            annotation_lineage_root_id=row["annotation_lineage_root_id"],
            source_session_lineage_id=row["source_session_lineage_id"],
            source_message_id=row["source_message_id"],
            system_prompt=row["system_prompt"],
            model=row["model"],
            model_config=model_config,
            cwd=row["cwd"],
            body=row["body"],
            anchor_revision_id=row["anchor_revision_id"],
            capture_digest=row["capture_digest"],
            reply_to_message_id=row["reply_to_message_id"],
            context_digest=row["context_digest"],
            anchor_stale_at_submit=bool(row["anchor_stale_at_submit"]),
            completed_history=history,
        )

    def renew_turn_lease(
        self,
        turn_id: str,
        *,
        lease_owner: str,
        attempt: int,
        lease_seconds: float,
        now: float | None = None,
    ) -> float:
        when = _require_time(now)
        lease_seconds = float(lease_seconds)
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be finite and positive")
        expires = when + lease_seconds
        with self.connect() as conn, write_txn(conn):
            changed = conn.execute(
                """UPDATE annotation_turn_route SET lease_expires_at=?
                   WHERE turn_id=? AND status='running' AND lease_owner=? AND attempt=?
                     AND lease_expires_at > ?""",
                (
                    expires,
                    _require_text("turn_id", turn_id),
                    _require_text("lease_owner", lease_owner),
                    int(attempt),
                    when,
                ),
            )
            if changed.rowcount != 1:
                raise RuntimeError("annotation turn is not held by this lease")
        return expires

    def fail_turn(
        self,
        turn_id: str,
        *,
        lease_owner: str,
        attempt: int,
        error_code: str,
        failed_at: float | None = None,
    ) -> None:
        when = _require_time(failed_at)
        turn_id = _require_text("turn_id", turn_id)
        error_code = _require_text("error_code", error_code)
        if error_code not in _STABLE_TURN_ERROR_CODES:
            raise ValueError("error_code is not a stable redacted annotation error")
        with self.connect() as conn, write_txn(conn):
            changed = conn.execute(
                """UPDATE annotation_turn_route
                   SET status='failed',lease_owner=NULL,lease_expires_at=NULL,
                       error_code=?,finished_at=?
                   WHERE turn_id=? AND status='running' AND lease_owner=? AND attempt=?
                     AND lease_expires_at > ?""",
                (
                    error_code,
                    when,
                    turn_id,
                    _require_text("lease_owner", lease_owner),
                    int(attempt),
                    when,
                ),
            )
            if changed.rowcount != 1:
                raise RuntimeError("annotation turn is not held by this lease")
            conn.execute(
                """UPDATE annotation_message_context SET model_participation='failed'
                   WHERE message_id=(SELECT trigger_message_id FROM annotation_turn_route
                                     WHERE turn_id=?)
                     AND model_participation='pending'""",
                (turn_id,),
            )

    def queued_lineages(self) -> list[tuple[str, int]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT t.annotation_id,t.thread_generation
                   FROM annotation_turn_route t
                   JOIN annotation_thread_route r
                     ON r.annotation_id=t.annotation_id
                    AND r.thread_generation=t.thread_generation
                   WHERE r.profile_id=? AND r.state='active' AND t.status='queued'
                   ORDER BY t.annotation_id,t.thread_generation""",
                (self.profile_id,),
            ).fetchall()
        return [(row["annotation_id"], row["thread_generation"]) for row in rows]

    def _require_route(
        self, conn: sqlite3.Connection, annotation_id: str, thread_generation: int
    ) -> sqlite3.Row:
        row = conn.execute(
            """SELECT * FROM annotation_thread_route
               WHERE annotation_id=? AND thread_generation=? AND profile_id=?""",
            (annotation_id, thread_generation, self.profile_id),
        ).fetchone()
        if row is None:
            raise KeyError("annotation thread is unavailable in the active profile")
        root = conn.execute(
            "SELECT source FROM sessions WHERE id=?", (row["annotation_lineage_root_id"],)
        ).fetchone()
        if root is None or root["source"] != ANNOTATION_SESSION_SOURCE:
            # Detection is side-effect free: callers may be inside a transaction
            # that must roll back, while read callers have no writer fence. A
            # dedicated repair/maintenance operation owns durable orphan marking.
            raise RuntimeError("annotation thread is orphaned")
        return row

    def _require_active_route(
        self, conn: sqlite3.Connection, annotation_id: str, thread_generation: int
    ) -> sqlite3.Row:
        row = self._require_route(conn, annotation_id, thread_generation)
        if row["state"] != "active":
            raise RuntimeError(f"annotation thread is {row['state']}")
        return row

    @staticmethod
    def _insert_message(
        conn: sqlite3.Connection, session_id: str, role: str, body: str, timestamp: float
    ) -> int:
        cursor = conn.execute(
            """INSERT INTO messages(session_id,role,content,timestamp,observed,active)
               VALUES(?,?,?,?,0,1)""",
            (session_id, role, body, timestamp),
        )
        conn.execute(
            """UPDATE sessions SET message_count=message_count+1,
                   last_active=CASE WHEN last_active IS NULL OR last_active < ? THEN ? ELSE last_active END
               WHERE id=?""",
            (timestamp, timestamp, session_id),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _session_descends_from(
        conn: sqlite3.Connection, session_id: str, root_id: str
    ) -> bool:
        current = session_id
        seen: set[str] = set()
        while current and current not in seen:
            if current == root_id:
                return True
            seen.add(current)
            row = conn.execute(
                "SELECT parent_session_id FROM sessions WHERE id=?", (current,)
            ).fetchone()
            if row is None:
                return False
            current = row["parent_session_id"]
        return False

    @staticmethod
    def _compression_tip(conn: sqlite3.Connection, root_id: str) -> str:
        current = root_id
        seen = {current}
        for _ in range(100):
            row = conn.execute(
                """SELECT child.id
                   FROM sessions parent
                   JOIN sessions child ON child.parent_session_id=parent.id
                   WHERE parent.id=? AND parent.end_reason='compression'
                     AND json_extract(COALESCE(child.model_config,'{}'),'$._branched_from') IS NULL
                     AND json_extract(COALESCE(child.model_config,'{}'),'$._delegate_from') IS NULL
                     AND COALESCE(child.source,'') != 'tool'
                   ORDER BY CASE WHEN child.end_reason='compression' THEN 0
                                      WHEN child.ended_at IS NULL THEN 1 ELSE 2 END,
                            COALESCE((SELECT MAX(m.timestamp) FROM messages m
                                      WHERE m.session_id=child.id),child.started_at) DESC,
                            child.started_at DESC,child.id DESC
                   LIMIT 1""",
                (current,),
            ).fetchone()
            if row is None or not row["id"] or row["id"] in seen:
                return current
            current = row["id"]
            seen.add(current)
        return current
