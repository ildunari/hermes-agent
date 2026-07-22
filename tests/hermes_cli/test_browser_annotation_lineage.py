from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli.browser_annotation_lineage import AnnotationLineageRepository
from hermes_state import SessionDB


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _state_db(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.close()
    return path


def _repo(tmp_path, *, profile="coding"):
    path = _state_db(tmp_path)
    return AnnotationLineageRepository(profile_id=profile, state_db_path=path)


def _create(repo, *, source=None):
    repo.create_lineage(
        annotation_id="ann-1",
        annotation_lineage_root_id="annotation-root",
        source_session_lineage_id=source,
        model="test-model",
        model_config={"provider": "test", "_delegate_from": "must-strip"},
        system_prompt="stable annotation prompt",
        cwd="/tmp/workspace",
        created_at=100.0,
    )


def _submit(
    repo,
    request,
    *,
    intent="ask_agent",
    body=None,
    turn=None,
    stale=False,
    created=101.0,
):
    return repo.submit_human_message(
        annotation_id="ann-1",
        thread_generation=1,
        body=body or f"body-{request}",
        intent=intent,
        anchor_revision_id="revision-1",
        capture_digest=_digest("capture"),
        reply_to_message_id=None,
        context_digest=_digest(f"context-{request}"),
        client_request_id=request,
        actor_id="actor-1",
        turn_id=turn or (f"turn-{request}" if intent == "ask_agent" else None),
        anchor_stale_at_submit=stale,
        created_at=created,
    )


def test_idempotency_digest_binds_message_intent(tmp_path):
    repo = _repo(tmp_path)
    _create(repo)
    _submit(repo, "same-request", intent="comment_only")

    with pytest.raises(RuntimeError, match="idempotency collision"):
        _submit(repo, "same-request", intent="ask_agent")


def test_private_workspace_refuses_before_opening_state_db(tmp_path):
    path = tmp_path / "state.db"
    repo = AnnotationLineageRepository(
        profile_id="coding", state_db_path=path, private_workspace=True
    )

    with pytest.raises(PermissionError, match="private"):
        repo.create_lineage(
            annotation_id="ann-1", annotation_lineage_root_id="annotation-root"
        )

    assert not path.exists()


def test_schema_is_profile_bound_body_free_and_root_is_hidden_reserved_session(tmp_path):
    repo = _repo(tmp_path)
    _create(repo)

    with sqlite3.connect(repo.db_path) as conn:
        conn.row_factory = sqlite3.Row
        identity = conn.execute(
            "SELECT schema_version,profile_id FROM annotation_lineage_schema"
        ).fetchone()
        assert tuple(identity) == (3, "coding")
        session = conn.execute(
            "SELECT source,parent_session_id,archived,model_config FROM sessions WHERE id='annotation-root'"
        ).fetchone()
        assert session["source"] == "browser_annotation"
        assert session["parent_session_id"] is None
        assert session["archived"] == 1
        assert '"_session_kind":"browser_annotation"' in session["model_config"]
        assert "_delegate_from" not in session["model_config"]
        for table in (
            "annotation_thread_route",
            "annotation_message_context",
            "annotation_turn_route",
        ):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert not columns & {
                "body",
                "content",
                "prompt",
                "response",
                "reasoning",
                "tool_output",
                "screenshot_bytes",
            }

    foreign = AnnotationLineageRepository(
        profile_id="guest", state_db_path=repo.db_path
    )
    with pytest.raises(PermissionError, match="another profile"):
        foreign.turn("anything")


def test_comment_and_ask_are_atomic_idempotent_and_body_exists_once(tmp_path):
    repo = _repo(tmp_path)
    _create(repo)

    comment = _submit(repo, "comment", intent="comment_only")
    asked = _submit(repo, "ask")
    duplicate = _submit(repo, "ask")

    assert comment.turn_id is None
    assert asked.duplicate is False
    assert duplicate == asked.__class__(
        message_id=asked.message_id,
        thread_sequence=asked.thread_sequence,
        turn_id=asked.turn_id,
        turn_sequence=asked.turn_sequence,
        duplicate=True,
    )
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM messages WHERE content='body-ask'"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM annotation_turn_route").fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM annotation_turn_route WHERE trigger_message_id=?",
            (comment.message_id,),
        ).fetchone()[0] == 0
        canary = "body-ask"
        for table in (
            "annotation_thread_route",
            "annotation_message_context",
            "annotation_turn_route",
        ):
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            assert all(canary not in repr(tuple(row)) for row in rows)

    with pytest.raises(RuntimeError, match="idempotency collision"):
        _submit(repo, "ask", body="changed body")


def test_lost_ack_retry_returns_original_after_route_becomes_read_only(tmp_path):
    repo = _repo(tmp_path)
    _create(repo)
    accepted = _submit(repo, "accepted")
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute("UPDATE annotation_thread_route SET state='read_only'")

    duplicate = _submit(repo, "accepted")
    assert duplicate.message_id == accepted.message_id
    assert duplicate.duplicate is True
    with pytest.raises(RuntimeError, match="read_only"):
        _submit(repo, "new-after-read-only")

    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_turn_insert_failure_rolls_back_message_and_context(tmp_path):
    repo = _repo(tmp_path)
    _create(repo)
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute(
            """CREATE TRIGGER reject_annotation_turn BEFORE INSERT ON annotation_turn_route
               BEGIN SELECT RAISE(ABORT, 'injected turn failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        _submit(repo, "rollback")

    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM annotation_message_context").fetchone()[0] == 0
        assert conn.execute(
            "SELECT next_thread_sequence FROM annotation_thread_route"
        ).fetchone()[0] == 1


def test_concurrent_idempotency_and_fifo_claims(tmp_path):
    repo = _repo(tmp_path)
    _create(repo)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: _submit(repo, "same"), range(4)))
    assert len({item.message_id for item in results}) == 1
    assert sum(not item.duplicate for item in results) == 1

    _submit(repo, "second", created=102.0)
    first = repo.claim_next_turn(
        annotation_id="ann-1",
        thread_generation=1,
        lease_owner="worker-1",
        lease_seconds=30,
        now=110.0,
    )
    assert first is not None
    assert first.turn_sequence == 1
    assert repo.claim_next_turn(
        annotation_id="ann-1",
        thread_generation=1,
        lease_owner="worker-2",
        lease_seconds=30,
        now=110.0,
    ) is None


def test_pre_dispatch_expiry_requeues_but_dispatched_expiry_never_replays(tmp_path):
    repo = _repo(tmp_path)
    _create(repo)
    _submit(repo, "first")
    claimed = repo.claim_next_turn(
        annotation_id="ann-1",
        thread_generation=1,
        lease_owner="worker",
        lease_seconds=10,
        now=200.0,
    )
    assert claimed is not None
    assert repo.recover_expired_turns(now=211.0) == {
        "requeued": 1,
        "outcome_unknown": 0,
    }
    reclaimed = repo.claim_next_turn(
        annotation_id="ann-1",
        thread_generation=1,
        lease_owner="worker-2",
        lease_seconds=10,
        now=212.0,
    )
    assert reclaimed is not None
    assert reclaimed.trigger_message_id == claimed.trigger_message_id
    assert reclaimed.attempt == 1
    repo.mark_dispatched(
        reclaimed.turn_id, lease_owner="worker-2", attempt=reclaimed.attempt, now=213.0
    )

    assert repo.recover_expired_turns(now=223.0) == {
        "requeued": 0,
        "outcome_unknown": 1,
    }
    turn = repo.turn(reclaimed.turn_id)
    assert turn is not None
    assert turn["status"] == "failed"
    assert turn["dispatch_state"] == "outcome_unknown"
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute(
            """SELECT model_participation FROM annotation_message_context
               WHERE message_id=?""",
            (reclaimed.trigger_message_id,),
        ).fetchone()[0] == "failed"
    assert repo.claim_next_turn(
        annotation_id="ann-1",
        thread_generation=1,
        lease_owner="worker-3",
        lease_seconds=10,
        now=224.0,
    ) is None


def test_completion_is_atomic_retryable_and_history_excludes_comments_and_queued(tmp_path):
    repo = _repo(tmp_path)
    _create(repo)
    _submit(repo, "comment", intent="comment_only")
    asked = _submit(repo, "ask")
    _submit(repo, "future")
    claimed = repo.claim_next_turn(
        annotation_id="ann-1",
        thread_generation=1,
        lease_owner="worker",
        lease_seconds=30,
        now=300.0,
    )
    assert claimed is not None and claimed.turn_id == asked.turn_id
    repo.mark_dispatched(
        claimed.turn_id, lease_owner="worker", attempt=claimed.attempt, now=300.0
    )
    assistant_id = repo.complete_turn(
        turn_id=claimed.turn_id,
        lease_owner="worker",
        attempt=claimed.attempt,
        assistant_body="assistant answer",
        context_digest=_digest("assistant-context"),
        actor_id="hermes",
        completed_at=301.0,
    )
    assert repo.complete_turn(
        turn_id=claimed.turn_id,
        lease_owner="ignored-after-completion",
        attempt=claimed.attempt,
        assistant_body="must not duplicate",
        context_digest=_digest("assistant-context"),
        actor_id="hermes",
        completed_at=302.0,
    ) == assistant_id

    assert repo.provider_history("ann-1", 1) == [
        {"role": "user", "content": "body-ask", "message_id": asked.message_id},
        {"role": "assistant", "content": "assistant answer", "message_id": assistant_id},
    ]
    with sqlite3.connect(repo.db_path) as conn:
        roles = conn.execute(
            "SELECT role FROM messages WHERE id IN (?,?) ORDER BY id",
            (asked.message_id, assistant_id),
        ).fetchall()
        assert roles == [("user",), ("assistant",)]
        assert conn.execute(
            "SELECT count(*) FROM messages WHERE content='assistant answer'"
        ).fetchone()[0] == 1


def test_source_deletion_does_not_break_dedicated_lineage_or_fallback(tmp_path):
    path = _state_db(tmp_path)
    db = SessionDB(db_path=path)
    db.create_session("source-root", source="tui")
    db.close()
    repo = AnnotationLineageRepository(profile_id="coding", state_db_path=path)
    _create(repo, source="source-root")

    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DELETE FROM sessions WHERE id='source-root'")

    accepted = _submit(repo, "after-source-delete")
    with sqlite3.connect(path) as conn:
        stored_session = conn.execute(
            "SELECT session_id FROM messages WHERE id=?", (accepted.message_id,)
        ).fetchone()[0]
    assert stored_session == "annotation-root"


def test_reply_target_must_be_same_annotation_generation(tmp_path):
    repo = _repo(tmp_path)
    _create(repo)
    first = _submit(repo, "comment", intent="comment_only")

    with pytest.raises(ValueError, match="reply target"):
        repo.submit_human_message(
            annotation_id="ann-1",
            thread_generation=1,
            body="bad reply",
            intent="comment_only",
            anchor_revision_id="revision-1",
            capture_digest=None,
            reply_to_message_id=first.message_id + 1000,
            context_digest=_digest("bad-reply"),
            client_request_id="bad-reply",
            actor_id="actor-1",
            created_at=500.0,
        )

    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_worker_input_lease_failure_and_fifo_recovery_primitives(tmp_path):
    repo = _repo(tmp_path)
    _create(repo)
    first = _submit(repo, "first", stale=True)
    second = _submit(repo, "second", created=102.0)

    assert repo.queued_lineages() == [("ann-1", 1)]
    claimed = repo.claim_next_turn(
        annotation_id="ann-1",
        thread_generation=1,
        lease_owner="worker-1",
        lease_seconds=10,
        now=200.0,
    )
    assert claimed is not None and claimed.turn_id == first.turn_id
    turn_input = repo.claimed_turn_input(
        claimed.turn_id, lease_owner="worker-1", attempt=claimed.attempt, now=200.0
    )
    assert turn_input.system_prompt == "stable annotation prompt"
    assert turn_input.model == "test-model"
    assert turn_input.body == "body-first"
    assert turn_input.anchor_stale_at_submit is True
    assert turn_input.completed_history == ()
    assert repo.renew_turn_lease(
        claimed.turn_id,
        lease_owner="worker-1",
        attempt=claimed.attempt,
        lease_seconds=10,
        now=205.0,
    ) == 215.0

    repo.mark_dispatched(
        claimed.turn_id, lease_owner="worker-1", attempt=claimed.attempt, now=205.0
    )
    repo.complete_turn(
        turn_id=claimed.turn_id,
        lease_owner="worker-1",
        attempt=claimed.attempt,
        assistant_body="answer-first",
        context_digest=_digest("answer-first-context"),
        actor_id="hermes",
        completed_at=206.0,
    )
    next_claim = repo.claim_next_turn(
        annotation_id="ann-1",
        thread_generation=1,
        lease_owner="worker-2",
        lease_seconds=10,
        now=207.0,
    )
    assert next_claim is not None and next_claim.turn_id == second.turn_id
    next_input = repo.claimed_turn_input(
        next_claim.turn_id, lease_owner="worker-2", attempt=next_claim.attempt, now=207.0
    )
    assert [entry["user_content"] for entry in next_input.completed_history] == ["body-first"]
    assert next_input.completed_history[0]["anchor_stale_at_submit"] is True

    with pytest.raises(ValueError, match="stable redacted"):
        repo.fail_turn(
            next_claim.turn_id,
            lease_owner="worker-2",
            attempt=next_claim.attempt,
            error_code="secret provider exception text",
            failed_at=208.0,
        )
    repo.fail_turn(
        next_claim.turn_id,
        lease_owner="worker-2",
        attempt=next_claim.attempt,
        error_code="provider_unavailable",
        failed_at=208.0,
    )
    assert repo.turn(next_claim.turn_id)["status"] == "failed"
    with sqlite3.connect(repo.db_path) as conn:
        participation = conn.execute(
            "SELECT model_participation FROM annotation_message_context WHERE message_id=?",
            (second.message_id,),
        ).fetchone()[0]
    assert participation == "failed"
    assert repo.queued_lineages() == []


def test_reclaimed_same_owner_rejects_stale_attempt_operations(tmp_path):
    repo = _repo(tmp_path)
    _create(repo)
    _submit(repo, "attempt-fence")
    first = repo.claim_next_turn(
        annotation_id="ann-1",
        thread_generation=1,
        lease_owner="stable-worker-id",
        lease_seconds=10,
        now=100.0,
    )
    assert first is not None
    assert repo.recover_expired_turns(now=111.0)["requeued"] == 1
    second = repo.claim_next_turn(
        annotation_id="ann-1",
        thread_generation=1,
        lease_owner="stable-worker-id",
        lease_seconds=10,
        now=112.0,
    )
    assert second is not None and second.attempt == first.attempt + 1

    with pytest.raises(RuntimeError, match="not held"):
        repo.fail_turn(
            first.turn_id,
            lease_owner="stable-worker-id",
            attempt=first.attempt,
            error_code="agent_error",
            failed_at=113.0,
        )
    assert repo.turn(second.turn_id)["status"] == "running"
    repo.mark_dispatched(
        second.turn_id, lease_owner="stable-worker-id", attempt=second.attempt, now=113.0
    )
    assistant_id = repo.complete_turn(
        turn_id=second.turn_id,
        lease_owner="stable-worker-id",
        attempt=second.attempt,
        assistant_body="second-attempt answer",
        context_digest=_digest("second-attempt context"),
        actor_id="hermes",
        completed_at=114.0,
    )
    assert assistant_id > 0
    with pytest.raises(RuntimeError, match="another attempt"):
        repo.complete_turn(
            turn_id=first.turn_id,
            lease_owner="stable-worker-id",
            attempt=first.attempt,
            assistant_body="stale answer",
            context_digest=_digest("stale context"),
            actor_id="hermes",
            completed_at=115.0,
        )


def test_expired_lease_cannot_project_renew_dispatch_fail_or_complete(tmp_path):
    repo = _repo(tmp_path)
    _create(repo)
    _submit(repo, "expired-authority")
    claimed = repo.claim_next_turn(
        annotation_id="ann-1",
        thread_generation=1,
        lease_owner="worker",
        lease_seconds=1,
        now=10.0,
    )
    assert claimed is not None
    with pytest.raises(RuntimeError, match="not held"):
        repo.claimed_turn_input(
            claimed.turn_id, lease_owner="worker", attempt=claimed.attempt, now=12.0
        )
    with pytest.raises(RuntimeError, match="not held"):
        repo.renew_turn_lease(
            claimed.turn_id,
            lease_owner="worker",
            attempt=claimed.attempt,
            lease_seconds=10,
            now=12.0,
        )
    with pytest.raises(RuntimeError, match="pre-dispatch"):
        repo.mark_dispatched(
            claimed.turn_id, lease_owner="worker", attempt=claimed.attempt, now=12.0
        )
    with pytest.raises(RuntimeError, match="not held"):
        repo.fail_turn(
            claimed.turn_id,
            lease_owner="worker",
            attempt=claimed.attempt,
            error_code="agent_error",
            failed_at=12.0,
        )


def test_worker_projection_and_completion_refuse_non_active_route(tmp_path):
    repo = _repo(tmp_path)
    _create(repo)
    _submit(repo, "delete-race")
    claimed = repo.claim_next_turn(
        annotation_id="ann-1",
        thread_generation=1,
        lease_owner="worker",
        lease_seconds=30,
        now=100.0,
    )
    assert claimed is not None
    assert repo.claimed_turn_input(
        claimed.turn_id, lease_owner="worker", attempt=claimed.attempt, now=100.0
    ).body == "body-delete-race"
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute("UPDATE annotation_thread_route SET state='deleting'")

    with pytest.raises(RuntimeError, match="not held"):
        repo.claimed_turn_input(
            claimed.turn_id, lease_owner="worker", attempt=claimed.attempt, now=100.0
        )
    with pytest.raises(RuntimeError, match="pre-dispatch"):
        repo.mark_dispatched(
            claimed.turn_id, lease_owner="worker", attempt=claimed.attempt, now=100.0
        )
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute("UPDATE annotation_thread_route SET state='active'")
    repo.mark_dispatched(
        claimed.turn_id, lease_owner="worker", attempt=claimed.attempt, now=100.0
    )
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute("UPDATE annotation_thread_route SET state='deleting'")
    with pytest.raises(RuntimeError, match="deleting"):
        repo.complete_turn(
            turn_id=claimed.turn_id,
            lease_owner="worker",
            attempt=claimed.attempt,
            assistant_body="must not persist",
            context_digest=_digest("must-not-persist"),
            actor_id="hermes",
            completed_at=101.0,
        )
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM messages WHERE content='must not persist'"
        ).fetchone()[0] == 0


def test_v1_schema_migrates_stale_at_submit_without_body_copy(tmp_path):
    repo = _repo(tmp_path)
    _create(repo)
    accepted = _submit(repo, "legacy-retry")
    legacy_digest = hashlib.sha256(
        json.dumps(
            {
                "body": "body-legacy-retry",
                "contextDigest": _digest("context-legacy-retry"),
                "replyToMessageId": None,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute("UPDATE annotation_lineage_schema SET schema_version=1")
        conn.execute(
            "UPDATE annotation_message_context SET idempotency_digest=?",
            (legacy_digest,),
        )
        conn.execute("ALTER TABLE annotation_message_context DROP COLUMN anchor_stale_at_submit")

    assert repo.turn("missing") is None
    duplicate = _submit(repo, "legacy-retry")
    assert duplicate.message_id == accepted.message_id
    assert duplicate.duplicate is True
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute(
            "SELECT schema_version FROM annotation_lineage_schema"
        ).fetchone()[0] == 3
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(annotation_message_context)")
        }
    assert "anchor_stale_at_submit" in columns
