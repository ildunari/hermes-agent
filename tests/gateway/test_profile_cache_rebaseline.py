"""The post-turn count must belong to the cached agent's profile database."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from hermes_state import AsyncSessionDB, SessionDB


@pytest.mark.asyncio
async def test_rebaseline_uses_agent_database_and_preserves_foreign_write_guard(tmp_path):
    root = SessionDB(db_path=tmp_path / "root.db")
    profile = SessionDB(db_path=tmp_path / "poke.db")
    try:
        for db in (root, profile):
            db.create_session("same-id", source="bluebubbles")
        root.append_message("same-id", role="user", content="old root row")
        for role in ("user", "assistant"):
            profile.append_message("same-id", role=role, content="profile turn")
        runner = GatewayRunner.__new__(GatewayRunner)
        runner._session_db = AsyncSessionDB(root)
        runner._agent_cache_lock = threading.Lock()
        agent = SimpleNamespace(_session_db=profile, _db_flush_scan_prefix=[
            {"_row_id": row["id"]} for row in profile.get_messages("same-id")])
        runner._agent_cache = {"poke-dm": (agent, "sig", 0, "same-id")}
        runner._init_cached_agent_for_turn = lambda *args: None
        turn = TurnRunner.__new__(TurnRunner)
        turn._runner = runner
        turn._ctx = SimpleNamespace(session_key="poke-dm", session_id="same-id", _interrupt_depth=0)

        await runner._refresh_agent_cache_message_count("poke-dm", "same-id")
        assert runner._agent_cache["poke-dm"][2] == profile.get_session("same-id")["message_count"]
        lookup = lambda count: turn._lookup_cached_agent(
            "sig", runner._agent_cache_lock, runner._agent_cache, 10, "same-id", False, count)
        assert lookup(2).reused
        profile.append_message("same-id", role="user", content="another process")
        assert not lookup(3).reused
    finally:
        root.close()
        profile.close()


@pytest.mark.asyncio
async def test_foreign_append_during_count_read_is_not_absorbed(tmp_path):
    db = SessionDB(db_path=tmp_path / "profile.db")
    entered, release = threading.Event(), threading.Event()
    task = None
    try:
        db.create_session("sid", source="bluebubbles")
        db.append_message("sid", role="user", content="own user")
        last_id = db.append_message("sid", role="assistant", content="own assistant")
        original = db.get_session

        def blocked_read(sid):
            entered.set()
            assert release.wait(5)
            return original(sid)

        db.get_session = blocked_read
        runner = GatewayRunner.__new__(GatewayRunner)
        runner._agent_cache_lock = threading.Lock()
        agent = SimpleNamespace(_session_db=db, _db_flush_scan_prefix=[{"_row_id": last_id}])
        runner._agent_cache = {"poke-dm": (agent, "sig", 0, "sid")}
        task = asyncio.create_task(runner._refresh_agent_cache_message_count("poke-dm", "sid"))
        assert await asyncio.to_thread(entered.wait, 5)
        db.append_message("sid", role="user", content="foreign writer")
        release.set()
        await task
        assert runner._agent_cache["poke-dm"][2] != original("sid")["message_count"]
    finally:
        release.set()
        if task:
            await task
        db.close()
