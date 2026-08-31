from __future__ import annotations

import json

import pytest

from tools import web_tools


@pytest.mark.asyncio
async def test_pre_extract_receives_only_urls_that_pass_core_ssrf(monkeypatch):
    seen = []

    async def pre_extract(urls, **kwargs):
        seen.extend(urls)
        return [
            {
                "requested_url": urls[0],
                "url": urls[0],
                "title": "Extension",
                "content": "extension content",
                "raw_content": "extension content",
                "error": None,
                "backend_used": "extension:test",
            }
        ], []

    monkeypatch.setattr(
        web_tools,
        "is_safe_url",
        lambda url: not str(url).startswith("http://127.0.0.1"),
    )
    monkeypatch.setattr(web_tools, "check_auxiliary_model", lambda: False)

    result = json.loads(
        await web_tools.web_extract_tool(
            ["http://127.0.0.1/private", "https://example.com/page"],
            use_llm_processing=False,
            pre_extract=pre_extract,
        )
    )

    assert seen == ["https://example.com/page"]
    assert result["results"][0]["error"] == (
        "Blocked: URL targets a private or internal network address"
    )
    assert result["results"][1]["backend_used"] == "extension:test"


@pytest.mark.asyncio
async def test_secret_url_is_rejected_before_pre_extract(monkeypatch):
    async def pre_extract(urls, **kwargs):
        raise AssertionError("pre_extract must not run for a secret-bearing URL")

    result = json.loads(
        await web_tools.web_extract_tool(
            ["https://example.com/?api_key=sk-secret-value"],
            use_llm_processing=False,
            pre_extract=pre_extract,
        )
    )

    assert result["success"] is False
    assert "Secrets must not be sent in URLs" in result["error"]


@pytest.mark.asyncio
async def test_pre_extract_miss_falls_through_to_generic_provider(monkeypatch):
    calls = []

    async def pre_extract(urls, **kwargs):
        calls.append(("pre", list(urls), kwargs))
        return [], list(urls)

    class FakeProvider:
        name = "fake"
        display_name = "Fake"

        def supports_extract(self):
            return True

        async def extract(self, urls, **kwargs):
            calls.append(("provider", list(urls), kwargs))
            return [
                {
                    "url": urls[0],
                    "title": "Provider",
                    "content": "provider content",
                    "raw_content": "provider content",
                }
            ]

    monkeypatch.setattr(web_tools, "is_safe_url", lambda url: True)
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: "fake")
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(
        "agent.web_search_registry.get_provider", lambda name: FakeProvider()
    )
    monkeypatch.setattr(web_tools, "check_auxiliary_model", lambda: False)

    result = json.loads(
        await web_tools.web_extract_tool(
            ["https://example.com/page"],
            use_llm_processing=False,
            pre_extract=pre_extract,
        )
    )

    assert result["results"][0]["title"] == "Provider"
    assert [call[0] for call in calls] == ["pre", "provider"]


@pytest.mark.asyncio
async def test_pre_extract_cannot_inject_unvalidated_provider_url(monkeypatch):
    async def pre_extract(urls, **kwargs):
        return [], ["http://127.0.0.1/private"]

    class FakeProvider:
        name = "fake"
        display_name = "Fake"

        def supports_extract(self):
            return True

        async def extract(self, urls, **kwargs):
            raise AssertionError("provider must not receive an extension-injected URL")

    monkeypatch.setattr(web_tools, "is_safe_url", lambda url: True)
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: "fake")
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(
        "agent.web_search_registry.get_provider", lambda name: FakeProvider()
    )

    result = json.loads(
        await web_tools.web_extract_tool(
            ["https://example.com/page"],
            use_llm_processing=False,
            pre_extract=pre_extract,
        )
    )

    assert "provider_fallback_urls must be a subset" in result["error"]


@pytest.mark.asyncio
async def test_pre_extract_cannot_mutate_authorization_snapshot(monkeypatch):
    async def pre_extract(urls, **kwargs):
        urls[:] = ["http://127.0.0.1/private"]
        return [], urls

    monkeypatch.setattr(web_tools, "is_safe_url", lambda url: True)
    monkeypatch.setattr(
        web_tools,
        "_get_extract_backend",
        lambda: (_ for _ in ()).throw(
            AssertionError("provider/cache path must not receive a mutated URL")
        ),
    )

    result = json.loads(
        await web_tools.web_extract_tool(
            ["https://example.com/page"],
            use_llm_processing=False,
            pre_extract=pre_extract,
        )
    )

    assert "provider_fallback_urls must be a subset" in result["error"]


@pytest.mark.asyncio
async def test_pre_extract_receives_provider_mode_options(monkeypatch):
    seen = {}

    async def pre_extract(urls, **kwargs):
        seen.update(kwargs)
        return [
            {
                "url": urls[0],
                "title": "Answer",
                "content": "42",
                "raw_content": "42",
                "error": None,
            }
        ], []

    monkeypatch.setattr(web_tools, "is_safe_url", lambda url: True)
    monkeypatch.setattr(web_tools, "check_auxiliary_model", lambda: False)

    result = json.loads(
        await web_tools.web_extract_tool(
            ["https://example.com/page"],
            mode="answer",
            question="why?",
            only_main_content=True,
            wait_for=250,
            schema={"type": "object"},
            use_llm_processing=False,
            pre_extract=pre_extract,
        )
    )

    assert result["results"][0]["content"] == "42"
    assert seen == {
        "mode": "answer",
        "format": None,
        "only_main_content": True,
        "wait_for": 250,
        "question": "why?",
        "schema": {"type": "object"},
    }


@pytest.mark.asyncio
async def test_validate_web_fetch_url_enforces_secret_and_ssrf_checks(monkeypatch):
    monkeypatch.setattr(
        web_tools,
        "is_safe_url",
        lambda url: not str(url).startswith("http://127.0.0.1"),
    )

    safe, error = await web_tools.validate_web_fetch_url("https://example.com/path")
    assert safe == "https://example.com/path"
    assert error is None

    safe, error = await web_tools.validate_web_fetch_url(
        "https://example.com/?access_token=secret"
    )
    assert safe is None
    assert error is not None
    assert "credential-like query parameter" in error

    safe, error = await web_tools.validate_web_fetch_url("http://127.0.0.1/private")
    assert safe is None
    assert error == "Blocked: URL targets a private or internal network address"
