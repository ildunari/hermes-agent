from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agent.conversation_loop import append_api_only_user_context
from agent.prompt_caching import apply_anthropic_cache_control
from gateway.contact_memory.admin import (
    activation_self_check, main as admin_main, resolve_contact_memory_root,
)
from gateway.contact_memory.broker import ContactMemoryBroker
from gateway.contact_memory.embeddings import EmbeddingGemmaBackend, backend_from_config
from gateway.contact_memory.rerankers import (
    Qwen3RerankerBackend, RerankerWorkerError, reranker_from_config,
)
from gateway.contact_memory.schema import (
    AssertionType, Audience, FactProposal, MentionPolicy, RetrievalPrincipal,
    SearchResult,
)
from gateway.contact_memory.store import ContactMemoryStore
from gateway.run import (
    TrustedContactScope,
    _compile_contact_memory_candidate,
    _compile_contact_memory_prompt,
    _contact_memory_brokers,
    _trusted_contact_scope_from_metadata,
)
from gateway.config import Platform
from gateway.session import SessionSource
from tests.gateway.test_run_cleanup_progress import (
    CleanupCaptureAdapter, ProgressAgent, _install_fakes, _make_runner,
)


def _seed(root):
    store = ContactMemoryStore(root / "contact-memory", "contact-a")
    store.supersede_fact(FactProposal(
        logical_id="car", subject_id="person:contact", predicate="owns",
        object_text="The car under discussion is the green hatchback.",
        audience=Audience.GUEST_OK, mention_policy=MentionPolicy.MENTIONABLE,
        assertion_type=AssertionType.STATED, source_id="synthetic-1",
        source_contact_id="contact-a", trust=.95, confidence=.95,
    ))


def _scope(principal: str = "guest") -> TrustedContactScope:
    return TrustedContactScope(principal=principal, contact_id="contact-a")


def test_gateway_lane_a_is_default_off_and_requires_immutable_trusted_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _contact_memory_brokers.clear()
    _seed(tmp_path)
    args = dict(
        message="should i keep the car", history=[], session_key="session",
        now_ts=1000.0, texture_prompt="response_class: answer",
    )
    assert _compile_contact_memory_prompt(config_raw={}, trusted_scope=None, **args) == ""
    enabled = {"enabled": True, "lane_a": True}
    assert _compile_contact_memory_prompt(config_raw=enabled, trusted_scope=None, **args) == ""
    assert _compile_contact_memory_prompt(
        config_raw=enabled, trusted_scope={"principal": "guest", "session_contact_id": "contact-a"}, **args,
    ) == ""
    rendered = _compile_contact_memory_prompt(
        config_raw=enabled, trusted_scope=_scope(), **args,
    )
    assert "green hatchback" in rendered
    with pytest.raises(FrozenInstanceError):
        _scope().contact_id = "other"  # type: ignore[misc]


def test_explicit_routed_profile_root_overrides_host_home(tmp_path, monkeypatch):
    host = tmp_path / "host"
    routed = tmp_path / "routed"
    host.mkdir()
    routed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(host))
    _contact_memory_brokers.clear()
    _seed(routed)
    rendered = _compile_contact_memory_prompt(
        config_raw={"enabled": True, "lane_a": True},
        trusted_scope=_scope(), message="should i keep the car", history=[],
        session_key="session", now_ts=1000.0, profile_home=routed,
    )
    assert "green hatchback" in rendered
    assert not (host / "contact-memory").exists()


def test_recall_uses_api_copy_and_preserves_system_cache_and_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _contact_memory_brokers.clear()
    _seed(tmp_path)
    base = "stable profile prompt"
    config = {"enabled": True, "lane_a": True}
    recalls = [
        _compile_contact_memory_prompt(
            config_raw=config,
            trusted_scope=_scope(),
            message=message,
            history=[],
            session_key=f"session-{index}",
            now_ts=now,
        )
        for index, (message, now) in enumerate(
            (("should i keep the car", 1000.0), ("should i sell the car", 1200.0))
        )
    ]
    assert all("green hatchback" in recall for recall in recalls)

    marked = [
        apply_anthropic_cache_control([
            {"role": "system", "content": base},
            {"role": "user", "content": "turn"},
        ], cache_ttl="1h")[0]["content"]
        for _ in recalls
    ]
    assert marked[0] == marked[1]
    assert marked[0][0]["text"] == base
    assert marked[0][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    transcript = {"role": "user", "content": "turn"}
    api_copies = []
    for recall in recalls:
        api_copy = transcript.copy()
        append_api_only_user_context(api_copy, [recall])
        api_copies.append(api_copy)
    assert transcript == {"role": "user", "content": "turn"}
    assert all("green hatchback" in row["content"] for row in api_copies)


@pytest.mark.asyncio
async def test_gateway_run_hands_recall_to_agent_api_only_lane(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _contact_memory_brokers.clear()
    _seed(tmp_path)

    class CaptureAgent(ProgressAgent):
        seen_context = None
        def run_conversation(self, message, conversation_history=None, task_id=None, **kwargs):
            type(self).seen_context = getattr(self, "per_turn_user_context", "")
            return {
                "final_response": "done",
                "messages": [{"role": "user", "content": message}],
                "api_calls": 1,
            }

    adapter = CleanupCaptureAdapter(platform=Platform.BLUEBUBBLES)
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(monkeypatch, CaptureAgent, cleanup_on=False)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    config = {"agent": {"contact_memory": {"enabled": True, "lane_a": True}}}
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)

    source = SessionSource(
        platform=Platform.BLUEBUBBLES, chat_id="contact-chat", chat_type="dm",
        user_id="trusted-contact",
    )
    result = await runner._run_agent(
        message="should i keep the car",
        context_prompt="stable context",
        history=[],
        source=source,
        session_id="session-contact",
        session_key="bluebubbles:contact",
        trusted_contact_scope=_scope(),
    )
    assert "green hatchback" in (CaptureAgent.seen_context or "")
    assert result["messages"] == [{"role": "user", "content": "should i keep the car"}]

    # Cached agents must clear the per-turn lane when a later turn has no scope;
    # otherwise one authenticated contact's recall could leak into a queued or
    # untrusted turn.
    await runner._run_agent(
        message="unscoped follow-up",
        context_prompt="stable context",
        history=result["messages"],
        source=source,
        session_id="session-contact",
        session_key="bluebubbles:contact",
        trusted_contact_scope=None,
    )
    assert CaptureAgent.seen_context == ""


def test_only_authenticated_metadata_shape_becomes_scope():
    raw = {"_hermes_contact_scope": {"principal": "guest", "session_contact_id": "contact-a"}}
    assert _trusted_contact_scope_from_metadata(raw) == _scope()
    assert _trusted_contact_scope_from_metadata({"_hermes_contact_scope": {
        "principal": "admin", "session_contact_id": "contact-a"
    }}) is None
    assert _trusted_contact_scope_from_metadata({"_hermes_contact_scope": {
        "principal": "guest", "session_contact_id": "bad\ncontact"
    }}) is None


def test_hostile_or_approved_group_scope_cannot_enable_lane_a():
    common = dict(
        config_raw={"enabled": True, "lane_a": True}, message="should i keep the car",
        history=[], session_key="session", now_ts=1000.0,
    )
    assert _compile_contact_memory_prompt(
        trusted_scope={"principal": "guest", "session_contact_id": "../../owner"}, **common
    ) == ""
    assert _compile_contact_memory_prompt(trusted_scope=None, **common) == ""


def test_gateway_lane_a_fails_open_on_backend_error(monkeypatch):
    _contact_memory_brokers.clear()

    def explode(*args, **kwargs):
        raise RuntimeError("synthetic corrupt store")

    monkeypatch.setattr(ContactMemoryBroker, "prefetch", explode)
    rendered = _compile_contact_memory_candidate(
        config_raw={"enabled": True, "lane_a": True},
        trusted_scope=_scope(),
        message="should i keep it", history=[], session_key="session",
        now_ts=1000.0,
    )
    assert rendered == ""


def test_embeddinggemma_worker_protocol_is_persistent_and_distinguishes_documents(tmp_path):
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "print(json.dumps({'ready':True}),flush=True)\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line); v=[1.0,0.0] if r['kind']=='query' else [0.0,1.0]\n"
        " print(json.dumps({'ok':True,'vectors':[v for _ in r['texts']]}),flush=True)\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    backend = EmbeddingGemmaBackend(python_path=fake_python, timeout_seconds=2)
    try:
        assert backend.encode(["query"]) == [[1.0, 0.0]]
        pid = backend._process.pid  # type: ignore[union-attr]
        assert backend.encode_documents(["fact"]) == [[0.0, 1.0]]
        assert backend._process.pid == pid  # type: ignore[union-attr]
        assert backend.dimensions == 2
    finally:
        backend.close()


def test_profile_embedding_config_and_index_round_trip(tmp_path):
    assert backend_from_config({"embedding": {"backend": "off"}}) is None
    configured = backend_from_config({
        "embedding": {
            "backend": "embeddinggemma",
            "model": "synthetic/model",
            "python_path": "/tmp/synthetic-python",
            "timeout_seconds": 7,
            "startup_timeout_seconds": 19,
        }
    })
    assert isinstance(configured, EmbeddingGemmaBackend)
    assert configured.model_id == "synthetic/model"
    assert configured.timeout_seconds == 7
    assert configured.startup_timeout_seconds == 19

    _seed(tmp_path)

    class FakeBackend:
        model_id = "synthetic-embedding-v1"
        dimensions = 2
        def encode(self, texts):
            return [[1.0, 0.0] for _ in texts]
        def encode_documents(self, texts):
            assert any("green hatchback" in text for text in texts)
            return [[0.0, 1.0] for _ in texts]

    broker = ContactMemoryBroker(tmp_path / "contact-memory", embedding_backend=FakeBackend())
    assert broker.index_approved_facts("contact-a") == 1
    result = ContactMemoryStore(tmp_path / "contact-memory", "contact-a").vector_search(
        RetrievalPrincipal.GUEST,
        [0.0, 1.0], model_id="synthetic-embedding-v1",
    )
    assert result and "green hatchback" in result[0].fact.object_text


def test_admin_index_command_embeds_approved_facts(tmp_path, monkeypatch, capsys):
    _seed(tmp_path)

    class FakeBackend:
        def __init__(self, model, python_path=None):
            self.model_id = model
            self.dimensions = 2
        def encode_documents(self, texts):
            return [[0.0, 1.0] for _ in texts]
        def encode(self, texts):
            return [[1.0, 0.0] for _ in texts]
        def close(self):
            pass

    monkeypatch.setattr(
        "gateway.contact_memory.embeddings.EmbeddingGemmaBackend", FakeBackend
    )
    assert admin_main([
        "--contact-id", "contact-a", "--root", str(tmp_path / "contact-memory"),
        "index", "--model", "synthetic-admin-v1",
    ]) == 0
    payload = capsys.readouterr().out
    assert '"indexed": 1' in payload
    assert '"model": "synthetic-admin-v1"' in payload


def test_qwen3_config_worker_persistence_and_timeout(tmp_path):
    assert reranker_from_config({"reranker": {"backend": "off"}}) is None
    fake_python = tmp_path / "fake-reranker-python"
    fake_python.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys,time\n"
        "print(json.dumps({'ready':True}),flush=True)\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line)\n"
        " if r['query']=='timeout': time.sleep(.2)\n"
        " print(json.dumps({'ok':True,'scores':list(range(len(r['documents'])))}),flush=True)\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    backend = reranker_from_config({"reranker": {
        "backend": "qwen3-mlx", "model": "synthetic/reranker",
        "python_path": str(fake_python), "timeout_seconds": .05,
        "startup_timeout_seconds": 2,
    }})
    assert isinstance(backend, Qwen3RerankerBackend)
    assert backend.startup_timeout_seconds == 2
    try:
        assert backend.score("query", ["one", "two"]) == [0.0, 1.0]
        pid = backend._process.pid  # type: ignore[union-attr]
        assert backend.score("query", ["three"]) == [0.0]
        assert backend._process.pid == pid  # type: ignore[union-attr]
        with pytest.raises(RerankerWorkerError, match="timed out"):
            backend.score("timeout", ["fact"])
        assert backend._process is None
    finally:
        backend.close()


def test_broker_reranker_reorders_and_fails_open():
    class Fact:
        subject_id = "person:test"
        predicate = "knows"
        object_text = "synthetic fact"

    class Candidate:
        fact = Fact()

    first, second = Candidate(), Candidate()

    class ReverseReranker:
        model_id = "synthetic"
        def score(self, query, documents):
            assert len(documents) == 2
            return [0.1, 0.9]

    broker = ContactMemoryBroker("/tmp", reranker=ReverseReranker())
    reranked = broker._rerank("query", [first, second])  # type: ignore[arg-type]
    assert [result.score for result in reranked] == [0.9, 0.1]

    class BrokenReranker(ReverseReranker):
        def score(self, query, documents):
            raise TimeoutError("synthetic")

    broker.reranker = BrokenReranker()
    assert broker._rerank("query", [first, second]) == [first, second]  # type: ignore[arg-type]


def test_hybrid_candidates_reserve_lexical_quota():
    class Fact:
        def __init__(self, version_id):
            self.version_id = version_id
            self.trust = self.confidence = 1.0

    semantic = [SearchResult(Fact(f"dense-{i}"), 1 - i / 100, semantic_score=1 - i / 100) for i in range(10)]  # type: ignore[arg-type]
    lexical = [SearchResult(Fact("category"), 0.4, lexical_score=0.4)]  # type: ignore[arg-type]
    selected = ContactMemoryBroker._hybrid_candidates(lexical, semantic, set(), 10)
    assert len(selected) == 10
    assert any(result.fact.version_id == "category" for result in selected)


def test_profile_contact_memory_root_resolves_to_selected_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "coding"))
    assert resolve_contact_memory_root(profile="guest") == tmp_path / "profiles" / "guest" / "contact-memory"
    assert resolve_contact_memory_root(root=str(tmp_path / "explicit")) == tmp_path / "explicit"


def test_activation_self_check_is_text_free_and_exercises_workers(tmp_path, monkeypatch):
    _seed(tmp_path)
    store = ContactMemoryStore(tmp_path / "contact-memory", "contact-a")

    class FakeEmbedding:
        model_id = "synthetic-model"
        dimensions = 2
        def encode(self, texts):
            return [[1.0, 0.0] for _ in texts]
        def close(self): pass

    class FakeReranker:
        model_id = "synthetic-reranker"
        def score(self, query, documents):
            return [float(index) for index, _ in enumerate(documents)]
        def close(self): pass

    fact = store.active_facts(RetrievalPrincipal.GUEST)[0]
    store.put_embedding(fact.version_id, "synthetic-model", [1.0, 0.0])
    monkeypatch.setattr("gateway.contact_memory.embeddings.backend_from_config", lambda config: FakeEmbedding())
    monkeypatch.setattr("gateway.contact_memory.rerankers.reranker_from_config", lambda config: FakeReranker())
    result = activation_self_check(
        tmp_path / "contact-memory", "contact-a",
        {"enabled": True, "lane_a": True, "embedding": {}, "reranker": {}},
        RetrievalPrincipal.GUEST,
    )
    assert result["ok"] is True
    assert result["synthetic_retrieval"] is True
    assert "green hatchback" not in str(result)
