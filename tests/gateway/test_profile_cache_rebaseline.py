"""The post-turn count must belong to the cached agent's profile database."""
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
        agent = SimpleNamespace(_session_db=profile)
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
