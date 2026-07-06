"""Focused tests for consolidated web_extract surface."""

from __future__ import annotations

import json

import pytest


@pytest.mark.asyncio
async def test_firecrawl_extract_summary_forwards_scrape_options(monkeypatch):
    from plugins.web.firecrawl.provider import FirecrawlWebSearchProvider
    from plugins.web.firecrawl import provider as firecrawl_provider

    calls = []

    class FakeClient:
        def scrape(self, **kwargs):
            calls.append(kwargs)
            return {
                "summary": "short summary",
                "metadata": {"title": "Example", "sourceURL": kwargs["url"]},
            }

    monkeypatch.setattr(firecrawl_provider, "check_website_access", lambda url: None)
    monkeypatch.setattr(firecrawl_provider, "_get_firecrawl_client", lambda: FakeClient())
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

    results = await FirecrawlWebSearchProvider().extract(
        ["https://example.com"],
        mode="summary",
        only_main_content=True,
        wait_for=250,
    )

    assert results[0]["content"] == "short summary"
    assert calls == [
        {
            "url": "https://example.com",
            "formats": ["summary"],
            "only_main_content": True,
            "wait_for": 250,
        }
    ]


@pytest.mark.asyncio
async def test_firecrawl_extract_answer_uses_question_prompt(monkeypatch):
    from plugins.web.firecrawl.provider import FirecrawlWebSearchProvider
    from plugins.web.firecrawl import provider as firecrawl_provider

    calls = []

    class FakeClient:
        def extract(self, **kwargs):
            calls.append(kwargs)
            return {"data": {"answer": "42"}}

    monkeypatch.setattr(firecrawl_provider, "check_website_access", lambda url: None)
    monkeypatch.setattr(firecrawl_provider, "_get_firecrawl_client", lambda: FakeClient())
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

    results = await FirecrawlWebSearchProvider().extract(
        ["https://example.com"],
        mode="answer",
        question="What is the answer?",
    )

    assert results[0]["content"] == "42"
    assert calls == [{"urls": ["https://example.com"], "prompt": "What is the answer?"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "extra_kwargs", "response", "expected_content"),
    [
        ("answer", {"question": "What is the answer?"}, {"data": {"answer": "42"}}, "42"),
        ("json", {"schema": {"type": "object"}}, {"data": {"value": 42}}, '{"value": 42}'),
    ],
)
async def test_firecrawl_extract_answer_json_forwards_scrape_options_model(
    monkeypatch, mode, extra_kwargs, response, expected_content
):
    from plugins.web.firecrawl.provider import FirecrawlWebSearchProvider
    from plugins.web.firecrawl import provider as firecrawl_provider

    calls = []

    class FakeClient:
        def extract(self, **kwargs):
            calls.append(kwargs)
            return response

    monkeypatch.setattr(firecrawl_provider, "check_website_access", lambda url: None)
    monkeypatch.setattr(firecrawl_provider, "_get_firecrawl_client", lambda: FakeClient())
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

    results = await FirecrawlWebSearchProvider().extract(
        ["https://example.com"],
        mode=mode,
        only_main_content=True,
        wait_for=250,
        **extra_kwargs,
    )

    assert results[0]["content"] == expected_content
    scrape_options = calls[0]["scrape_options"]
    assert not isinstance(scrape_options, dict)
    assert getattr(scrape_options, "only_main_content") is True
    assert getattr(scrape_options, "wait_for") == 250
    assert calls[0]["urls"] == ["https://example.com"]
    if mode == "answer":
        assert calls[0]["prompt"] == "What is the answer?"
    else:
        assert calls[0]["schema"] == {"type": "object"}


def test_web_extract_schema_keeps_format_alias_and_modes():
    from tools.web_tools import WEB_EXTRACT_SCHEMA

    props = WEB_EXTRACT_SCHEMA["parameters"]["properties"]
    assert props["mode"]["enum"] == ["markdown", "html", "answer", "summary", "json", "links"]
    assert props["format"]["enum"] == ["markdown", "html"]
    assert {"question", "max_chars", "only_main_content", "wait_for", "schema"}.issubset(props)


@pytest.mark.asyncio
async def test_web_extract_max_chars_trims_after_provider(monkeypatch):
    from tools import web_tools

    class FakeProvider:
        name = "firecrawl"
        display_name = "Firecrawl"

        def supports_extract(self):
            return True

        async def extract(self, urls, **kwargs):
            content = "abcdef" * 500
            return [{"url": urls[0], "title": "", "content": content, "raw_content": content}]

    monkeypatch.setattr(web_tools, "is_safe_url", lambda url: True)
    async def no_fast(urls, **kwargs):
        return [], urls
    monkeypatch.setattr(web_tools, "try_fast_extract_urls", no_fast)
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: "firecrawl")
    monkeypatch.setattr("agent.web_search_registry.get_provider", lambda name: FakeProvider())
    monkeypatch.setattr(web_tools, "check_auxiliary_model", lambda: False)

    result = json.loads(
        await web_tools.web_extract_tool(["https://example.com"], max_chars=2000, use_llm_processing=False)
    )
    assert result["results"][0]["content"].startswith("abcdef")
    assert result["results"][0]["truncated"] is True


@pytest.mark.asyncio
async def test_web_extract_summary_skips_aux_llm_processing(monkeypatch):
    from tools import web_tools

    class FakeProvider:
        name = "firecrawl"
        display_name = "Firecrawl"

        def supports_extract(self):
            return True

        async def extract(self, urls, **kwargs):
            assert kwargs["mode"] == "summary"
            return [{"url": urls[0], "title": "", "content": "backend summary", "raw_content": "x" * 6000}]

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("summary mode should not run auxiliary LLM processing")

    monkeypatch.setattr(web_tools, "is_safe_url", lambda url: True)
    async def no_fast(urls, **kwargs):
        return [], urls
    monkeypatch.setattr(web_tools, "try_fast_extract_urls", no_fast)
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: "firecrawl")
    monkeypatch.setattr("agent.web_search_registry.get_provider", lambda name: FakeProvider())
    monkeypatch.setattr(web_tools, "check_auxiliary_model", lambda: True)
    monkeypatch.setattr(web_tools, "process_content_with_llm", fail_if_called)

    result = json.loads(await web_tools.web_extract_tool(["https://example.com"], mode="summary"))

    assert result["results"][0]["content"] == "backend summary"


@pytest.mark.asyncio
async def test_web_extract_registry_handler_disables_llm_for_summary(monkeypatch):
    from tools import web_tools
    from tools.registry import registry

    calls = []

    async def fake_web_extract_tool(urls, **kwargs):
        calls.append((urls, kwargs))
        return "{}"

    monkeypatch.setattr(web_tools, "web_extract_tool", fake_web_extract_tool)

    await registry._tools["web_extract"].handler({"urls": ["https://example.com"], "mode": "summary"})

    assert calls[0][1]["use_llm_processing"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_format", ["markdown", "html"])
async def test_web_extract_registry_handler_keeps_legacy_format_aux_processing(
    monkeypatch, legacy_format
):
    from tools import web_tools
    from tools.registry import registry

    calls = []

    async def fake_web_extract_tool(urls, **kwargs):
        calls.append((urls, kwargs))
        return "{}"

    monkeypatch.setattr(web_tools, "web_extract_tool", fake_web_extract_tool)

    await registry._tools["web_extract"].handler(
        {"urls": ["https://example.com"], "format": legacy_format}
    )

    assert calls == [
        (
            ["https://example.com"],
            {
                "format": legacy_format,
                "mode": legacy_format,
                "question": None,
                "max_chars": None,
                "char_limit": None,
                "only_main_content": None,
                "wait_for": None,
                "schema": None,
                "use_llm_processing": True,
            },
        )
    ]


@pytest.mark.asyncio
async def test_web_wrapper_curlmd_uses_plugin_helper(monkeypatch):
    from tools import web_tools

    class FakeCurlmdTool:
        @staticmethod
        def curlmd_fetch_tool(**kwargs):
            return json.dumps({"ok": True, "kwargs": kwargs})

    monkeypatch.setattr(web_tools, "_load_curlmd_tool_module", lambda: FakeCurlmdTool)

    result = json.loads(
        await web_tools._handle_web(
            {
                "action": "curlmd",
                "url": "https://example.com",
                "objective": "extract docs",
                "keywords": ["api"],
                "curlmd_mode": "rush",
                "fresh": True,
                "retries": 1,
                "timeout_seconds": 9,
                "fallback_to_curl": False,
                "max_chars": 123,
            }
        )
    )

    assert result["ok"] is True
    assert result["kwargs"] == {
        "url": "https://example.com",
        "objective": "extract docs",
        "keywords": ["api"],
        "mode": "rush",
        "fresh": True,
        "retries": 1,
        "timeout_seconds": 9,
        "fallback_to_curl": False,
        "max_chars": 123,
    }
