from gateway.run import _resolve_platform_default_personality_prompt


def test_resolves_platform_default_personality_from_agent_config():
    config = {
        "agent": {
            "personalities": {
                "imessage": {
                    "system_prompt": "write like a text",
                    "tone": "casual",
                    "style": "short",
                }
            }
        },
        "platforms": {
            "bluebubbles": {
                "extra": {
                    "default_personality": "imessage",
                }
            }
        },
    }

    prompt = _resolve_platform_default_personality_prompt(config, "bluebubbles")

    assert "write like a text" in prompt
    assert "Tone: casual" in prompt
    assert "Style: short" in prompt


def test_missing_platform_default_personality_is_noop():
    config = {
        "agent": {"personalities": {"imessage": "text prompt"}},
        "platforms": {"telegram": {"extra": {"default_personality": "imessage"}}},
    }

    assert _resolve_platform_default_personality_prompt(config, "bluebubbles") == ""
