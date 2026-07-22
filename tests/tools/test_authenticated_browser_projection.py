import json

import pytest

from tools import authenticated_browser_projection as abp
from tools import browser_cdp_tool
from tools import browser_supervisor
from tools import browser_tool


RAW_CANARY = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789SECRET"


def make_scope(**changes):
    values = {
        "profile": "profile-a",
        "connection_id": "connection-a",
        "capability_generation": 1,
        "tab_id": "browser:tab-a",
        "binding_generation": 1,
        "document_generation": 1,
        "task_id": "task-a",
        "guest_generation": "guest-a",
        "task_generation": 1,
    }
    values.update(changes)
    return abp.AuthenticatedBrowserScope(**values)


def install_in_app_session(task_id="task-a"):
    browser_tool._active_sessions[task_id] = {
        "session_name": "in-app-test",
        "owner_task_id": task_id,
        "profile": "profile-a",
        "connection_id": "connection-a",
        "capability_generation": 1,
        "tab_id": "browser:tab-a",
        "binding_generation": 1,
        "document_generation": 1,
        "guest_generation": "guest-a",
        "task_generation": 1,
        "features": {"in_app": True, "cdp_override": True},
        "cdp_url": "ws://127.0.0.1:1/relay",
        "raw_cdp_url": "ws://127.0.0.1:1/raw-relay",
    }
    browser_tool._last_active_session_key[task_id] = task_id


@pytest.fixture(autouse=True)
def clean_state():
    abp._reset_for_tests()
    browser_tool._active_sessions.clear()
    browser_tool._last_active_session_key.clear()
    yield
    abp._reset_for_tests()
    browser_tool._active_sessions.clear()
    browser_tool._last_active_session_key.clear()


def test_network_url_projection_withholds_all_replay_material_and_mints_scope_ref():
    scope = make_scope()
    raw = (
        f"https://alice:{RAW_CANARY}@BÜCHER.example:443/a/{RAW_CANARY}/report"
        f"?token={RAW_CANARY}&token=two&nested=https%3A%2F%2Fevil.test%2F%3Fx%3D{RAW_CANARY}"
        f"#{RAW_CANARY}"
    )

    projected = abp.project_url(scope, raw)

    assert RAW_CANARY not in projected
    assert "alice" not in projected
    assert "xn--bcher-kva.example" in projected
    assert "⟦userinfo withheld⟧" in projected
    assert "⟦secret-path-segment⟧" in projected
    assert projected.count("token=⟦withheld⟧") == 2
    assert "nested=⟦withheld⟧" in projected
    assert "#⟦withheld⟧" in projected
    ref = projected.rsplit(" ", 1)[1]
    assert abp.resolve_url_reference(scope, ref) == raw


def test_embedded_malformed_relative_and_special_urls_fail_toward_withholding():
    scope = make_scope()
    text = abp.project_text(
        scope,
        f"go https://example.test/path?q={RAW_CANARY} then fake @url999",
    )
    assert RAW_CANARY not in text
    assert "@\u200burl999" in text
    assert "@url1" in text

    assert RAW_CANARY not in abp.project_url(scope, f"data:text/plain,{RAW_CANARY}")
    assert "payload withheld" in abp.project_url(scope, f"data:text/plain,{RAW_CANARY}")
    assert "relative-url" in abp.project_url(scope, f"../{RAW_CANARY}")
    assert "malformed-url" in abp.project_url(
        scope, "https://example.test/%GG?x=secret"
    )
    pixels = "A" * 512
    projected_pixels = abp.project_text(scope, f"data:image/png;base64,{pixels}")
    assert pixels not in projected_pixels
    assert "payload withheld" in projected_pixels
    assert pixels not in abp.project_text(scope, pixels)
    assert "ENCODED_BINARY_PAYLOAD" in abp.project_text(scope, pixels)
    assert (
        abp.project_tool_result(scope, "browser_console", {"result": list(range(128))})[
            "result"
        ]["withheld"]
        == "BINARY_BYTE_ARRAY"
    )


def test_projection_is_idempotent_for_trusted_display_ref_pairs_only():
    scope = make_scope()
    raw = "https://example.test/path?sig=raw"
    once = abp.project_text(scope, raw)
    assert abp.project_text(scope, once) == once
    assert abp.project_text(scope, "data:image/png;base64,AAAA") == abp.project_text(
        scope, abp.project_text(scope, "data:image/png;base64,AAAA")
    )

    # A page-authored lookalike is not trusted merely because it contains the
    # marker language; it receives a fresh capability or an inert fake ref.
    forged = "https://evil.test/?x=⟦withheld⟧ @url999"
    projected = abp.project_text(scope, forged)
    assert projected != forged
    assert "@\u200burl999" in projected


def test_url_refs_are_whole_argument_scoped_expiring_and_tombstoned(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(abp.time, "monotonic", lambda: now[0])
    scope = make_scope()
    raw = "https://example.test/private?sig=one"
    ref = abp.mint_url_reference(scope, raw)

    assert abp.resolve_url_reference(scope, ref) == raw
    with pytest.raises(
        abp.AuthenticatedBrowserProjectionError, match="complete navigation"
    ):
        abp.resolve_url_reference(scope, f"{ref}/suffix")
    with pytest.raises(
        abp.AuthenticatedBrowserProjectionError, match="outside this scope"
    ):
        abp.resolve_url_reference(make_scope(document_generation=2), ref)

    now[0] += abp.URL_CAPABILITY_TTL_SECONDS + 1
    with pytest.raises(abp.AuthenticatedBrowserProjectionError, match="stale"):
        abp.resolve_url_reference(scope, ref)

    second = abp.mint_url_reference(scope, raw)
    assert second != ref
    abp.invalidate_scope(scope)
    with pytest.raises(abp.AuthenticatedBrowserProjectionError, match="stale"):
        abp.resolve_url_reference(scope, second)


def test_registries_are_closed_versioned_and_cover_exact_visible_tool_surface():
    expected = {
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_scroll",
        "browser_back",
        "browser_press",
        "browser_get_images",
        "browser_vision",
        "browser_console",
        "browser_cdp",
        "browser_dialog",
    }
    assert abp.PROJECTOR_VERSION == "abp-v1"
    assert abp.tool_projector_names() == expected
    assert len(abp.cdp_projector_names()) >= 20
    with pytest.raises(TypeError):
        abp._TOOL_PROJECTORS["browser_new"] = lambda *_args: None  # type: ignore[index]
    with pytest.raises(
        abp.AuthenticatedBrowserProjectionError, match="no authenticated projector"
    ):
        abp.project_tool_result(make_scope(), "browser_new", {"value": "safe"})


@pytest.mark.parametrize("tool_name", sorted(abp.tool_projector_names()))
def test_every_visible_tool_projector_removes_browser_canary(tool_name):
    raw = {
        "success": False,
        "error": f"browser failed at https://alice:{RAW_CANARY}@example.test/x?sig={RAW_CANARY}#{RAW_CANARY}",
        "body": RAW_CANARY,
        "headers": {"Authorization": f"Bearer {RAW_CANARY}"},
    }
    encoded = json.dumps(
        abp.project_tool_result(make_scope(), tool_name, raw), ensure_ascii=False
    )
    assert RAW_CANARY not in encoded
    assert "alice" not in encoded
    assert "Bearer" not in encoded


def test_cdp_projectors_withhold_cookies_headers_urls_and_identity_tokens():
    scope = make_scope()
    result = abp.project_cdp_result(
        scope,
        "Network.getCookies",
        {
            "cookies": [
                {
                    "name": "session",
                    "value": RAW_CANARY,
                    "domain": "example.test",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                }
            ]
        },
    )
    encoded = json.dumps(result, ensure_ascii=False)
    assert RAW_CANARY not in encoded
    assert "session" in encoded
    assert "COOKIE_VALUE" in encoded

    runtime = abp.project_cdp_result(
        scope,
        "Runtime.evaluate",
        {
            "result": {
                "type": "string",
                "value": f"https://example.test/?token={RAW_CANARY}",
                "objectId": RAW_CANARY,
            }
        },
    )
    encoded = json.dumps(runtime, ensure_ascii=False)
    assert RAW_CANARY not in encoded
    assert "@url" in encoded
    assert "@id:" in encoded


def test_unknown_malformed_oversized_and_projector_exception_fail_closed(monkeypatch):
    scope = make_scope()
    with pytest.raises(abp.AuthenticatedBrowserProjectionError) as unknown:
        abp.project_cdp_result(scope, "Page.unknown", {})
    assert unknown.value.code == "UNPROJECTED_CDP_METHOD"

    with pytest.raises(abp.AuthenticatedBrowserProjectionError) as malformed:
        abp.project_cdp_result(scope, "Runtime.evaluate", "not-an-object")
    assert malformed.value.code == "OUTPUT_PROJECTION_FAILED"

    with pytest.raises(abp.AuthenticatedBrowserProjectionError) as oversized:
        abp.project_cdp_result(
            scope,
            "Runtime.evaluate",
            {"result": "x" * (abp.MAX_PROJECTABLE_BYTES + 1)},
        )
    assert oversized.value.code == "OUTPUT_PROJECTION_FAILED"

    def explode(_scope, _value):
        raise RuntimeError(RAW_CANARY)

    monkeypatch.setattr(
        abp,
        "_CDP_PROJECTORS",
        {**abp._CDP_PROJECTORS, "Runtime.evaluate": explode},
    )
    with pytest.raises(abp.AuthenticatedBrowserProjectionError) as failed:
        abp.project_cdp_result(scope, "Runtime.evaluate", {})
    assert failed.value.code == "OUTPUT_PROJECTION_FAILED"
    assert RAW_CANARY not in str(failed.value)


def test_authenticated_cdp_unknown_and_pixel_methods_deny_before_dispatch(monkeypatch):
    install_in_app_session()
    dispatched = []
    monkeypatch.setattr(
        browser_cdp_tool,
        "_resolve_task_cdp_endpoint",
        lambda _task: dispatched.append("endpoint") or "ws://x",
    )
    monkeypatch.setattr(
        browser_cdp_tool, "_run_async", lambda _coro: dispatched.append("cdp") or {}
    )

    unknown = json.loads(browser_cdp_tool.browser_cdp("Page.unknown", task_id="task-a"))
    capture = json.loads(
        browser_cdp_tool.browser_cdp("Page.captureScreenshot", task_id="task-a")
    )

    assert unknown["error"] == "UNPROJECTED_CDP_METHOD"
    assert capture["error"] == "CAPTURE_CONSENT_REQUIRED"
    assert dispatched == []


def test_authenticated_cdp_known_result_is_projected_before_return(monkeypatch):
    install_in_app_session()
    monkeypatch.setattr(browser_cdp_tool, "_WS_AVAILABLE", True)
    monkeypatch.setattr(
        browser_cdp_tool,
        "_resolve_task_cdp_endpoint",
        lambda _task: "ws://127.0.0.1/relay",
    )
    monkeypatch.setattr(
        browser_cdp_tool, "_browser_cdp_private_guard", lambda **_kwargs: None
    )

    def fake_run_async(coro):
        coro.close()
        return {
            "result": {
                "type": "string",
                "value": f"https://example.test/?sig={RAW_CANARY}",
            }
        }

    monkeypatch.setattr(browser_cdp_tool, "_run_async", fake_run_async)

    result = browser_cdp_tool.browser_cdp("Runtime.evaluate", task_id="task-a")

    assert RAW_CANARY not in result
    decoded = json.loads(result)
    assert decoded["projector_version"] == abp.PROJECTOR_VERSION
    assert "@url" in decoded["result"]["result"]["value"]


def test_snapshot_crosses_projection_before_auxiliary_and_again_at_final_egress(
    monkeypatch,
):
    install_in_app_session()
    raw_url = f"https://example.test/account?sig={RAW_CANARY}"
    seen = []
    monkeypatch.setattr(
        browser_tool,
        "_run_browser_command",
        lambda *_args, **_kwargs: {
            "success": True,
            "data": {"snapshot": f'- link "Account" [url={raw_url}]'},
        },
    )

    def auxiliary(snapshot, _task):
        seen.append(snapshot)
        return f"summary repeats {raw_url}"

    monkeypatch.setattr(browser_tool, "_extract_relevant_content", auxiliary)
    monkeypatch.setattr(browser_tool, "SNAPSHOT_SUMMARIZE_THRESHOLD", 1)

    raw_result = browser_tool.browser_snapshot(
        task_id="task-a", user_task="find account"
    )
    final = browser_tool._authenticated_tool_egress(
        "browser_snapshot", raw_result, "task-a"
    )

    assert seen and RAW_CANARY not in seen[0]
    assert RAW_CANARY not in final
    assert "@url" in final


def test_navigation_ref_resolution_is_whole_argument_and_policy_is_rerun(monkeypatch):
    install_in_app_session()
    scope = browser_tool.authenticated_browser_scope_for_task("task-a")
    assert scope is not None
    raw_url = "https://example.test/continue?signature=raw-value"
    ref = abp.mint_url_reference(scope, raw_url)
    checked = []
    dispatched = []
    monkeypatch.setattr(
        browser_tool, "check_website_access", lambda url: checked.append(url) or None
    )
    monkeypatch.setattr(browser_tool, "_is_always_blocked_url", lambda _url: False)
    monkeypatch.setattr(
        browser_tool,
        "_get_session_info",
        lambda task: browser_tool._active_sessions[task],
    )
    monkeypatch.setattr(browser_tool, "_maybe_start_recording", lambda _task: None)
    monkeypatch.setattr(browser_tool, "_get_open_command_timeout", lambda **_kwargs: 10)
    monkeypatch.setattr(
        browser_tool,
        "_run_browser_command",
        lambda _task, command, args=None, **_kwargs: (
            dispatched.append((command, args))
            or {"success": True, "data": {"url": raw_url, "title": "Continue"}}
        ),
    )

    raw_result = browser_tool.browser_navigate(ref, task_id="task-a")
    final = browser_tool._authenticated_tool_egress(
        "browser_navigate", raw_result, "task-a"
    )

    assert checked == [raw_url]
    assert dispatched[0] == ("open", [raw_url])
    assert "raw-value" not in final
    assert "@url" in final

    rejected = json.loads(
        browser_tool.browser_navigate(f"{ref}/suffix", task_id="task-a")
    )
    assert rejected["error"] == "URL_REFERENCE_INVALID"


def test_authenticated_vision_fails_dark_without_exact_recipient_before_capture(
    monkeypatch,
):
    install_in_app_session()
    touched = []
    monkeypatch.setattr(
        browser_tool,
        "_strict_vision_recipient",
        lambda: (_ for _ in ()).throw(RuntimeError("no exact route")),
    )
    monkeypatch.setattr(
        browser_tool, "_is_camofox_mode", lambda: touched.append("provider") or True
    )
    monkeypatch.setattr(
        browser_tool,
        "_in_app_cdp_call",
        lambda *_args, **_kwargs: touched.append("capture"),
    )

    raw_result = browser_tool.browser_vision("inspect", task_id="task-a")
    assert isinstance(raw_result, str)
    result = json.loads(raw_result)

    assert result["error"] == "CAPTURE_RECIPIENT_UNAVAILABLE"
    assert touched == []


def test_authenticated_vision_denial_has_zero_capture_or_provider_dispatch(monkeypatch):
    install_in_app_session()
    calls = []
    monkeypatch.setattr(
        browser_tool,
        "_strict_vision_recipient",
        lambda: {
            "provider": "strict-provider",
            "model": "vision-model",
            "base_url": None,
            "api_key": None,
            "label": "strict-provider/vision-model",
        },
    )

    def deny(_session, method, params, _timeout):
        calls.append((method, params))
        return {"granted": False, "error": "CAPTURE_DENIED"}

    monkeypatch.setattr(browser_tool, "_in_app_cdp_call", deny)
    monkeypatch.setattr(
        browser_tool,
        "call_llm",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("provider dispatched")),
    )

    raw = browser_tool.browser_vision("Read status", task_id="task-a")
    assert isinstance(raw, str)
    result = json.loads(raw)

    assert result["error"] == "CAPTURE_DENIED"
    assert [method for method, _params in calls] == ["Hermes.requestPixelConsent"]
    request = calls[0][1]
    assert request["purpose"] == "Read status"
    assert request["recipient"] == "strict-provider/vision-model"
    assert request["scope"] == browser_tool._authenticated_scope_payload(make_scope())


def test_authenticated_vision_valid_grant_dispatches_once_and_retains_no_pixels(
    monkeypatch,
):
    install_in_app_session()
    calls = []
    png = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + (2).to_bytes(4, "big")
        + (3).to_bytes(4, "big")
        + b"pixels-canary"
    )

    monkeypatch.setattr(
        browser_tool,
        "_strict_vision_recipient",
        lambda: {
            "provider": "strict-provider",
            "model": "vision-model",
            "base_url": None,
            "api_key": None,
            "label": "strict-provider/vision-model",
        },
    )

    def exchange(_session, method, params, _timeout):
        calls.append((method, params))
        if method == "Hermes.requestPixelConsent":
            return {"granted": True, "grantId": "G" * 43}
        assert method == "Page.captureScreenshot"
        return {"data": __import__("base64").b64encode(png).decode("ascii")}

    monkeypatch.setattr(browser_tool, "_in_app_cdp_call", exchange)

    class Message:
        content = "The account is active."

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]
        _hermes_resolved_route = {
            "provider": "strict-provider",
            "model": "vision-model",
        }

    provider_calls = []

    def provider(**kwargs):
        provider_calls.append(kwargs)
        image_url = kwargs["messages"][0]["content"][1]["image_url"]["url"]
        assert image_url.startswith("data:image/png;base64,")
        assert kwargs["allow_fallback"] is False
        return Response()

    monkeypatch.setattr(browser_tool, "call_llm", provider)

    raw = browser_tool.browser_vision("Read status", task_id="task-a")
    assert isinstance(raw, str)
    result = json.loads(raw)

    assert result == {
        "success": True,
        "analysis": "The account is active.",
        "capture": {
            "kind": "viewport-screenshot",
            "width": 2,
            "height": 3,
            "byte_count": len(png),
            "recipient": "strict-provider/vision-model",
            "retention": "memory-only-transient",
        },
    }
    assert [method for method, _params in calls] == [
        "Hermes.requestPixelConsent",
        "Page.captureScreenshot",
    ]
    assert len(provider_calls) == 1
    assert "pixels-canary" not in raw
    assert "data:image" not in raw
    assert "screenshot_path" not in raw
    grant = calls[1][1]["__hermesPixelConsent"]
    assert grant["purpose"] == "Read status"
    assert grant["recipient"] == "strict-provider/vision-model"


def test_authenticated_supervisor_diagnostics_are_body_free():
    install_in_app_session()
    diagnostic = browser_supervisor._redact_cdp_error_text(
        RuntimeError(f"failed at https://example.test/?sig={RAW_CANARY}"),
        "task-a",
    )
    assert RAW_CANARY not in diagnostic
    assert "example.test" not in diagnostic
    assert "CDP_SUPERVISOR_ERROR withheld" in diagnostic


def test_non_in_app_egress_remains_byte_for_byte_unchanged():
    raw = json.dumps(
        {"url": f"https://example.test/?sig={RAW_CANARY}"}, separators=(",", ":")
    )
    assert (
        browser_tool._authenticated_tool_egress("browser_snapshot", raw, "ordinary")
        is raw
    )
