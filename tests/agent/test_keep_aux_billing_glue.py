"""KEEP billing-boundary wiring retained across the origin/main land."""

from unittest.mock import patch

from agent.auxiliary_client import _resolve_auto_route, _try_payment_fallback


def test_auto_route_does_not_enter_metered_chain_for_keyless_main():
    with (
        patch("agent.auxiliary_client._read_main_model", return_value=""),
        patch(
            "agent.auxiliary_client._try_main_fallback_chain",
            return_value=(None, None, ""),
        ),
        patch(
            "agent.auxiliary_client.aux_auto_metered_fallback_blocked",
            return_value=True,
        ),
        patch("agent.auxiliary_client._get_provider_chain") as provider_chain,
    ):
        assert _resolve_auto_route(
            main_runtime={"provider": "gateway-local"}
        ) == (None, None, "")

    provider_chain.assert_not_called()


def test_payment_recovery_does_not_enter_metered_chain_for_oauth_main():
    with (
        patch(
            "agent.auxiliary_client._read_main_provider",
            return_value="openai-codex",
        ),
        patch(
            "agent.auxiliary_client.aux_auto_metered_fallback_blocked",
            return_value=True,
        ),
        patch("agent.auxiliary_client._get_provider_chain") as provider_chain,
    ):
        assert _try_payment_fallback(
            "openai-codex", task="compression"
        ) == (None, None, "")

    provider_chain.assert_not_called()
