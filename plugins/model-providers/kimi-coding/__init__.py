"""Kimi / Moonshot provider profiles.

Kimi has dual endpoints:
  - sk-kimi-* keys → api.kimi.com/coding (Anthropic Messages API)
  - legacy keys → api.moonshot.ai/v1 (OpenAI chat completions)

This module covers the chat_completions path (/v1 endpoint).
"""

from typing import Any

from providers import register_provider
from providers.base import OMIT_TEMPERATURE, ProviderProfile


class KimiProfile(ProviderProfile):
    """Kimi/Moonshot — temperature omitted, thinking xor reasoning_effort."""

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, **context
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Kimi reasoning controls.

        K3 always reasons, does not accept the K2 ``thinking`` object, and as of
        July 2026 accepts only ``reasoning_effort="max"``. K2 models retain the
        older xor contract: ``extra_body.thinking`` (a binary toggle) and a
        top-level ``reasoning_effort`` are mutually exclusive.
        """
        extra_body = {}
        top_level = {}

        model = str(context.get("model") or "").strip().lower().rsplit("/", 1)[-1]
        if model == "kimi-k3":
            # K3 reasoning cannot be disabled. Map every Hermes effort (and an
            # unset effort) to the sole API-supported value instead of leaking
            # low/medium/high or the K2-only ``thinking`` field onto the wire.
            top_level["reasoning_effort"] = "max"
            return extra_body, top_level

        if not reasoning_config or not isinstance(reasoning_config, dict):
            # No config → thinking enabled, let the server pick the depth.
            # (Previously also sent reasoning_effort="medium", which paired
            # thinking + effort on every default call.)
            extra_body["thinking"] = {"type": "enabled"}
            return extra_body, top_level

        enabled = reasoning_config.get("enabled", True)
        if enabled is False:
            extra_body["thinking"] = {"type": "disabled"}
            return extra_body, top_level

        # Enabled: prefer an explicit effort; only fall back to extra_body
        # thinking when no recognized effort is requested.
        effort = (reasoning_config.get("effort") or "").strip().lower()
        if effort in {"low", "medium", "high"}:
            top_level["reasoning_effort"] = effort
        else:
            extra_body["thinking"] = {"type": "enabled"}

        return extra_body, top_level

    def finalize_api_kwargs(
        self,
        api_kwargs: dict[str, Any],
        *,
        model: str | None = None,
        **context: Any,
    ) -> dict[str, Any]:
        """Reassert K3's wire contract after caller and config overrides."""
        normalized_model = str(model or "").strip().lower().rsplit("/", 1)[-1]
        if normalized_model != "kimi-k3":
            return api_kwargs

        finalized = dict(api_kwargs)
        finalized["reasoning_effort"] = "max"
        extra_body = finalized.get("extra_body")
        if isinstance(extra_body, dict) and "thinking" in extra_body:
            cleaned_extra_body = dict(extra_body)
            cleaned_extra_body.pop("thinking", None)
            if cleaned_extra_body:
                finalized["extra_body"] = cleaned_extra_body
            else:
                finalized.pop("extra_body", None)
        return finalized


kimi = KimiProfile(
    name="kimi-coding",
    aliases=("kimi", "moonshot", "kimi-for-coding"),
    env_vars=("KIMI_API_KEY", "KIMI_CODING_API_KEY"),
    base_url="https://api.moonshot.ai/v1",
    fixed_temperature=OMIT_TEMPERATURE,
    default_max_tokens=32000,
    default_headers={"User-Agent": "hermes-agent/1.0"},
    default_aux_model="kimi-k2-turbo-preview",
)

kimi_cn = KimiProfile(
    name="kimi-coding-cn",
    aliases=("kimi-cn", "moonshot-cn"),
    env_vars=("KIMI_CN_API_KEY",),
    base_url="https://api.moonshot.cn/v1",
    fixed_temperature=OMIT_TEMPERATURE,
    default_max_tokens=32000,
    default_headers={"User-Agent": "hermes-agent/1.0"},
    default_aux_model="kimi-k2-turbo-preview",
)

register_provider(kimi)
register_provider(kimi_cn)
