"""Recoverable annotation metadata + dedicated lineage bundle operations."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Callable

from hermes_cli.browser_annotation_lineage import AnnotationLineageRepository
from hermes_cli.browser_annotation_worker_registry import AnnotationWorkerRegistry
from hermes_cli.browser_annotations_db import AnnotationRepository
from hermes_cli.browser_annotations_models import (
    AnchorRevision,
    AnnotationRecordV1,
    ThreadRef,
    canonical_json,
)
from hermes_cli.sqlite_util import write_txn

RuntimeSnapshot = dict[str, object]
SnapshotFactory = Callable[[str | None, int | None], RuntimeSnapshot]


def _canonical_digest(value: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class AnnotationBundleCoordinator:
    """Coordinate the two profile-local SQLite authorities without pretending 2PC."""

    def __init__(
        self,
        *,
        profile_id: str,
        annotations: AnnotationRepository,
        lineage: AnnotationLineageRepository,
        registry: AnnotationWorkerRegistry,
        snapshot_factory: SnapshotFactory,
    ) -> None:
        if annotations.profile_id != profile_id or lineage.profile_id != profile_id:
            raise PermissionError("annotation bundle repositories do not match active profile")
        if registry.repository is not lineage:
            raise ValueError("annotation bundle registry must own the lineage repository")
        self.profile_id = profile_id
        self.annotations = annotations
        self.lineage = lineage
        self.registry = registry
        self.snapshot_factory = snapshot_factory

    def create(
        self,
        record: AnnotationRecordV1,
        first_revision: AnchorRevision,
        *,
        source_message_id: int | None = None,
    ) -> AnnotationRecordV1:
        """Create metadata and lineage as one recoverable, never-partially-visible unit."""

        source_id = record.scope.session_lineage_id
        snapshot = self.snapshot_factory(source_id, source_message_id)
        model = snapshot.get("model")
        model_config = snapshot.get("model_config")
        system_prompt = snapshot.get("system_prompt")
        cwd = snapshot.get("cwd")
        if not isinstance(model, str) or not model.strip():
            raise RuntimeError("annotation runtime snapshot lacks a model")
        if not isinstance(model_config, dict) or not isinstance(model_config.get("tools"), list):
            raise RuntimeError("annotation runtime snapshot lacks frozen tools")
        if not isinstance(system_prompt, str) or not system_prompt:
            raise RuntimeError("annotation runtime snapshot lacks a stable system prompt")
        if cwd is not None and not isinstance(cwd, str):
            raise RuntimeError("annotation runtime snapshot has invalid cwd")

        root_id = f"annotation-lineage:{uuid.uuid4().hex}"
        coordinated_record = record.model_copy(
            update={
                "thread": ThreadRef(session_lineage_id=root_id, branch_id="main"),
            }
        )
        digest = _canonical_digest(
            {
                "record": coordinated_record.model_dump(mode="json", by_alias=True),
                "firstRevision": first_revision.model_dump(mode="json", by_alias=True),
                "runtime": snapshot,
                "sourceMessageId": source_message_id,
            }
        )
        self.annotations.stage_bundle_create(
            coordinated_record, first_revision, operation_digest=digest
        )
        try:
            self.lineage.create_lineage(
                annotation_id=coordinated_record.annotation_id,
                annotation_lineage_root_id=root_id,
                source_session_lineage_id=source_id,
                source_message_id=source_message_id,
                model=model,
                model_config=model_config,
                system_prompt=system_prompt,
                cwd=cwd,
                created_at=coordinated_record.created_at.timestamp(),
            )
        except Exception:
            # A normal exception is known not to have committed create_lineage's
            # transaction. A process crash is instead settled by recover().
            if not self.lineage.lineage_matches(
                annotation_id=coordinated_record.annotation_id,
                thread_generation=1,
                annotation_lineage_root_id=root_id,
            ):
                self.annotations.abort_bundle_create(
                    coordinated_record.annotation_id, root_id
                )
            else:
                self.annotations.finish_bundle_create(
                    coordinated_record.annotation_id, root_id
                )
            raise
        self.annotations.finish_bundle_create(coordinated_record.annotation_id, root_id)
        return coordinated_record

    def recover(self) -> dict[str, int]:
        """Settle crash windows before queue recovery or authenticated reads."""

        finalized = 0
        rolled_back = 0
        deleted = 0
        for pending in self.annotations.pending_bundle_creations():
            annotation_id = pending["annotation_id"]
            root_id = pending["annotation_lineage_root_id"]
            if self.lineage.lineage_matches(
                annotation_id=annotation_id,
                thread_generation=1,
                annotation_lineage_root_id=root_id,
            ):
                self.annotations.finish_bundle_create(annotation_id, root_id)
                finalized += 1
            else:
                self.annotations.abort_bundle_create(annotation_id, root_id)
                rolled_back += 1
        for annotation_id in self.lineage.deleting_annotations():
            self.delete(annotation_id)
            deleted += 1
        return {"creates_finalized": finalized, "creates_rolled_back": rolled_back, "deletes_completed": deleted}

    def export(
        self, annotation_id: str, *, include_screenshot_bytes: bool = False
    ) -> str:
        """Freeze both WAL databases under writer fences and emit bodies once."""

        # Fixed lock order (annotation metadata, then state.db) prevents a
        # coordinator deadlock. SQLite's BEGIN IMMEDIATE includes committed WAL
        # pages and blocks both authorities' writers until both projections exist.
        with self.annotations.connect() as annotation_conn:
            with write_txn(annotation_conn):
                with self.lineage.connect() as lineage_conn:
                    with write_txn(lineage_conn):
                        metadata = self.annotations.frozen_export_snapshot(
                            annotation_conn,
                            annotation_id,
                            include_screenshot_bytes=include_screenshot_bytes,
                        )
                        thread = self.lineage.frozen_export_snapshot(
                            lineage_conn, annotation_id
                        )
        bundle = {
            "bundleVersion": 1,
            "kind": "hermes-browser-annotation-thread",
            "repositoryScope": "annotation-metadata-captures-and-dedicated-thread",
            "includesThreadBodies": True,
            "includesSourceConversationBodies": False,
            "profileId": self.profile_id,
            "annotationMetadata": metadata.model_dump(mode="json", by_alias=True),
            **thread,
        }
        return canonical_json(bundle)

    def delete(self, annotation_id: str) -> dict[str, object]:
        """Resume freeze -> metadata/blob cleanup -> dedicated lineage removal."""

        turn_ids = self.lineage.begin_bundle_delete(annotation_id)
        self.registry.signal_cancelled(turn_ids)
        result = self.annotations.delete_result_for_annotation(annotation_id)
        if result is None or result.state != "complete":
            try:
                result = self.annotations.delete_annotation(
                    annotation_id,
                    requested_at=datetime.now(UTC),
                )
            except Exception:
                partial = self.annotations.delete_result_for_annotation(annotation_id)
                if partial is None:
                    raise
                return {
                    "outcome": "delete_partial",
                    **partial.model_dump(mode="json", by_alias=True),
                    "threadDeleted": False,
                }
        if result.state != "complete":
            return {
                "outcome": "delete_partial",
                **result.model_dump(mode="json", by_alias=True),
            }
        self.lineage.delete_bundle_lineage(annotation_id)
        return {
            "outcome": "deleted",
            **result.model_dump(mode="json", by_alias=True),
            "threadDeleted": True,
        }
