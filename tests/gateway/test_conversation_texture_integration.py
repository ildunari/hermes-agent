import inspect

import gateway.conversation_texture_v2 as texture_v2
from gateway.run import _compile_conversation_texture_prompt, _with_conversation_texture


def raw(**extra):
    return {"enabled": True, **extra}


def test_engine_defaults_to_v1():
    prompt = _compile_conversation_texture_prompt(
        texture_raw=raw(), message="yo", history=[], session_key="s",
        user_config={}, now_ts=100,
    )
    assert 'engine="v2"' not in prompt
    assert "target_effort:" in prompt


def test_explicit_v2_gets_raw_timestamps_timezone_and_current_exclusion():
    prompt = _compile_conversation_texture_prompt(
        texture_raw=raw(engine="v2", timezone="America/New_York"),
        message="current",
        history=[
            {"role": "user", "content": "old", "timestamp": 100, "id": "old"},
            {"role": "assistant", "content": "ok", "timestamp": 101},
            {"role": "user", "content": "current", "timestamp": 200, "id": "new"},
        ],
        session_key="s", user_config={}, now_ts=200, current_message_id="new",
    )
    assert 'engine="v2"' in prompt
    assert "turn_index: 1" in prompt
    assert "timezone: America/New_York" in prompt


def test_cached_history_and_compacted_turn_ordinal_contract():
    prompt = _compile_conversation_texture_prompt(
        texture_raw=raw(engine="v2"), message="yo",
        history=[{"role": "user", "content": "cached tail"}], session_key="s",
        user_config={}, now_ts=200, turn_ordinal=50,
    )
    assert "seed_turn_ordinal: 50" in prompt


def test_texture_failure_fails_open(monkeypatch):
    def explode(**kwargs):
        raise RuntimeError("compiler broke")

    monkeypatch.setattr(texture_v2, "compile_turn_guidance", explode)
    prompt = _compile_conversation_texture_prompt(
        texture_raw=raw(engine="v2"), message="must survive", history=[],
        session_key="s", user_config={}, now_ts=100,
    )
    assert prompt == ""


def test_private_texture_is_excluded_from_cache_signature_input():
    cached, execution = _with_conversation_texture("static channel prompt", "<turn_texture private=\"true\">x</turn_texture>")
    assert cached == "static channel prompt"
    assert "turn_texture" not in cached
    assert "turn_texture" in execution


def test_gateway_callsite_passes_clock_and_keeps_default_v1():
    source = inspect.getsource(__import__("gateway.run", fromlist=["x"]))
    assert "persist_user_timestamp or time.time()" in source
    assert 'str(texture_raw.get("engine") or "v1")' in source
