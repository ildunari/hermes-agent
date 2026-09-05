"""Route permissions must survive the real startup YAML pipeline."""

import pytest
import yaml

from gateway.config import load_gateway_config


@pytest.mark.parametrize("nested", [False, True])
def test_route_permissions_survive_startup(tmp_path, monkeypatch, nested):
    routes = {"default": ["poke", "guest"]}
    settings = {"permitted_conversation_routes": routes}
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"gateway": settings} if nested else settings)
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert load_gateway_config().permitted_conversation_routes == routes


@pytest.mark.parametrize("override", [{}, None, "invalid"])
def test_explicit_route_denial_overrides_nested_and_legacy(tmp_path, monkeypatch, override):
    routes = {"default": ["poke", "guest"]}
    (tmp_path / "gateway.json").write_text(
        '{"permitted_conversation_routes": {"default": ["poke", "guest"]}}'
    )
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "permitted_conversation_routes": override,
        "gateway": {"permitted_conversation_routes": routes},
    }))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert load_gateway_config().permitted_conversation_routes == {}
