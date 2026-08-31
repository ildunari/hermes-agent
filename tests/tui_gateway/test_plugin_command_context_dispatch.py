"""Plugin command parity and profile isolation for TUI RPC dispatch."""

from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli.plugins import (
    PluginContext,
    PluginManifest,
    _reset_plugin_managers_for_tests,
    get_plugin_manager,
)
from tui_gateway.methods_tools import _dispatch_plugin_command_for_session


@pytest.fixture(autouse=True)
def clean_plugin_managers():
    _reset_plugin_managers_for_tests()
    yield
    _reset_plugin_managers_for_tests()


def _register(home: Path, label: str, *, context: bool) -> None:
    token = set_hermes_home_override(str(home))
    try:
        manager = get_plugin_manager()
        manager._discovered = True
        ctx = PluginContext(PluginManifest(name=f"probe-{label}"), manager)
        if context:
            ctx.register_command(
                "profile-probe",
                lambda invocation: (
                    f"{label}:{invocation.surface}:{invocation.profile}:"
                    f"{invocation.raw_args}"
                ),
                context=True,
            )
        else:
            ctx.register_command("profile-probe", lambda raw: f"{label}:{raw}")
    finally:
        reset_hermes_home_override(token)


def test_command_dispatch_uses_context_signature_and_session_profile(tmp_path, monkeypatch):
    alpha = tmp_path / "profiles" / "alpha"
    beta = tmp_path / "profiles" / "beta"
    alpha.mkdir(parents=True)
    beta.mkdir(parents=True)
    monkeypatch.setattr(
        "hermes_cli.profiles._get_profiles_root", lambda: tmp_path / "profiles"
    )
    _register(alpha, "alpha", context=True)
    _register(beta, "beta", context=True)

    matched, result = _dispatch_plugin_command_for_session(
        "profile-probe", "exact arg", {"profile_home": str(beta)}
    )

    assert matched is True
    assert result == "beta:cli:beta:exact arg"


def test_command_dispatch_preserves_legacy_raw_argument_handler(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    _register(home, "legacy", context=False)

    matched, result = _dispatch_plugin_command_for_session(
        "profile-probe", "  exact args  ", {"profile_home": str(home)}
    )

    assert matched is True
    assert result == "legacy:  exact args  "


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("command.dispatch", {"name": "profile-probe", "arg": "rpc arg"}),
        ("slash.exec", {"command": "/profile-probe rpc arg"}),
    ],
)
def test_tui_rpc_surfaces_share_context_aware_dispatch(tmp_path, method, params):
    from tui_gateway import server

    home = tmp_path / "profile"
    home.mkdir()
    _register(home, "rpc", context=True)
    session_id = "plugin-context-rpc"
    server._sessions[session_id] = {
        "profile_home": str(home),
        "session_key": "session-key",
    }
    try:
        response = server._methods[method](
            "request-1", {"session_id": session_id, **params}
        )
    finally:
        server._sessions.pop(session_id, None)

    assert "error" not in response
    result = response["result"]
    output = result["output"]
    assert "rpc:cli:custom:rpc arg" in output