"""Contract tests for the native Google Gemini provider profile."""

from __future__ import annotations

import pytest


@pytest.fixture
def gemini_profile():
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("gemini")
    assert profile is not None, "gemini provider profile must be registered"
    return profile


def test_native_gemini_auxiliary_default_uses_current_flash_model(gemini_profile):
    assert gemini_profile.default_aux_model == "gemini-3.6-flash"


def test_native_gemini_curated_catalog_uses_current_flash_model():
    from hermes_cli.models import _PROVIDER_MODELS

    models = _PROVIDER_MODELS["gemini"]
    assert "gemini-3.6-flash" in models
    assert "gemini-3.5-flash" not in models
