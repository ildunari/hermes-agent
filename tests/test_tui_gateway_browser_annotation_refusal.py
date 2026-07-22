import sqlite3
import threading

import pytest

from tui_gateway import server


ANNOTATION_ERROR = {
    "code": 4026,
    "message": "browser_annotation_session_requires_dedicated_rpc",
}
CLASSIFICATION_ERROR = {
    "code": 5027,
    "message": "session_lineage_classification_unavailable",
}


def _compressed_annotation_db(tmp_path):
    """Real durable lineage whose descendants look like ordinary TUI rows."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    marker = {"_session_kind": "browser_annotation"}
    # A rewritten legacy source ensures this exercises the durable marker.
    db.create_session("annotation-root", source="tui", model_config=marker)
    db.end_session("annotation-root", "compression")
    db.create_session(
        "annotation-compressed-1",
        source="desktop",
        model_config=marker,
        parent_session_id="annotation-root",
    )
    db.end_session("annotation-compressed-1", "compression")
    db.create_session(
        "annotation-compressed-2",
        source="tui",
        model_config=marker,
        parent_session_id="annotation-compressed-1",
    )
    db.set_session_title("annotation-compressed-2", "Compressed annotation")
    db.append_message("annotation-compressed-2", role="user", content="must stay hidden")
    db.create_session("generic", source="tui")
    db.append_message("generic", role="user", content="visible")
    return db


class _RowsDB:
    def __init__(self, rows):
        self.rows = rows

    def list_sessions_rich(self, **_kwargs):
        return list(self.rows)

    def get_session(self, session_id):
        return next((row for row in self.rows if row.get("id") == session_id), None)


def test_generic_session_discovery_omits_browser_annotation_roots(monkeypatch):
    db = _RowsDB(
        [
            {"id": "annotation", "source": "browser_annotation", "title": "hidden"},
            {"id": "chat", "source": "tui", "title": "visible"},
        ]
    )
    monkeypatch.setattr(server, "_get_db", lambda: db)

    listed = server.handle_request({"id": "list", "method": "session.list", "params": {}})
    recent = server.handle_request(
        {"id": "recent", "method": "session.most_recent", "params": {}}
    )

    assert listed is not None
    assert recent is not None
    assert [row["id"] for row in listed["result"]["sessions"]] == ["chat"]
    assert recent["result"]["session_id"] == "chat"


@pytest.mark.parametrize("target", ["annotation-id", "Annotation title"])
def test_session_resume_refuses_annotation_by_id_or_title_before_reuse(monkeypatch, target):
    annotation = {
        "id": "annotation-id",
        "source": "browser_annotation",
        "title": "Annotation title",
    }

    class _DB:
        def get_session(self, session_id):
            return annotation if session_id == "annotation-id" else None

        def get_session_by_title(self, title):
            return annotation if title == "Annotation title" else None

        def resolve_resume_session_id(self, _session_id):
            pytest.fail("annotation resume reached continuation resolution")

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(
        server,
        "_find_live_session_by_key",
        lambda _key: pytest.fail("annotation resume reached live reuse"),
    )

    response = server.handle_request(
        {"id": "resume", "method": "session.resume", "params": {"session_id": target}}
    )

    assert response is not None
    assert response["error"] == ANNOTATION_ERROR


def test_session_create_refuses_annotation_parent_in_selected_profile(monkeypatch, tmp_path):
    profile_home = tmp_path / "profiles" / "review"
    profile_home.mkdir(parents=True)
    (profile_home / "state.db").touch()

    class _DB:
        def get_session(self, _session_id):
            return {"id": "annotation-id", "source": "browser_annotation"}

        def close(self):
            return None

    monkeypatch.setattr(server, "_resolve_profile_dir", lambda _profile: profile_home)
    monkeypatch.setattr(server, "_open_profile_db", lambda _home: _DB())
    monkeypatch.setattr(
        server,
        "_claim_active_session_slot",
        lambda *_args, **_kwargs: pytest.fail("annotation child claimed a live slot"),
    )

    response = server.handle_request(
        {
            "id": "create",
            "method": "session.create",
            "params": {"profile": "review", "parent_session_id": "annotation-id"},
        }
    )

    assert response is not None
    assert response["error"] == ANNOTATION_ERROR


def test_session_resume_checks_and_closes_selected_profile_db(monkeypatch, tmp_path):
    profile_home = tmp_path / "profiles" / "review"
    profile_home.mkdir(parents=True)
    closed = []

    class _DB:
        def __init__(self, *, db_path):
            assert db_path == profile_home / "state.db"

        def get_session(self, _session_id):
            return {"id": "annotation-id", "source": "browser_annotation"}

        def close(self):
            closed.append(True)

    import hermes_state

    monkeypatch.setattr(server, "_profile_home", lambda _profile: profile_home)
    monkeypatch.setattr(hermes_state, "SessionDB", _DB)

    response = server.handle_request(
        {
            "id": "resume-profile",
            "method": "session.resume",
            "params": {"profile": "review", "session_id": "annotation-id"},
        }
    )

    assert response is not None
    assert response["error"] == ANNOTATION_ERROR
    assert closed == [True]


@pytest.mark.parametrize("method", ["session.branch", "prompt.submit"])
def test_live_annotation_root_refuses_generic_mutation_before_runtime(monkeypatch, method):
    sid = "live-annotation"
    session = {
        "agent": None,
        "history": [{"role": "user", "content": "annotation question"}],
        "history_lock": threading.Lock(),
        "running": False,
        "session_key": "annotation-id",
        "source": "browser_annotation",
    }
    monkeypatch.setitem(server._sessions, sid, session)
    monkeypatch.setattr(
        server,
        "_start_agent_build",
        lambda *_args, **_kwargs: pytest.fail("generic path started annotation agent"),
    )

    params = {"session_id": sid}
    if method == "prompt.submit":
        params["text"] = "must not run"
    response = server.handle_request({"id": "mutation", "method": method, "params": params})

    assert response is not None
    assert response["error"] == ANNOTATION_ERROR
    assert session["running"] is False


def test_compressed_annotation_descendant_is_hidden_from_generic_discovery(monkeypatch, tmp_path):
    db = _compressed_annotation_db(tmp_path)
    monkeypatch.setattr(server, "_get_db", lambda: db)

    listed = server.handle_request({"id": "list", "method": "session.list", "params": {}})
    recent = server.handle_request({"id": "recent", "method": "session.most_recent", "params": {}})

    assert listed is not None
    assert recent is not None
    assert [row["id"] for row in listed["result"]["sessions"]] == ["generic"]
    assert recent["result"]["session_id"] == "generic"
    db.close()


@pytest.mark.parametrize("target", ["annotation-compressed-2", "Compressed annotation"])
def test_resume_refuses_compressed_annotation_descendant_by_id_or_title(
    monkeypatch, tmp_path, target
):
    db = _compressed_annotation_db(tmp_path)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(
        server,
        "_find_live_session_by_key",
        lambda _key: pytest.fail("compressed annotation reached live reuse"),
    )

    response = server.handle_request(
        {"id": "resume", "method": "session.resume", "params": {"session_id": target}}
    )

    assert response is not None
    assert response["error"] == ANNOTATION_ERROR
    db.close()


def test_create_refuses_compressed_annotation_parent(monkeypatch, tmp_path):
    db = _compressed_annotation_db(tmp_path)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(
        server,
        "_claim_active_session_slot",
        lambda *_args, **_kwargs: pytest.fail("annotation descendant claimed a live slot"),
    )

    response = server.handle_request(
        {
            "id": "create",
            "method": "session.create",
            "params": {"parent_session_id": "annotation-compressed-2"},
        }
    )

    assert response is not None
    assert response["error"] == ANNOTATION_ERROR
    db.close()


@pytest.mark.parametrize("method", ["session.branch", "prompt.submit"])
def test_live_compressed_annotation_descendant_is_classified_from_real_db(
    monkeypatch, tmp_path, method
):
    db = _compressed_annotation_db(tmp_path)
    sid = "live-compressed-annotation"
    session = {
        "agent": None,
        "history": [{"role": "user", "content": "annotation"}],
        "history_lock": threading.Lock(),
        "running": False,
        "session_key": "annotation-compressed-2",
        "source": "tui",
        # Deliberately no root_source: force durable descendant classification.
    }
    monkeypatch.setitem(server._sessions, sid, session)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(
        server,
        "_start_agent_build",
        lambda *_args, **_kwargs: pytest.fail("compressed annotation started an agent"),
    )

    params = {"session_id": sid}
    if method == "prompt.submit":
        params["text"] = "must not run"
    response = server.handle_request({"id": "mutation", "method": method, "params": params})

    assert response is not None
    assert response["error"] == ANNOTATION_ERROR
    assert session["root_source"] == "browser_annotation"
    assert session["running"] is False
    db.close()


@pytest.mark.parametrize("method", ["session.branch", "prompt.submit"])
def test_unknown_live_lineage_fails_closed_when_db_unavailable(monkeypatch, method):
    sid = "legacy-unknown"
    session = {
        "agent": None,
        "history": [{"role": "user", "content": "legacy"}],
        "history_lock": threading.Lock(),
        "running": False,
        "session_key": "legacy-key",
        "source": "tui",
        # No root_source: this legacy record requires durable proof.
    }
    monkeypatch.setitem(server._sessions, sid, session)
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(
        server,
        "_start_agent_build",
        lambda *_args, **_kwargs: pytest.fail("unclassified live session started an agent"),
    )

    params = {"session_id": sid}
    if method == "prompt.submit":
        params["text"] = "must not run"
    response = server.handle_request({"id": "mutation", "method": method, "params": params})

    assert response is not None
    assert response["error"] == CLASSIFICATION_ERROR
    assert session["running"] is False


def test_proven_generic_live_session_does_not_query_db(monkeypatch):
    session = {
        "session_key": "fresh-generic",
        "source": "tui",
        "root_source": "tui",
    }
    monkeypatch.setattr(
        server,
        "_get_db",
        lambda: pytest.fail("proven generic live session queried the DB"),
    )

    assert server._live_session_browser_annotation_error(session, "request") is None


def _rewritten_annotation_route_db(tmp_path):
    """Real route ledger whose redundant session markers were corrupted."""
    from hermes_cli.browser_annotation_lineage import AnnotationLineageRepository
    from hermes_state import SessionDB

    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.close()
    repo = AnnotationLineageRepository(profile_id="coding", state_db_path=path)
    repo.create_lineage(
        annotation_id="annotation-1",
        annotation_lineage_root_id="annotation-route-root",
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE sessions SET source='tui', model_config='[]', archived=0 "
            "WHERE id='annotation-route-root'"
        )
    db = SessionDB(db_path=path)
    db.create_session(
        "annotation-route-descendant",
        source="tui",
        parent_session_id="annotation-route-root",
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE sessions SET model_config='[]' "
            "WHERE id='annotation-route-descendant'"
        )
    db.create_session("generic", source="tui")
    db.append_message("generic", role="user", content="visible")
    return db


def _projected_mixed_annotation_db(tmp_path, kind):
    """Build a real compression projection whose id and other fields disagree."""
    from hermes_state import SessionDB

    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("generic", source="tui")
    db.append_message("generic", role="user", content="visible generic preview")

    if kind == "route-root":
        from hermes_cli.browser_annotation_lineage import AnnotationLineageRepository

        db.close()
        repo = AnnotationLineageRepository(profile_id="coding", state_db_path=path)
        repo.create_lineage(
            annotation_id="annotation-projected",
            annotation_lineage_root_id="mixed-root",
        )
        with sqlite3.connect(path) as conn:
            conn.execute(
                "UPDATE sessions SET source='tui', model_config='[]', archived=0 "
                "WHERE id='mixed-root'"
            )
        db = SessionDB(db_path=path)
        tip_source = "tui"
    else:
        db.create_session("mixed-root", source="tui")
        tip_source = "browser_annotation"

    db.append_message("mixed-root", role="user", content=f"secret root {kind}")
    db.end_session("mixed-root", "compression")
    db.create_session("mixed-tip", source=tip_source, parent_session_id="mixed-root")
    db.append_message("mixed-tip", role="user", content=f"secret tip {kind}")
    return db


@pytest.mark.parametrize("kind", ["route-root", "annotation-tip"])
def test_projected_mixed_annotation_rows_never_leak_from_discovery(
    monkeypatch, tmp_path, kind
):
    """Projection may pair a tip id with root source/config/preview fields."""
    db = _projected_mixed_annotation_db(tmp_path, kind)
    projected = next(
        row
        for row in db.list_sessions_rich(
            limit=20, order_by_last_active=True, compact_rows=True
        )
        if row["id"] == "mixed-tip"
    )
    # Prove this is the synthetic mixed-row shape from the P1 report rather
    # than a test that happens to classify an already-authoritative tip row.
    assert projected["source"] == "tui"
    assert projected.get("parent_session_id") is None

    monkeypatch.setattr(server, "_get_db", lambda: db)
    listed = server.handle_request({"id": "list", "method": "session.list", "params": {}})
    recent = server.handle_request(
        {"id": "recent", "method": "session.most_recent", "params": {}}
    )

    assert listed is not None and "error" not in listed
    assert recent is not None and "error" not in recent
    assert [row["id"] for row in listed["result"]["sessions"]] == ["generic"]
    assert recent["result"]["session_id"] == "generic"
    serialized = repr((listed, recent))
    assert f"secret root {kind}" not in serialized
    assert f"secret tip {kind}" not in serialized
    db.close()


def test_authoritative_route_blocks_rewritten_malformed_annotation_everywhere(
    monkeypatch, tmp_path
):
    db = _rewritten_annotation_route_db(tmp_path)
    monkeypatch.setattr(server, "_get_db", lambda: db)

    listed = server.handle_request({"id": "list", "method": "session.list", "params": {}})
    assert listed is not None
    assert [row["id"] for row in listed["result"]["sessions"]] == ["generic"]

    resumed = server.handle_request(
        {
            "id": "resume",
            "method": "session.resume",
            "params": {"session_id": "annotation-route-descendant"},
        }
    )
    assert resumed is not None
    assert resumed["error"] == ANNOTATION_ERROR

    live = {
        "agent": None,
        "history": [],
        "history_lock": threading.Lock(),
        "running": False,
        "session_key": "annotation-route-descendant",
        "source": "tui",
    }
    monkeypatch.setitem(server._sessions, "rewritten-live", live)
    monkeypatch.setattr(
        server,
        "_start_agent_build",
        lambda *_args, **_kwargs: pytest.fail("rewritten annotation started an agent"),
    )
    submitted = server.handle_request(
        {
            "id": "submit",
            "method": "prompt.submit",
            "params": {"session_id": "rewritten-live", "text": "must not run"},
        }
    )
    assert submitted is not None
    assert submitted["error"] == ANNOTATION_ERROR
    assert live["root_source"] == "browser_annotation"
    db.close()


@pytest.mark.parametrize("encoded_config", ["[]", "{not-json"])
def test_session_list_fails_closed_for_unclassified_model_config(
    monkeypatch, tmp_path, encoded_config
):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("unclassified", source="tui")
    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            "UPDATE sessions SET model_config=? WHERE id='unclassified'", (encoded_config,)
        )
    monkeypatch.setattr(server, "_get_db", lambda: db)

    response = server.handle_request({"id": "list", "method": "session.list", "params": {}})

    assert response is not None
    assert response["error"]["code"] == 5006
    assert "result" not in response
    db.close()


@pytest.mark.parametrize(
    ("tip_source", "tip_config", "expected_error"),
    [
        ("browser_annotation", "{}", ANNOTATION_ERROR),
        ("tui", "[]", CLASSIFICATION_ERROR),
    ],
)
def test_resume_reclassifies_resolved_tip_and_closes_foreign_db(
    monkeypatch, tmp_path, tip_source, tip_config, expected_error
):
    import hermes_state
    from hermes_state import SessionDB

    profile_home = tmp_path / "profiles" / "review"
    profile_home.mkdir(parents=True)
    db = SessionDB(db_path=profile_home / "state.db")
    db.create_session("generic-root", source="tui")
    db.end_session("generic-root", "compression")
    db.create_session("resolved-tip", source=tip_source, parent_session_id="generic-root")
    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            "UPDATE sessions SET model_config=? WHERE id='resolved-tip'", (tip_config,)
        )
    db.append_message("resolved-tip", role="user", content="must not load")

    monkeypatch.setattr(server, "_profile_home", lambda _profile: profile_home)
    monkeypatch.setattr(hermes_state, "SessionDB", lambda **_kwargs: db)
    monkeypatch.setattr(
        server,
        "_find_live_session_by_key",
        lambda _key: pytest.fail("unclassified resolved tip reached live reuse"),
    )

    response = server.handle_request(
        {
            "id": "resume-tip",
            "method": "session.resume",
            "params": {"profile": "review", "session_id": "generic-root"},
        }
    )

    assert response is not None
    assert response["error"] == expected_error
    assert db._conn is None


def test_active_list_omits_live_annotation_and_unknown_without_touching_row_builders(
    monkeypatch,
):
    body_canary = "ANNOTATION BODY MUST NEVER APPEAR"
    sid_canary = "annotation-live-sid-canary"
    annotation = {
        "agent": object(),
        "history": [{"role": "user", "content": body_canary}],
        "history_lock": threading.Lock(),
        "session_key": "annotation-durable-sid-canary",
        "source": "browser_annotation",
        "transport": object(),
    }
    unknown = {
        "agent": object(),
        "history": [{"role": "user", "content": "UNKNOWN BODY CANARY"}],
        "history_lock": threading.Lock(),
        "session_key": "unknown-durable-sid-canary",
        "source": "tui",
    }
    generic = {
        "agent": None,
        "history": [{"role": "user", "content": "visible generic"}],
        "history_lock": threading.Lock(),
        "session_key": "generic-key",
        "source": "tui",
        "root_source": "tui",
    }
    monkeypatch.setitem(server._sessions, sid_canary, annotation)
    monkeypatch.setitem(server._sessions, "unknown-live-sid-canary", unknown)
    monkeypatch.setitem(server._sessions, "generic-live", generic)
    monkeypatch.setattr(server, "_get_db", lambda: None)

    response = server.handle_request(
        {"id": "active", "method": "session.active_list", "params": {}}
    )

    assert response is not None and "error" not in response
    serialized = repr(response)
    assert [row["id"] for row in response["result"]["sessions"]] == ["generic-live"]
    for canary in (
        body_canary,
        sid_canary,
        "annotation-durable-sid-canary",
        "UNKNOWN BODY CANARY",
        "unknown-durable-sid-canary",
    ):
        assert canary not in serialized


def test_active_list_omits_durably_classified_compressed_live_annotation(
    monkeypatch, tmp_path
):
    db = _compressed_annotation_db(tmp_path)
    body_canary = "COMPRESSED LIVE BODY CANARY"
    session = {
        "agent": None,
        "history": [{"role": "user", "content": body_canary}],
        "history_lock": threading.Lock(),
        "session_key": "annotation-compressed-2",
        "source": "tui",
        # No root_source: this is the legacy live-record path.
    }
    monkeypatch.setitem(server._sessions, "compressed-live-sid-canary", session)
    monkeypatch.setattr(server, "_get_db", lambda: db)

    response = server.handle_request(
        {"id": "active-compressed", "method": "session.active_list", "params": {}}
    )

    assert response is not None and "error" not in response
    assert response["result"]["sessions"] == []
    assert body_canary not in repr(response)
    assert "compressed-live-sid-canary" not in repr(response)
    assert session["root_source"] == "browser_annotation"
    db.close()


@pytest.mark.parametrize(
    ("source", "root_source", "expected_error"),
    [
        ("browser_annotation", None, ANNOTATION_ERROR),
        ("tui", None, CLASSIFICATION_ERROR),
    ],
)
def test_activate_refuses_before_transport_rebind_or_transcript_payload(
    monkeypatch, source, root_source, expected_error
):
    class _Transport:
        def write(self, _obj: dict) -> bool:
            return True

        def close(self) -> None:
            return None

    original_transport = _Transport()
    observing_transport = _Transport()
    body_canary = "ACTIVATE ANNOTATION BODY CANARY"
    session = {
        "agent": None,
        "history": [{"role": "user", "content": body_canary}],
        "history_lock": threading.Lock(),
        "session_key": "activate-durable-sid-canary",
        "source": source,
        "transport": original_transport,
    }
    if root_source is not None:
        session["root_source"] = root_source
    monkeypatch.setitem(server._sessions, "activate-live-sid-canary", session)
    monkeypatch.setattr(server, "_get_db", lambda: None)

    response = server.dispatch(
        {
            "id": "activate",
            "method": "session.activate",
            "params": {"session_id": "activate-live-sid-canary"},
        },
        observing_transport,
    )

    assert response is not None
    assert response["error"] == expected_error
    assert "result" not in response
    assert body_canary not in repr(response)
    assert "activate-durable-sid-canary" not in repr(response)
    assert session["transport"] is original_transport


def test_steer_never_calls_live_annotation_agent(monkeypatch):
    calls = []

    class _Agent:
        def steer(self, text):
            calls.append(text)
            return True

    session = {
        "agent": _Agent(),
        "history": [],
        "history_lock": threading.Lock(),
        "session_key": "annotation-steer-key",
        "source": "browser_annotation",
    }
    monkeypatch.setitem(server._sessions, "annotation-steer-live", session)

    response = server.handle_request(
        {
            "id": "steer",
            "method": "session.steer",
            "params": {"session_id": "annotation-steer-live", "text": "INJECTION CANARY"},
        }
    )

    assert response is not None
    assert response["error"] == ANNOTATION_ERROR
    assert calls == []
    assert "INJECTION CANARY" not in repr(response)


@pytest.mark.parametrize(
    "method",
    [
        "prompt.background",
        "preview.restart",
        "approval.respond",
        "slash.exec",
        "handoff.request",
        "handoff.state",
        "config.set",
        "config.get",
        "commands.catalog",
        "command.resolve",
        "command.dispatch",
    ],
)
@pytest.mark.parametrize(
    ("source", "root_source", "expected_error"),
    [
        ("browser_annotation", None, ANNOTATION_ERROR),
        ("tui", None, CLASSIFICATION_ERROR),
    ],
)
def test_central_live_sid_guard_refuses_generic_rpc_before_handler(
    monkeypatch, method, source, root_source, expected_error
):
    """Every generic live-SID route is denied centrally, not handler by handler."""
    sid = "central-guard-live"
    session = {
        "session_key": "central-guard-durable",
        "source": source,
    }
    if root_source is not None:
        session["root_source"] = root_source
    monkeypatch.setitem(server._sessions, sid, session)
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setitem(
        server._methods,
        method,
        lambda *_args, **_kwargs: pytest.fail(f"{method} handler ran before live-SID guard"),
    )

    response = server.handle_request(
        {"id": "central-guard", "method": method, "params": {"session_id": sid}}
    )

    assert response is not None
    assert response["error"] == expected_error


def test_central_live_sid_guard_allows_annotation_terminal_resize(monkeypatch):
    sid = "annotation-resize-live"
    session = {
        "session_key": "annotation-resize-durable",
        "source": "browser_annotation",
        "cols": 80,
    }
    monkeypatch.setitem(server._sessions, sid, session)

    response = server.handle_request(
        {
            "id": "resize",
            "method": "terminal.resize",
            "params": {"session_id": sid, "cols": 132},
        }
    )

    assert response == {"jsonrpc": "2.0", "id": "resize", "result": {"cols": 132}}
    assert session["cols"] == 132


def test_central_live_sid_guard_allows_annotation_session_close(monkeypatch):
    sid = "annotation-close-live"
    session = {
        "session_key": "annotation-close-durable",
        "source": "browser_annotation",
    }
    monkeypatch.setitem(server._sessions, sid, session)
    torn_down = []
    monkeypatch.setattr(
        server,
        "_teardown_session",
        lambda closed, *, end_reason: torn_down.append((closed, end_reason)),
    )

    response = server.handle_request(
        {"id": "close", "method": "session.close", "params": {"session_id": sid}}
    )

    assert response == {"jsonrpc": "2.0", "id": "close", "result": {"closed": True}}
    assert sid not in server._sessions
    assert torn_down == [(session, "tui_close")]


@pytest.mark.parametrize(
    "method",
    [
        "session.cwd.set",
        "session.title",
        "session.usage",
        "session.context_breakdown",
        "session.status",
        "session.history",
        "session.undo",
        "session.compress",
        "session.save",
        "session.interrupt",
    ],
)
def test_other_generic_live_semantic_endpoints_refuse_annotation_before_agent(
    monkeypatch, method
):
    calls = []

    class _Agent:
        def interrupt(self):
            calls.append("interrupt")

    session = {
        "agent": _Agent(),
        "history": [{"role": "user", "content": "SEMANTIC BODY CANARY"}],
        "history_lock": threading.Lock(),
        "running": True,
        "session_key": "annotation-semantic-key",
        "source": "browser_annotation",
    }
    monkeypatch.setitem(server._sessions, "annotation-semantic-live", session)
    monkeypatch.setattr(
        server,
        "_start_agent_build",
        lambda *_args, **_kwargs: pytest.fail("annotation endpoint started agent runtime"),
    )
    params = {"session_id": "annotation-semantic-live", "cwd": "/tmp", "title": "changed"}

    response = server.handle_request({"id": "semantic", "method": method, "params": params})

    assert response is not None
    assert response["error"] == ANNOTATION_ERROR
    assert calls == []
    assert "SEMANTIC BODY CANARY" not in repr(response)


def test_delete_refuses_durable_annotation_before_active_check_or_mutation(monkeypatch):
    class _DB:
        def get_session(self, session_id):
            assert session_id == "annotation-delete-key"
            return {"id": session_id, "source": "browser_annotation"}

        def delete_session(self, *_args, **_kwargs):
            pytest.fail("generic delete mutated annotation lineage")

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    response = server.handle_request(
        {
            "id": "delete",
            "method": "session.delete",
            "params": {"session_id": "annotation-delete-key"},
        }
    )

    assert response is not None
    assert response["error"] == ANNOTATION_ERROR
