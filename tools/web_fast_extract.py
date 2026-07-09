"""Cheap first-pass web extraction before paid/rendered backends.

These helpers intentionally cover common machine-readable surfaces that are
faster and cleaner than a rendered scrape: Shopify product JSON, WooCommerce /
WordPress REST endpoints, and generic JSON-LD/OpenGraph metadata in HTML.
They are conservative: a miss returns ``None`` so callers can fall back to the
configured extract provider.
"""
from __future__ import annotations

import html
import json
import logging
import re
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlparse

import httpx

from tools.url_safety import is_safe_url
from tools.website_policy import check_website_access

logger = logging.getLogger(__name__)


class FastExtractBlocked(RuntimeError):
    """Raised when the local fast fetch sees an unsafe/policy-blocked target."""

    def __init__(self, url: str, message: str) -> None:
        super().__init__(message)
        self.url = url
        self.message = message


FAST_EXTRACT_TIMEOUT = 2.5
FAST_EXTRACT_USER_AGENT = "HermesWebFastExtract/1.0 (+https://hermes-agent.nousresearch.com)"


async def try_fast_extract_urls(
    urls: List[str],
    *,
    mode: str,
    format: Optional[str] = None,
    only_main_content: Optional[bool] = None,
    wait_for: Optional[int] = None,
    question: Optional[str] = None,
    schema: Optional[dict] = None,
) -> tuple[List[Dict[str, Any]], List[str]]:
    """Return fast-path results plus URLs that still need provider fallback.

    Fast paths are deliberately limited to default markdown/text fetches. HTML,
    answer, summary, JSON schema, links, waits, and focused questions continue
    to use the configured backend because their semantics are provider-specific.
    """
    requested_mode = (mode or format or "markdown").lower()
    if (
        requested_mode != "markdown"
        or (format and format.lower() == "html")
        or wait_for is not None
        or question is not None
        or schema is not None
    ):
        return [], urls

    results: List[Dict[str, Any]] = []
    fallback_urls: List[str] = []
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=FAST_EXTRACT_TIMEOUT,
        headers={"User-Agent": FAST_EXTRACT_USER_AGENT, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"},
    ) as client:
        for url in urls:
            try:
                result = await _try_fast_extract_one(client, url, only_main_content=only_main_content)
            except FastExtractBlocked as exc:
                result = {
                    "url": exc.url,
                    "requested_url": url,
                    "title": "",
                    "content": "",
                    "raw_content": "",
                    "error": exc.message,
                    "backend_used": "fast:blocked_redirect",
                }
            except Exception as exc:  # noqa: BLE001 - fast path must never block fallback
                logger.debug("Fast web extract miss for %s: %s", url, exc)
                result = None
            if result is None:
                fallback_urls.append(url)
            else:
                result.setdefault("requested_url", url)
                results.append(result)
    return results, fallback_urls


async def _try_fast_extract_one(
    client: httpx.AsyncClient,
    url: str,
    *,
    only_main_content: Optional[bool] = None,
) -> Optional[Dict[str, Any]]:
    blocked = check_website_access(url)
    if blocked:
        return {
            "url": url,
            "title": "",
            "content": "",
            "error": blocked["message"],
            "blocked_by_policy": {
                "host": blocked["host"],
                "rule": blocked["rule"],
                "source": blocked["source"],
            },
            "backend_used": "fast:policy",
        }

    shopify = await _try_shopify_product_json(client, url)
    if shopify is not None:
        return shopify

    wordpress = await _try_wordpress_rest(client, url)
    if wordpress is not None:
        return wordpress

    response = await _safe_get(client, url)
    final_url = str(response.url)
    if not is_safe_url(final_url):
        return {
            "url": final_url,
            "title": "",
            "content": "",
            "error": "Blocked: URL targets a private or internal network address",
            "backend_used": "fast:blocked_redirect",
        }
    final_blocked = check_website_access(final_url)
    if final_blocked:
        return {
            "url": final_url,
            "title": "",
            "content": "",
            "error": final_blocked["message"],
            "blocked_by_policy": {
                "host": final_blocked["host"],
                "rule": final_blocked["rule"],
                "source": final_blocked["source"],
            },
            "backend_used": "fast:policy_redirect",
        }
    ctype = response.headers.get("content-type", "").lower()
    if "html" not in ctype and not response.text.lstrip().startswith("<"):
        return None
    text = response.text

    structured = _extract_jsonld_markdown(text, final_url)
    if structured is not None:
        return structured

    # For main-content requests, a basic article/main extraction is often cleaner
    # than falling straight to a rendered backend on static pages.
    if only_main_content:
        main = _extract_mainish_markdown(text, final_url)
        if main is not None:
            return main

    # Metadata-only OpenGraph cards are intentionally not treated as full
    # markdown extraction; fall back to the configured provider instead.
    return None


async def _safe_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    max_redirects: int = 5,
) -> httpx.Response:
    """GET a URL while validating each redirect target before following it."""
    current = url
    for _ in range(max_redirects + 1):
        blocked = check_website_access(current)
        if not is_safe_url(current):
            raise FastExtractBlocked(
                current,
                "Blocked: URL targets a private or internal network address",
            )
        if blocked:
            raise FastExtractBlocked(current, blocked["message"])
        response = await client.get(current, headers=headers, follow_redirects=False)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("location")
        if not location:
            return response
        current = urljoin(str(response.url), location)
    raise ValueError("Too many redirects")


async def _try_shopify_product_json(client: httpx.AsyncClient, url: str) -> Optional[Dict[str, Any]]:
    parsed = urlparse(url)
    match = re.match(r"^(.*/products/[^/?#]+)", parsed.path)
    if not match:
        return None
    product_url = parsed._replace(path=match.group(1) + ".js", query="", fragment="").geturl()
    try:
        response = await _safe_get(client, product_url, headers={"Accept": "application/json"})
    except FastExtractBlocked:
        raise
    except Exception:  # noqa: BLE001
        return None
    final_url = str(response.url)
    if not is_safe_url(final_url) or check_website_access(final_url):
        return None
    if response.status_code != 200:
        return None
    try:
        data = response.json()
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("title"):
        return None
    title = str(data.get("title") or "").strip()
    content = _shopify_product_to_markdown(data)
    return {
        "url": url,
        "title": title,
        "content": content,
        "raw_content": content,
        "error": None,
        "backend_used": "fast:shopify_product_json",
        "metadata": {"fast_url": product_url},
    }


async def _try_wordpress_rest(client: httpx.AsyncClient, url: str) -> Optional[Dict[str, Any]]:
    parsed = urlparse(url)
    slug = _last_slug(parsed.path)
    if not slug:
        return None
    base = f"{parsed.scheme}://{parsed.netloc}"
    candidates: List[tuple[str, str]] = []
    if "/product/" in parsed.path:
        candidates.append(("fast:woocommerce_store_product", f"{base}/wp-json/wc/store/products?slug={slug}"))
    candidates.extend(
        [
            ("fast:wordpress_post", f"{base}/wp-json/wp/v2/posts?slug={slug}&_fields=link,title,excerpt,content,date,modified"),
            ("fast:wordpress_page", f"{base}/wp-json/wp/v2/pages?slug={slug}&_fields=link,title,excerpt,content,date,modified"),
        ]
    )
    for backend, api_url in candidates:
        try:
            response = await _safe_get(client, api_url, headers={"Accept": "application/json"})
        except Exception:  # noqa: BLE001
            continue
        if not is_safe_url(str(response.url)) or check_website_access(str(response.url)):
            continue
        if response.status_code != 200 or "json" not in response.headers.get("content-type", "").lower():
            continue
        try:
            payload = response.json()
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, list) or not payload:
            continue
        item = payload[0]
        if not isinstance(item, dict):
            continue
        if backend.startswith("fast:woocommerce"):
            content = _woocommerce_product_to_markdown(item)
        else:
            content = _wordpress_item_to_markdown(item)
        title = _clean_html_text(_nested_rendered(item.get("title")) or item.get("name") or "")
        if content and title:
            return {
                "url": item.get("link") or url,
                "title": title,
                "content": content,
                "raw_content": content,
                "error": None,
                "backend_used": backend,
                "metadata": {"fast_url": api_url},
            }
    return None


def _shopify_product_to_markdown(data: Dict[str, Any]) -> str:
    title = str(data.get("title") or "").strip()
    vendor = str(data.get("vendor") or "").strip()
    product_type = str(data.get("type") or "").strip()
    description = _clean_html_text(data.get("description") or "")
    price = _format_cents(data.get("price"))
    available = data.get("available")
    variants = data.get("variants") if isinstance(data.get("variants"), list) else []
    sku = ", ".join(str(v.get("sku")) for v in variants if isinstance(v, dict) and v.get("sku"))
    lines = [f"# {title}", ""]
    if price:
        lines.append(f"Price: {price}")
    if available is not None:
        lines.append(f"Available: {'yes' if available else 'no'}")
    if vendor:
        lines.append(f"Vendor: {vendor}")
    if product_type:
        lines.append(f"Type: {product_type}")
    if sku:
        lines.append(f"SKU: {sku}")
    if variants:
        stockish = _variant_availability_summary(variants)
        if stockish:
            lines.append(f"Variants: {stockish}")
    if description:
        lines.extend(["", "## Description", description])
    image = data.get("featured_image") or (data.get("images") or [None])[0]
    if image:
        image_url = str(image)
        if image_url.startswith("//"):
            image_url = "https:" + image_url
        lines.extend(["", f"Image: {image_url}"])
    return "\n".join(lines).strip() + "\n"


def _woocommerce_product_to_markdown(item: Dict[str, Any]) -> str:
    title = _clean_html_text(item.get("name") or _nested_rendered(item.get("title")) or "")
    description = _clean_html_text(item.get("description") or item.get("short_description") or "")
    prices = item.get("prices") if isinstance(item.get("prices"), dict) else {}
    price = _format_woo_price(prices)
    availability = item.get("is_in_stock")
    lines = [f"# {title}", ""]
    if price:
        lines.append(f"Price: {price}")
    if availability is not None:
        lines.append(f"Available: {'yes' if availability else 'no'}")
    if item.get("sku"):
        lines.append(f"SKU: {item['sku']}")
    if description:
        lines.extend(["", "## Description", description])
    return "\n".join(lines).strip() + "\n"


def _wordpress_item_to_markdown(item: Dict[str, Any]) -> str:
    title = _clean_html_text(_nested_rendered(item.get("title")) or "")
    excerpt = _clean_html_text(_nested_rendered(item.get("excerpt")) or "")
    body = _clean_html_text(_nested_rendered(item.get("content")) or "")
    lines = [f"# {title}", ""]
    if item.get("date"):
        lines.append(f"Date: {item['date']}")
    if excerpt:
        lines.extend(["", excerpt])
    if body and body != excerpt:
        lines.extend(["", body])
    return "\n".join(lines).strip() + "\n"


def _extract_jsonld_markdown(html_text: str, url: str) -> Optional[Dict[str, Any]]:
    scripts = re.findall(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    nodes: List[Dict[str, Any]] = []
    for script in scripts:
        raw = html.unescape(script).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        nodes.extend(_flatten_jsonld(data))
    preferred_types = ("Product", "Recipe", "FAQPage", "LocalBusiness", "NewsArticle", "Article", "BlogPosting")
    for typ in preferred_types:
        for node in nodes:
            if _jsonld_has_type(node, typ):
                if typ in {"NewsArticle", "Article", "BlogPosting"} and not node.get("articleBody"):
                    continue
                content = _jsonld_node_to_markdown(node)
                title = _clean_html_text(node.get("name") or node.get("headline") or "")
                if content and title:
                    return {
                        "url": url,
                        "title": title,
                        "content": content,
                        "raw_content": content,
                        "error": None,
                        "backend_used": f"fast:jsonld:{typ}",
                    }
    return None


def _jsonld_node_to_markdown(node: Dict[str, Any]) -> str:
    title = _clean_html_text(node.get("name") or node.get("headline") or "")
    lines = [f"# {title}", ""]
    description = _clean_html_text(node.get("description") or node.get("articleBody") or "")
    offers = node.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if isinstance(offers, dict):
        price = offers.get("price") or offers.get("lowPrice")
        currency = offers.get("priceCurrency")
        availability = offers.get("availability")
        if price:
            lines.append(f"Price: {currency + ' ' if currency else ''}{price}")
        if availability:
            lines.append(f"Availability: {str(availability).rsplit('/', 1)[-1]}")
    author = node.get("author")
    if isinstance(author, dict):
        author = author.get("name")
    if author:
        lines.append(f"Author: {_clean_html_text(author)}")
    date = node.get("datePublished") or node.get("dateModified")
    if date:
        lines.append(f"Date: {date}")
    if description:
        lines.extend(["", description])
    return "\n".join(lines).strip() + "\n"


def _extract_og_markdown(html_text: str, url: str) -> Optional[Dict[str, Any]]:
    parser = _HeadMetaParser()
    parser.feed(html_text[:200_000])
    title = parser.meta.get("og:title") or parser.title
    description = parser.meta.get("og:description") or parser.meta.get("description") or parser.meta.get("twitter:description")
    if not title or not description:
        return None
    lines = [f"# {_clean_html_text(title)}", "", _clean_html_text(description)]
    if parser.meta.get("og:image"):
        lines.extend(["", f"Image: {urljoin(url, parser.meta['og:image'])}"])
    content = "\n".join(lines).strip() + "\n"
    return {"url": url, "title": _clean_html_text(title), "content": content, "raw_content": content, "error": None, "backend_used": "fast:opengraph"}


def _extract_mainish_markdown(html_text: str, url: str) -> Optional[Dict[str, Any]]:
    match = re.search(r"<(article|main)\b[^>]*>(.*?)</\1>", html_text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    body = _clean_html_text(match.group(2))
    if len(body) < 200:
        return None
    title_match = re.search(r"<h1\b[^>]*>(.*?)</h1>", html_text, flags=re.IGNORECASE | re.DOTALL)
    title = _clean_html_text(title_match.group(1)) if title_match else _clean_html_text(_HeadMetaParser.title_from(html_text))
    if not title:
        return None
    content = f"# {title}\n\n{body}\n"
    return {"url": url, "title": title, "content": content, "raw_content": content, "error": None, "backend_used": "fast:main_html"}


def _flatten_jsonld(data: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        if isinstance(data.get("@graph"), list):
            for item in data["@graph"]:
                out.extend(_flatten_jsonld(item))
        out.append(data)
    elif isinstance(data, list):
        for item in data:
            out.extend(_flatten_jsonld(item))
    return out


def _jsonld_has_type(node: Dict[str, Any], typ: str) -> bool:
    value = node.get("@type")
    if isinstance(value, list):
        return typ in value
    return value == typ


def _last_slug(path: str) -> str:
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        return ""
    slug = parts[-1]
    return re.sub(r"\.(html?|php)$", "", slug, flags=re.IGNORECASE)


def _nested_rendered(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("rendered") or "")
    return str(value or "")


def _format_cents(value: Any) -> str:
    try:
        cents = int(value)
    except (TypeError, ValueError):
        return ""
    return f"${cents / 100:.2f}"


def _format_woo_price(prices: Dict[str, Any]) -> str:
    raw = prices.get("price") or prices.get("regular_price")
    if raw is None:
        return ""
    currency = prices.get("currency_code") or ""
    minor = int(prices.get("currency_minor_unit") or 2)
    try:
        amount = int(raw) / (10 ** minor)
        return f"{currency + ' ' if currency else ''}{amount:.{minor}f}"
    except (TypeError, ValueError):
        return str(raw)


def _variant_availability_summary(variants: Iterable[Any]) -> str:
    bits: List[str] = []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        title = variant.get("title") or variant.get("name") or "Variant"
        price = _format_cents(variant.get("price"))
        available = variant.get("available")
        parts = [str(title)]
        if price:
            parts.append(price)
        if available is not None:
            parts.append("available" if available else "unavailable")
        bits.append(" / ".join(parts))
    return "; ".join(bits[:8])


def _clean_html_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|section|article)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


class _HeadMetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: Dict[str, str] = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        attrs_dict = {k.lower(): v or "" for k, v in attrs}
        if tag.lower() == "title":
            self._in_title = True
        if tag.lower() != "meta":
            return
        key = attrs_dict.get("property") or attrs_dict.get("name")
        content = attrs_dict.get("content")
        if key and content:
            self.meta[key.lower()] = content

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data

    @staticmethod
    def title_from(html_text: str) -> str:
        parser = _HeadMetaParser()
        parser.feed(html_text[:100_000])
        return parser.title
