from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.browser_annotation_lineage import AnnotationLineageRepository
from hermes_cli.browser_annotation_worker import AnnotationTurnWorker, project_provider_input
from hermes_state import SessionDB

_EVIDENCE = {
    "annotationKind": "text_range",
    "documentUrl": "https://example.test/document",
    "documentTitle": "Example document",
    "selectedText": "untrusted selected text",
}


def _evidence(_annotation_id: str, _revision_id: str) -> dict[str, object]:
    return dict(_EVIDENCE)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _repo(tmp_path) -> AnnotationLineageRepository:
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.close()
    repo = AnnotationLineageRepository(profile_id="coding", state_db_path=path)
    repo.create_lineage(
        annotation_id="ann-1",
        annotation_lineage_root_id="annotation-root",
        model="test-model",
        model_config={
            "provider": "test",
            "reasoning": {"effort": "high"},
            "tools": [],
        },
        system_prompt="stable system bytes",
        cwd=str(tmp_path),
        created_at=10.0,
    )
    return repo


def _submit(repo, request: str, *, stale: bool = False, created: float = 20.0):
    return repo.submit_human_message(
        annotation_id="ann-1",
        thread_generation=1,
        body=f"body-{request}",
        intent="ask_agent",
        anchor_revision_id=f"revision-{request}",
        capture_digest=_digest(f"capture-{request}"),
        reply_to_message_id=None,
        context_digest=_digest(f"context-{request}"),
        client_request_id=request,
        actor_id="actor",
        turn_id=f"turn-{request}",
        anchor_stale_at_submit=stale,
        created_at=created,
    )


class _FakeAgent:
    def __init__(self, response: str = "answer", *, invoke_dispatch: bool = True, delay: float = 0):
        self.response = response
        self.invoke_dispatch = invoke_dispatch
        self.delay = delay
        self.pre_provider_dispatch_callback = None
        self._persist_disabled = False
        self.calls = []
        self.interrupts = []
        self.model = "test-model"
        self.tools = []

    def run_conversation(self, user_message, **kwargs):
        self.calls.append((user_message, kwargs))
        if self.invoke_dispatch:
            request = {
                "model": "test-model",
                "tools": [],
                "messages": [
                    {"role": "system", "content": kwargs["system_message"]},
                    *kwargs["conversation_history"],
                    {"role": "user", "content": user_message},
                ],
            }
            self.pre_provider_dispatch_callback(request)
        if self.delay:
            time.sleep(self.delay)
        return {"final_response": self.response, "completed": True}

    def interrupt(self, message=None):
        self.interrupts.append(message)


def test_worker_projects_exact_context_dispatches_once_and_persists_once(tmp_path):
    repo = _repo(tmp_path)
    accepted = _submit(repo, "one", stale=True)
    fake = _FakeAgent("first answer", delay=0.03)
    worker = AnnotationTurnWorker(
        repo,
        lambda snapshot: fake,
        _evidence,
        worker_id="worker",
        lease_seconds=1,
        heartbeat_seconds=0.01,
    )

    result = worker.run_next("ann-1", 1)

    assert result.status == "completed"
    assert result.turn_id == accepted.turn_id
    assert fake._persist_disabled is True
    assert fake._session_json_enabled is False
    assert fake.save_trajectories is False
    assert fake._memory_manager is None
    assert fake.compression_enabled is False
    assert fake._annotation_isolated is True
    assert len(fake.calls) == 1
    current, kwargs = fake.calls[0]
    decoded = json.loads(current)
    assert decoded == {
        "anchorRevisionId": "revision-one",
        "anchorStaleAtSubmit": True,
        "annotationId": "ann-1",
        "annotationKind": "text_range",
        "body": "body-one",
        "captureDigest": _digest("capture-one"),
        "contextDigest": _digest("context-one"),
        "documentTitle": "Example document",
        "documentUrl": "https://example.test/document",
        "evidenceTrust": "untrusted_page_evidence",
        "replyToMessageId": None,
        "schemaVersion": 1,
        "selectedText": "untrusted selected text",
        "sourceMessageId": None,
        "sourceSessionLineageId": None,
        "threadGeneration": 1,
        "turnSequence": 1,
    }
    assert kwargs["system_message"] == "stable system bytes"
    assert kwargs["conversation_history"] == []
    assert kwargs["task_id"] == accepted.turn_id
    assert repo.turn(accepted.turn_id)["status"] == "completed"
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM messages WHERE content='body-one'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM messages WHERE content='first answer'"
        ).fetchone()[0] == 1


def test_worker_history_is_completed_pairs_only_and_projection_is_byte_stable(tmp_path):
    repo = _repo(tmp_path)
    _submit(repo, "first")
    first_agent = _FakeAgent("answer-first")
    first_worker = AnnotationTurnWorker(
        repo, lambda _snapshot: first_agent, _evidence, worker_id="first"
    )
    assert first_worker.run_next("ann-1", 1).status == "completed"
    _submit(repo, "second", stale=True, created=21.0)

    claimed = repo.claim_next_turn(
        annotation_id="ann-1",
        thread_generation=1,
        lease_owner="projection",
        lease_seconds=30,
        now=30.0,
    )
    assert claimed is not None
    snapshot = repo.claimed_turn_input(
        claimed.turn_id, lease_owner="projection", attempt=claimed.attempt, now=30.0
    )
    def revision_evidence(_annotation_id, revision_id):
        return {
            **_EVIDENCE,
            "documentUrl": f"https://example.test/{revision_id}",
            "selectedText": f"text-{revision_id}",
        }

    first_projection = project_provider_input(snapshot, revision_evidence)
    second_projection = project_provider_input(snapshot, revision_evidence)

    assert first_projection == second_projection
    history, current, digest = first_projection
    assert [message["role"] for message in history] == ["user", "assistant"]
    historical_envelope = json.loads(history[0]["content"])
    assert historical_envelope["body"] == "body-first"
    assert historical_envelope["documentUrl"].endswith("revision-first")
    assert history[1]["content"] == "answer-first"
    current_envelope = json.loads(current)
    assert current_envelope["body"] == "body-second"
    assert current_envelope["documentUrl"].endswith("revision-second")
    assert len(digest) == 64


def test_worker_refuses_completion_when_agent_never_reaches_provider_dispatch(tmp_path):
    repo = _repo(tmp_path)
    accepted = _submit(repo, "no-dispatch")
    fake = _FakeAgent("fabricated answer", invoke_dispatch=False)
    result = AnnotationTurnWorker(
        repo, lambda _snapshot: fake, _evidence, worker_id="worker"
    ).run_next("ann-1", 1)

    assert result.status == "failed"
    assert result.error_code == "agent_error"
    turn = repo.turn(accepted.turn_id)
    assert turn["status"] == "failed"
    assert turn["dispatch_state"] == "not_dispatched"
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM messages WHERE content='fabricated answer'"
        ).fetchone()[0] == 0


def test_worker_refuses_an_identical_provider_request_replay(tmp_path):
    repo = _repo(tmp_path)
    accepted = _submit(repo, "replay")

    class _ReplayAgent(_FakeAgent):
        def run_conversation(self, user_message, **kwargs):
            request = {
                "model": "test-model",
                "tools": [],
                "messages": [
                    {"role": "system", "content": kwargs["system_message"]},
                    {"role": "user", "content": user_message},
                ],
            }
            self.pre_provider_dispatch_callback(request)
            self.pre_provider_dispatch_callback(request)
            return {"final_response": "must not persist", "completed": True}

    result = AnnotationTurnWorker(
        repo, lambda _snapshot: _ReplayAgent(), _evidence, worker_id="worker"
    ).run_next("ann-1", 1)

    assert result.status == "failed"
    assert repo.turn(accepted.turn_id)["dispatch_state"] == "dispatched"
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM messages WHERE content='must not persist'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("mutation", ["call_id", "name", "arguments", "result", "order"])
def test_worker_authenticates_complete_tool_round_before_continuation(
    tmp_path, mutation
):
    repo = _repo(tmp_path)
    accepted = _submit(repo, f"ledger-{mutation}")

    class _LedgerMutationAgent(_FakeAgent):
        def run_conversation(self, user_message, **kwargs):
            base = [
                {"role": "system", "content": kwargs["system_message"]},
                {"role": "user", "content": user_message},
            ]
            self.pre_provider_dispatch_callback(
                {"model": "test-model", "tools": [], "messages": base}
            )
            assistant = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"a"}'},
                    },
                    {
                        "id": "call-2",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"b"}'},
                    },
                ],
            }
            tool_messages = [
                {"role": "tool", "name": "read_file", "tool_call_id": "call-1", "content": "result-a"},
                {"role": "tool", "name": "read_file", "tool_call_id": "call-2", "content": "result-b"},
            ]
            self._annotation_provider_response_callback(assistant)
            self._annotation_tool_round_callback(assistant, tool_messages)
            self._annotation_provider_rounds_completed = 1
            suffix = json.loads(json.dumps([assistant, *tool_messages]))
            if mutation == "call_id":
                suffix[0]["tool_calls"][0]["id"] = "other-call"
            elif mutation == "name":
                suffix[0]["tool_calls"][0]["function"]["name"] = "write_file"
            elif mutation == "arguments":
                suffix[0]["tool_calls"][0]["function"]["arguments"] = '{"path":"other"}'
            elif mutation == "result":
                suffix[1]["content"] = "other-result"
            else:
                suffix[1], suffix[2] = suffix[2], suffix[1]
            self._annotation_canonical_request_messages = [*base, *suffix]
            self.pre_provider_dispatch_callback(
                {"model": "test-model", "tools": [], "messages": [*base, *suffix]}
            )
            return {"final_response": "must not persist", "completed": True}

    result = AnnotationTurnWorker(
        repo,
        lambda _snapshot: _LedgerMutationAgent(),
        _evidence,
        worker_id="worker",
    ).run_next("ann-1", 1)

    assert result.status == "failed"
    assert repo.turn(accepted.turn_id)["dispatch_state"] == "dispatched"
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM messages WHERE content='must not persist'"
        ).fetchone()[0] == 0


def test_worker_accepts_exact_non_fifo_tool_ledger_continuation(tmp_path):
    repo = _repo(tmp_path)
    _submit(repo, "ledger-valid")

    class _ValidLedgerAgent(_FakeAgent):
        def run_conversation(self, user_message, **kwargs):
            base = [
                {"role": "system", "content": kwargs["system_message"]},
                {"role": "user", "content": user_message},
            ]
            self.pre_provider_dispatch_callback(
                {"model": "test-model", "tools": [], "messages": base}
            )
            assistant = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call-b", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"b"}'}},
                    {"id": "call-a", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a"}'}},
                ],
            }
            results = [
                {"role": "tool", "name": "read_file", "tool_call_id": "call-b", "content": "B"},
                {"role": "tool", "name": "read_file", "tool_call_id": "call-a", "content": "A"},
            ]
            self._annotation_provider_response_callback(assistant)
            self._annotation_tool_round_callback(assistant, results)
            self._annotation_provider_rounds_completed = 1
            self._annotation_canonical_request_messages = [*base, assistant, *results]
            self.pre_provider_dispatch_callback(
                {"model": "test-model", "tools": [], "messages": [*base, assistant, *results]}
            )
            self._annotation_provider_response_callback(
                {"role": "assistant", "content": "ledger answer"}
            )
            self._annotation_provider_rounds_completed = 2
            return {"final_response": "ledger answer", "completed": True}

    result = AnnotationTurnWorker(
        repo, lambda _snapshot: _ValidLedgerAgent(), _evidence, worker_id="worker"
    ).run_next("ann-1", 1)
    assert result.status == "completed"


@pytest.mark.parametrize("mutation", ["image", "model", "tools"])
def test_worker_rejects_provider_context_or_runtime_mutation(tmp_path, mutation):
    repo = _repo(tmp_path)
    accepted = _submit(repo, f"mutated-{mutation}")

    class _MutatedAgent(_FakeAgent):
        def run_conversation(self, user_message, **kwargs):
            content = user_message
            if mutation == "image":
                content = [
                    {"type": "text", "text": user_message},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ]
            request = {
                "model": "mutated-model" if mutation == "model" else "test-model",
                "tools": (
                    [{"type": "function", "function": {"name": "unexpected"}}]
                    if mutation == "tools"
                    else []
                ),
                "messages": [
                    {"role": "system", "content": kwargs["system_message"]},
                    {"role": "user", "content": content},
                ],
            }
            self.pre_provider_dispatch_callback(request)
            return {"final_response": "must not persist", "completed": True}

    result = AnnotationTurnWorker(
        repo, lambda _snapshot: _MutatedAgent(), _evidence, worker_id="worker"
    ).run_next("ann-1", 1)
    assert result.status == "failed"
    turn = repo.turn(accepted.turn_id)
    assert turn["dispatch_state"] == "not_dispatched"
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM messages WHERE content='must not persist'"
        ).fetchone()[0] == 0


def test_worker_failure_does_not_replay_dispatched_turn(tmp_path):
    repo = _repo(tmp_path)
    accepted = _submit(repo, "provider-failure")

    class _FailingAgent(_FakeAgent):
        def run_conversation(self, user_message, **kwargs):
            self.pre_provider_dispatch_callback(
                {
                    "model": "test-model",
                    "tools": [],
                    "messages": [
                        {"role": "system", "content": kwargs["system_message"]},
                        *kwargs["conversation_history"],
                        {"role": "user", "content": user_message},
                    ],
                }
            )
            return {"failed": True, "error": "secret provider prose"}

    result = AnnotationTurnWorker(
        repo, lambda _snapshot: _FailingAgent(), _evidence, worker_id="worker"
    ).run_next("ann-1", 1)
    assert result.status == "failed"
    turn = repo.turn(accepted.turn_id)
    assert turn["status"] == "failed"
    assert turn["error_code"] == "agent_error"
    assert "secret" not in json.dumps(turn)
    assert repo.claim_next_turn(
        annotation_id="ann-1",
        thread_generation=1,
        lease_owner="other",
        lease_seconds=30,
    ) is None


def test_worker_does_not_commit_incomplete_result_with_response_prose(tmp_path):
    repo = _repo(tmp_path)
    accepted = _submit(repo, "incomplete")

    class _IncompleteAgent(_FakeAgent):
        def run_conversation(self, user_message, **kwargs):
            request = {
                "model": "test-model",
                "tools": [],
                "messages": [
                    {"role": "system", "content": kwargs["system_message"]},
                    *kwargs["conversation_history"],
                    {"role": "user", "content": user_message},
                ],
            }
            self.pre_provider_dispatch_callback(request)
            return {"final_response": "terminal failure prose", "completed": False}

    result = AnnotationTurnWorker(
        repo, lambda _snapshot: _IncompleteAgent(), _evidence, worker_id="worker"
    ).run_next("ann-1", 1)
    assert result.status == "failed"
    assert repo.turn(accepted.turn_id)["status"] == "failed"
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM messages WHERE content='terminal failure prose'"
        ).fetchone()[0] == 0


def test_setup_lease_loss_returns_durable_requeued_authority(tmp_path):
    repo = _repo(tmp_path)
    accepted = _submit(repo, "setup-expiry")

    def expiring_evidence(_annotation_id, _revision_id):
        assert repo.recover_expired_turns(now=200.0)["requeued"] == 1
        return dict(_EVIDENCE)

    result = AnnotationTurnWorker(
        repo,
        lambda _snapshot: _FakeAgent(),
        expiring_evidence,
        worker_id="worker",
        lease_seconds=10,
        heartbeat_seconds=1,
        clock=lambda: 100.0,
    ).run_next("ann-1", 1)

    assert result.status == "queued"
    assert result.error_code == "lease_expired_before_dispatch"
    assert repo.turn(accepted.turn_id)["status"] == "queued"


def test_real_agent_path_is_persistence_and_plugin_context_isolated(tmp_path):
    from run_agent import AIAgent

    repo = _repo(tmp_path)
    _submit(repo, "real-agent")

    def factory(_snapshot):
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            agent = AIAgent(
                model="test-model",
                provider="openrouter",
                api_key="test-key-1234567890",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
        message = SimpleNamespace(content="real answer", tool_calls=None)
        agent.client = MagicMock()
        agent.client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            model="test-model",
            usage=None,
        )
        agent.logs_dir = tmp_path / "session-json"
        agent._cached_system_prompt = "stable system bytes"
        agent.compression_enabled = False
        agent.save_trajectories = False
        return agent

    with (
        patch(
            "hermes_cli.plugins.invoke_hook",
            return_value=[{"context": "MUTABLE_PLUGIN_CONTEXT"}],
        ) as invoke_hook,
        patch.dict(os.environ, {"HERMES_DUMP_REQUESTS": "1"}),
    ):
        result = AnnotationTurnWorker(repo, factory, _evidence, worker_id="worker").run_next(
            "ann-1", 1
        )

    assert result.status == "completed"
    assert not list((tmp_path / "session-json").glob("session_*.json"))
    assert invoke_hook.call_args_list == []
    assert not list((tmp_path / "session-json").glob("request_dump_*.json"))
