from datetime import datetime, timezone

import pytest

from gateway.conversation_texture_v2 import (
    TextureConfig,
    _replay_prior_class,
    _seriousness,
    compile_turn_guidance,
)


def cfg(**kwargs):
    return TextureConfig(enabled=True, **kwargs)


def rows(*items):
    return [{"role": role, "content": content, **extra} for role, content, extra in items]


def field(guidance, name):
    return next(line.split(": ", 1)[1] for line in guidance.splitlines() if line.startswith(name + ": "))


def test_long_thread_rolls_do_not_freeze():
    history = []
    plans = []
    for i in range(30):
        text = f"mundane turn {i} lol"
        guidance = compile_turn_guidance(message=text, history=history, session_key="long", config=cfg())
        plans.append((field(guidance, "response_class"), field(guidance, "target_effort")))
        history += [{"role": "user", "content": text}, {"role": "assistant", "content": "lol"}]
    assert len(set(plans[5:])) > 1


@pytest.mark.parametrize("text", [
    "the process died", "kill the hospital cron job", "is this unsafe to run in prod",
    "the worker is spiraling CPU", "the abuse detector killed the build",
])
def test_technical_language_is_not_serious(text):
    guidance = compile_turn_guidance(message=text, history=[], session_key="s", config=cfg())
    assert "serious_mode: off" in guidance


@pytest.mark.parametrize("text", ["should i go to the funeral", "what do i tell her after the biopsy", "can i visit him in hospice"])
def test_punctuation_free_serious_questions_require_answer(text):
    guidance = compile_turn_guidance(message=text, history=[], session_key="s", config=cfg())
    assert "register: serious" in guidance
    assert "response_class: answer" in guidance


def test_long_gap_and_topic_pivot_reset_serious_hysteresis():
    history = rows(
        ("user", "my dog died", {"timestamp": 1_700_000_000}),
        ("assistant", "I'm sorry.", {"timestamp": 1_700_000_010}),
    )
    guidance = compile_turn_guidance(
        message="anyway what should i get for dinner", history=history, session_key="s",
        config=cfg(), now_ts=1_700_000_000 + 6 * 3600, timezone_name="America/New_York",
    )
    assert "serious_mode: off" in guidance
    assert "fresh beat" in guidance


def test_topic_pivot_alone_resets_serious_hysteresis():
    history = rows(("user", "my dog died", {}), ("assistant", "I'm sorry.", {}))
    guidance = compile_turn_guidance(
        message="anyway what should i get for dinner", history=history,
        session_key="s", config=cfg(),
    )
    assert "serious_mode: off" in guidance
    assert "register: advice" in guidance


def test_visible_message_gap_ignores_tool_timestamp_and_uses_explicit_timezone():
    history = rows(
        ("user", "yo", {"timestamp": 1_700_000_000}),
        ("assistant", "yo", {"timestamp": 1_700_000_010}),
        ("tool", "background result", {"timestamp": 1_700_021_000}),
    )
    guidance = compile_turn_guidance(
        message="hey", history=history, session_key="s", config=cfg(),
        now_ts=1_700_000_010 + 6 * 3600, timezone_name="America/New_York",
    )
    assert "fresh beat" in guidance
    expected_hour = datetime.fromtimestamp(1_700_021_610, timezone.utc).astimezone().hour
    # The assertion is host-independent: the emitted timezone must be explicit.
    assert "timezone: America/New_York" in guidance
    assert "timezone: host-local" not in guidance


@pytest.mark.parametrize("text", ["$540 for a lamp", "the total is 500"])
def test_bare_5xx_like_numbers_are_not_tasks(text):
    guidance = compile_turn_guidance(message=text, history=[], session_key="s", config=cfg())
    assert "register: task" not in guidance


@pytest.mark.parametrize("text", ["nginx returned 502", "HTTP status 503", "server has a 500 error"])
def test_contextual_5xx_is_task(text):
    guidance = compile_turn_guidance(message=text, history=[], session_key="s", config=cfg())
    assert "register: task" in guidance


def test_explicit_word_count_writing_request_is_high_effort_task():
    guidance = compile_turn_guidance(
        message="write this in 500 words", history=[], session_key="s", config=cfg()
    )
    assert "register: task" in guidance
    assert "target_effort: high" in guidance


def test_declarative_casual_turns_rarely_select_questions():
    questions = 0
    for index in range(500):
        guidance = compile_turn_guidance(
            message="anyway im home", history=[], session_key=f"s-{index}", config=cfg()
        )
        questions += field(guidance, "response_class") == "question"
    assert questions <= 20


def test_burst_second_slot_is_usually_observation_not_question():
    questions = 0
    bursts = 0
    for index in range(500):
        guidance = compile_turn_guidance(
            message="wait no way!!", history=[], session_key=f"burst-{index}",
            config=cfg(burst_probability=1.0),
        )
        slots = [line for line in guidance.splitlines() if line.startswith("slot_")]
        if len(slots) == 2:
            bursts += 1
            questions += "class=question" in slots[1]
    assert bursts > 100
    assert questions / bursts <= .16


def test_burst_probability_controls_semantic_burst_shape():
    eligible = 0
    for index in range(500):
        off = compile_turn_guidance(
            message="wait no way!!", history=[], session_key=f"burst-knob-{index}",
            config=cfg(burst_probability=0.0),
        )
        on = compile_turn_guidance(
            message="wait no way!!", history=[], session_key=f"burst-knob-{index}",
            config=cfg(burst_probability=1.0),
        )
        assert field(off, "bubble_count") == "1"
        if field(on, "response_class") == "reaction":
            eligible += 1
            assert field(on, "bubble_count") == "2"
    assert eligible > 100


def test_task_continuation_requires_unresolved_assistant_state():
    unresolved = rows(
        ("user", "remind me to call the pharmacy", {}),
        ("assistant", "What time?", {}),
    )
    guidance = compile_turn_guidance(message="morning", history=unresolved, session_key="s", config=cfg())
    assert "register: task" in guidance

    completed = unresolved + rows(
        ("user", "9", {}),
        ("assistant", "Done, reminder set for 9.", {}),
        ("tool", '{"success": true}', {}),
    )
    guidance = compile_turn_guidance(message="anyway lol", history=completed, session_key="s", config=cfg())
    assert "register: casual" in guidance

    unavailable = rows(
        ("user", "nginx returned 502", {}),
        ("assistant", "I cannot access that server from here.", {}),
    )
    guidance = compile_turn_guidance(message="the show was wild", history=unavailable, session_key="s", config=cfg())
    assert "register: casual" in guidance


def test_pending_assistant_tool_call_is_task_state_without_user_regex():
    history = [
        {"role": "user", "content": "please do it"},
        {"role": "assistant", "content": "", "tool_calls": [{"name": "reminder"}]},
    ]
    guidance = compile_turn_guidance(message="tomorrow", history=history, session_key="s", config=cfg())
    assert "register: task" in guidance


def test_untriggered_casual_reaction_never_splits_randomly():
    for index in range(200):
        guidance = compile_turn_guidance(
            message="thinking thai", history=[], session_key=f"plain-{index}",
            config=cfg(burst_probability=1.0, follow_through_probability=0.0),
        )
        assert field(guidance, "bubble_count") == "1"


def test_follow_through_adds_related_observation_without_interviewing():
    found = 0
    for index in range(500):
        guidance = compile_turn_guidance(
            message="what is the real thing then?", history=[],
            session_key=f"follow-{index}",
            config=cfg(follow_through_probability=1.0),
        )
        slots = [line for line in guidance.splitlines() if line.startswith("slot_")]
        if field(guidance, "response_class") in {"answer", "observation"}:
            found += 1
            assert field(guidance, "bubble_count") == "2"
            assert "class=observation" in slots[1]
            assert "class=question" not in slots[1]
            assert "Make it a statement, not a question" in guidance
    assert found > 100


def test_follow_through_keeps_disposable_reactions_single_bubble():
    found = 0
    for index in range(500):
        guidance = compile_turn_guidance(
            message="lol same", history=[], session_key=f"reaction-{index}",
            config=cfg(follow_through_probability=1.0),
        )
        if field(guidance, "response_class") == "reaction":
            found += 1
            assert field(guidance, "bubble_count") == "1"
    assert found > 100


@pytest.mark.parametrize("message", ["thanks", "my aunt died", "remind me to call tomorrow"])
def test_follow_through_respects_closure_serious_and_task_turns(message):
    guidance = compile_turn_guidance(
        message=message, history=[], session_key="excluded",
        config=cfg(follow_through_probability=1.0),
    )
    assert field(guidance, "bubble_count") == "1"


def test_response_plan_never_contradicts_slot_count_or_class():
    for key in [f"key-{i}" for i in range(100)]:
        guidance = compile_turn_guidance(
            message="wait she found out??", history=[], session_key=key,
            config=cfg(burst_probability=1.0),
        )
        slot_lines = [line for line in guidance.splitlines() if line.startswith("slot_")]
        assert len(slot_lines) == int(field(guidance, "bubble_count"))
        if field(guidance, "response_class") == "craft":
            assert len(slot_lines) == 1
        assert not ("no question" in guidance.lower() and any("class=question" in x for x in slot_lines))


def test_response_classes_false_emits_working_legacy_guidance():
    guidance = compile_turn_guidance(
        message="look at this lol", history=[], session_key="s", config=cfg(response_classes=False)
    )
    assert "response_class: legacy" in guidance
    assert "legacy_effort_rule:" in guidance
    assert "1-5 words" in guidance or "one natural thought" in guidance
    assert "slot_" not in guidance


def test_response_classes_false_preserves_task_truthfulness():
    guidance = compile_turn_guidance(
        message="remind me to call tomorrow", history=[], session_key="s",
        config=cfg(response_classes=False),
    )
    assert "response_class: legacy" in guidance
    assert "never claim completion before tool success" in guidance


def test_feature_matrix_has_no_plan_contradictions():
    messages = [
        "lol", "thanks", "should i sell it", "my aunt died", "should i go to the funeral",
        "wait what??", "remind me to call", "the process died", "nginx returned 502",
        "anyway what should i eat",
    ]
    histories = [
        [],
        rows(("user", "my dog died", {"timestamp": 100}), ("assistant", "I'm sorry", {"timestamp": 101})),
        rows(("user", "remind me to call", {}), ("assistant", "What time?", {})),
    ]
    for message in messages:
        for history in histories:
            for response_classes in (True, False):
                guidance = compile_turn_guidance(
                    message=message, history=history, session_key="matrix",
                    config=cfg(response_classes=response_classes, burst_probability=1),
                    now_ts=30_000, timezone_name="UTC",
                )
                slots = [line for line in guidance.splitlines() if line.startswith("slot_")]
                if response_classes:
                    assert len(slots) == int(field(guidance, "bubble_count"))
                else:
                    assert not slots
                if "They are winding down" in guidance:
                    assert not any("class=question" in slot for slot in slots)


def test_exemplars_require_response_class_match():
    # Advice direct questions deterministically use answer.
    guidance = compile_turn_guidance(
        message="should i sell the car", history=[], session_key="s", config=cfg(exemplar_count=3),
        exemplars=[
            {"register": "advice", "response_class": "question", "tags": ["advice"], "user": "Wrong", "assistant": "Why?"},
            {"register": "advice", "response_class": "answer", "tags": ["advice"], "user": "Right", "assistant": "Sell it."},
        ],
    )
    assert "User: Right" in guidance
    assert "User: Wrong" not in guidance


def test_replay_recomputes_prior_craft_ineligibility():
    history = rows(
        ("user", "my dog died", {}),
        ("assistant", "I'm sorry.", {}),
        ("user", "i keep expecting him", {}),
        ("assistant", "Yeah :/", {}),
    )
    assert _replay_prior_class(history, "s", cfg()) != "craft"


def test_current_message_can_be_excluded_by_message_id():
    history = rows(
        ("user", "older", {"id": "old", "timestamp": 100}),
        ("assistant", "ok", {"timestamp": 101}),
        ("user", "current", {"id": "new", "timestamp": 200}),
    )
    guidance = compile_turn_guidance(
        message="current", history=history, current_message_id="new", now_ts=200,
        session_key="s", config=cfg(),
    )
    assert "turn_index: 1" in guidance


def test_compacted_history_is_deterministic_and_durable_ordinal_can_preserve_seed():
    full = rows(*[("user", f"u{i}", {}) if j % 2 == 0 else ("assistant", "ok", {}) for i in range(12) for j in range(2)])
    compacted = full[-8:]
    a = compile_turn_guidance(message="yo", history=compacted, session_key="s", config=cfg(), turn_ordinal=12)
    b = compile_turn_guidance(message="yo", history=full, session_key="s", config=cfg(), turn_ordinal=12)
    assert field(a, "seed_turn_ordinal") == field(b, "seed_turn_ordinal") == "12"


def test_seriousness_api_retained():
    assert _seriousness("my dog died", []) == 1
    assert _seriousness("the process died", []) == 0
