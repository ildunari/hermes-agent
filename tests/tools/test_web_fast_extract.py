import json

import httpx
import pytest

from tools import web_fast_extract
from tools import web_tools


class _AsyncTrue:
    async def __call__(self, *args, **kwargs):
        return True


@pytest.mark.asyncio
async def test_shopify_product_fast_path_uses_product_json(monkeypatch):
    product_json = {
        "title": "MCT Oil 60/40, 32 fl oz",
        "description": "<p>Pure MCT Oil</p>",
        "vendor": "Medical and Lab Supplies",
        "type": "Carrier Oils",
        "price": 2649,
        "available": True,
        "variants": [{"title": "Default Title", "price": 2649, "available": True, "sku": "OILSMCT32OZ"}],
        "featured_image": "//cdn.shopify.com/image.png",
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/products/mct-oil.js")
        return httpx.Response(200, json=product_json, headers={"content-type": "application/json"})

    monkeypatch.setattr(web_fast_extract, "check_website_access", lambda url: None)
    monkeypatch.setattr(web_fast_extract, "is_safe_url", lambda url: True)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        result = await web_fast_extract._try_shopify_product_json(
            client, "https://store.test/products/mct-oil?variant=1"
        )

    assert result["backend_used"] == "fast:shopify_product_json"
    assert result["title"] == "MCT Oil 60/40, 32 fl oz"
    assert "Price: $26.49" in result["content"]
    assert "Available: yes" in result["content"]
    assert "Pure MCT Oil" in result["content"]


@pytest.mark.asyncio
async def test_shopify_product_fast_path_rejects_unsafe_redirect(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"title": "Bad"},
            headers={"content-type": "application/json"},
            request=request,
            extensions={"network_stream": None},
        )

    monkeypatch.setattr(web_fast_extract, "check_website_access", lambda url: None)
    monkeypatch.setattr(web_fast_extract, "is_safe_url", lambda url: not str(url).startswith("http://127.0.0.1"))
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        # The helper must block before any unsafe target is fetched.
        with pytest.raises(web_fast_extract.FastExtractBlocked):
            await web_fast_extract._try_shopify_product_json(
                client, "http://127.0.0.1/products/bad"
            )


@pytest.mark.asyncio
async def test_safe_get_rejects_redirect_to_unsafe_url(monkeypatch):
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == "https://safe.test/start":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
        raise AssertionError("unsafe redirect target should not be fetched")

    monkeypatch.setattr(web_fast_extract, "check_website_access", lambda url: None)
    monkeypatch.setattr(web_fast_extract, "is_safe_url", lambda url: not str(url).startswith("http://127.0.0.1"))
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        with pytest.raises(web_fast_extract.FastExtractBlocked):
            await web_fast_extract._safe_get(client, "https://safe.test/start")

    assert seen == ["https://safe.test/start"]


@pytest.mark.asyncio
async def test_try_fast_extract_returns_blocked_result_without_provider_fallback(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    monkeypatch.setattr(web_fast_extract, "check_website_access", lambda url: None)
    monkeypatch.setattr(web_fast_extract, "is_safe_url", lambda url: not str(url).startswith("http://127.0.0.1"))

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(web_fast_extract.httpx, "AsyncClient", MockAsyncClient)

    results, fallback_urls = await web_fast_extract.try_fast_extract_urls(
        ["https://safe.test/start"],
        mode="markdown",
    )

    assert fallback_urls == []
    assert results[0]["error"] == "Blocked: URL targets a private or internal network address"
    assert results[0]["requested_url"] == "https://safe.test/start"


@pytest.mark.asyncio
async def test_web_extract_preserves_input_order_with_mixed_fast_and_provider(monkeypatch):
    async def fake_fast(urls, **kwargs):
        return [
            {
                "requested_url": urls[1],
                "url": urls[1],
                "title": "Fast",
                "content": "fast content",
                "raw_content": "fast content",
                "error": None,
                "backend_used": "fast:shopify_product_json",
            }
        ], [urls[0]]

    class FakeProvider:
        name = "fake"
        display_name = "Fake"

        def supports_extract(self):
            return True

        async def extract(self, urls, **kwargs):
            return [{"url": urls[0], "title": "Provider", "content": "provider content", "raw_content": "provider content"}]

    monkeypatch.setattr(web_tools, "try_fast_extract_urls", fake_fast)
    monkeypatch.setattr(web_tools, "is_safe_url", lambda url: True)
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: "fake")
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr("agent.web_search_registry.get_provider", lambda name: FakeProvider())

    result = json.loads(
        await web_tools.web_extract_tool(
            ["https://example.com/miss", "https://example.com/fast"],
            use_llm_processing=False,
        )
    )

    assert [r["url"] for r in result["results"]] == ["https://example.com/miss", "https://example.com/fast"]


@pytest.mark.asyncio
async def test_web_extract_preserves_order_when_provider_returns_final_url(monkeypatch):
    async def fake_fast(urls, **kwargs):
        return [
            {
                "requested_url": urls[1],
                "url": urls[1],
                "title": "Fast",
                "content": "fast content",
                "raw_content": "fast content",
                "error": None,
                "backend_used": "fast:shopify_product_json",
            }
        ], [urls[0]]

    class FakeProvider:
        name = "fake"
        display_name = "Fake"

        def supports_extract(self):
            return True

        async def extract(self, urls, **kwargs):
            return [{"url": "https://example.com/final", "title": "Provider", "content": "provider content", "raw_content": "provider content"}]

    monkeypatch.setattr(web_tools, "try_fast_extract_urls", fake_fast)
    monkeypatch.setattr(web_tools, "is_safe_url", lambda url: True)
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: "fake")
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr("agent.web_search_registry.get_provider", lambda name: FakeProvider())

    result = json.loads(
        await web_tools.web_extract_tool(
            ["https://example.com/redirect", "https://example.com/fast"],
            use_llm_processing=False,
        )
    )

    assert [r["title"] for r in result["results"]] == ["Provider", "Fast"]
    assert result["results"][0]["url"] == "https://example.com/final"


@pytest.mark.asyncio
async def test_jsonld_product_fast_path_from_html(monkeypatch):
    html = """
    <html><head><script type="application/ld+json">
    {"@type":"Product","name":"Sterile Vials","description":"Clear glass vials",
     "offers":{"price":"12.50","priceCurrency":"USD","availability":"https://schema.org/InStock"}}
    </script></head><body></body></html>
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    monkeypatch.setattr(web_fast_extract, "check_website_access", lambda url: None)
    monkeypatch.setattr(web_fast_extract, "is_safe_url", lambda url: True)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        result = await web_fast_extract._try_fast_extract_one(client, "https://example.test/product")

    assert result["backend_used"] == "fast:jsonld:Product"
    assert result["title"] == "Sterile Vials"
    assert "Price: USD 12.50" in result["content"]
    assert "Availability: InStock" in result["content"]


@pytest.mark.asyncio
async def test_web_extract_uses_fast_result_without_provider(monkeypatch):
    async def fake_fast(urls, **kwargs):
        return [
            {
                "url": urls[0],
                "title": "Fast Page",
                "content": "# Fast Page\n\nfast content",
                "raw_content": "# Fast Page\n\nfast content",
                "error": None,
                "backend_used": "fast:jsonld:Article",
            }
        ], []

    def fail_if_provider_loaded():
        raise AssertionError("provider fallback should not be loaded")

    monkeypatch.setattr(web_tools, "try_fast_extract_urls", fake_fast)
    monkeypatch.setattr(web_tools, "is_safe_url", lambda url: True)
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", fail_if_provider_loaded)

    result = json.loads(await web_tools.web_extract_tool(["https://example.com/page"], use_llm_processing=False))

    assert result["results"][0]["title"] == "Fast Page"
    assert result["results"][0]["backend_used"] == "fast:jsonld:Article"


@pytest.mark.asyncio
async def test_web_extract_falls_back_for_fast_miss(monkeypatch):
    async def fake_fast(urls, **kwargs):
        return [], urls

    class FakeProvider:
        name = "fake"
        display_name = "Fake"

        def supports_extract(self):
            return True

        async def extract(self, urls, **kwargs):
            return [{"url": urls[0], "title": "Provider", "content": "provider content", "raw_content": "provider content"}]

    monkeypatch.setattr(web_tools, "try_fast_extract_urls", fake_fast)
    monkeypatch.setattr(web_tools, "is_safe_url", lambda url: True)
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: "fake")
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr("agent.web_search_registry.get_provider", lambda name: FakeProvider())

    result = json.loads(await web_tools.web_extract_tool(["https://example.com/page"], use_llm_processing=False))

    assert result["results"][0]["title"] == "Provider"
    assert result["results"][0]["content"] == "provider content"
