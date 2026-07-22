from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from pydantic import ValidationError

import hermes_cli.browser_annotations_db as annotation_db
from hermes_cli.browser_annotations_db import AnnotationRepository
from hermes_cli.browser_annotations_models import (
    AnchorRevision,
    AnnotationRecordV1,
    AnnotationScope,
    AuthorRef,
    BlobRef,
    CaptureEvidence,
    CssRect,
    ElementAnchor,
    ElementFingerprint,
    ThreadRef,
    ViewportEvidence,
    canonical_digest,
    canonical_json,
)

NOW = datetime(2026, 7, 18, 13, 30, tzinfo=UTC)


def _scope(profile_id: str = "coding", generation: str = "doc-1") -> AnnotationScope:
    return AnnotationScope(
        profile_id=profile_id,
        browser_workspace_id="workspace-1",
        tab_id="tab-1",
        document_generation_id=generation,
        session_lineage_id="source-lineage",
        requested_url="https://example.test/page",
        committed_url="https://example.test/page",
        canonical_url="https://example.test/page",
        origin="https://example.test",
    )


def _author() -> AuthorRef:
    return AuthorRef(
        kind="human", actor_id="actor-1", profile_id="coding", display_name="Kosta"
    )


def _anchor(tag: str = "button") -> ElementAnchor:
    return ElementAnchor(
        frame_path=(),
        fingerprint=ElementFingerprint(
            tag=tag, role="button", stable_attributes={"id": "save"}
        ),
    )


def _capture(generation: str = "doc-1") -> CaptureEvidence:
    return CaptureEvidence(
        viewport=ViewportEvidence(
            css_width=1200,
            css_height=800,
            layout_scroll_x=0,
            layout_scroll_y=10,
            visual_offset_x=0,
            visual_offset_y=0,
            visual_scale=1,
            page_zoom_factor=1,
            device_scale_factor=2,
        ),
        guest_bounds=CssRect(x=0, y=0, width=1200, height=800),
        top_frame_id="frame-root",
        target_frame_id="frame-root",
        committed_url="https://example.test/page",
        captured_at=NOW,
        document_generation_id=generation,
        observed_target_rects=(CssRect(x=10, y=20, width=80, height=30),),
        visible=True,
    )


def _capture_with_blob(content: bytes, *, redacted: bool = False) -> CaptureEvidence:
    return _capture().model_copy(
        update={
            "blob": BlobRef(
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                media_type="image/png",
                privacy_redacted=redacted,
            ),
            "image_pixel_width": 20,
            "image_pixel_height": 10,
        }
    )


def _required_blob(capture: CaptureEvidence) -> BlobRef:
    assert capture.blob is not None
    return capture.blob


def _record(profile_id: str = "coding") -> AnnotationRecordV1:
    return AnnotationRecordV1(
        annotation_id="ann-1",
        kind="element",
        scope=_scope(profile_id),
        anchor=_anchor(),
        capture=_capture(),
        author=_author(),
        thread=ThreadRef(session_lineage_id="annotation-lineage", branch_id="main"),
        created_at=NOW,
        updated_at=NOW,
        current_anchor_revision_id="ar-1",
        revision=1,
    )


def _first_revision(record: AnnotationRecordV1 | None = None) -> AnchorRevision:
    record = record or _record()
    return AnchorRevision(
        anchor_revision_id="ar-1",
        annotation_id=record.annotation_id,
        revision=1,
        anchor=record.anchor,
        capture=record.capture,
        changed_by=record.author,
        changed_at=NOW,
        reason="created",
    )


def test_private_workspace_refuses_before_creating_files(tmp_path):
    repo = AnnotationRepository(
        profile_id="coding", profile_home=tmp_path / "profile", private_workspace=True
    )

    with pytest.raises(PermissionError, match="private"):
        repo.create(_record(), _first_revision())

    assert not (tmp_path / "profile").exists()


def test_backdated_status_is_rejected_without_mutating_record(tmp_path):
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    repo.create(_record(), _first_revision())

    with pytest.raises(ValueError, match="predate"):
        repo.set_status(
            "ann-1",
            status="resolved",
            expected_revision=1,
            changed_at=NOW - timedelta(days=1),
        )

    record = repo.get("ann-1")
    assert record is not None
    assert record.status == "open"
    assert record.revision == 1


def test_repository_creates_dedicated_v1_schema_and_round_trips(tmp_path):
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    record = _record()
    repo.create(record, _first_revision(record))

    assert repo.db_path == tmp_path / "browser/annotations/v1/annotations.v1.sqlite3"
    assert repo.get(record.annotation_id) == record
    assert repo.list_for_workspace("workspace-1") == [record]

    with sqlite3.connect(repo.db_path) as conn:
        assert (
            conn.execute("PRAGMA user_version").fetchone()[0]
            == annotation_db.SCHEMA_VERSION
        )
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {
        "annotation_repository_identity",
        "annotation_record",
        "annotation_anchor_revision",
        "annotation_capture",
        "annotation_blob",
        "annotation_capture_blob_ref",
        "annotation_delete_job",
        "annotation_blob_cleanup_journal",
        "annotation_import_receipt",
    } <= names


def test_profile_isolation_rejects_before_insert_and_queries_exact_profile(tmp_path):
    coding = AnnotationRepository(profile_id="coding", profile_home=tmp_path / "coding")
    foreign = _record(profile_id="guest")

    with pytest.raises(PermissionError, match="active profile"):
        coding.create(foreign, _first_revision(foreign))
    assert not coding.db_path.exists()

    coding.create(_record(), _first_revision())
    with sqlite3.connect(coding.db_path) as conn:
        conn.execute(
            "UPDATE annotation_record SET profile_id='guest' WHERE annotation_id='ann-1'"
        )
        conn.commit()
    assert coding.get("ann-1") is None
    assert coding.list_for_workspace("workspace-1") == []


def test_anchor_revisions_are_immutable_at_database_boundary(tmp_path):
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    repo.create(_record(), _first_revision())

    with sqlite3.connect(repo.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE annotation_anchor_revision SET reason='imported' "
                "WHERE anchor_revision_id='ar-1'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "DELETE FROM annotation_anchor_revision WHERE anchor_revision_id='ar-1'"
            )


def test_status_update_is_optimistic_and_preserves_anchor_history(tmp_path):
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    repo.create(_record(), _first_revision())

    changed = NOW + timedelta(seconds=1)
    resolved = repo.set_status(
        "ann-1", status="resolved", expected_revision=1, changed_at=changed
    )
    assert resolved.status == "resolved"
    assert resolved.resolved_at == changed
    assert resolved.revision == 2
    assert [revision.revision for revision in repo.anchor_revisions("ann-1")] == [1]

    with pytest.raises(RuntimeError, match="conflict"):
        repo.set_status("ann-1", status="open", expected_revision=1, changed_at=changed)


def test_reattach_appends_revision_and_moves_scope_without_rewriting_history(tmp_path):
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    repo.create(_record(), _first_revision())
    new_scope = _scope(generation="doc-2")
    new_capture = _capture(generation="doc-2")
    new_anchor = _anchor(tag="a")
    revision = AnchorRevision(
        anchor_revision_id="ar-2",
        annotation_id="ann-1",
        revision=2,
        anchor=new_anchor,
        capture=new_capture,
        changed_by=_author(),
        changed_at=NOW + timedelta(seconds=2),
        reason="reattached",
        previous_anchor_digest=canonical_digest(_anchor()),
    )

    changed = repo.reattach(
        "ann-1", revision, new_scope=new_scope, expected_record_revision=1
    )

    assert changed.scope == new_scope
    assert changed.anchor == new_anchor
    assert changed.capture == new_capture
    assert changed.revision == 2
    assert [item.anchor_revision_id for item in repo.anchor_revisions("ann-1")] == [
        "ar-1",
        "ar-2",
    ]

    with pytest.raises(ValueError, match="contiguous"):
        repo.reattach(
            "ann-1",
            revision.model_copy(update={"anchor_revision_id": "ar-4", "revision": 4}),
            new_scope=new_scope,
            expected_record_revision=2,
        )


def test_orphan_projection_is_read_only_without_lineage_fallback(tmp_path):
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    repo.create(_record(), _first_revision())

    orphan = repo.projection("ann-1", lineage_exists=lambda _lineage: False)
    assert orphan is not None
    assert orphan.orphaned is True
    assert orphan.read_only is True

    live = repo.projection(
        "ann-1", lineage_exists=lambda lineage: lineage == "annotation-lineage"
    )
    assert live is not None
    assert live.orphaned is False
    assert live.read_only is False


def test_unknown_future_schema_version_fails_without_mutating_database(tmp_path):
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    future_version = annotation_db.SCHEMA_VERSION + 1
    repo.db_path.parent.mkdir(parents=True)
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute(f"PRAGMA user_version={future_version}")

    with pytest.raises(RuntimeError, match="newer than supported"):
        repo.get("anything")
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == future_version
        assert (
            conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
            == 0
        )


def test_incomplete_version_one_schema_is_rejected(tmp_path):
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    repo.db_path.parent.mkdir(parents=True)
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute("CREATE TABLE annotation_record(annotation_id TEXT PRIMARY KEY)")
        conn.execute("PRAGMA user_version=1")

    with pytest.raises(RuntimeError, match="schema is incomplete"):
        repo.get("anything")


def test_failed_v1_migration_is_atomic_and_retryable(tmp_path, monkeypatch):
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    broken_schema = (
        annotation_db._SCHEMA_V1
        + "\nCREATE TABLE annotation_record(duplicate_name TEXT);"
    )
    monkeypatch.setattr(annotation_db, "_SCHEMA_V1", broken_schema)

    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        repo.get("anything")

    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type IN ('table', 'trigger')"
            ).fetchone()[0]
            == 0
        )

    monkeypatch.setattr(annotation_db, "_SCHEMA_V1", broken_schema.rsplit("\n", 1)[0])
    assert repo.get("anything") is None
    with sqlite3.connect(repo.db_path) as conn:
        assert (
            conn.execute("PRAGMA user_version").fetchone()[0]
            == annotation_db.SCHEMA_VERSION
        )


def test_models_are_strict_body_free_and_canonical_digest_is_stable():
    record = _record()
    by_alias = record.model_dump(mode="json", by_alias=True)
    by_alias["anchor"] = {
        "fingerprint": by_alias["anchor"]["fingerprint"],
        "type": "element",
        "body": "no",
    }
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AnnotationRecordV1.model_validate(by_alias)

    left = {"z": [2, 1], "a": "é"}
    right = {"a": "é", "z": [2, 1]}
    assert canonical_json(left) == canonical_json(right)
    assert canonical_digest(left) == canonical_digest(right)


def test_capture_and_scope_generation_must_match():
    data = _record().model_dump(mode="json", by_alias=True)
    data["capture"]["documentGenerationId"] = "other"
    with pytest.raises(ValidationError, match="document generations"):
        AnnotationRecordV1.model_validate(data)


def test_capture_blob_is_missing_until_exact_bytes_are_durably_retained(tmp_path):
    content = b"verified screenshot bytes"
    capture = _capture_with_blob(content)
    record = _record().model_copy(update={"capture": capture})
    revision = _first_revision(record).model_copy(update={"capture": capture})
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    repo.create(record, revision)
    capture_id = "ar-1:capture"

    missing = repo.capture_blob(capture_id, _required_blob(capture).sha256)
    assert missing is not None
    assert missing.retention_state == "missing"
    assert missing.available is False
    assert repo.read_capture_blob(capture_id, _required_blob(capture).sha256) is None

    retained = repo.retain_capture_blob(
        capture_id, _required_blob(capture), content, created_at=NOW
    )
    assert retained.retention_state == "retained"
    assert retained.available is True
    assert repo.read_capture_blob(capture_id, _required_blob(capture).sha256) == content
    path = annotation_db.annotation_blob_root(tmp_path) / repo._blob_relative_path(
        _required_blob(capture).sha256
    )
    assert path.read_bytes() == content
    assert path.stat().st_mode & 0o777 == 0o600


def test_capture_blob_rejects_wrong_hash_size_purpose_and_foreign_capture(tmp_path):
    content = b"capture"
    capture = _capture_with_blob(content, redacted=True)
    record = _record().model_copy(update={"capture": capture})
    revision = _first_revision(record).model_copy(update={"capture": capture})
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    repo.create(record, revision)

    with pytest.raises(ValueError, match="SHA-256"):
        repo.retain_capture_blob(
            "ar-1:capture", _required_blob(capture), b"tampered", created_at=NOW
        )
    wrong_size = _required_blob(capture).model_copy(
        update={
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": 999,
        }
    )
    with pytest.raises(ValueError, match="byte count"):
        repo.retain_capture_blob("ar-1:capture", wrong_size, content, created_at=NOW)
    with pytest.raises(ValueError, match="disagree"):
        repo.retain_capture_blob(
            "ar-1:capture",
            _required_blob(capture),
            content,
            purpose="screenshot",
            created_at=NOW,
        )
    with pytest.raises(KeyError, match="active profile"):
        repo.retain_capture_blob(
            "foreign", _required_blob(capture), content, created_at=NOW
        )
    assert not annotation_db.annotation_blob_root(tmp_path).exists()


def test_missing_or_corrupt_retained_blob_is_settled_missing_and_never_returned(
    tmp_path,
):
    content = b"capture"
    capture = _capture_with_blob(content)
    record = _record().model_copy(update={"capture": capture})
    revision = _first_revision(record).model_copy(update={"capture": capture})
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    repo.create(record, revision)
    capture_id = "ar-1:capture"
    repo.retain_capture_blob(
        capture_id, _required_blob(capture), content, created_at=NOW
    )
    path = annotation_db.annotation_blob_root(tmp_path) / repo._blob_relative_path(
        _required_blob(capture).sha256
    )

    path.write_bytes(b"corrupt!")
    projection = repo.capture_blob(capture_id, _required_blob(capture).sha256)
    assert projection is not None
    assert projection.retention_state == "missing"
    assert projection.available is False
    assert repo.read_capture_blob(capture_id, _required_blob(capture).sha256) is None

    # Exact retry repairs both bytes and retention metadata idempotently.
    repaired = repo.retain_capture_blob(
        capture_id,
        _required_blob(capture),
        content,
        created_at=NOW + timedelta(seconds=1),
    )
    assert repaired.available is True
    assert repo.read_capture_blob(capture_id, _required_blob(capture).sha256) == content


def test_blob_hierarchy_rejects_symlink_ancestor_without_external_write(tmp_path):
    content = b"capture"
    capture = _capture_with_blob(content)
    record = _record().model_copy(update={"capture": capture})
    revision = _first_revision(record).model_copy(update={"capture": capture})
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    repo.create(record, revision)
    blob = _required_blob(capture)
    root = annotation_db.annotation_blob_root(tmp_path)
    root.mkdir(parents=True)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (root / blob.sha256[:2]).symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        repo.retain_capture_blob("ar-1:capture", blob, content, created_at=NOW)

    assert list(outside.iterdir()) == []
    missing = repo.capture_blob("ar-1:capture", blob.sha256)
    assert missing is not None
    assert missing.available is False


def test_exact_preexisting_blob_is_rewritten_owner_only_before_retention(tmp_path):
    content = b"capture"
    capture = _capture_with_blob(content)
    record = _record().model_copy(update={"capture": capture})
    revision = _first_revision(record).model_copy(update={"capture": capture})
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    repo.create(record, revision)
    blob = _required_blob(capture)
    path = annotation_db.annotation_blob_root(tmp_path) / repo._blob_relative_path(
        blob.sha256
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    path.chmod(0o644)

    retained = repo.retain_capture_blob("ar-1:capture", blob, content, created_at=NOW)

    assert retained.available is True
    assert path.stat().st_uid == os.getuid()
    assert path.stat().st_mode & 0o777 == 0o600


def test_repository_root_is_structurally_bound_to_one_profile_id(tmp_path):
    coding = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    coding.create(_record(), _first_revision())
    guest = AnnotationRepository(profile_id="guest", profile_home=tmp_path)

    with pytest.raises(PermissionError, match="different active profile"):
        guest.get("ann-1")


def test_retry_repeats_durability_sync_after_post_rename_fsync_failure(
    tmp_path, monkeypatch
):
    content = b"capture"
    capture = _capture_with_blob(content)
    record = _record().model_copy(update={"capture": capture})
    revision = _first_revision(record).model_copy(update={"capture": capture})
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    repo.create(record, revision)
    blob = _required_blob(capture)
    path = annotation_db.annotation_blob_root(tmp_path) / repo._blob_relative_path(
        blob.sha256
    )
    real_fsync = os.fsync
    sync_calls = 0
    failed = False

    def fail_first_post_rename_directory_sync(descriptor: int) -> None:
        nonlocal sync_calls, failed
        sync_calls += 1
        if not failed and path.exists() and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            failed = True
            raise OSError("injected directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_first_post_rename_directory_sync)
    with pytest.raises(OSError, match="injected"):
        repo.retain_capture_blob("ar-1:capture", blob, content, created_at=NOW)
    assert path.exists()
    before_retry = sync_calls

    retained = repo.retain_capture_blob(
        "ar-1:capture", blob, content, created_at=NOW + timedelta(seconds=1)
    )

    assert sync_calls > before_retry
    assert retained.available is True


def test_delete_metadata_commit_survives_cleanup_crash_and_retry(tmp_path, monkeypatch):
    content = b"delete me"
    capture = _capture_with_blob(content)
    record = _record().model_copy(update={"capture": capture})
    revision = _first_revision(record).model_copy(update={"capture": capture})
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    repo.create(record, revision)
    blob = _required_blob(capture)
    repo.retain_capture_blob("ar-1:capture", blob, content, created_at=NOW)
    path = annotation_db.annotation_blob_root(tmp_path) / repo._blob_relative_path(
        blob.sha256
    )
    cleanup = repo._cleanup_delete_job

    class InjectedCrash(BaseException):
        pass

    def crash_after_metadata(*_args, **_kwargs):
        raise InjectedCrash("injected cleanup crash")

    monkeypatch.setattr(repo, "_cleanup_delete_job", crash_after_metadata)
    with pytest.raises(InjectedCrash, match="injected cleanup crash"):
        repo.delete_annotation("ann-1", requested_at=NOW, delete_job_id="delete-1")

    assert repo.get("ann-1") is None
    assert path.exists()
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute(
            "SELECT state, phase FROM annotation_delete_job WHERE delete_job_id='delete-1'"
        ).fetchone() == ("deleting", "blob_cleanup")
        assert (
            conn.execute(
                "SELECT state FROM annotation_blob_cleanup_journal"
            ).fetchone()[0]
            == "pending"
        )

    monkeypatch.setattr(repo, "_cleanup_delete_job", cleanup)
    result = repo.delete_annotation("ann-1", requested_at=NOW + timedelta(seconds=1))
    assert result.state == "complete"
    assert result.phase == "complete"
    assert result.blob_states == {blob.sha256: "deleted"}
    assert not path.exists()


def test_delete_preserves_blob_still_shared_by_another_annotation(tmp_path):
    content = b"shared capture"
    capture = _capture_with_blob(content)
    first = _record().model_copy(update={"capture": capture})
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    repo.create(first, _first_revision(first).model_copy(update={"capture": capture}))
    second = first.model_copy(
        update={"annotation_id": "ann-2", "current_anchor_revision_id": "ar-2"}
    )
    second_revision = _first_revision(second).model_copy(
        update={
            "anchor_revision_id": "ar-2",
            "annotation_id": "ann-2",
            "capture": capture,
        }
    )
    repo.create(second, second_revision)
    blob = _required_blob(capture)
    repo.retain_capture_blob("ar-1:capture", blob, content, created_at=NOW)

    result = repo.delete_annotation("ann-1", requested_at=NOW, delete_job_id="delete-1")

    assert result.blob_states == {blob.sha256: "preserved"}
    assert repo.get("ann-2") == second
    assert repo.read_capture_blob("ar-2:capture", blob.sha256) == content


def test_export_is_deterministic_body_free_and_capture_payload_is_opt_in(tmp_path):
    content = b"redacted capture"
    capture = _capture_with_blob(content, redacted=True)
    record = _record().model_copy(update={"capture": capture})
    revision = _first_revision(record).model_copy(update={"capture": capture})
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    repo.create(record, revision)
    repo.retain_capture_blob(
        "ar-1:capture", _required_blob(capture), content, created_at=NOW
    )

    metadata_only = repo.export_annotation("ann-1")
    assert metadata_only == repo.export_annotation("ann-1")
    metadata = json.loads(metadata_only)
    assert metadata["repositoryScope"] == "annotation-metadata-and-captures"
    assert metadata["includesThreadBodies"] is False
    assert metadata["captureBlobs"][0]["blob"]["privacyRedacted"] is True
    assert metadata["captureBlobs"][0]["payloadState"] == "omitted"
    assert metadata["captureBlobs"][0]["payloadBase64"] is None

    with_payload = json.loads(
        repo.export_annotation("ann-1", include_screenshot_bytes=True)
    )
    assert with_payload["captureBlobs"][0]["payloadState"] == "included"
    assert with_payload["captureBlobs"][0]["payloadBase64"] is not None


def test_export_declares_missing_capture_and_import_round_trips_opted_in_bytes(
    tmp_path,
):
    content = b"portable capture"
    capture = _capture_with_blob(content)
    record = _record().model_copy(update={"capture": capture})
    revision = _first_revision(record).model_copy(update={"capture": capture})
    source = AnnotationRepository(profile_id="coding", profile_home=tmp_path / "source")
    source.create(record, revision)
    missing = json.loads(source.export_annotation("ann-1"))
    assert missing["captureBlobs"][0]["retentionState"] == "missing"
    assert missing["captureBlobs"][0]["payloadState"] == "missing"

    source.retain_capture_blob(
        "ar-1:capture", _required_blob(capture), content, created_at=NOW
    )
    manifest = source.export_annotation("ann-1", include_screenshot_bytes=True)
    target = AnnotationRepository(profile_id="coding", profile_home=tmp_path / "target")
    result = target.import_annotation(manifest)
    assert result.metadata_committed is True
    assert result.exact_retry is False
    assert target.get("ann-1") == record
    assert target.anchor_revisions("ann-1") == [revision]
    assert (
        target.read_capture_blob("ar-1:capture", _required_blob(capture).sha256)
        == content
    )


def test_import_refuses_cross_profile_malformed_hash_mismatch_and_collision(tmp_path):
    content = b"portable capture"
    capture = _capture_with_blob(content)
    record = _record().model_copy(update={"capture": capture})
    revision = _first_revision(record).model_copy(update={"capture": capture})
    source = AnnotationRepository(profile_id="coding", profile_home=tmp_path / "source")
    source.create(record, revision)
    source.retain_capture_blob(
        "ar-1:capture", _required_blob(capture), content, created_at=NOW
    )
    manifest = source.export_annotation("ann-1", include_screenshot_bytes=True)

    guest_home = tmp_path / "guest"
    guest = AnnotationRepository(profile_id="guest", profile_home=guest_home)
    with pytest.raises(PermissionError, match="active profile"):
        guest.import_annotation(manifest)
    assert not guest_home.exists()

    target = AnnotationRepository(profile_id="coding", profile_home=tmp_path / "target")
    with pytest.raises(ValueError, match="malformed"):
        target.import_annotation("{")
    tampered = json.loads(manifest)
    tampered["captureBlobs"][0]["payloadBase64"] = "dGFtcGVyZWQ="
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        target.import_annotation(tampered)
    assert not target.db_path.exists()

    target.create(record, revision)
    with pytest.raises(RuntimeError, match="collision"):
        target.import_annotation(manifest)


def test_exact_import_retry_resumes_payload_after_metadata_commit(
    tmp_path, monkeypatch
):
    content = b"resume payload"
    capture = _capture_with_blob(content)
    record = _record().model_copy(update={"capture": capture})
    revision = _first_revision(record).model_copy(update={"capture": capture})
    source = AnnotationRepository(profile_id="coding", profile_home=tmp_path / "source")
    source.create(record, revision)
    source.retain_capture_blob(
        "ar-1:capture", _required_blob(capture), content, created_at=NOW
    )
    manifest = source.export_annotation("ann-1", include_screenshot_bytes=True)
    target = AnnotationRepository(profile_id="coding", profile_home=tmp_path / "target")
    retain = target.retain_capture_blob

    def fail_payload(*_args, **_kwargs):
        raise OSError("injected payload write failure")

    monkeypatch.setattr(target, "retain_capture_blob", fail_payload)
    with pytest.raises(OSError, match="injected payload"):
        target.import_annotation(manifest)
    assert target.get("ann-1") == record
    assert (
        target.read_capture_blob("ar-1:capture", _required_blob(capture).sha256) is None
    )

    monkeypatch.setattr(target, "retain_capture_blob", retain)
    retry = target.import_annotation(manifest)
    assert retry.exact_retry is True
    assert retry.payloads_retained == 1
    assert (
        target.read_capture_blob("ar-1:capture", _required_blob(capture).sha256)
        == content
    )


def test_v1_to_v2_migration_failure_rolls_back_and_retries(tmp_path, monkeypatch):
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    repo.db_path.parent.mkdir(parents=True)
    with sqlite3.connect(repo.db_path) as conn:
        annotation_db._migrate_v1(conn)
    schema_v2 = annotation_db._SCHEMA_V2
    broken_v2 = schema_v2 + "\nCREATE TABLE annotation_record(nope TEXT);"
    monkeypatch.setattr(annotation_db, "_SCHEMA_V2", broken_v2)

    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        repo.get("anything")
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(annotation_delete_job)")
        }
        assert "phase" not in columns
        assert (
            conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='annotation_blob_cleanup_journal'"
            ).fetchone()[0]
            == 0
        )

    monkeypatch.setattr(annotation_db, "_SCHEMA_V2", schema_v2)
    assert repo.get("anything") is None
    with sqlite3.connect(repo.db_path) as conn:
        assert (
            conn.execute("PRAGMA user_version").fetchone()[0]
            == annotation_db.SCHEMA_VERSION
        )
        assert "phase" in {
            row[1] for row in conn.execute("PRAGMA table_info(annotation_delete_job)")
        }


def test_v2_to_v3_bundle_journal_migration_is_atomic_and_retryable(tmp_path, monkeypatch):
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    repo.db_path.parent.mkdir(parents=True)
    with sqlite3.connect(repo.db_path) as conn:
        annotation_db._migrate_v1(conn)
        annotation_db._migrate_v2(conn)
    schema_v3 = annotation_db._SCHEMA_V3
    monkeypatch.setattr(
        annotation_db,
        "_SCHEMA_V3",
        schema_v3 + "\nCREATE TABLE annotation_record(nope TEXT);",
    )

    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        repo.get("anything")
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='annotation_bundle_create_journal'"
        ).fetchone()[0] == 0

    monkeypatch.setattr(annotation_db, "_SCHEMA_V3", schema_v3)
    assert repo.get("anything") is None
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == annotation_db.SCHEMA_VERSION
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='annotation_bundle_create_journal'"
        ).fetchone()[0] == 1


def test_pending_coordinated_creation_cannot_publish_capture_bytes(tmp_path):
    content = b"pending capture"
    capture = _capture_with_blob(content)
    record = _record().model_copy(update={"capture": capture})
    revision = _first_revision(record).model_copy(update={"capture": capture})
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    repo.stage_bundle_create(record, revision, operation_digest="a" * 64)

    with pytest.raises(KeyError, match="active profile"):
        repo.retain_capture_blob(
            "ar-1:capture",
            _required_blob(capture),
            content,
            created_at=NOW,
        )

    assert repo.get(record.annotation_id) is None
    assert not annotation_db.annotation_blob_root(tmp_path).exists()


def test_retain_and_delete_are_serialized_from_authorization_through_publish(
    tmp_path, monkeypatch
):
    content = b"race payload"
    capture = _capture_with_blob(content)
    record = _record().model_copy(update={"capture": capture})
    revision = _first_revision(record).model_copy(update={"capture": capture})
    retaining = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    deleting = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    retaining.create(record, revision)
    blob = _required_blob(capture)
    path = annotation_db.annotation_blob_root(tmp_path) / retaining._blob_relative_path(
        blob.sha256
    )
    authorized = Event()
    publish_allowed = Event()
    delete_attempted_write = Event()
    real_write = retaining._write_verified_blob
    real_write_txn = annotation_db.write_txn

    def pause_after_authorization(payload: bytes, sha256: str) -> None:
        authorized.set()
        assert publish_allowed.wait(timeout=5)
        real_write(payload, sha256)

    def delete_while_publish_is_paused():
        return deleting.delete_annotation(
            "ann-1", requested_at=NOW, delete_job_id="delete-race"
        )

    monkeypatch.setattr(retaining, "_write_verified_blob", pause_after_authorization)
    with ThreadPoolExecutor(max_workers=2) as pool:
        retain_future = pool.submit(
            retaining.retain_capture_blob,
            "ar-1:capture",
            blob,
            content,
            created_at=NOW,
        )
        assert authorized.wait(timeout=5)

        @contextmanager
        def observe_delete_writer(conn):
            delete_attempted_write.set()
            with real_write_txn(conn):
                yield conn

        monkeypatch.setattr(annotation_db, "write_txn", observe_delete_writer)
        delete_future = pool.submit(delete_while_publish_is_paused)
        assert delete_attempted_write.wait(timeout=5)
        assert not delete_future.done()
        publish_allowed.set()
        assert retain_future.result(timeout=5).available is True
        result = delete_future.result(timeout=5)

    assert result.state == "complete"
    assert result.blob_states == {blob.sha256: "deleted"}
    assert not path.exists()
    with sqlite3.connect(retaining.db_path) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM annotation_blob WHERE sha256=?", (blob.sha256,)
            ).fetchone()[0]
            == 0
        )


def test_delete_declared_never_retained_blob_is_missing_and_retryable(tmp_path):
    content = b"never retained"
    capture = _capture_with_blob(content)
    record = _record().model_copy(update={"capture": capture})
    revision = _first_revision(record).model_copy(update={"capture": capture})
    repo = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    repo.create(record, revision)
    blob = _required_blob(capture)

    for component in ("browser", "browser/annotations", "browser/annotations/v1"):
        assert stat.S_IMODE((tmp_path / component).stat().st_mode) == 0o700
    assert not annotation_db.annotation_blob_root(tmp_path).exists()

    deleted = repo.delete_annotation(
        "ann-1", requested_at=NOW, delete_job_id="delete-missing"
    )
    retried = repo.delete_annotation("ann-1", requested_at=NOW + timedelta(seconds=1))

    assert deleted.state == "complete"
    assert deleted.blob_states == {blob.sha256: "missing"}
    assert retried == deleted
    assert not annotation_db.annotation_blob_root(tmp_path).exists()


@pytest.mark.parametrize("mutation", ["reattach", "delete"])
def test_export_fences_reattach_and_delete_through_payload_snapshot(
    tmp_path, monkeypatch, mutation
):
    content = b"frozen export"
    capture = _capture_with_blob(content)
    record = _record().model_copy(update={"capture": capture})
    revision = _first_revision(record).model_copy(update={"capture": capture})
    exporting = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    mutating = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    exporting.create(record, revision)
    blob = _required_blob(capture)
    exporting.retain_capture_blob("ar-1:capture", blob, content, created_at=NOW)
    payload_read = Event()
    payload_allowed = Event()
    mutation_attempted_write = Event()
    real_read = exporting._read_verified_blob
    real_write_txn = annotation_db.write_txn

    def pause_payload_read(sha256: str, size_bytes: int):
        payload_read.set()
        assert payload_allowed.wait(timeout=5)
        return real_read(sha256, size_bytes)

    new_scope = _scope(generation="doc-2")
    new_capture = _capture(generation="doc-2")
    new_revision = AnchorRevision(
        anchor_revision_id="ar-2",
        annotation_id="ann-1",
        revision=2,
        anchor=_anchor(tag="a"),
        capture=new_capture,
        changed_by=_author(),
        changed_at=NOW + timedelta(seconds=1),
        reason="reattached",
        previous_anchor_digest=canonical_digest(record.anchor),
    )

    def mutate_while_export_is_paused():
        if mutation == "reattach":
            return mutating.reattach(
                "ann-1",
                new_revision,
                new_scope=new_scope,
                expected_record_revision=1,
            )
        return mutating.delete_annotation(
            "ann-1",
            requested_at=NOW + timedelta(seconds=1),
            delete_job_id="delete-export",
        )

    monkeypatch.setattr(exporting, "_read_verified_blob", pause_payload_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        export_future = pool.submit(
            exporting.export_annotation,
            "ann-1",
            include_screenshot_bytes=True,
        )
        assert payload_read.wait(timeout=5)

        @contextmanager
        def observe_mutation_writer(conn):
            mutation_attempted_write.set()
            with real_write_txn(conn):
                yield conn

        monkeypatch.setattr(annotation_db, "write_txn", observe_mutation_writer)
        mutation_future = pool.submit(mutate_while_export_is_paused)
        assert mutation_attempted_write.wait(timeout=5)
        assert not mutation_future.done()
        payload_allowed.set()
        exported = json.loads(export_future.result(timeout=5))
        mutation_future.result(timeout=5)

    assert exported["annotation"]["currentAnchorRevisionId"] == "ar-1"
    assert [item["anchorRevisionId"] for item in exported["anchorRevisions"]] == [
        "ar-1"
    ]
    assert exported["captureBlobs"][0]["payloadState"] == "included"
    if mutation == "reattach":
        changed = mutating.get("ann-1")
        assert changed is not None
        assert changed.current_anchor_revision_id == "ar-2"
    else:
        assert mutating.get("ann-1") is None


def test_non_finite_numbers_are_rejected_by_json_models_and_canonical_json():
    with pytest.raises(ValidationError, match="finite_number"):
        CssRect(x=float("inf"), y=0, width=1, height=1)
    with pytest.raises(ValueError, match="JSON compliant"):
        canonical_json({"value": float("nan")})
    with pytest.raises(ValueError, match="non-finite"):
        AnnotationRepository._parse_import_bundle('{"value":NaN}')


def test_import_rejects_noncanonical_base64_pad_bits(tmp_path):
    content = b"a"
    capture = _capture_with_blob(content)
    record = _record().model_copy(update={"capture": capture})
    revision = _first_revision(record).model_copy(update={"capture": capture})
    source = AnnotationRepository(profile_id="coding", profile_home=tmp_path / "source")
    source.create(record, revision)
    source.retain_capture_blob(
        "ar-1:capture", _required_blob(capture), content, created_at=NOW
    )
    manifest = json.loads(
        source.export_annotation("ann-1", include_screenshot_bytes=True)
    )
    assert manifest["captureBlobs"][0]["payloadBase64"] == "YQ=="
    manifest["captureBlobs"][0]["payloadBase64"] = "YR=="

    target = AnnotationRepository(profile_id="coding", profile_home=tmp_path / "target")
    with pytest.raises(ValueError, match="canonical base64"):
        target.import_annotation(manifest)
    assert not target.db_path.exists()


@pytest.mark.parametrize(
    ("first_reason", "second_reason", "valid"),
    [
        (first, second, first in {"created", "imported"} and second == "reattached")
        for first in ("created", "imported", "reattached")
        for second in ("created", "imported", "reattached")
    ],
)
def test_import_revision_reason_matrix(tmp_path, first_reason, second_reason, valid):
    source = AnnotationRepository(profile_id="coding", profile_home=tmp_path / "source")
    source.create(_record(), _first_revision())
    second = AnchorRevision(
        anchor_revision_id="ar-2",
        annotation_id="ann-1",
        revision=2,
        anchor=_anchor(tag="a"),
        capture=_capture(generation="doc-2"),
        changed_by=_author(),
        changed_at=NOW + timedelta(seconds=1),
        reason="reattached",
        previous_anchor_digest=canonical_digest(_anchor()),
    )
    source.reattach(
        "ann-1",
        second,
        new_scope=_scope(generation="doc-2"),
        expected_record_revision=1,
    )
    manifest = json.loads(source.export_annotation("ann-1"))
    manifest["anchorRevisions"][0]["reason"] = first_reason
    manifest["anchorRevisions"][1]["reason"] = second_reason
    target = AnnotationRepository(
        profile_id="coding",
        profile_home=tmp_path / f"target-{first_reason}-{second_reason}",
    )

    if valid:
        target.import_annotation(manifest)
        assert [item.reason for item in target.anchor_revisions("ann-1")] == [
            first_reason,
            second_reason,
        ]
    else:
        with pytest.raises(ValueError, match="revision|reason"):
            target.import_annotation(manifest)
        assert not target.db_path.exists()
