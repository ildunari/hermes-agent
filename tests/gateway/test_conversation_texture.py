from gateway.conversation_texture import TextureConfig, compile_turn_guidance, sample_effort


def test_turn_index_does_not_freeze_after_four_user_turns():
    config = TextureConfig(enabled=True)
    history = []
    efforts = []
    guidance_values = []
    for i in range(30):
        guidance = compile_turn_guidance(
            message=f"random banter number {i} lol",
            history=history,
            session_key="long-session",
            config=config,
            exemplars=[],
        )
        effort_line = next(line for line in guidance.splitlines() if line.startswith("target_effort:"))
        efforts.append(effort_line)
        guidance_values.append(guidance)
        history.extend(
            [
                {"role": "user", "content": f"random banter number {i} lol"},
                {"role": "assistant", "content": "Ok"},
            ]
        )

    # The old bug used len(_recent(...)), so every roll after turn four was identical.
    assert len(set(efforts[4:])) > 1
    assert len(set(guidance_values[4:])) > 1


def test_effort_sampling_is_deterministic_and_low_weighted():
    config = TextureConfig(enabled=True, low_weight=70, medium_weight=25, high_weight=5)
    values = [sample_effort("session-a", i, config) for i in range(200)]
    assert values == [sample_effort("session-a", i, config) for i in range(200)]
    assert values.count("low") > values.count("medium") > values.count("high")
    assert values.count("low") >= 120


def test_low_effort_guidance_explicitly_permits_boring_fragments():
    config = TextureConfig(enabled=True, low_weight=100, medium_weight=0, high_weight=0)
    guidance = compile_turn_guidance(
        message="look at this lol",
        history=[],
        session_key="s",
        config=config,
        exemplars=[{"tags": ["casual"], "user": "made it", "assistant": "Okii"}],
    )
    assert "target_effort: low" in guidance
    assert "1-5 words" in guidance
    assert "disposable reply" in guidance
    assert "User: made it" in guidance


def test_serious_hysteresis_survives_followup_and_suppresses_jokes():
    config = TextureConfig(enabled=True)
    history = [
        {"role": "user", "content": "my dog died this morning"},
        {"role": "assistant", "content": "I’m so sorry."},
    ]
    guidance = compile_turn_guidance(
        message="i keep expecting him at the door",
        history=history,
        session_key="s",
        config=config,
        exemplars=[],
    )
    assert "serious_mode: on" in guidance
    assert "No joke" in guidance


def test_prior_crafted_reply_forces_plain_followup():
    config = TextureConfig(enabled=True)
    history = [
        {"role": "user", "content": "lamp was 140 lol"},
        {"role": "assistant", "content": "A $140 shrine to seeing your keyboard."},
    ]
    guidance = compile_turn_guidance(
        message="dont enable me",
        history=history,
        session_key="s",
        config=config,
        exemplars=[],
    )
    assert "joke_cooldown: on" in guidance
    assert "plain and literal" in guidance


def test_style_complaint_is_not_new_evidence():
    config = TextureConfig(enabled=True)
    history = [
        {"role": "user", "content": "should i sell the car"},
        {"role": "assistant", "content": "Keep it for now."},
    ]
    guidance = compile_turn_guidance(
        message="thats such an ai answer",
        history=history,
        session_key="s",
        config=config,
        exemplars=[],
    )
    assert "style_complaint: yes" in guidance
    assert "not new evidence" in guidance


def test_task_context_forces_task_register_and_blocks_fake_completion():
    config = TextureConfig(enabled=True, low_weight=100, medium_weight=0, high_weight=0)
    history = [
        {"role": "user", "content": "remind me to call the pharmacy"},
        {"role": "assistant", "content": "When?"},
        {"role": "user", "content": "tomorrow"},
        {"role": "assistant", "content": "What time?"},
    ]
    guidance = compile_turn_guidance(
        message="morning", history=history, session_key="s", config=config, exemplars=[]
    )
    assert "register: task" in guidance
    assert "target_effort: medium" in guidance
    assert "must never suppress a required tool call" in guidance
    assert "Ask for missing required parameters" in guidance


def test_advice_is_never_randomly_downgraded_to_low():
    config = TextureConfig(enabled=True, low_weight=100, medium_weight=0, high_weight=0)
    guidance = compile_turn_guidance(
        message="do i tell his wife", history=[], session_key="s", config=config, exemplars=[]
    )
    assert "register: advice" in guidance
    assert "target_effort: medium" in guidance


def test_serious_turn_does_not_receive_casual_examples():
    config = TextureConfig(enabled=True, exemplar_count=3)
    guidance = compile_turn_guidance(
        message="my dog died",
        history=[],
        session_key="s",
        config=config,
        exemplars=[
            {"tags": ["casual"], "user": "made it", "assistant": "Okii"},
            {"tags": ["serious"], "user": "ugh sorry", "assistant": ":/"},
        ],
    )
    assert "User: ugh sorry" in guidance
    assert "User: made it" not in guidance


def test_explicit_technical_order_is_high_effort_task():
    config = TextureConfig(enabled=True, low_weight=100, medium_weight=0, high_weight=0)
    guidance = compile_turn_guidance(
        message="give me the fastest isolation order",
        history=[], session_key="s", config=config, exemplars=[]
    )
    assert "register: task" in guidance
    assert "target_effort: high" in guidance


def test_burst_guidance_is_optional_not_forced():
    config = TextureConfig(enabled=True, burst_probability=1.0)
    guidance = compile_turn_guidance(
        message="wait she found out at the party??",
        history=[],
        session_key="burst",
        config=config,
        exemplars=[],
    )
    assert "bubble_shape: burst_allowed" in guidance
    assert "blank line" in guidance
