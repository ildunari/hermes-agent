"""Catalog-API-key fallback for the Copilot ``/model`` picker.

Regression for #16708: when the user's only Copilot credential is a
``gho_*`` token (typically obtained via device-code login) stored in
``auth.json`` under ``credential_pool.copilot[]`` — placed there by
``hermes auth add copilot`` or by ``_seed_from_env`` when the env var
is set in ``~/.hermes/.env`` — the picker was silently dropping back to
a stale hardcoded list because ``_resolve_copilot_catalog_api_key``
only consulted env vars / ``gh auth token`` and never read the
credential pool.
"""

import time
from unittest.mock import patch

from hermes_cli.models import _resolve_copilot_catalog_api_key


class TestCopilotCatalogApiKeyResolution:
    def test_exchangeable_env_var_token_wins_over_pool(self):
        """An exchangeable env token still short-circuits the pool fallback."""
        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={"api_key": "gho_env_token"},
        ), patch(
            "hermes_cli.auth.read_credential_pool",
        ) as mock_pool, patch(
            "hermes_cli.copilot_auth.exchange_copilot_token",
            return_value=("tid=env;exp=9999999999", 9999999999.0, None),
        ) as mock_exchange:
            assert _resolve_copilot_catalog_api_key() == "tid=env;exp=9999999999"
            mock_exchange.assert_called_once_with("gho_env_token")
            mock_pool.assert_not_called()

    def test_malformed_or_expired_ambient_falls_through_to_valid_pool(self):
        """A merely non-ghp ambient token must not hide a valid pool token."""
        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={"api_key": "arbitrary-or-expired-token"},
        ), patch(
            "hermes_cli.auth.read_credential_pool",
            return_value=[{"access_token": "gho_valid_pool"}],
        ), patch(
            "hermes_cli.copilot_auth.exchange_copilot_token",
            side_effect=[
                ValueError("ambient token expired"),
                ("tid=pool;exp=9999999999", 9999999999.0, None),
            ],
        ) as mock_exchange:
            assert _resolve_copilot_catalog_api_key() == "tid=pool;exp=9999999999"
            assert [call.args[0] for call in mock_exchange.call_args_list] == [
                "arbitrary-or-expired-token",
                "gho_valid_pool",
            ]

    def test_all_nonclassic_candidates_invalid_returns_empty(self):
        """Failed ambient and pool exchanges yield no catalog credential."""
        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={"api_key": "expired-ambient-token"},
        ), patch(
            "hermes_cli.auth.read_credential_pool",
            return_value=[{"access_token": "expired-pool-token"}],
        ), patch(
            "hermes_cli.copilot_auth.exchange_copilot_token",
            side_effect=ValueError("token expired"),
        ) as mock_exchange:
            assert _resolve_copilot_catalog_api_key() == ""
            assert mock_exchange.call_count == 2

    def test_fake_future_tid_ambient_falls_through_to_valid_pool(self):
        """Unverifiable ambient tid/exp fields must not hide a valid pool token."""
        fake = f"tid=forged;exp={time.time() + 86400};sku=copilot_individual"
        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={"api_key": fake},
        ), patch(
            "hermes_cli.auth.read_credential_pool",
            return_value=[{"access_token": "gho_valid_pool"}],
        ) as mock_pool, patch(
            "hermes_cli.copilot_auth.exchange_copilot_token",
            return_value=("tid=pool;exp=9999999999", 9999999999.0, None),
        ) as mock_exchange:
            assert _resolve_copilot_catalog_api_key() == "tid=pool;exp=9999999999"
            mock_exchange.assert_called_once_with("gho_valid_pool")
            mock_pool.assert_called_once_with("copilot")

    def test_expired_exchanged_token_falls_through_without_reexchange(self):
        """An expired tid token is neither accepted nor sent to GitHub exchange."""
        expired = f"tid=ambient;exp={time.time() - 1};sku=copilot_individual"
        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={"api_key": expired},
        ), patch(
            "hermes_cli.auth.read_credential_pool",
            return_value=[{"access_token": "gho_valid_pool"}],
        ), patch(
            "hermes_cli.copilot_auth.exchange_copilot_token",
            return_value=("tid=pool;exp=9999999999", 9999999999.0, None),
        ) as mock_exchange:
            assert _resolve_copilot_catalog_api_key() == "tid=pool;exp=9999999999"
            mock_exchange.assert_called_once_with("gho_valid_pool")

    def test_classic_ambient_token_falls_through_to_valid_pool_token(self):
        """An ambient classic PAT must not hide a later usable pool credential."""
        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={"api_key": "ghp_ambient_classic_pat"},
        ), patch(
            "hermes_cli.auth.read_credential_pool",
            return_value=[{"access_token": "ghu_pool_token"}],
        ), patch(
            "hermes_cli.copilot_auth.exchange_copilot_token",
            return_value=("tid_from_pool", 1234567890.0, None),
        ) as mock_exchange:
            assert _resolve_copilot_catalog_api_key() == "tid_from_pool"
            mock_exchange.assert_called_once_with("ghu_pool_token")

    def test_falls_back_to_pool_oauth_token(self):
        """Empty env → walk credential_pool.copilot[] for an OAuth access_token."""
        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={"api_key": ""},
        ), patch(
            "hermes_cli.auth.read_credential_pool",
            return_value=[{"access_token": "gho_abc123"}],
        ), patch(
            "hermes_cli.copilot_auth.exchange_copilot_token",
            return_value=("tid_exchanged_xyz", 1234567890.0, None),
        ):
            assert _resolve_copilot_catalog_api_key() == "tid_exchanged_xyz"

    def test_falls_back_when_env_resolution_raises(self):
        """Env path raising an exception still falls through to the pool."""
        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            side_effect=RuntimeError("auth.json corrupt"),
        ), patch(
            "hermes_cli.auth.read_credential_pool",
            return_value=[{"access_token": "gho_xyz"}],
        ), patch(
            "hermes_cli.copilot_auth.exchange_copilot_token",
            return_value=("tid_exchanged_xyz", 1234567890.0, None),
        ):
            assert _resolve_copilot_catalog_api_key() == "tid_exchanged_xyz"

    def test_skips_classic_pat_in_pool(self):
        """Classic PATs (``ghp_…``) are unsupported by the Copilot API — skip them."""
        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={"api_key": ""},
        ), patch(
            "hermes_cli.auth.read_credential_pool",
            return_value=[{"access_token": "ghp_classic_pat"}],
        ), patch(
            "hermes_cli.copilot_auth.exchange_copilot_token",
        ) as mock_exchange:
            assert _resolve_copilot_catalog_api_key() == ""
            mock_exchange.assert_not_called()

    def test_skips_invalid_pool_entries_until_first_exchangeable(self):
        """Non-dict entries and entries without an ``access_token`` are skipped."""
        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={"api_key": ""},
        ), patch(
            "hermes_cli.auth.read_credential_pool",
            return_value=[
                "not-a-dict",
                {"label": "no-token-here"},
                {"access_token": ""},
                {"access_token": "gho_first_real_token"},
                {"access_token": "gho_should_not_reach"},
            ],
        ), patch(
            "hermes_cli.copilot_auth.exchange_copilot_token",
            return_value=("tid_from_first", 1234567890.0, None),
        ) as mock_exchange:
            assert _resolve_copilot_catalog_api_key() == "tid_from_first"
            mock_exchange.assert_called_once_with("gho_first_real_token")

    def test_skips_pool_entry_that_fails_to_exchange(self):
        """If the first entry won't exchange, try the next — an unsupported pool[0]
        must not wedge a later valid entry (Copilot review #16868 finding)."""
        attempts: list[str] = []

        def fake_exchange(raw_token: str):
            attempts.append(raw_token)
            if raw_token == "gho_unsupported_account":
                raise ValueError("Copilot token exchange failed: HTTP 401")
            return ("tid_from_second", 1234567890.0, None)

        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={"api_key": ""},
        ), patch(
            "hermes_cli.auth.read_credential_pool",
            return_value=[
                {"access_token": "gho_unsupported_account"},
                {"access_token": "gho_valid_token"},
            ],
        ), patch(
            "hermes_cli.copilot_auth.exchange_copilot_token",
            side_effect=fake_exchange,
        ):
            assert _resolve_copilot_catalog_api_key() == "tid_from_second"
            assert attempts == ["gho_unsupported_account", "gho_valid_token"]

    def test_all_pool_entries_fail_exchange_returns_empty(self):
        """All exchanges fail → return "" so the caller falls back to curated."""
        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={"api_key": ""},
        ), patch(
            "hermes_cli.auth.read_credential_pool",
            return_value=[
                {"access_token": "gho_expired_a"},
                {"access_token": "gho_expired_b"},
            ],
        ), patch(
            "hermes_cli.copilot_auth.exchange_copilot_token",
            side_effect=ValueError("Copilot token exchange failed"),
        ):
            assert _resolve_copilot_catalog_api_key() == ""

    def test_all_candidates_invalid_returns_empty(self):
        """Unsupported resolver and pool candidates produce no catalog credential."""
        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={"api_key": "ghp_ambient_classic_pat"},
        ), patch(
            "hermes_cli.auth.read_credential_pool",
            return_value=[
                {"access_token": "ghp_pool_classic_pat"},
                {"access_token": ""},
                {"label": "missing-token"},
            ],
        ), patch(
            "hermes_cli.copilot_auth.exchange_copilot_token",
        ) as mock_exchange:
            assert _resolve_copilot_catalog_api_key() == ""
            mock_exchange.assert_not_called()

    def test_accepts_three_value_exchange_result(self):
        """Catalog resolution follows exchange_copilot_token's current contract."""
        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={"api_key": ""},
        ), patch(
            "hermes_cli.auth.read_credential_pool",
            return_value=[{"access_token": "github_pat_fine_grained"}],
        ), patch(
            "hermes_cli.copilot_auth.exchange_copilot_token",
            return_value=(
                "tid_exchanged",
                1234567890.0,
                "https://api.enterprise.githubcopilot.com",
            ),
        ):
            assert _resolve_copilot_catalog_api_key() == "tid_exchanged"

    def test_returns_empty_string_when_no_credentials_anywhere(self):
        """No env, no pool → empty string (caller falls back to curated list)."""
        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={"api_key": ""},
        ), patch(
            "hermes_cli.auth.read_credential_pool",
            return_value=[],
        ):
            assert _resolve_copilot_catalog_api_key() == ""

    def test_pool_failure_returns_empty_string(self):
        """If the pool read itself raises, swallow and return ""."""
        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={"api_key": ""},
        ), patch(
            "hermes_cli.auth.read_credential_pool",
            side_effect=RuntimeError("auth.json locked"),
        ):
            assert _resolve_copilot_catalog_api_key() == ""
