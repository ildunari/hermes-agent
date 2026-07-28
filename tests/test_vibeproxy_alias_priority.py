from tui_gateway import server


def test_startup_runtime_resolves_short_alias_without_network(monkeypatch):
    monkeypatch.setenv("HERMES_MODEL", "sonnet")
    monkeypatch.delenv("HERMES_TUI_PROVIDER", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    monkeypatch.setattr(server, "_load_cfg", lambda: {"model": {"provider": "auto"}})
    monkeypatch.setattr(
        "hermes_cli.models.fetch_openrouter_models",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("network lookup should not run")
        ),
    )

    model, provider = server._resolve_startup_runtime()

    # Subscription routing owns the short Claude aliases when available.
    assert provider == "vibeproxy"
    assert model.startswith("claude-sonnet")


def test_startup_runtime_does_not_call_network_detector(monkeypatch):
    monkeypatch.setenv("HERMES_MODEL", "sonnet")
    monkeypatch.delenv("HERMES_TUI_PROVIDER", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    monkeypatch.setattr(server, "_load_cfg", lambda: {"model": {"provider": "auto"}})
    monkeypatch.setattr(
        "hermes_cli.models.detect_provider_for_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("network detector called")
        ),
    )

    model, provider = server._resolve_startup_runtime()

    assert model
    assert provider == "vibeproxy"
