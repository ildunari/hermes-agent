"""Coverage for a70243d3d — ephemeral-by-default Codex subtask sessions.

``CodexAppServerSession(ephemeral=True)`` must add ``ephemeral: true`` to the
``thread/start`` request params (codex keeps the thread in-memory only; no
rollout file lands in ~/.codex/sessions, so the thread never shows up in the
Codex Desktop sidebar). The default and an explicit ``ephemeral=False`` must
omit the key entirely so durable sessions keep codex's on-disk behaviour.
"""

from __future__ import annotations

from agent.transports.codex_app_server_session import CodexAppServerSession


class _FakeClient:
    """Records JSON-RPC traffic instead of spawning a codex subprocess."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.requests: list[tuple[str, dict]] = []

    def initialize(self, **kwargs):
        self.init_kwargs = kwargs

    def request(self, method, params, timeout=None):
        self.requests.append((method, dict(params)))
        return {"thread": {"id": "thread-eph-1"}}

    def close(self):
        pass


def _start_session(**session_kwargs):
    holder = {}

    def factory(**kwargs):
        holder["client"] = _FakeClient(**kwargs)
        return holder["client"]

    session = CodexAppServerSession(
        cwd="/tmp", client_factory=factory, **session_kwargs
    )
    thread_id = session.ensure_started()
    return thread_id, holder["client"]


def _thread_start_params(client):
    starts = [p for (m, p) in client.requests if m == "thread/start"]
    assert len(starts) == 1, client.requests
    return starts[0]


def test_ephemeral_true_sets_thread_start_param():
    thread_id, client = _start_session(ephemeral=True)
    assert thread_id == "thread-eph-1"
    assert _thread_start_params(client)["ephemeral"] is True


def test_default_omits_ephemeral_param():
    _, client = _start_session()
    assert "ephemeral" not in _thread_start_params(client)


def test_explicit_false_omits_ephemeral_param():
    _, client = _start_session(ephemeral=False)
    assert "ephemeral" not in _thread_start_params(client)
