from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from hermes_cli.browser_annotation_bundle import AnnotationBundleCoordinator
from hermes_cli.browser_annotation_lineage import (
    AnnotationLineageRepository,
    ClaimedAnnotationTurn,
    ClaimedAnnotationTurnInput,
)
from hermes_cli.browser_annotation_rpc import (
    AnnotationRpcError,
    AnnotationRpcFacade,
    production_annotation_agent_factory,
)
from hermes_cli.browser_annotations_db import AnnotationRepository
from hermes_cli.browser_annotations_models import (
    AnchorRevision,
    AnnotationRecordV1,
    AnnotationScope,
    AuthorRef,
    CaptureEvidence,
    CssRect,
    ElementAnchor,
    ElementFingerprint,
    ThreadRef,
    ViewportEvidence,
    canonical_digest,
)
from hermes_state import SessionDB

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


class _RecordingRegistry:
    def __init__(self, repository):
        self.repository = repository
        self.notifications = []

    def notify(self, annotation_id, thread_generation):
        self.notifications.append((annotation_id, thread_generation))

    def cancel(self, turn_id):
        return self.repository.cancel_turn(turn_id)

    def retry(self, turn_id):
        return self.repository.retry_turn(turn_id)

    def signal_cancelled(self, turn_ids):
        self.cancelled = tuple(turn_ids)


def _capture(url: str = "https://example.test/page") -> CaptureEvidence:
    return CaptureEvidence(
        viewport=ViewportEvidence(
            css_width=1200,
            css_height=800,
            layout_scroll_x=0,
            layout_scroll_y=0,
            visual_offset_x=0,
            visual_offset_y=0,
            visual_scale=1,
            page_zoom_factor=1,
            device_scale_factor=2,
        ),
        guest_bounds=CssRect(x=0, y=0, width=1200, height=800),
        top_frame_id="root-frame",
        target_frame_id="root-frame",
        committed_url=url,
        captured_at=NOW,
        document_generation_id="doc-1",
        visible=True,
    )


def _repositories(tmp_path):
    SessionDB(db_path=tmp_path / "state.db").close()
    annotations = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    author = AuthorRef(
        kind="human",
        actor_id="actor",
        profile_id="coding",
        display_name="Kosta",
    )
    capture = _capture()
    anchor = ElementAnchor(
        frame_path=(), fingerprint=ElementFingerprint(tag="button", role="button")
    )
    record = AnnotationRecordV1(
        annotation_id="ann-1",
        kind="element",
        scope=AnnotationScope(
            profile_id="coding",
            browser_workspace_id="workspace",
            tab_id="tab",
            document_generation_id="doc-1",
            requested_url=capture.committed_url,
            committed_url=capture.committed_url,
            origin="https://example.test",
        ),
        anchor=anchor,
        capture=capture,
        author=author,
        thread=ThreadRef(session_lineage_id="annotation-root", branch_id="main"),
        created_at=NOW,
        updated_at=NOW,
        current_anchor_revision_id="revision-1",
        revision=1,
    )
    revision = AnchorRevision(
        anchor_revision_id="revision-1",
        annotation_id="ann-1",
        revision=1,
        anchor=anchor,
        capture=capture,
        changed_by=author,
        changed_at=NOW,
        reason="created",
    )
    annotations.create(record, revision)

    lineage = AnnotationLineageRepository(
        profile_id="coding", state_db_path=tmp_path / "state.db"
    )
    lineage.create_lineage(
        annotation_id="ann-1",
        annotation_lineage_root_id="annotation-root",
        model="frozen-model",
        model_config={"provider": "openrouter", "tools": []},
        system_prompt="frozen system bytes",
        cwd=str(tmp_path),
    )
    registry = _RecordingRegistry(lineage)
    facade = AnnotationRpcFacade(
        profile_id="coding",
        annotation_repository=annotations,
        lineage_repository=lineage,
        worker_registry=registry,
    )
    return annotations, lineage, registry, facade


def _submit(facade, request_id, intent="ask_agent"):
    return facade.submit_message(
        annotation_id="ann-1",
        thread_generation=1,
        body=f"body-{request_id}",
        intent=intent,
        anchor_revision_id="revision-1",
        reply_to_message_id=None,
        client_request_id=request_id,
        actor_id="principal",
        anchor_stale_at_submit=False,
    )


def test_comment_only_never_wakes_worker_or_creates_turn(tmp_path):
    _annotations, lineage, registry, facade = _repositories(tmp_path)

    result = _submit(facade, "comment", intent="comment_only")

    assert result["outcome"] == "comment_only"
    assert result["turnId"] is None
    assert registry.notifications == []
    with sqlite3.connect(lineage.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM annotation_turn_route").fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM messages WHERE content='body-comment'"
        ).fetchone()[0] == 1


def test_ask_agent_is_idempotently_acked_then_wakes_only_dedicated_registry(tmp_path):
    _annotations, lineage, registry, facade = _repositories(tmp_path)

    first = _submit(facade, "ask")
    duplicate = _submit(facade, "ask")

    assert first["outcome"] == "queued"
    assert duplicate["outcome"] == "duplicate_request"
    assert duplicate["messageId"] == first["messageId"]
    assert duplicate["turnId"] == first["turnId"]
    assert registry.notifications == [("ann-1", 1), ("ann-1", 1)]
    with sqlite3.connect(lineage.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM annotation_turn_route").fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM messages WHERE content='body-ask'"
        ).fetchone()[0] == 1


def test_same_request_id_with_changed_intent_fails_closed(tmp_path):
    _annotations, _lineage, registry, facade = _repositories(tmp_path)
    _submit(facade, "same", intent="comment_only")

    with pytest.raises(AnnotationRpcError) as exc_info:
        _submit(facade, "same", intent="ask_agent")

    assert exc_info.value.code == "duplicate_request"
    assert registry.notifications == []


def test_submission_uses_exact_immutable_revision_evidence(tmp_path):
    annotations, lineage, _registry, facade = _repositories(tmp_path)

    result = _submit(facade, "evidence")
    evidence = annotations.immutable_evidence("ann-1", "revision-1")

    assert evidence == {
        "annotationKind": "element",
        "documentUrl": "https://example.test/page",
        "documentTitle": None,
        "selectedText": None,
        "captureDigest": canonical_digest(_capture()),
    }
    with sqlite3.connect(lineage.db_path) as conn:
        row = conn.execute(
            "SELECT capture_digest,context_digest FROM annotation_message_context "
            "WHERE message_id=?",
            (result["messageId"],),
        ).fetchone()
    assert row[0] == evidence["captureDigest"]
    assert len(row[1]) == 64
    with pytest.raises(AnnotationRpcError) as exc_info:
        facade.submit_message(
            annotation_id="ann-1",
            thread_generation=1,
            body="wrong revision",
            intent="ask_agent",
            anchor_revision_id="missing-revision",
            reply_to_message_id=None,
            client_request_id="missing-revision",
            actor_id="principal",
            anchor_stale_at_submit=False,
        )
    assert exc_info.value.code == "revision_conflict"


def test_thread_projection_groups_completed_pair_before_concurrent_comment(tmp_path):
    _annotations, lineage, _registry, facade = _repositories(tmp_path)
    ask = _submit(facade, "first")
    comment = _submit(facade, "between", intent="comment_only")
    claim = lineage.claim_next_turn(
        annotation_id="ann-1",
        thread_generation=1,
        lease_owner="test-worker",
        lease_seconds=30,
    )
    assert claim is not None
    lineage.mark_dispatched(
        claim.turn_id, lease_owner="test-worker", attempt=claim.attempt
    )
    assistant_id = lineage.complete_turn(
        turn_id=claim.turn_id,
        lease_owner="test-worker",
        attempt=claim.attempt,
        assistant_body="answer-first",
        context_digest="a" * 64,
        actor_id="hermes:browser-annotation",
    )

    projection = facade.thread(annotation_id="ann-1", thread_generation=1)

    assert projection["orderedMessageIds"] == [
        ask["messageId"],
        assistant_id,
        comment["messageId"],
    ]
    assert [item["role"] for item in projection["messages"]] == [
        "user",
        "assistant",
        "user",
    ]
    assert projection["annotationLineageRootId"] == "annotation-root"
    assert projection["sourceSessionLineageId"] is None
    assert projection["sourceAvailable"] is False


def test_cancel_and_retry_are_exact_annotation_turn_scoped(tmp_path):
    _annotations, lineage, _registry, facade = _repositories(tmp_path)
    accepted = _submit(facade, "cancel")

    cancelled = facade.cancel(
        annotation_id="ann-1",
        thread_generation=1,
        turn_id=accepted["turnId"],
    )
    assert cancelled["outcome"] == "turn_cancelled"
    assert lineage.turn(accepted["turnId"])["status"] == "cancelled"

    with pytest.raises(AnnotationRpcError) as exc_info:
        facade.turn(
            annotation_id="other-annotation",
            thread_generation=1,
            turn_id=accepted["turnId"],
        )
    assert exc_info.value.code == "turn_failed"


def test_production_agent_factory_uses_frozen_runtime_without_fallback_or_db(
    monkeypatch, tmp_path
):
    import hermes_cli.runtime_provider as runtime_provider
    import run_agent

    resolved = []

    def fake_resolve(**kwargs):
        resolved.append(kwargs)
        return {
            "provider": "openrouter",
            "base_url": "https://provider.test/v1",
            "api_key": "secret",
            "api_mode": "chat_completions",
        }

    created = []

    class DummyAgent:
        def __init__(self, **kwargs):
            created.append(kwargs)
            self._cached_system_prompt = None
            self.session_cwd = None

    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", fake_resolve)
    monkeypatch.setattr(run_agent, "AIAgent", DummyAgent)
    monkeypatch.setattr(
        "tools.terminal_tool.register_task_env_overrides", lambda *_args, **_kwargs: None
    )
    claimed = ClaimedAnnotationTurnInput(
        turn=ClaimedAnnotationTurn(
            turn_id="turn-1",
            annotation_id="ann-1",
            thread_generation=1,
            turn_sequence=1,
            trigger_message_id=1,
            attempt=0,
            lease_owner="worker",
            lease_expires_at=100,
        ),
        annotation_lineage_root_id="annotation-root",
        source_session_lineage_id=None,
        source_message_id=None,
        system_prompt="exact frozen prompt",
        model="frozen-model",
        model_config={
            "provider": "openrouter",
            "base_url": "https://provider.test/v1",
            "api_mode": "chat_completions",
            "reasoning_config": {"effort": "high"},
            "enabled_toolsets": ["browser"],
            "tools": [],
        },
        cwd=str(tmp_path),
        body="body",
        anchor_revision_id="revision-1",
        capture_digest="b" * 64,
        reply_to_message_id=None,
        context_digest="c" * 64,
        anchor_stale_at_submit=False,
        completed_history=(),
    )

    agent = production_annotation_agent_factory(tmp_path)(claimed)

    assert resolved == [
        {
            "requested": "openrouter",
            "target_model": "frozen-model",
            "explicit_base_url": "https://provider.test/v1",
        }
    ]
    kwargs = created[0]
    assert kwargs["model"] == "frozen-model"
    assert kwargs["enabled_toolsets"] == ["browser"]
    assert kwargs["session_db"] is None
    assert kwargs["fallback_model"] is None
    assert kwargs["skip_context_files"] is True
    assert kwargs["skip_memory"] is True
    assert agent._cached_system_prompt == "exact frozen prompt"
    assert agent.session_cwd == str(tmp_path)


def _coordinated_bundle(tmp_path, *, source_id=None):
    SessionDB(db_path=tmp_path / "state.db").close()
    if source_id:
        with sqlite3.connect(tmp_path / "state.db") as conn:
            conn.execute(
                """INSERT INTO sessions(id,source,model,model_config,system_prompt,
                       parent_session_id,started_at,last_active,archived)
                   VALUES(?,?,?,?,?,NULL,?,?,0)""",
                (
                    source_id,
                    "desktop",
                    "source-model",
                    "{}",
                    "source prompt",
                    NOW.timestamp(),
                    NOW.timestamp(),
                ),
            )
            source_message_id = conn.execute(
                """INSERT INTO messages(session_id,role,content,timestamp,observed,active)
                   VALUES(?,?,?, ?,0,1)""",
                (source_id, "user", "source conversation secret", NOW.timestamp()),
            ).lastrowid
    else:
        source_message_id = None
    annotations = AnnotationRepository(profile_id="coding", profile_home=tmp_path)
    lineage = AnnotationLineageRepository(
        profile_id="coding", state_db_path=tmp_path / "state.db"
    )
    registry = _RecordingRegistry(lineage)
    snapshot = {
        "model": "frozen-model",
        "model_config": {"provider": "openrouter", "tools": []},
        "system_prompt": "frozen system bytes",
        "cwd": str(tmp_path),
    }
    coordinator = AnnotationBundleCoordinator(
        profile_id="coding",
        annotations=annotations,
        lineage=lineage,
        registry=registry,  # type: ignore[arg-type]
        snapshot_factory=lambda _source, _message: snapshot,
    )
    capture = _capture()
    anchor = ElementAnchor(
        frame_path=(), fingerprint=ElementFingerprint(tag="button", role="button")
    )
    author = AuthorRef(
        kind="human",
        actor_id="actor",
        profile_id="coding",
        display_name="Kosta",
    )
    record = AnnotationRecordV1(
        annotation_id="ann-bundle",
        kind="element",
        scope=AnnotationScope(
            profile_id="coding",
            browser_workspace_id="workspace",
            tab_id="tab",
            document_generation_id="doc-1",
            session_lineage_id=source_id,
            requested_url=capture.committed_url,
            committed_url=capture.committed_url,
            origin="https://example.test",
        ),
        anchor=anchor,
        capture=capture,
        author=author,
        thread=None,
        created_at=NOW,
        updated_at=NOW,
        current_anchor_revision_id="revision-bundle",
        revision=1,
    )
    revision = AnchorRevision(
        anchor_revision_id="revision-bundle",
        annotation_id=record.annotation_id,
        revision=1,
        anchor=anchor,
        capture=capture,
        changed_by=author,
        changed_at=NOW,
        reason="created",
    )
    return annotations, lineage, registry, coordinator, record, revision, source_message_id


def test_coordinated_creation_never_exposes_metadata_without_lineage(tmp_path, monkeypatch):
    annotations, lineage, _registry, coordinator, record, revision, _source = (
        _coordinated_bundle(tmp_path)
    )

    def fail_create(**_kwargs):
        raise RuntimeError("injected lineage failure")

    monkeypatch.setattr(lineage, "create_lineage", fail_create)
    with pytest.raises(RuntimeError, match="injected lineage"):
        coordinator.create(record, revision)

    assert annotations.get(record.annotation_id) is None
    assert annotations.pending_bundle_creations() == ()
    with sqlite3.connect(annotations.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM annotation_record").fetchone()[0] == 0
    with sqlite3.connect(lineage.db_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM sessions WHERE source='browser_annotation'"
        ).fetchone()[0] == 0


def test_creation_recovery_finalizes_both_committed_or_rolls_back_metadata_only(tmp_path):
    annotations, lineage, _registry, coordinator, record, revision, _source = (
        _coordinated_bundle(tmp_path)
    )
    root = "annotation-lineage:crash-window"
    staged = record.model_copy(
        update={"thread": ThreadRef(session_lineage_id=root, branch_id="main")}
    )
    annotations.stage_bundle_create(staged, revision, operation_digest="a" * 64)
    assert annotations.get(record.annotation_id) is None

    recovered = coordinator.recover()
    assert recovered["creates_rolled_back"] == 1
    assert annotations.get(record.annotation_id) is None

    annotations.stage_bundle_create(staged, revision, operation_digest="a" * 64)
    lineage.create_lineage(
        annotation_id=record.annotation_id,
        annotation_lineage_root_id=root,
        model="frozen-model",
        model_config={"provider": "openrouter", "tools": []},
        system_prompt="frozen system bytes",
    )
    recovered = coordinator.recover()
    assert recovered["creates_finalized"] == 1
    assert annotations.get(record.annotation_id) == staged
    assert lineage.lineage_matches(
        annotation_id=record.annotation_id,
        thread_generation=1,
        annotation_lineage_root_id=root,
    )


def test_frozen_bundle_export_contains_bodies_once_and_never_source_transcript(tmp_path):
    annotations, lineage, registry, coordinator, record, revision, source_message_id = (
        _coordinated_bundle(tmp_path, source_id="source-root")
    )
    created = coordinator.create(
        record, revision, source_message_id=source_message_id
    )
    facade = AnnotationRpcFacade(
        profile_id="coding",
        annotation_repository=annotations,
        lineage_repository=lineage,
        worker_registry=registry,  # type: ignore[arg-type]
        bundle_coordinator=coordinator,
    )
    facade.submit_message(
        annotation_id=record.annotation_id,
        thread_generation=1,
        body="comment body unique",
        intent="comment_only",
        anchor_revision_id=revision.anchor_revision_id,
        reply_to_message_id=None,
        client_request_id="comment-export",
        actor_id="principal",
        anchor_stale_at_submit=False,
    )
    facade.submit_message(
        annotation_id=record.annotation_id,
        thread_generation=1,
        body="ask body unique",
        intent="ask_agent",
        anchor_revision_id=revision.anchor_revision_id,
        reply_to_message_id=None,
        client_request_id="ask-export",
        actor_id="principal",
        anchor_stale_at_submit=False,
    )

    first = coordinator.export(record.annotation_id)
    second = coordinator.export(record.annotation_id)
    bundle = json.loads(first)

    assert first == second
    assert bundle["includesThreadBodies"] is True
    assert bundle["includesSourceConversationBodies"] is False
    assert bundle["annotationMetadata"]["includesThreadBodies"] is False
    assert bundle["threadGenerations"][0]["route"]["annotationLineageRootId"] == (
        created.thread.session_lineage_id
    )
    assert first.count("comment body unique") == 1
    assert first.count("ask body unique") == 1
    assert "source conversation secret" not in first
    assert all(
        "content" not in context
        for context in bundle["threadGenerations"][0]["contexts"]
    )
    with sqlite3.connect(annotations.db_path) as annotation_conn:
        assert annotation_conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with sqlite3.connect(lineage.db_path) as lineage_conn:
        assert lineage_conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_resumable_bundle_delete_removes_thread_bodies_but_not_source(tmp_path):
    annotations, lineage, registry, coordinator, record, revision, source_message_id = (
        _coordinated_bundle(tmp_path, source_id="source-root")
    )
    coordinator.create(record, revision, source_message_id=source_message_id)
    accepted = lineage.submit_human_message(
        annotation_id=record.annotation_id,
        thread_generation=1,
        body="delete this annotation body",
        intent="ask_agent",
        anchor_revision_id=revision.anchor_revision_id,
        capture_digest=canonical_digest(record.capture),
        reply_to_message_id=None,
        context_digest="c" * 64,
        client_request_id="delete-request",
        actor_id="principal",
        turn_id="turn-delete",
    )
    assert accepted.turn_id is not None

    result = coordinator.delete(record.annotation_id)

    assert result["outcome"] == "deleted"
    assert result["threadDeleted"] is True
    assert annotations.get(record.annotation_id) is None
    assert lineage.turn(accepted.turn_id) is None
    with sqlite3.connect(lineage.db_path) as conn:
        assert conn.execute(
            "SELECT content FROM messages WHERE session_id='source-root'"
        ).fetchone()[0] == "source conversation secret"
        assert conn.execute(
            "SELECT count(*) FROM messages WHERE content='delete this annotation body'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM sessions WHERE source='browser_annotation'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM annotation_thread_route WHERE annotation_id=?",
            (record.annotation_id,),
        ).fetchone()[0] == 0
    assert registry.cancelled == ("turn-delete",)
    retry = coordinator.delete(record.annotation_id)
    assert retry["outcome"] == "deleted"


def test_bundle_delete_surfaces_partial_and_retry_finishes_without_reopening(tmp_path, monkeypatch):
    annotations, lineage, _registry, coordinator, record, revision, _source = (
        _coordinated_bundle(tmp_path)
    )
    coordinator.create(record, revision)
    cleanup = annotations._cleanup_delete_job
    def fail_cleanup(*_args, **_kwargs):
        raise OSError("injected cleanup failure")
    monkeypatch.setattr(annotations, "_cleanup_delete_job", fail_cleanup)
    partial = coordinator.delete(record.annotation_id)
    assert partial["outcome"] == "delete_partial"
    assert partial["threadDeleted"] is False
    assert annotations.get(record.annotation_id) is None
    with sqlite3.connect(lineage.db_path) as conn:
        assert conn.execute(
            "SELECT state FROM annotation_thread_route WHERE annotation_id=?",
            (record.annotation_id,),
        ).fetchone()[0] == "deleting"
    monkeypatch.setattr(annotations, "_cleanup_delete_job", cleanup)
    completed = coordinator.delete(record.annotation_id)
    assert completed["outcome"] == "deleted"
    with sqlite3.connect(lineage.db_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM annotation_thread_route WHERE annotation_id=?",
            (record.annotation_id,),
        ).fetchone()[0] == 0
