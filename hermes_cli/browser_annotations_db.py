"""Versioned profile repository for browser annotation records.

The database is intentionally separate from session ``state.db`` and from
Electron's device-local browser activity store. A repository instance is bound
to exactly one active profile; every query repeats that boundary defensively.

This repository slice owns annotation metadata, immutable anchor/capture revisions,
and optional capture bytes. It deliberately does not own the dedicated annotation
thread/session bodies specified by D-021; ``AnnotationBundleCoordinator`` composes
this repository with their separate ``state.db`` authority.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterator, Literal

from hermes_cli.browser_annotations_models import (
    AnchorRevision,
    AnnotationDeleteResult,
    AnnotationExportBlob,
    AnnotationExportBundleV1,
    AnnotationImportResult,
    AnnotationProjection,
    AnnotationRecordV1,
    AnnotationScope,
    BlobRef,
    CaptureEvidence,
    RetainedBlob,
    canonical_digest,
    canonical_json,
)
from hermes_cli.sqlite_util import write_txn
from hermes_constants import get_hermes_home

SCHEMA_VERSION = 3

_SCHEMA_V1 = """
CREATE TABLE annotation_repository_identity (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    profile_id TEXT NOT NULL UNIQUE
);

CREATE TABLE annotation_record (
    annotation_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    browser_workspace_id TEXT NOT NULL,
    tab_id TEXT NOT NULL,
    document_generation_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN
        ('agent-marker','element','text','region','drawing','comment')),
    scope_json TEXT NOT NULL,
    author_json TEXT NOT NULL,
    thread_json TEXT,
    status TEXT NOT NULL CHECK(status IN ('open','resolved','dismissed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    current_anchor_revision_id TEXT NOT NULL,
    record_revision INTEGER NOT NULL CHECK(record_revision >= 1)
);

CREATE INDEX annotation_record_scope_idx ON annotation_record(
    profile_id, browser_workspace_id, tab_id, created_at, annotation_id
);

CREATE TABLE annotation_capture (
    capture_id TEXT PRIMARY KEY,
    annotation_id TEXT NOT NULL REFERENCES annotation_record(annotation_id),
    profile_id TEXT NOT NULL,
    capture_json TEXT NOT NULL,
    capture_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE annotation_anchor_revision (
    anchor_revision_id TEXT PRIMARY KEY,
    annotation_id TEXT NOT NULL REFERENCES annotation_record(annotation_id),
    profile_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    anchor_json TEXT NOT NULL,
    anchor_digest TEXT NOT NULL,
    capture_id TEXT NOT NULL REFERENCES annotation_capture(capture_id),
    changed_by_json TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN ('created','reattached','imported')),
    previous_anchor_digest TEXT,
    UNIQUE(annotation_id, revision)
);

CREATE TRIGGER annotation_anchor_revision_no_update
BEFORE UPDATE ON annotation_anchor_revision
BEGIN SELECT RAISE(ABORT, 'anchor revisions are immutable'); END;

CREATE TRIGGER annotation_anchor_revision_no_delete
BEFORE DELETE ON annotation_anchor_revision
BEGIN SELECT RAISE(ABORT, 'anchor revisions are immutable'); END;

CREATE TABLE annotation_blob (
    sha256 TEXT PRIMARY KEY,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    media_type TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    retention_state TEXT NOT NULL CHECK(retention_state IN ('retained','missing','expired')),
    created_at TEXT NOT NULL
);

CREATE TABLE annotation_capture_blob_ref (
    capture_id TEXT NOT NULL REFERENCES annotation_capture(capture_id),
    blob_sha256 TEXT NOT NULL REFERENCES annotation_blob(sha256),
    purpose TEXT NOT NULL CHECK(purpose IN ('screenshot','redacted-crop')),
    PRIMARY KEY(capture_id, blob_sha256, purpose)
);

CREATE TABLE annotation_delete_job (
    delete_job_id TEXT PRIMARY KEY,
    annotation_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','deleting','complete','failed')),
    requested_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT
);
"""

_SCHEMA_V2 = """
ALTER TABLE annotation_delete_job
    ADD COLUMN phase TEXT NOT NULL DEFAULT 'metadata';

CREATE TABLE annotation_blob_cleanup_journal (
    delete_job_id TEXT NOT NULL REFERENCES annotation_delete_job(delete_job_id),
    blob_sha256 TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN
        ('pending','deleted','missing','preserved','failed')),
    error TEXT,
    PRIMARY KEY(delete_job_id, blob_sha256)
);

CREATE TABLE annotation_import_receipt (
    annotation_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    manifest_digest TEXT NOT NULL
);

DROP TRIGGER annotation_anchor_revision_no_delete;
CREATE TRIGGER annotation_anchor_revision_no_delete
BEFORE DELETE ON annotation_anchor_revision
WHEN NOT EXISTS (
    SELECT 1 FROM annotation_delete_job j
     WHERE j.annotation_id = OLD.annotation_id
       AND j.profile_id = OLD.profile_id
       AND j.state = 'deleting'
)
BEGIN SELECT RAISE(ABORT, 'anchor revisions are immutable'); END;
"""

_SCHEMA_V3 = """
CREATE TABLE annotation_bundle_create_journal (
    annotation_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    annotation_lineage_root_id TEXT NOT NULL UNIQUE,
    operation_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);

DROP TRIGGER annotation_anchor_revision_no_delete;
CREATE TRIGGER annotation_anchor_revision_no_delete
BEFORE DELETE ON annotation_anchor_revision
WHEN NOT EXISTS (
    SELECT 1 FROM annotation_delete_job j
     WHERE j.annotation_id = OLD.annotation_id
       AND j.profile_id = OLD.profile_id
       AND j.state = 'deleting'
) AND NOT EXISTS (
    SELECT 1 FROM annotation_bundle_create_journal j
     WHERE j.annotation_id = OLD.annotation_id
       AND j.profile_id = OLD.profile_id
)
BEGIN SELECT RAISE(ABORT, 'anchor revisions are immutable'); END;
"""


def annotation_store_root(profile_home: Path | None = None) -> Path:
    home = profile_home if profile_home is not None else get_hermes_home()
    return Path(home) / "browser" / "annotations" / "v1"


def annotation_db_path(profile_home: Path | None = None) -> Path:
    return annotation_store_root(profile_home) / "annotations.v1.sqlite3"


def annotation_blob_root(profile_home: Path | None = None) -> Path:
    return annotation_store_root(profile_home) / "blobs" / "sha256"


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("annotation timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _open_and_migrate(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        from hermes_state import apply_wal_with_fallback

        apply_wal_with_fallback(conn, db_label="browser annotations")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"annotation database version {version} is newer than supported "
                f"version {SCHEMA_VERSION}"
            )
        if version == 0:
            _migrate_v1(conn)
            version = 1
        if version == 1:
            _validate_schema(conn, expected_version=1)
            _migrate_v2(conn)
            version = 2
        if version == 2:
            _validate_schema(conn, expected_version=2)
            _migrate_v3(conn)
        _validate_schema(conn, expected_version=SCHEMA_VERSION)
        return conn
    except Exception:
        conn.close()
        raise


def _migrate_v1(conn: sqlite3.Connection, schema_sql: str | None = None) -> None:
    """Install v1 atomically, including the schema-version marker.

    Python's ``executescript`` commits any pending transaction before running its
    input, so wrapping it in ``with conn`` is not atomic.  Put the transaction in
    the script itself and explicitly roll it back on any failed DDL statement.
    A retry then sees the untouched version-0 database instead of a permanently
    partial schema.
    """

    schema_sql = _SCHEMA_V1 if schema_sql is None else schema_sql
    script = "BEGIN IMMEDIATE;\n" + schema_sql + "\nPRAGMA user_version=1;\nCOMMIT;"
    try:
        conn.executescript(script)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _migrate_v2(conn: sqlite3.Connection, schema_sql: str | None = None) -> None:
    """Upgrade deployed v1 repositories atomically and retryably."""

    schema_sql = _SCHEMA_V2 if schema_sql is None else schema_sql
    script = (
        "BEGIN IMMEDIATE;\n"
        + schema_sql
        + "\nPRAGMA user_version=2;\nCOMMIT;"
    )
    try:
        conn.executescript(script)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _migrate_v3(conn: sqlite3.Connection, schema_sql: str | None = None) -> None:
    """Install the recoverable cross-store creation journal atomically."""

    schema_sql = _SCHEMA_V3 if schema_sql is None else schema_sql
    script = (
        "BEGIN IMMEDIATE;\n"
        + schema_sql
        + f"\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;"
    )
    try:
        conn.executescript(script)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _validate_schema(conn: sqlite3.Connection, *, expected_version: int) -> None:
    required = {
        "annotation_repository_identity",
        "annotation_record",
        "annotation_anchor_revision",
        "annotation_capture",
        "annotation_blob",
        "annotation_capture_blob_ref",
        "annotation_delete_job",
    }
    if expected_version >= 2:
        required.update({
            "annotation_blob_cleanup_journal",
            "annotation_import_receipt",
        })
    if expected_version >= 3:
        required.add("annotation_bundle_create_journal")
    present = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    missing = required - present
    if missing:
        raise RuntimeError(
            "annotation database schema is incomplete: " + ", ".join(sorted(missing))
        )


class AnnotationRepository:
    """Repository bound to one profile and one private/durable workspace mode."""

    def __init__(
        self,
        *,
        profile_id: str,
        profile_home: Path | None = None,
        private_workspace: bool = False,
    ) -> None:
        profile_id = str(profile_id).strip()
        if not profile_id:
            raise ValueError("profile_id must not be empty")
        self.profile_id = profile_id
        self.profile_home = (
            Path(profile_home) if profile_home is not None else get_hermes_home()
        )
        self.private_workspace = bool(private_workspace)

    @property
    def db_path(self) -> Path:
        return annotation_db_path(self.profile_home)

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        # This check must precede mkdir/open so private browsing leaves no durable trace.
        if self.private_workspace:
            raise PermissionError(
                "private browser workspaces cannot persist annotations"
            )
        self._ensure_store_root()
        conn = _open_and_migrate(self.db_path)
        try:
            with write_txn(conn):
                identity = conn.execute(
                    "SELECT profile_id FROM annotation_repository_identity WHERE singleton = 1"
                ).fetchone()
                if identity is None:
                    conn.execute(
                        """INSERT INTO annotation_repository_identity(singleton, profile_id)
                           VALUES (1, ?)""",
                        (self.profile_id,),
                    )
                elif identity["profile_id"] != self.profile_id:
                    raise PermissionError(
                        "annotation repository belongs to a different active profile"
                    )
            yield conn
        finally:
            conn.close()

    def create(
        self, record: AnnotationRecordV1, first_revision: AnchorRevision
    ) -> None:
        self._validate_new_annotation(record, first_revision)

        capture_id = f"{first_revision.anchor_revision_id}:capture"
        with self.connect() as conn, write_txn(conn):
            self._insert_record(conn, record)
            self._insert_capture(
                conn, record.annotation_id, capture_id, first_revision.capture
            )
            self._insert_anchor_revision(conn, first_revision, capture_id)

    def stage_bundle_create(
        self,
        record: AnnotationRecordV1,
        first_revision: AnchorRevision,
        *,
        operation_digest: str,
    ) -> None:
        """Atomically stage metadata with a recoverable cross-store marker."""

        self._validate_new_annotation(record, first_revision)
        if record.thread is None:
            raise ValueError("coordinated annotation creation requires a thread root")
        if len(operation_digest) != 64 or any(
            char not in "0123456789abcdef" for char in operation_digest
        ):
            raise ValueError("operation_digest must be a lowercase SHA-256 digest")
        capture_id = f"{first_revision.anchor_revision_id}:capture"
        with self.connect() as conn, write_txn(conn):
            pending = conn.execute(
                """SELECT annotation_lineage_root_id,operation_digest
                   FROM annotation_bundle_create_journal
                   WHERE annotation_id=? AND profile_id=?""",
                (record.annotation_id, self.profile_id),
            ).fetchone()
            if pending is not None:
                if (
                    pending["annotation_lineage_root_id"]
                    != record.thread.session_lineage_id
                    or pending["operation_digest"] != operation_digest
                ):
                    raise RuntimeError("annotation creation idempotency conflict")
                return
            if conn.execute(
                "SELECT 1 FROM annotation_record WHERE annotation_id=?",
                (record.annotation_id,),
            ).fetchone():
                raise RuntimeError("annotation creation collision")
            self._insert_record(conn, record)
            self._insert_capture(
                conn, record.annotation_id, capture_id, first_revision.capture
            )
            self._insert_anchor_revision(conn, first_revision, capture_id)
            conn.execute(
                """INSERT INTO annotation_bundle_create_journal(
                       annotation_id,profile_id,annotation_lineage_root_id,
                       operation_digest,created_at) VALUES(?,?,?,?,?)""",
                (
                    record.annotation_id,
                    self.profile_id,
                    record.thread.session_lineage_id,
                    operation_digest,
                    _iso(record.created_at),
                ),
            )

    def pending_bundle_creations(self) -> tuple[dict[str, str], ...]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT annotation_id,annotation_lineage_root_id,operation_digest
                   FROM annotation_bundle_create_journal
                   WHERE profile_id=? ORDER BY annotation_id""",
                (self.profile_id,),
            ).fetchall()
        return tuple(dict(row) for row in rows)  # type: ignore[return-value]

    def finish_bundle_create(
        self, annotation_id: str, annotation_lineage_root_id: str
    ) -> None:
        with self.connect() as conn, write_txn(conn):
            changed = conn.execute(
                """DELETE FROM annotation_bundle_create_journal
                   WHERE annotation_id=? AND profile_id=?
                     AND annotation_lineage_root_id=?""",
                (annotation_id, self.profile_id, annotation_lineage_root_id),
            )
            if changed.rowcount != 1:
                raise RuntimeError("annotation creation marker is unavailable")

    def abort_bundle_create(
        self, annotation_id: str, annotation_lineage_root_id: str
    ) -> bool:
        """Compensate a metadata-only creation without exposing a partial record."""

        with self.connect() as conn, write_txn(conn):
            pending = conn.execute(
                """SELECT 1 FROM annotation_bundle_create_journal
                   WHERE annotation_id=? AND profile_id=?
                     AND annotation_lineage_root_id=?""",
                (annotation_id, self.profile_id, annotation_lineage_root_id),
            ).fetchone()
            if pending is None:
                return False
            capture_ids = [
                row["capture_id"]
                for row in conn.execute(
                    """SELECT capture_id FROM annotation_capture
                       WHERE annotation_id=? AND profile_id=?""",
                    (annotation_id, self.profile_id),
                ).fetchall()
            ]
            conn.execute(
                "DELETE FROM annotation_anchor_revision WHERE annotation_id=? AND profile_id=?",
                (annotation_id, self.profile_id),
            )
            if capture_ids:
                placeholders = ",".join("?" for _ in capture_ids)
                conn.execute(
                    f"DELETE FROM annotation_capture_blob_ref WHERE capture_id IN ({placeholders})",
                    capture_ids,
                )
            conn.execute(
                "DELETE FROM annotation_capture WHERE annotation_id=? AND profile_id=?",
                (annotation_id, self.profile_id),
            )
            conn.execute(
                "DELETE FROM annotation_record WHERE annotation_id=? AND profile_id=?",
                (annotation_id, self.profile_id),
            )
            conn.execute(
                "DELETE FROM annotation_bundle_create_journal WHERE annotation_id=?",
                (annotation_id,),
            )
            conn.execute(
                """DELETE FROM annotation_blob
                   WHERE NOT EXISTS (SELECT 1 FROM annotation_capture_blob_ref r
                                     WHERE r.blob_sha256=annotation_blob.sha256)
                     AND retention_state!='retained'"""
            )
            return True

    def delete_result_for_annotation(self, annotation_id: str) -> AnnotationDeleteResult | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT delete_job_id FROM annotation_delete_job
                   WHERE annotation_id=? AND profile_id=?
                   ORDER BY requested_at DESC,delete_job_id DESC LIMIT 1""",
                (annotation_id, self.profile_id),
            ).fetchone()
            return self._delete_result(conn, row["delete_job_id"]) if row else None

    def get(self, annotation_id: str) -> AnnotationRecordV1 | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT r.*, ar.anchor_json, c.capture_json
                   FROM annotation_record r
                   JOIN annotation_anchor_revision ar
                     ON ar.anchor_revision_id = r.current_anchor_revision_id
                    AND ar.annotation_id = r.annotation_id
                   JOIN annotation_capture c ON c.capture_id = ar.capture_id
                   WHERE r.annotation_id = ? AND r.profile_id = ?
                     AND NOT EXISTS (
                         SELECT 1 FROM annotation_bundle_create_journal j
                          WHERE j.annotation_id=r.annotation_id AND j.profile_id=r.profile_id
                     )""",
                (annotation_id, self.profile_id),
            ).fetchone()
        return self._record_from_row(row) if row is not None else None

    def list_for_workspace(self, browser_workspace_id: str) -> list[AnnotationRecordV1]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT r.*, ar.anchor_json, c.capture_json
                   FROM annotation_record r
                   JOIN annotation_anchor_revision ar
                     ON ar.anchor_revision_id = r.current_anchor_revision_id
                    AND ar.annotation_id = r.annotation_id
                   JOIN annotation_capture c ON c.capture_id = ar.capture_id
                   WHERE r.profile_id = ? AND r.browser_workspace_id = ?
                     AND NOT EXISTS (
                         SELECT 1 FROM annotation_bundle_create_journal j
                          WHERE j.annotation_id=r.annotation_id AND j.profile_id=r.profile_id
                     )
                   ORDER BY r.created_at, r.annotation_id""",
                (self.profile_id, browser_workspace_id),
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def set_status(
        self,
        annotation_id: str,
        *,
        status: str,
        expected_revision: int,
        changed_at: datetime,
    ) -> AnnotationRecordV1:
        if status not in {"open", "resolved", "dismissed"}:
            raise ValueError(f"invalid annotation status: {status}")
        resolved_at = _iso(changed_at) if status == "resolved" else None
        with self.connect() as conn, write_txn(conn):
            current = conn.execute(
                """SELECT created_at FROM annotation_record
                   WHERE annotation_id = ? AND profile_id = ?""",
                (annotation_id, self.profile_id),
            ).fetchone()
            if current is not None and changed_at < datetime.fromisoformat(
                current["created_at"]
            ):
                raise ValueError("annotation update cannot predate its creation")
            cursor = conn.execute(
                """UPDATE annotation_record
                   SET status = ?, updated_at = ?, resolved_at = ?,
                       record_revision = record_revision + 1
                   WHERE annotation_id = ? AND profile_id = ? AND record_revision = ?""",
                (
                    status,
                    _iso(changed_at),
                    resolved_at,
                    annotation_id,
                    self.profile_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                self._raise_missing_or_conflict(conn, annotation_id)
        record = self.get(annotation_id)
        assert record is not None
        return record

    def reattach(
        self,
        annotation_id: str,
        revision: AnchorRevision,
        *,
        new_scope: AnnotationScope,
        expected_record_revision: int,
    ) -> AnnotationRecordV1:
        if revision.annotation_id != annotation_id or revision.reason != "reattached":
            raise ValueError("reattach requires a matching reattached anchor revision")
        self._require_profile(new_scope.profile_id)
        if revision.capture.document_generation_id != new_scope.document_generation_id:
            raise ValueError(
                "reattach capture and scope document generations must match"
            )
        if revision.capture.committed_url != new_scope.committed_url:
            raise ValueError("reattach capture and scope committed URLs must match")
        with self.connect() as conn, write_txn(conn):
            current = conn.execute(
                """SELECT r.current_anchor_revision_id, r.record_revision, r.created_at,
                          ar.anchor_digest, ar.revision AS anchor_revision
                   FROM annotation_record r
                   JOIN annotation_anchor_revision ar
                     ON ar.anchor_revision_id = r.current_anchor_revision_id
                   WHERE r.annotation_id = ? AND r.profile_id = ?""",
                (annotation_id, self.profile_id),
            ).fetchone()
            if current is None:
                raise KeyError(f"no annotation {annotation_id!r} in active profile")
            if revision.changed_at < datetime.fromisoformat(current["created_at"]):
                raise ValueError("annotation update cannot predate its creation")
            if current["record_revision"] != expected_record_revision:
                raise RuntimeError("annotation revision conflict")
            if revision.revision != current["anchor_revision"] + 1:
                raise ValueError("anchor revision must be monotonic and contiguous")
            if revision.previous_anchor_digest != current["anchor_digest"]:
                raise ValueError("previousAnchorDigest does not match current anchor")

            capture_id = f"{revision.anchor_revision_id}:capture"
            self._insert_capture(conn, annotation_id, capture_id, revision.capture)
            self._insert_anchor_revision(conn, revision, capture_id)
            cursor = conn.execute(
                """UPDATE annotation_record
                   SET browser_workspace_id = ?, tab_id = ?, document_generation_id = ?,
                       scope_json = ?, current_anchor_revision_id = ?, updated_at = ?,
                       record_revision = record_revision + 1
                   WHERE annotation_id = ? AND profile_id = ? AND record_revision = ?""",
                (
                    new_scope.browser_workspace_id,
                    new_scope.tab_id,
                    new_scope.document_generation_id,
                    canonical_json(new_scope),
                    revision.anchor_revision_id,
                    _iso(revision.changed_at),
                    annotation_id,
                    self.profile_id,
                    expected_record_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("annotation revision conflict")
        record = self.get(annotation_id)
        assert record is not None
        return record

    def anchor_revisions(self, annotation_id: str) -> list[AnchorRevision]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT ar.*, c.capture_json
                   FROM annotation_anchor_revision ar
                   JOIN annotation_capture c ON c.capture_id = ar.capture_id
                   WHERE ar.annotation_id = ? AND ar.profile_id = ?
                   ORDER BY ar.revision""",
                (annotation_id, self.profile_id),
            ).fetchall()
        return [self._anchor_revision_from_row(row) for row in rows]

    def immutable_evidence(
        self, annotation_id: str, anchor_revision_id: str
    ) -> dict[str, object]:
        """Project provider-safe evidence from one exact immutable revision.

        The current annotation revision/status is deliberately not consulted:
        prior turns must keep dereferencing the revision frozen at submission.
        """

        with self.connect() as conn:
            row = conn.execute(
                """SELECT r.kind,ar.anchor_json,c.capture_json,c.capture_digest
                   FROM annotation_anchor_revision ar
                   JOIN annotation_record r
                     ON r.annotation_id=ar.annotation_id AND r.profile_id=ar.profile_id
                   JOIN annotation_capture c
                     ON c.capture_id=ar.capture_id AND c.profile_id=ar.profile_id
                   WHERE ar.annotation_id=? AND ar.anchor_revision_id=?
                     AND ar.profile_id=?""",
                (annotation_id, anchor_revision_id, self.profile_id),
            ).fetchone()
        if row is None:
            raise KeyError("annotation revision is unavailable in the active profile")
        anchor = json.loads(row["anchor_json"])
        capture = json.loads(row["capture_json"])
        selected_text = None
        if isinstance(anchor, dict) and anchor.get("type") == "text":
            quote = anchor.get("quote")
            if isinstance(quote, dict) and isinstance(quote.get("exact"), str):
                selected_text = quote["exact"]
        document_url = capture.get("committedUrl") if isinstance(capture, dict) else None
        if not isinstance(document_url, str) or not document_url:
            raise RuntimeError("annotation revision evidence is malformed")
        return {
            "annotationKind": row["kind"],
            "documentUrl": document_url,
            "documentTitle": None,
            "selectedText": selected_text,
            "captureDigest": row["capture_digest"],
        }

    def delete_annotation(
        self,
        annotation_id: str,
        *,
        requested_at: datetime,
        delete_job_id: str | None = None,
    ) -> AnnotationDeleteResult:
        """Delete repository metadata, then resumably release unshared blob files.

        This is the metadata/blob phase only. The D-021 bundle coordinator calls it
        after freezing the dedicated route and before removing its state.db lineage.
        """

        changed_at = _iso(requested_at)
        with self.connect() as conn, write_txn(conn):
            job = conn.execute(
                """SELECT * FROM annotation_delete_job
                   WHERE annotation_id = ? AND profile_id = ?
                   ORDER BY requested_at DESC, delete_job_id DESC LIMIT 1""",
                (annotation_id, self.profile_id),
            ).fetchone()
            record_exists = conn.execute(
                """SELECT 1 FROM annotation_record
                   WHERE annotation_id = ? AND profile_id = ?""",
                (annotation_id, self.profile_id),
            ).fetchone()
            if job is None:
                if record_exists is None:
                    raise KeyError(f"no annotation {annotation_id!r} in active profile")
                resolved_job_id = delete_job_id or f"delete-{secrets.token_hex(16)}"
                conn.execute(
                    """INSERT INTO annotation_delete_job
                           (delete_job_id, annotation_id, profile_id, state,
                            requested_at, updated_at, error, phase)
                       VALUES (?, ?, ?, 'deleting', ?, ?, NULL, 'metadata')""",
                    (
                        resolved_job_id,
                        annotation_id,
                        self.profile_id,
                        changed_at,
                        changed_at,
                    ),
                )
                phase = "metadata"
            else:
                resolved_job_id = job["delete_job_id"]
                if delete_job_id is not None and delete_job_id != resolved_job_id:
                    raise RuntimeError(
                        "annotation deletion already has a different job id"
                    )
                if job["state"] == "complete":
                    return self._delete_result(conn, resolved_job_id)
                phase = job["phase"]
                conn.execute(
                    """UPDATE annotation_delete_job
                       SET state='deleting', updated_at=?, error=NULL
                       WHERE delete_job_id=? AND profile_id=?""",
                    (changed_at, resolved_job_id, self.profile_id),
                )

            if phase == "metadata":
                blobs = conn.execute(
                    """SELECT DISTINCT b.sha256, b.relative_path
                       FROM annotation_capture c
                       JOIN annotation_capture_blob_ref r ON r.capture_id = c.capture_id
                       JOIN annotation_blob b ON b.sha256 = r.blob_sha256
                       WHERE c.annotation_id = ? AND c.profile_id = ?
                       ORDER BY b.sha256""",
                    (annotation_id, self.profile_id),
                ).fetchall()
                for blob in blobs:
                    conn.execute(
                        """INSERT OR IGNORE INTO annotation_blob_cleanup_journal
                               (delete_job_id, blob_sha256, relative_path, state, error)
                           VALUES (?, ?, ?, 'pending', NULL)""",
                        (resolved_job_id, blob["sha256"], blob["relative_path"]),
                    )
                conn.execute(
                    """DELETE FROM annotation_anchor_revision
                       WHERE annotation_id = ? AND profile_id = ?""",
                    (annotation_id, self.profile_id),
                )
                conn.execute(
                    """DELETE FROM annotation_capture_blob_ref
                       WHERE capture_id IN (
                           SELECT capture_id FROM annotation_capture
                            WHERE annotation_id = ? AND profile_id = ?
                       )""",
                    (annotation_id, self.profile_id),
                )
                conn.execute(
                    """DELETE FROM annotation_capture
                       WHERE annotation_id = ? AND profile_id = ?""",
                    (annotation_id, self.profile_id),
                )
                conn.execute(
                    """DELETE FROM annotation_record
                       WHERE annotation_id = ? AND profile_id = ?""",
                    (annotation_id, self.profile_id),
                )
                conn.execute(
                    """UPDATE annotation_delete_job
                       SET phase='blob_cleanup', updated_at=?
                       WHERE delete_job_id=? AND profile_id=?""",
                    (changed_at, resolved_job_id, self.profile_id),
                )

        try:
            self._cleanup_delete_job(resolved_job_id, changed_at)
        except Exception as exc:
            with self.connect() as conn, write_txn(conn):
                conn.execute(
                    """UPDATE annotation_delete_job
                       SET state='failed', updated_at=?, error=?
                       WHERE delete_job_id=? AND profile_id=?""",
                    (changed_at, str(exc), resolved_job_id, self.profile_id),
                )
            raise
        with self.connect() as conn:
            return self._delete_result(conn, resolved_job_id)

    def export_annotation(
        self, annotation_id: str, *, include_screenshot_bytes: bool = False
    ) -> str:
        """Return canonical JSON from one writer-fenced metadata/payload snapshot."""

        with self.connect() as conn, write_txn(conn):
            bundle = self.frozen_export_snapshot(
                conn,
                annotation_id,
                include_screenshot_bytes=include_screenshot_bytes,
            )
        return canonical_json(bundle)

    def frozen_export_snapshot(
        self,
        conn: sqlite3.Connection,
        annotation_id: str,
        *,
        include_screenshot_bytes: bool = False,
    ) -> AnnotationExportBundleV1:
        """Read one metadata snapshot from a caller-held cross-store writer fence."""

        exported_blobs: list[AnnotationExportBlob] = []
        record_row = conn.execute(
            """SELECT r.*, ar.anchor_json, c.capture_json
               FROM annotation_record r
               JOIN annotation_anchor_revision ar
                 ON ar.anchor_revision_id = r.current_anchor_revision_id
                AND ar.annotation_id = r.annotation_id
               JOIN annotation_capture c ON c.capture_id = ar.capture_id
               WHERE r.annotation_id = ? AND r.profile_id = ?
                 AND NOT EXISTS (
                     SELECT 1 FROM annotation_bundle_create_journal j
                      WHERE j.annotation_id=r.annotation_id AND j.profile_id=r.profile_id
                 )""",
            (annotation_id, self.profile_id),
        ).fetchone()
        if record_row is None:
            raise KeyError(f"no annotation {annotation_id!r} in active profile")
        record = self._record_from_row(record_row)
        revision_rows = conn.execute(
            """SELECT ar.*, c.capture_json
               FROM annotation_anchor_revision ar
               JOIN annotation_capture c ON c.capture_id = ar.capture_id
               WHERE ar.annotation_id = ? AND ar.profile_id = ?
               ORDER BY ar.revision""",
            (annotation_id, self.profile_id),
        ).fetchall()
        revisions = tuple(self._anchor_revision_from_row(row) for row in revision_rows)
        blob_rows = conn.execute(
            """SELECT c.capture_id, r.blob_sha256, r.purpose, b.*
               FROM annotation_capture c
               JOIN annotation_capture_blob_ref r ON r.capture_id = c.capture_id
               LEFT JOIN annotation_blob b ON b.sha256 = r.blob_sha256
               WHERE c.annotation_id = ? AND c.profile_id = ?
               ORDER BY c.capture_id, r.blob_sha256, r.purpose""",
            (annotation_id, self.profile_id),
        ).fetchall()

        for row in blob_rows:
            if row["sha256"] is None:
                raise RuntimeError("annotation capture blob metadata is incomplete")
            expected_path = self._blob_relative_path(row["sha256"])
            content = None
            if (
                row["relative_path"] == expected_path
                and row["retention_state"] == "retained"
            ):
                content = self._read_verified_blob(row["sha256"], row["size_bytes"])
            retention_state = row["retention_state"]
            available = content is not None
            if retention_state == "retained" and not available:
                retention_state = "missing"
                conn.execute(
                    """UPDATE annotation_blob SET retention_state='missing'
                       WHERE sha256=? AND retention_state='retained'""",
                    (row["sha256"],),
                )
            if include_screenshot_bytes and content is not None:
                payload_state = "included"
                payload_base64 = base64.b64encode(content).decode("ascii")
            elif retention_state == "retained" and available:
                payload_state = "omitted"
                payload_base64 = None
            else:
                payload_state = "missing"
                payload_base64 = None
            exported_blobs.append(
                AnnotationExportBlob(
                    capture_id=row["capture_id"],
                    blob=BlobRef(
                        sha256=row["sha256"],
                        size_bytes=row["size_bytes"],
                        media_type=row["media_type"],
                        privacy_redacted=row["purpose"] == "redacted-crop",
                    ),
                    purpose=row["purpose"],
                    retention_state=retention_state,
                    payload_state=payload_state,
                    payload_base64=payload_base64,
                )
            )

        return AnnotationExportBundleV1(
            profile_id=self.profile_id,
            annotation=record,
            anchor_revisions=revisions,
            capture_blobs=tuple(exported_blobs),
        )

    def import_annotation(
        self, manifest: str | bytes | dict[str, object]
    ) -> AnnotationImportResult:
        """Validate a frozen bundle, commit all metadata, then retain payload bytes.

        An exact retry is recognized by a durable manifest receipt and resumes only
        the optional payload phase. Any other identifier collision fails closed.
        """

        bundle = self._parse_import_bundle(manifest)
        self._require_profile(bundle.profile_id)
        self._validate_import_metadata(bundle)
        canonical_manifest = canonical_json(bundle)
        manifest_digest = hashlib.sha256(canonical_manifest.encode("utf-8")).hexdigest()
        payloads = self._validate_import_payloads(bundle)
        exact_retry = False

        try:
            with self.connect() as conn, write_txn(conn):
                existing = conn.execute(
                    """SELECT profile_id, manifest_digest FROM annotation_import_receipt
                       WHERE annotation_id = ?""",
                    (bundle.annotation.annotation_id,),
                ).fetchone()
                record_exists = conn.execute(
                    "SELECT 1 FROM annotation_record WHERE annotation_id = ?",
                    (bundle.annotation.annotation_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["profile_id"] != self.profile_id
                        or existing["manifest_digest"] != manifest_digest
                        or record_exists is None
                    ):
                        raise RuntimeError("annotation import collision")
                    exact_retry = True
                elif record_exists is not None:
                    raise RuntimeError("annotation import collision")
                else:
                    self._insert_record(conn, bundle.annotation)
                    for revision in bundle.anchor_revisions:
                        capture_id = f"{revision.anchor_revision_id}:capture"
                        self._insert_capture(
                            conn,
                            bundle.annotation.annotation_id,
                            capture_id,
                            revision.capture,
                        )
                        self._insert_anchor_revision(conn, revision, capture_id)
                    conn.execute(
                        """INSERT INTO annotation_import_receipt
                               (annotation_id, profile_id, manifest_digest)
                           VALUES (?, ?, ?)""",
                        (
                            bundle.annotation.annotation_id,
                            self.profile_id,
                            manifest_digest,
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("annotation import collision") from exc

        retained_count = 0
        omitted_count = 0
        missing_count = 0
        captures = {
            f"{revision.anchor_revision_id}:capture": revision.capture
            for revision in bundle.anchor_revisions
        }
        for item in bundle.capture_blobs:
            if item.payload_state == "included":
                self.retain_capture_blob(
                    item.capture_id,
                    item.blob,
                    payloads[item.capture_id],
                    purpose=item.purpose,
                    created_at=captures[item.capture_id].captured_at,
                )
                retained_count += 1
            elif item.payload_state == "omitted":
                omitted_count += 1
            else:
                missing_count += 1
        return AnnotationImportResult(
            annotation_id=bundle.annotation.annotation_id,
            metadata_committed=True,
            exact_retry=exact_retry,
            payloads_retained=retained_count,
            payloads_omitted=omitted_count,
            payloads_missing=missing_count,
        )

    def retain_capture_blob(
        self,
        capture_id: str,
        blob: BlobRef,
        content: bytes,
        *,
        purpose: str | None = None,
        created_at: datetime,
    ) -> RetainedBlob:
        """Verify and durably retain bytes for an existing capture.

        The file becomes visible before metadata is marked retained. A crash in
        that narrow interval can leave only an unreferenced content-addressed
        file, which is safe to reuse on retry; it can never leave metadata that
        falsely promises bytes are available.
        """

        if not isinstance(content, bytes):
            raise TypeError("annotation capture content must be bytes")
        actual_digest = hashlib.sha256(content).hexdigest()
        if actual_digest != blob.sha256:
            raise ValueError("annotation capture SHA-256 does not match BlobRef")
        if len(content) != blob.size_bytes:
            raise ValueError("annotation capture byte count does not match BlobRef")
        if purpose is not None and purpose not in {"screenshot", "redacted-crop"}:
            raise ValueError(f"invalid annotation blob purpose: {purpose}")
        if purpose == "screenshot":
            resolved_purpose: Literal["screenshot", "redacted-crop"] = "screenshot"
        elif purpose == "redacted-crop":
            resolved_purpose = "redacted-crop"
        else:
            resolved_purpose = (
                "redacted-crop" if blob.privacy_redacted else "screenshot"
            )
        if blob.privacy_redacted != (resolved_purpose == "redacted-crop"):
            raise ValueError("blob privacyRedacted and purpose disagree")

        relative_path = self._blob_relative_path(blob.sha256)
        # BEGIN IMMEDIATE is the cross-process fence: authorization, durable file
        # publication, verification, and metadata commit are one serialized unit.
        # Deletion therefore runs wholly before this transaction (the capture is
        # absent) or wholly after it (and journals/removes the retained bytes).
        with self.connect() as conn, write_txn(conn):
            capture = conn.execute(
                """SELECT c.capture_json FROM annotation_capture c
                   WHERE c.capture_id = ? AND c.profile_id = ?
                     AND NOT EXISTS (
                         SELECT 1 FROM annotation_bundle_create_journal j
                          WHERE j.annotation_id=c.annotation_id AND j.profile_id=c.profile_id
                     )""",
                (capture_id, self.profile_id),
            ).fetchone()
            if capture is None:
                raise KeyError(f"no capture {capture_id!r} in active profile")
            declared = CaptureEvidence.model_validate(
                json.loads(capture["capture_json"])
            )
            if declared.blob != blob:
                raise ValueError("BlobRef does not match the capture declaration")
            # Do not touch the filesystem until the immutable declaration has
            # authorized this exact digest while the writer fence is held.
            self._write_verified_blob(content, blob.sha256)
            existing = conn.execute(
                "SELECT * FROM annotation_blob WHERE sha256 = ?", (blob.sha256,)
            ).fetchone()
            if existing is not None and (
                existing["size_bytes"] != blob.size_bytes
                or existing["media_type"] != blob.media_type
                or existing["relative_path"] != relative_path
            ):
                raise RuntimeError("content-addressed blob metadata conflict")
            conn.execute(
                """INSERT INTO annotation_blob
                       (sha256, size_bytes, media_type, relative_path,
                        retention_state, created_at)
                   VALUES (?, ?, ?, ?, 'retained', ?)
                   ON CONFLICT(sha256) DO UPDATE SET retention_state='retained'""",
                (
                    blob.sha256,
                    blob.size_bytes,
                    blob.media_type,
                    relative_path,
                    _iso(created_at),
                ),
            )
            conn.execute(
                """INSERT OR IGNORE INTO annotation_capture_blob_ref
                       (capture_id, blob_sha256, purpose) VALUES (?, ?, ?)""",
                (capture_id, blob.sha256, resolved_purpose),
            )
            retained = RetainedBlob(
                capture_id=capture_id,
                blob=blob,
                purpose=resolved_purpose,
                retention_state="retained",
                available=True,
            )
        return retained

    def capture_blob(self, capture_id: str, sha256: str) -> RetainedBlob | None:
        """Project availability, conservatively settling absent/corrupt bytes."""

        with self.connect() as conn:
            row = conn.execute(
                """SELECT b.*, r.capture_id, r.purpose
                   FROM annotation_capture_blob_ref r
                   JOIN annotation_capture c ON c.capture_id = r.capture_id
                   JOIN annotation_blob b ON b.sha256 = r.blob_sha256
                   WHERE r.capture_id = ? AND r.blob_sha256 = ?
                     AND c.profile_id = ?""",
                (capture_id, sha256, self.profile_id),
            ).fetchone()
        if row is None:
            return None

        expected_path = self._blob_relative_path(row["sha256"])
        available = (
            row["relative_path"] == expected_path
            and row["retention_state"] == "retained"
            and self._blob_matches(row["sha256"], row["size_bytes"])
        )
        state = row["retention_state"]
        if state == "retained" and not available:
            state = "missing"
            with self.connect() as conn, write_txn(conn):
                conn.execute(
                    """UPDATE annotation_blob SET retention_state='missing'
                       WHERE sha256 = ? AND retention_state='retained'""",
                    (row["sha256"],),
                )
        return RetainedBlob(
            capture_id=row["capture_id"],
            blob=BlobRef(
                sha256=row["sha256"],
                size_bytes=row["size_bytes"],
                media_type=row["media_type"],
                privacy_redacted=row["purpose"] == "redacted-crop",
            ),
            purpose=row["purpose"],
            retention_state=state,
            available=available,
        )

    def read_capture_blob(self, capture_id: str, sha256: str) -> bytes | None:
        retained = self.capture_blob(capture_id, sha256)
        if retained is None or not retained.available:
            return None
        content = self._read_verified_blob(sha256, retained.blob.size_bytes)
        if content is None:
            # A replacement between projection and read is treated as missing,
            # never returned as trusted capture evidence.
            with self.connect() as conn, write_txn(conn):
                conn.execute(
                    "UPDATE annotation_blob SET retention_state='missing' WHERE sha256 = ?",
                    (sha256,),
                )
            return None
        return content

    def projection(
        self,
        annotation_id: str,
        *,
        lineage_exists: Callable[[str], bool],
    ) -> AnnotationProjection | None:
        record = self.get(annotation_id)
        if record is None:
            return None
        orphaned = record.thread is None or not lineage_exists(
            record.thread.session_lineage_id
        )
        return AnnotationProjection(
            record=record, orphaned=orphaned, read_only=orphaned
        )

    def _cleanup_delete_job(self, delete_job_id: str, changed_at: str) -> None:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT blob_sha256, relative_path, state
                   FROM annotation_blob_cleanup_journal
                   WHERE delete_job_id = ? AND state IN ('pending','failed')
                   ORDER BY blob_sha256""",
                (delete_job_id,),
            ).fetchall()
        for journal in rows:
            sha256 = journal["blob_sha256"]
            try:
                with self.connect() as conn, write_txn(conn):
                    reference_count = conn.execute(
                        """SELECT count(*) FROM annotation_capture_blob_ref
                           WHERE blob_sha256 = ?""",
                        (sha256,),
                    ).fetchone()[0]
                    if reference_count:
                        conn.execute(
                            """UPDATE annotation_blob_cleanup_journal
                               SET state='preserved', error=NULL
                               WHERE delete_job_id=? AND blob_sha256=?""",
                            (delete_job_id, sha256),
                        )
                        continue
                    blob = conn.execute(
                        "SELECT relative_path FROM annotation_blob WHERE sha256 = ?",
                        (sha256,),
                    ).fetchone()
                    if (
                        blob is not None
                        and blob["relative_path"] != journal["relative_path"]
                    ):
                        raise RuntimeError("blob cleanup journal path conflict")
                    deleted = self._unlink_blob_file(sha256)
                    conn.execute(
                        """DELETE FROM annotation_blob
                           WHERE sha256 = ? AND NOT EXISTS (
                               SELECT 1 FROM annotation_capture_blob_ref
                                WHERE blob_sha256 = ?
                           )""",
                        (sha256, sha256),
                    )
                    conn.execute(
                        """UPDATE annotation_blob_cleanup_journal
                           SET state=?, error=NULL
                           WHERE delete_job_id=? AND blob_sha256=?""",
                        ("deleted" if deleted else "missing", delete_job_id, sha256),
                    )
            except Exception as exc:
                with self.connect() as conn, write_txn(conn):
                    conn.execute(
                        """UPDATE annotation_blob_cleanup_journal
                           SET state='failed', error=?
                           WHERE delete_job_id=? AND blob_sha256=?""",
                        (str(exc), delete_job_id, sha256),
                    )
                raise
        with self.connect() as conn, write_txn(conn):
            unfinished = conn.execute(
                """SELECT 1 FROM annotation_blob_cleanup_journal
                   WHERE delete_job_id=? AND state IN ('pending','failed') LIMIT 1""",
                (delete_job_id,),
            ).fetchone()
            if unfinished is not None:
                raise RuntimeError("annotation blob cleanup remains incomplete")
            conn.execute(
                """UPDATE annotation_delete_job
                   SET state='complete', phase='complete', updated_at=?, error=NULL
                   WHERE delete_job_id=? AND profile_id=?""",
                (changed_at, delete_job_id, self.profile_id),
            )

    def _delete_result(
        self, conn: sqlite3.Connection, delete_job_id: str
    ) -> AnnotationDeleteResult:
        job = conn.execute(
            """SELECT * FROM annotation_delete_job
               WHERE delete_job_id=? AND profile_id=?""",
            (delete_job_id, self.profile_id),
        ).fetchone()
        if job is None:
            raise KeyError(f"no delete job {delete_job_id!r} in active profile")
        states = {
            row["blob_sha256"]: row["state"]
            for row in conn.execute(
                """SELECT blob_sha256, state FROM annotation_blob_cleanup_journal
                   WHERE delete_job_id=? ORDER BY blob_sha256""",
                (delete_job_id,),
            ).fetchall()
        }
        return AnnotationDeleteResult(
            delete_job_id=delete_job_id,
            annotation_id=job["annotation_id"],
            state=job["state"],
            phase=job["phase"],
            blob_states=states,
            error=job["error"],
        )

    def _unlink_blob_file(self, sha256: str) -> bool:
        """Unlink only an owner-only regular file below the no-follow blob root."""

        descriptor = -1
        try:
            with self._open_blob_parent(sha256, create=False) as parent:
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                try:
                    descriptor = os.open(sha256, flags, dir_fd=parent)
                except FileNotFoundError:
                    os.fsync(parent)
                    return False
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_nlink != 1
                ):
                    raise PermissionError("annotation blob file is not safe to release")
                os.close(descriptor)
                descriptor = -1
                os.unlink(sha256, dir_fd=parent)
                os.fsync(parent)
                return True
        except FileNotFoundError:
            return False
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def _parse_import_bundle(
        manifest: str | bytes | dict[str, object],
    ) -> AnnotationExportBundleV1:
        if isinstance(manifest, bytes):
            try:
                manifest = manifest.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("annotation manifest must be UTF-8 JSON") from exc
        if isinstance(manifest, str):

            def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
                result: dict[str, object] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError(f"duplicate annotation manifest key: {key}")
                    result[key] = value
                return result

            def reject_constant(value: str) -> object:
                raise ValueError(
                    f"annotation manifest contains non-finite number: {value}"
                )

            try:
                parsed = json.loads(
                    manifest,
                    object_pairs_hook=reject_duplicates,
                    parse_constant=reject_constant,
                )
            except json.JSONDecodeError as exc:
                raise ValueError("annotation manifest is malformed JSON") from exc
        elif isinstance(manifest, dict):
            parsed = manifest
        else:
            raise TypeError(
                "annotation manifest must be JSON text, bytes, or a mapping"
            )
        return AnnotationExportBundleV1.model_validate(parsed)

    def _validate_import_metadata(self, bundle: AnnotationExportBundleV1) -> None:
        record = bundle.annotation
        self._require_profile(record.scope.profile_id)
        if not bundle.anchor_revisions:
            raise ValueError("annotation import requires anchor revisions")
        revisions = bundle.anchor_revisions
        if tuple(item.revision for item in revisions) != tuple(
            range(1, len(revisions) + 1)
        ):
            raise ValueError("imported anchor revisions must be ordered and contiguous")
        revision_ids = [item.anchor_revision_id for item in revisions]
        if len(revision_ids) != len(set(revision_ids)):
            raise ValueError("imported anchor revision ids must be unique")
        for index, revision in enumerate(revisions):
            if revision.annotation_id != record.annotation_id:
                raise ValueError(
                    "imported anchor revision belongs to another annotation"
                )
            if revision.changed_by.profile_id not in {None, self.profile_id}:
                raise PermissionError(
                    "imported revision author belongs to another profile"
                )
            if index == 0:
                if revision.previous_anchor_digest is not None:
                    raise ValueError("first imported anchor revision has prior digest")
                if revision.reason not in {"created", "imported"}:
                    raise ValueError(
                        "first imported anchor revision has invalid reason"
                    )
            else:
                if revision.reason != "reattached":
                    raise ValueError(
                        "subsequent imported anchor revisions must be reattached"
                    )
                expected_digest = canonical_digest(revisions[index - 1].anchor)
                if revision.previous_anchor_digest != expected_digest:
                    raise ValueError(
                        "imported previousAnchorDigest does not match history"
                    )
        if record.author.profile_id not in {None, self.profile_id}:
            raise PermissionError(
                "imported annotation author belongs to another profile"
            )
        if record.current_anchor_revision_id != revisions[-1].anchor_revision_id:
            raise ValueError("imported current anchor revision must be the history tip")
        if record.revision < len(revisions):
            raise ValueError("imported record revision cannot precede anchor history")
        current = next(
            (
                revision
                for revision in revisions
                if revision.anchor_revision_id == record.current_anchor_revision_id
            ),
            None,
        )
        if (
            current is None
            or current.anchor != record.anchor
            or current.capture != record.capture
        ):
            raise ValueError(
                "imported record does not match its current anchor revision"
            )

        expected_blobs: dict[str, tuple[BlobRef, str]] = {}
        for revision in revisions:
            if revision.capture.blob is None:
                continue
            capture_id = f"{revision.anchor_revision_id}:capture"
            purpose = (
                "redacted-crop"
                if revision.capture.blob.privacy_redacted
                else "screenshot"
            )
            expected_blobs[capture_id] = (revision.capture.blob, purpose)
        actual_blobs: dict[str, tuple[BlobRef, str]] = {}
        for item in bundle.capture_blobs:
            if item.capture_id in actual_blobs:
                raise ValueError("duplicate imported capture blob declaration")
            actual_blobs[item.capture_id] = (item.blob, item.purpose)
        if actual_blobs != expected_blobs:
            raise ValueError(
                "imported capture blob declarations do not match revisions"
            )

    @staticmethod
    def _validate_import_payloads(
        bundle: AnnotationExportBundleV1,
    ) -> dict[str, bytes]:
        payloads: dict[str, bytes] = {}
        for item in bundle.capture_blobs:
            if item.payload_state != "included":
                continue
            assert item.payload_base64 is not None
            try:
                content = base64.b64decode(item.payload_base64, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError(
                    "annotation capture payload is not valid base64"
                ) from exc
            if base64.b64encode(content).decode("ascii") != item.payload_base64:
                raise ValueError("annotation capture payload is not canonical base64")
            if hashlib.sha256(content).hexdigest() != item.blob.sha256:
                raise ValueError("annotation capture payload SHA-256 mismatch")
            if len(content) != item.blob.size_bytes:
                raise ValueError("annotation capture payload byte count mismatch")
            payloads[item.capture_id] = content
        return payloads

    def _validate_new_annotation(
        self, record: AnnotationRecordV1, first_revision: AnchorRevision
    ) -> None:
        self._require_profile(record.scope.profile_id)
        if record.revision != 1 or first_revision.revision != 1:
            raise ValueError(
                "new annotations and their first anchor revision must start at 1"
            )
        if first_revision.reason not in {"created", "imported"}:
            raise ValueError("first anchor revision must be created or imported")
        if first_revision.annotation_id != record.annotation_id:
            raise ValueError("anchor revision belongs to a different annotation")
        if first_revision.anchor_revision_id != record.current_anchor_revision_id:
            raise ValueError(
                "record current anchor revision does not match first revision"
            )
        if (
            first_revision.anchor != record.anchor
            or first_revision.capture != record.capture
        ):
            raise ValueError(
                "record anchor/capture must match its current immutable revision"
            )
        if first_revision.previous_anchor_digest is not None:
            raise ValueError(
                "first anchor revision cannot have a previous anchor digest"
            )

    def _require_profile(self, profile_id: str) -> None:
        if profile_id != self.profile_id:
            raise PermissionError("annotation scope does not match the active profile")

    def _insert_record(
        self, conn: sqlite3.Connection, record: AnnotationRecordV1
    ) -> None:
        conn.execute(
            """INSERT INTO annotation_record (
                   annotation_id, profile_id, browser_workspace_id, tab_id,
                   document_generation_id, kind, scope_json, author_json, thread_json,
                   status, created_at, updated_at, resolved_at,
                   current_anchor_revision_id, record_revision
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.annotation_id,
                record.scope.profile_id,
                record.scope.browser_workspace_id,
                record.scope.tab_id,
                record.scope.document_generation_id,
                record.kind,
                canonical_json(record.scope),
                canonical_json(record.author),
                canonical_json(record.thread) if record.thread is not None else None,
                record.status,
                _iso(record.created_at),
                _iso(record.updated_at),
                _iso(record.resolved_at) if record.resolved_at is not None else None,
                record.current_anchor_revision_id,
                record.revision,
            ),
        )

    def _insert_capture(
        self,
        conn: sqlite3.Connection,
        annotation_id: str,
        capture_id: str,
        capture: CaptureEvidence,
    ) -> None:
        conn.execute(
            """INSERT INTO annotation_capture
               (capture_id, annotation_id, profile_id, capture_json, capture_digest, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                capture_id,
                annotation_id,
                self.profile_id,
                canonical_json(capture),
                canonical_digest(capture),
                _iso(capture.captured_at),
            ),
        )
        if capture.blob is not None:
            relative_path = self._blob_relative_path(capture.blob.sha256)
            conn.execute(
                """INSERT INTO annotation_blob
                       (sha256, size_bytes, media_type, relative_path,
                        retention_state, created_at)
                   VALUES (?, ?, ?, ?, 'missing', ?)
                   ON CONFLICT(sha256) DO NOTHING""",
                (
                    capture.blob.sha256,
                    capture.blob.size_bytes,
                    capture.blob.media_type,
                    relative_path,
                    _iso(capture.captured_at),
                ),
            )
            existing = conn.execute(
                """SELECT size_bytes, media_type, relative_path
                   FROM annotation_blob WHERE sha256 = ?""",
                (capture.blob.sha256,),
            ).fetchone()
            if existing is None or (
                existing["size_bytes"] != capture.blob.size_bytes
                or existing["media_type"] != capture.blob.media_type
                or existing["relative_path"] != relative_path
            ):
                raise RuntimeError("content-addressed blob metadata conflict")
            purpose = "redacted-crop" if capture.blob.privacy_redacted else "screenshot"
            conn.execute(
                """INSERT INTO annotation_capture_blob_ref
                       (capture_id, blob_sha256, purpose) VALUES (?, ?, ?)""",
                (capture_id, capture.blob.sha256, purpose),
            )

    @staticmethod
    def _blob_relative_path(sha256: str) -> str:
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise ValueError("invalid annotation blob SHA-256")
        return f"{sha256[:2]}/{sha256[2:4]}/{sha256}"

    @contextlib.contextmanager
    def _open_blob_parent(self, sha256: str, *, create: bool) -> Iterator[int]:
        """Open the digest fan-out using no-follow directory descriptors."""

        self._blob_relative_path(sha256)
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.profile_home, flags)
        try:
            for component in (
                "browser",
                "annotations",
                "v1",
                "blobs",
                "sha256",
                sha256[:2],
                sha256[2:4],
            ):
                # A prior attempt may have created this child but failed while
                # syncing its parent. Re-sync every traversed parent on writes
                # so retry cannot bypass that durability failure.
                if create:
                    os.fsync(descriptor)
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(component, 0o700, dir_fd=descriptor)
                    except FileExistsError:
                        # A concurrent retaining process created the same child.
                        pass
                    os.fsync(descriptor)
                    child = os.open(component, flags, dir_fd=descriptor)
                metadata = os.fstat(child)
                if metadata.st_uid != os.getuid():
                    os.close(child)
                    raise PermissionError(
                        "annotation blob directory has a foreign owner"
                    )
                mode = stat.S_IMODE(metadata.st_mode)
                if mode != 0o700:
                    if not create:
                        os.close(child)
                        raise PermissionError(
                            "annotation blob directory is not owner-only"
                        )
                    os.fchmod(child, 0o700)
                    os.fsync(child)
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)

    def _ensure_store_root(self) -> None:
        """Create and durably verify private, no-follow repository ancestors."""

        self.profile_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.profile_home, flags)
        try:
            root_metadata = os.fstat(descriptor)
            if root_metadata.st_uid != os.getuid():
                raise PermissionError(
                    "annotation profile directory has a foreign owner"
                )
            for component in ("browser", "annotations", "v1"):
                # Re-sync every parent on retries so a prior mkdir whose parent
                # fsync failed cannot be mistaken for a durable repository root.
                os.fsync(descriptor)
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    try:
                        os.mkdir(component, 0o700, dir_fd=descriptor)
                    except FileExistsError:
                        # A concurrent initializer won the same no-follow child.
                        pass
                    os.fsync(descriptor)
                    child = os.open(component, flags, dir_fd=descriptor)
                metadata = os.fstat(child)
                if metadata.st_uid != os.getuid():
                    os.close(child)
                    raise PermissionError(
                        "annotation repository directory has a foreign owner"
                    )
                if stat.S_IMODE(metadata.st_mode) != 0o700:
                    os.fchmod(child, 0o700)
                    os.fsync(child)
                os.close(descriptor)
                descriptor = child
        finally:
            os.close(descriptor)

    def _read_verified_blob(self, sha256: str, size_bytes: int) -> bytes | None:
        descriptor = -1
        try:
            with self._open_blob_parent(sha256, create=False) as parent:
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(sha256, flags, dir_fd=parent)
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_nlink != 1
                    or metadata.st_size != size_bytes
                ):
                    return None
                chunks: list[bytes] = []
                digest = hashlib.sha256()
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    digest.update(chunk)
                if digest.hexdigest() != sha256:
                    return None
                return b"".join(chunks)
        except (OSError, PermissionError):
            return None
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _blob_matches(self, sha256: str, size_bytes: int) -> bool:
        return self._read_verified_blob(sha256, size_bytes) is not None

    def _sync_verified_blob(self, sha256: str, size_bytes: int) -> bool:
        """Validate and durably sync an existing blob and its directory entry."""

        descriptor = -1
        try:
            with self._open_blob_parent(sha256, create=True) as parent:
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(sha256, flags, dir_fd=parent)
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_nlink != 1
                    or metadata.st_size != size_bytes
                ):
                    return False
                digest = hashlib.sha256()
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                if digest.hexdigest() != sha256:
                    return False
                os.fsync(descriptor)
                os.fsync(parent)
                return True
        except FileNotFoundError:
            return False
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _write_verified_blob(self, content: bytes, sha256: str) -> None:
        if self._sync_verified_blob(sha256, len(content)):
            return
        temporary_name = f".capture-{secrets.token_hex(16)}"
        descriptor = -1
        with self._open_blob_parent(sha256, create=True) as parent:
            try:
                flags = (
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                )
                descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent)
                view = memoryview(content)
                while view:
                    written = os.write(descriptor, view)
                    view = view[written:]
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = -1
                os.rename(
                    temporary_name,
                    sha256,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                )
                os.fsync(parent)
                if not self._blob_matches(sha256, len(content)):
                    raise OSError("retained annotation blob failed verification")
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary_name, dir_fd=parent)

    def _insert_anchor_revision(
        self,
        conn: sqlite3.Connection,
        revision: AnchorRevision,
        capture_id: str,
    ) -> None:
        conn.execute(
            """INSERT INTO annotation_anchor_revision (
                   anchor_revision_id, annotation_id, profile_id, revision,
                   anchor_json, anchor_digest, capture_id, changed_by_json,
                   changed_at, reason, previous_anchor_digest
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                revision.anchor_revision_id,
                revision.annotation_id,
                self.profile_id,
                revision.revision,
                canonical_json(revision.anchor),
                canonical_digest(revision.anchor),
                capture_id,
                canonical_json(revision.changed_by),
                _iso(revision.changed_at),
                revision.reason,
                revision.previous_anchor_digest,
            ),
        )

    def _record_from_row(self, row: sqlite3.Row) -> AnnotationRecordV1:
        import json

        return AnnotationRecordV1.model_validate({
            "schemaVersion": 1,
            "annotationId": row["annotation_id"],
            "kind": row["kind"],
            "scope": json.loads(row["scope_json"]),
            "anchor": json.loads(row["anchor_json"]),
            "capture": json.loads(row["capture_json"]),
            "author": json.loads(row["author_json"]),
            "thread": json.loads(row["thread_json"]) if row["thread_json"] else None,
            "status": row["status"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "resolvedAt": row["resolved_at"],
            "currentAnchorRevisionId": row["current_anchor_revision_id"],
            "revision": row["record_revision"],
        })

    def _anchor_revision_from_row(self, row: sqlite3.Row) -> AnchorRevision:
        import json

        return AnchorRevision.model_validate({
            "anchorRevisionId": row["anchor_revision_id"],
            "annotationId": row["annotation_id"],
            "revision": row["revision"],
            "anchor": json.loads(row["anchor_json"]),
            "capture": json.loads(row["capture_json"]),
            "changedBy": json.loads(row["changed_by_json"]),
            "changedAt": row["changed_at"],
            "reason": row["reason"],
            "previousAnchorDigest": row["previous_anchor_digest"],
        })

    def _raise_missing_or_conflict(
        self, conn: sqlite3.Connection, annotation_id: str
    ) -> None:
        exists = conn.execute(
            "SELECT 1 FROM annotation_record WHERE annotation_id = ? AND profile_id = ?",
            (annotation_id, self.profile_id),
        ).fetchone()
        if exists is None:
            raise KeyError(f"no annotation {annotation_id!r} in active profile")
        raise RuntimeError("annotation revision conflict")
