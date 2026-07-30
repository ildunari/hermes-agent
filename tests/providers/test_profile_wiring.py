"""Profile-path parity tests: verify profile path produces identical output to legacy flags.

Each test calls build_kwargs twice — once with legacy flags, once with provider_profile —
and asserts the output is identical. This catches any behavioral drift between the two paths.
"""

import pytest
from agent.transports.chat_completions import ChatCompletionsTransport
from providers import get_provider_profile
from providers.base import ProviderProfile


@pytest.fixture
def transport():
    return ChatCompletionsTransport()


def _msgs():
    return [{"role": "user", "content": "hello"}]


def _max_tokens_fn(n):
    return {"max_completion_tokens": n}


class TestCustomProviderProfile:
    def test_custom_named_provider_uses_custom_profile(self):
        assert get_provider_profile("custom:RTX").name == "custom"

    def test_qwopus_disables_llamacpp_thinking_for_plain_text(self, transport):
        kwargs = transport.build_kwargs(
            model="qwopus-gpu",
            messages=_msgs(),
            tools=None,
            provider_profile=get_provider_profile("custom:RTX"),
            base_url="http://endpoint.invalid/v1",
        )

        assert kwargs["extra_body"]["think"] is False
        assert kwargs["extra_body"]["enable_thinking"] is False
        assert kwargs["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
        assert kwargs["extra_body"]["reasoning"] == {"enabled": False}

    def test_qwopus_keeps_thinking_template_for_tool_turns(self, transport):
        kwargs = transport.build_kwargs(
            model="qwopus-gpu",
            messages=_msgs(),
            tools=[{"type": "function", "function": {"name": "session_search", "parameters": {"type": "object"}}}],
            provider_profile=get_provider_profile("custom:RTX"),
            base_url="http://endpoint.invalid/v1",
        )

        assert "extra_body" not in kwargs

    def test_atomic_qwen_keeps_configured_reasoning_for_plain_text(self, transport):
        kwargs = transport.build_kwargs(
            model="qwen36-ablit-atomic",
            messages=_msgs(),
            tools=None,
            provider_profile=get_provider_profile("custom:Atomic"),
            base_url="http://endpoint.invalid/v1",
            reasoning_config={"enabled": True, "effort": "high"},
        )
        assert kwargs["reasoning_effort"] == "high"
        assert "think" not in kwargs.get("extra_body", {})
        assert "enable_thinking" not in kwargs.get("extra_body", {})
        assert "chat_template_kwargs" not in kwargs.get("extra_body", {})


class TestNvidiaProfileParity:
    def test_max_tokens_match(self, transport):
        """NVIDIA profile sets max_tokens=16384; legacy flag is removed."""
        profile = transport.build_kwargs(
            model="nvidia/nemotron", messages=_msgs(), tools=None,
            provider_profile=get_provider_profile("nvidia"),
            max_tokens_param_fn=_max_tokens_fn,
        )
        assert profile["max_completion_tokens"] == 16384


class TestKimiProfileParity:
    def test_temperature_omitted(self, transport):
        legacy = transport.build_kwargs(
            model="kimi-k2", messages=_msgs(), tools=None,
            provider_profile=get_provider_profile("kimi-coding"), omit_temperature=True,
        )
        profile = transport.build_kwargs(
            model="kimi-k2", messages=_msgs(), tools=None,
            provider_profile=get_provider_profile("kimi"),
        )
        assert "temperature" not in legacy
        assert "temperature" not in profile


    def test_thinking_enabled(self, transport):
        # xor contract: explicit effort → reasoning_effort only, no thinking.
        rc = {"enabled": True, "effort": "high"}
        legacy = transport.build_kwargs(
            model="kimi-k2", messages=_msgs(), tools=None,
            provider_profile=get_provider_profile("kimi-coding"), reasoning_config=rc,
        )
        profile = transport.build_kwargs(
            model="kimi-k2", messages=_msgs(), tools=None,
            provider_profile=get_provider_profile("kimi"),
            reasoning_config=rc,
        )
        assert profile["reasoning_effort"] == legacy["reasoning_effort"] == "high"
        assert "thinking" not in profile.get("extra_body", {})
        assert "thinking" not in legacy.get("extra_body", {})



    def test_kimi_k3_maps_every_effort_to_max_without_thinking(self, transport):
        profile = get_provider_profile("kimi-coding")

        for reasoning_config in (
            None,
            {"enabled": False},
            {"enabled": True, "effort": "low"},
            {"enabled": True, "effort": "medium"},
            {"enabled": True, "effort": "high"},
            {"enabled": True, "effort": "max"},
        ):
            kwargs = transport.build_kwargs(
                model="kimi-k3",
                messages=_msgs(),
                tools=None,
                provider_profile=profile,
                reasoning_config=reasoning_config,
            )

            assert kwargs["reasoning_effort"] == "max"
            assert "thinking" not in kwargs.get("extra_body", {})

    def test_kimi_k3_finalizer_overrides_invalid_caller_fields(self, transport):
        kwargs = transport.build_kwargs(
            model="moonshotai/kimi-k3",
            messages=_msgs(),
            tools=None,
            provider_profile=get_provider_profile("kimi-coding"),
            extra_body_additions={"thinking": {"type": "enabled"}, "keep": True},
            request_overrides={"reasoning_effort": "low"},
        )

        assert kwargs["reasoning_effort"] == "max"
        assert kwargs["extra_body"] == {"keep": True}


class TestOpenRouterProfileParity:
    def test_provider_preferences(self, transport):
        prefs = {"allow": ["anthropic"]}
        legacy = transport.build_kwargs(
            model="anthropic/claude-sonnet-4.6", messages=_msgs(), tools=None,
            provider_profile=get_provider_profile("openrouter"), provider_preferences=prefs,
        )
        profile = transport.build_kwargs(
            model="anthropic/claude-sonnet-4.6", messages=_msgs(), tools=None,
            provider_profile=get_provider_profile("openrouter"),
            provider_preferences=prefs,
        )
        assert profile["extra_body"]["provider"] == legacy["extra_body"]["provider"]

    def test_reasoning_full_config(self, transport):
        rc = {"enabled": True, "effort": "high"}
        legacy = transport.build_kwargs(
            model="deepseek/deepseek-chat", messages=_msgs(), tools=None,
            provider_profile=get_provider_profile("openrouter"), supports_reasoning=True, reasoning_config=rc,
        )
        profile = transport.build_kwargs(
            model="deepseek/deepseek-chat", messages=_msgs(), tools=None,
            provider_profile=get_provider_profile("openrouter"),
            supports_reasoning=True, reasoning_config=rc,
        )
        assert profile["extra_body"]["reasoning"] == legacy["extra_body"]["reasoning"]



class TestNousProfileParity:
    def test_tags(self, transport):
        legacy = transport.build_kwargs(
            model="hermes-3", messages=_msgs(), tools=None, provider_profile=get_provider_profile("nous"),
        )
        profile = transport.build_kwargs(
            model="hermes-3", messages=_msgs(), tools=None,
            provider_profile=get_provider_profile("nous"),
        )
        assert profile["extra_body"]["tags"] == legacy["extra_body"]["tags"]



class TestQwenProfileParity:

    def test_vl_high_resolution(self, transport):
        legacy = transport.build_kwargs(
            model="qwen3.5", messages=_msgs(), tools=None, provider_profile=get_provider_profile("qwen-oauth"),
        )
        profile = transport.build_kwargs(
            model="qwen3.5", messages=_msgs(), tools=None,
            provider_profile=get_provider_profile("qwen"),
        )
        assert profile["extra_body"]["vl_high_resolution_images"] == legacy["extra_body"]["vl_high_resolution_images"]

    def test_metadata_top_level(self, transport):
        meta = {"sessionId": "s123", "promptId": "p456"}
        legacy = transport.build_kwargs(
            model="qwen3.5", messages=_msgs(), tools=None,
            provider_profile=get_provider_profile("qwen-oauth"), qwen_session_metadata=meta,
        )
        profile = transport.build_kwargs(
            model="qwen3.5", messages=_msgs(), tools=None,
            provider_profile=get_provider_profile("qwen"),
            qwen_session_metadata=meta,
        )
        assert profile["metadata"] == legacy["metadata"] == meta
        assert "metadata" not in profile.get("extra_body", {})



class TestDeveloperRoleParity:
    """Developer role swap must work on BOTH legacy and profile paths."""

    def test_legacy_path_swaps_for_gpt5(self, transport):
        msgs = [{"role": "system", "content": "Be helpful"}, {"role": "user", "content": "hi"}]
        kw = transport.build_kwargs(
            model="gpt-5.4", messages=msgs, tools=None,
        )
        assert kw["messages"][0]["role"] == "developer"

    def test_profile_path_swaps_for_gpt5(self, transport):
        msgs = [{"role": "system", "content": "Be helpful"}, {"role": "user", "content": "hi"}]
        kw = transport.build_kwargs(
            model="gpt-5.4", messages=msgs, tools=None,
            provider_profile=get_provider_profile("openrouter"),
        )
        assert kw["messages"][0]["role"] == "developer"



class TestRequestOverridesParity:
    """request_overrides with extra_body must merge identically on both paths."""

    def test_extra_body_override_legacy(self, transport):
        kw = transport.build_kwargs(
            model="gpt-5.4", messages=_msgs(), tools=None,
            provider_profile=get_provider_profile("openrouter"),
            request_overrides={"extra_body": {"custom_key": "custom_val"}},
        )
        assert kw["extra_body"]["custom_key"] == "custom_val"



    def test_top_level_override(self, transport):
        kw = transport.build_kwargs(
            model="gpt-5.4", messages=_msgs(), tools=None,
            provider_profile=get_provider_profile("openrouter"),
            request_overrides={"top_p": 0.9},
        )
        assert kw["top_p"] == 0.9


class TestProviderFinalizeHook:
    def test_finalize_runs_after_request_overrides(self, transport):
        class FinalizingProfile(ProviderProfile):
            def finalize_api_kwargs(self, api_kwargs, *, model=None, **context):
                cleaned = dict(api_kwargs)
                cleaned.pop("top_p", None)
                cleaned["finalized_model"] = model
                return cleaned

        kw = transport.build_kwargs(
            model="claude-opus-4-8",
            messages=_msgs(),
            tools=None,
            provider_profile=FinalizingProfile(name="test-finalizer"),
            request_overrides={"top_p": 0.9},
        )

        assert "top_p" not in kw
        assert kw["finalized_model"] == "claude-opus-4-8"
