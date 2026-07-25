#!/usr/bin/env python3
"""
Standalone Web Tools Module

This module provides generic web tools that work with multiple backend providers.
Backend is selected during ``hermes tools`` setup (web.backend in config.yaml).
When available, Hermes can route Firecrawl calls through a Nous-hosted tool-gateway
for Nous Subscribers only.

Available tools:
- web_search_tool: Search the web for information
- web_extract_tool: Extract content from specific web pages

Backend compatibility:
- Exa: https://exa.ai (search, extract)
- Firecrawl: https://docs.firecrawl.dev/introduction (search, extract; direct or derived firecrawl-gateway.<domain> for Nous Subscribers)
- Parallel: https://docs.parallel.ai (search, extract)
- Tavily: https://tavily.com (search, extract)

LLM Processing:
- Uses OpenRouter API with Gemini 3 Flash Preview for intelligent content extraction
- Extracts key excerpts and creates markdown summaries to reduce token usage

Debug Mode:
- Set WEB_TOOLS_DEBUG=true to enable detailed logging
- Creates web_tools_debug_UUID.json in ./logs directory
- Captures all tool calls, results, and compression metrics

Usage:
    from web_tools import web_search_tool, web_extract_tool
    
    # Search the web
    results = web_search_tool("Python machine learning libraries", limit=3)
    
    # Extract content from URLs  
    content = web_extract_tool(["https://example.com"], format="markdown")
"""

import json
import logging
import os
import re
import asyncio
import inspect
from typing import List, Dict, Any, Optional, TYPE_CHECKING
import httpx  # noqa: F401 — kept at module top so tests can patch tools.web_tools.httpx
# After the web-provider plugin migration (PR #25182), the Firecrawl SDK
# proxy, client construction, and response-shape normalizers all live in
# plugins.web.firecrawl.provider. We re-export the names that external
# code, integration tests, and unit-test patches reach for so the public
# surface stays stable.
if TYPE_CHECKING:
    from firecrawl import Firecrawl  # noqa: F401 — type hints only
from plugins.web.firecrawl.provider import (
    Firecrawl,  # noqa: F401  # re-exported for tests that mock.patch("tools.web_tools.Firecrawl")
    _firecrawl_backend_help_suffix,
    _get_firecrawl_client,  # noqa: F401  # re-exported for tests that `from tools.web_tools import _get_firecrawl_client`
    _get_firecrawl_gateway_url,
    _is_tool_gateway_ready,
    check_firecrawl_api_key,
)
# Tavily helpers re-exported for backward-compat with existing unit tests
# (tests/tools/test_web_tools_tavily.py imports these names directly).
from plugins.web.tavily.provider import (  # noqa: F401 — backward-compat names
    _normalize_tavily_documents,
    _normalize_tavily_search_results,
    _tavily_request,
)
# Parallel + Exa clients re-exported for backward-compat with existing
# unit tests (tests/tools/test_web_tools_config.py imports _get_parallel_client
# / _get_async_parallel_client / _get_exa_client directly).
from plugins.web.parallel.provider import (  # noqa: F401 — backward-compat names
    _get_async_parallel_client,
    _get_parallel_client,
)
from plugins.web.exa.provider import _get_exa_client  # noqa: F401

# Module-level cache slots for the per-vendor clients. The plugins read/write
# these via tools.web_tools so unit tests that reset
# ``tools.web_tools._<vendor>_client = None`` between cases keep working.
_firecrawl_client: Optional[Any] = None
_firecrawl_client_config: Optional[Any] = None
_parallel_client: Optional[Any] = None
_async_parallel_client: Optional[Any] = None
_exa_client: Optional[Any] = None

from tools.debug_helpers import DebugSession
# Imported solely so unit tests can monkeypatch these names on
# tools.web_tools (the firecrawl plugin reads them via its own import chain).
from tools.managed_tool_gateway import (  # noqa: F401 — backward-compat names for tests
    build_vendor_gateway_url,
    peek_nous_access_token as _peek_nous_access_token,
    read_nous_access_token as _read_nous_access_token,
    resolve_managed_tool_gateway,
)
from tools.tool_backend_helpers import (  # noqa: F401
    managed_nous_tools_enabled,
    nous_tool_gateway_unavailable_message,
    prefers_gateway,
)
from tools.url_safety import async_is_safe_url, is_safe_url, normalize_url_for_request, sensitive_query_param_name
from tools.web_fast_extract import try_fast_extract_urls
import sys

logger = logging.getLogger(__name__)


def _web_extract_url(value: Any) -> Optional[str]:
    """Return a usable URL from a model-supplied extract item.

    Models sometimes forward a complete web-search result instead of its URL.
    Accept the two common URL keys, but reject missing/non-string values rather
    than stringifying arbitrary objects into misleading fetch targets.
    """
    if isinstance(value, dict):
        value = value.get("url") or value.get("href")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


# ─── Backend Selection ────────────────────────────────────────────────────────

def _env_value(name: str) -> str:
    """Resolve ``name`` via Hermes config-aware env, falling back to process env.

    Mirrors the SearXNG provider's ``_searxng_url()`` so that values set
    through Hermes' config/.env layer (``hermes config set``, ``hermes tools``)
    are honored here too — not just raw process-env exports. Without this,
    a config-only ``SEARXNG_URL`` (or any provider key) leaves the backend
    auto-detect cascade and ``check_web_api_key()`` blind to it. See #34290.
    """
    try:
        from hermes_cli.config import get_env_value

        val = get_env_value(name)
    except Exception:
        val = None
    if val is None:
        val = os.getenv(name, "")
    return (val or "").strip()


def _has_env(name: str) -> bool:
    return bool(_env_value(name))

def _load_web_config() -> dict:
    """Load the ``web:`` section from ~/.hermes/config.yaml."""
    try:
        from hermes_cli.config import load_config
        # ``or {}``: a present-but-null ``web:`` section (YAML ``web:`` with no
        # body) makes ``.get("web", {})`` return None, which would break every
        # caller that does ``_load_web_config().get(...)``. Honor the ``-> dict``
        # contract so callers never see None.
        return load_config().get("web") or {}
    except (ImportError, Exception):
        return {}


# The built-in web backends whose availability is driven by hardcoded
# env-var / package / OAuth probes below. Any name NOT in this set is a
# candidate plugin-registered provider and must be resolved through the
# web_search_registry (``is_available()``) instead. Kept as a single named
# constant so the whitelist early-returns and the availability chokepoint
# stay in sync.
#
# NOTE: this intentionally includes ``xai``, which the registry's
# ``_LEGACY_PREFERENCE`` does NOT — xai availability is probed via
# ``has_xai_credentials()`` (env var OR auth.json OAuth), not a registered
# WebSearchProvider. Keep the two sets aligned by hand: if xai ever ships as
# a registered provider, drop it here so the registry path takes over.
_LEGACY_WEB_BACKENDS = frozenset(
    {"parallel", "firecrawl", "tavily", "exa", "searxng", "brave-free", "ddgs", "xai"}
)


def _registered_web_provider(backend: str):
    """Return a plugin-registered web provider by name, or ``None``.

    Consults ``agent.web_search_registry`` so backends contributed by the
    plugin system (which are absent from :data:`_LEGACY_WEB_BACKENDS`) are
    discoverable during availability/selection resolution. Returns ``None``
    on any lookup failure so callers can fall through to legacy checks.
    """
    if not backend:
        return None
    try:
        from agent.web_search_registry import get_provider

        return get_provider(backend)
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("web provider registry lookup failed for %r: %s", backend, exc)
        return None


def _registered_web_provider_available(backend: str):
    """Availability of a *registered* web provider, or ``None`` if unregistered.

    Returns ``True``/``False`` when *backend* names a registered provider
    (calling its ``is_available()``), or ``None`` when it isn't registered —
    letting the caller fall through to the legacy built-in probes.
    """
    provider = _registered_web_provider(backend)
    if provider is None:
        return None
    try:
        return bool(provider.is_available())
    except Exception as exc:  # noqa: BLE001 — a broken provider is "unavailable"
        logger.debug("web provider %r.is_available() raised: %s", backend, exc)
        return False


def _list_registered_web_providers():
    """Return all plugin-registered web providers (empty list on failure)."""
    try:
        from agent.web_search_registry import list_providers

        return list_providers()
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("web provider registry list failed: %s", exc)
        return []


def _get_backend() -> str:
    """Determine which web backend to use (shared fallback).

    Reads ``web.backend`` from config.yaml (set by ``hermes tools``).
    Falls back to whichever API key is present for users who configured
    keys manually without running setup.
    """
    configured = (_load_web_config().get("backend") or "").lower().strip()
    if configured in _LEGACY_WEB_BACKENDS or _registered_web_provider(configured) is not None:
        return configured

    # Fallback for manual / legacy config — pick the highest-priority
    # available backend. Explicit user credentials (TAVILY_API_KEY etc.)
    # beat the managed-tool-gateway probe so a deliberate setup is not
    # pre-empted by a Nous OAuth token whose subscription tier may not
    # actually grant web-search access (the gateway then fails at runtime
    # with "no subscription" and the tool returns an error to the agent
    # without falling back). Free-tier backends trail the paid ones.
    backend_candidates = (
        ("tavily", _has_env("TAVILY_API_KEY")),
        ("exa", _has_env("EXA_API_KEY")),
        ("parallel", _has_env("PARALLEL_API_KEY")),
        ("firecrawl", _has_env("FIRECRAWL_API_KEY") or _has_env("FIRECRAWL_API_URL")),
        ("firecrawl", _is_tool_gateway_ready()),
        ("searxng", _has_env("SEARXNG_URL")),
        ("brave-free", _has_env("BRAVE_SEARCH_API_KEY")),
        ("ddgs", _ddgs_package_importable()),
    )
    for backend, available in backend_candidates:
        if available:
            return backend

    # Final fallback: walk plugin-registered providers so a custom backend
    # (with no built-in creds present) still resolves. Built-in names are
    # already covered above, so this only surfaces plugin-contributed
    # providers via their own is_available() gate. We hold the provider
    # object already, so probe it directly rather than round-tripping through
    # _is_backend_available() (which would re-do the registry lookup).
    for provider in _list_registered_web_providers():
        if provider.name in _LEGACY_WEB_BACKENDS:
            continue
        try:
            if provider.is_available():
                return provider.name
        except Exception as exc:  # noqa: BLE001 — a broken provider is skipped
            logger.debug("web provider %r.is_available() raised: %s", provider.name, exc)

    return "firecrawl"  # default (backward compat)


def _get_search_backend() -> str:
    """Determine which backend to use for web_search specifically.

    Selection priority:
    1. ``web.search_backend`` (per-capability override)
    2. ``web.backend`` (shared fallback — existing behavior)
    3. Auto-detect from env vars

    This enables using different providers for search vs extract
    (e.g. SearXNG for search + Firecrawl for extract).
    """
    return _get_capability_backend("search")


def _get_extract_backend() -> str:
    """Determine which backend to use for web_extract specifically.

    Selection priority:
    1. ``web.extract_backend`` (per-capability override)
    2. ``web.backend`` (shared fallback — existing behavior)
    3. Auto-detect from env vars
    """
    return _get_capability_backend("extract")


def _get_capability_backend(capability: str) -> str:
    """Shared helper for per-capability backend selection.

    Reads ``web.{capability}_backend`` from config; if set and available,
    uses it. Otherwise falls through to the shared ``_get_backend()``.
    """
    cfg = _load_web_config()
    specific = (cfg.get(f"{capability}_backend") or "").lower().strip()
    if specific and _is_backend_available(specific):
        return specific
    return _get_backend()


def _is_backend_available(backend: str) -> bool:
    """Return True when the selected backend is currently usable.

    For plugin-registered backends (any name outside
    :data:`_LEGACY_WEB_BACKENDS`), availability is delegated to the
    provider's ``is_available()`` via the web_search_registry. This is the
    single chokepoint through which ``_get_backend``,
    ``_get_capability_backend``, and ``check_web_api_key`` all resolve
    availability — fixing custom-provider discovery for every caller at once
    (issues #28651, #31873, #32698). Built-in backends keep their cheap
    hardcoded probes below.
    """
    backend = (backend or "").lower().strip()
    if backend not in _LEGACY_WEB_BACKENDS:
        registered = _registered_web_provider_available(backend)
        if registered is not None:
            return registered
    if backend == "exa":
        return _has_env("EXA_API_KEY")
    if backend == "parallel":
        return _has_env("PARALLEL_API_KEY")
    if backend == "firecrawl":
        return check_firecrawl_api_key()
    if backend == "tavily":
        return _has_env("TAVILY_API_KEY")
    if backend == "searxng":
        return _has_env("SEARXNG_URL")
    if backend == "brave-free":
        return _has_env("BRAVE_SEARCH_API_KEY")
    if backend == "ddgs":
        return _ddgs_package_importable()
    if backend == "xai":
        # Cheap probe — env var OR auth.json has OAuth tokens. Must not
        # call resolve_xai_http_credentials() here because the OAuth path
        # can trigger a network token refresh, and _is_backend_available
        # runs on every web_search dispatch + every `hermes tools` repaint.
        try:
            from tools.xai_http import has_xai_credentials
            return has_xai_credentials()
        except Exception:
            return False
    return False


def _backend_usable(backend: str) -> bool:
    """Backward-compatible alias for tests and availability probes."""
    return _is_backend_available(backend)


async def _safe_url_allowed(url: str) -> bool:
    """URL safety hook that supports legacy sync test monkeypatches."""
    result = is_safe_url(url)
    if inspect.isawaitable(result):
        result = await result
    return bool(result)


def _ddgs_package_importable() -> bool:
    """Return True when the ``ddgs`` Python package can be imported.

    ddgs is the only backend whose availability is driven by a package
    presence rather than an env var / config entry.  Wrapped in a helper
    so auto-detect and ``_is_backend_available`` share the same check
    (and tests can monkeypatch a single symbol).
    """
    try:
        import ddgs  # noqa: F401
        return True
    except ImportError:
        return False

# ─── Firecrawl Client ────────────────────────────────────────────────────────

# ─── Firecrawl Client ────────────────────────────────────────────────────────
# After PR #25182, the firecrawl client, lazy SDK proxy, dual-auth config
# resolution, response normalizers, and check_firecrawl_api_key() all live
# in plugins.web.firecrawl.provider and are re-exported at the top of this
# module so external callers (integration tests, tool-registry gating) and
# unit tests that patch tools.web_tools.<name> continue to work.


def _web_requires_env() -> list[str]:
    """Return tool metadata env vars for the currently enabled web backends.

    The gateway env vars are always reported — they're metadata strings
    used by the tool registry to light up the tool when the variable is
    set.  Gating them on ``managed_nous_tools_enabled()`` only saved
    string noise in the metadata list, but cost a synchronous HTTP
    refresh against the Nous portal on every CLI startup (invoked at
    tool-registration time).  The behavioral contract is: if the env var
    is set, the tool sees it; if not, it doesn't.  Not-logged-in users
    simply don't have the vars set, so the extra entries are harmless.
    """
    return [
        "EXA_API_KEY",
        "PARALLEL_API_KEY",
        "TAVILY_API_KEY",
        "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL",
        "FIRECRAWL_GATEWAY_URL",
        "TOOL_GATEWAY_DOMAIN",
        "TOOL_GATEWAY_SCHEME",
        "TOOL_GATEWAY_USER_TOKEN",
    ]


# ─── Parallel / Tavily / Firecrawl helpers — moved into plugins ──────────────
# After PR #25182, the per-vendor client construction, request helpers, and
# response normalizers all live in plugins.web.<vendor>.provider:
#   - parallel: plugins/web/parallel/provider.py
#   - tavily:   plugins/web/tavily/provider.py
#   - firecrawl: plugins/web/firecrawl/provider.py
# The names from the firecrawl plugin (Firecrawl proxy, _get_firecrawl_client,
# _to_plain_object, _normalize_result_list, _extract_web_search_results,
# _extract_scrape_payload, _is_tool_gateway_ready, etc.) are re-exported at
# the top of this module for backward-compat with integration tests and
# unit-test patches.


# Default budget (characters) of clean page text sent to the model. Pages at
# or under this size are returned whole; larger pages are head+tail truncated
# and the full text is stored on disk (see _store_full_text). Spending context,
# not API dollars — so this is generous relative to the old 5k summary cap.
# Override via web.extract_char_limit in config.yaml.
DEFAULT_EXTRACT_CHAR_LIMIT = 15000
# Backward-compatible threshold for legacy LLM summarization paths that still
# call web_extract_tool(..., use_llm_processing=True). Upstream's new default
# path uses DEFAULT_EXTRACT_CHAR_LIMIT for direct extraction/truncation.
DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION = 5000


def _is_nous_auxiliary_client(client: Any) -> bool:
    """Return True when the resolved auxiliary backend is Nous Portal."""
    from urllib.parse import urlparse

    base_url = str(getattr(client, "base_url", "") or "")
    host = (urlparse(base_url).hostname or "").lower()
    return host == "nousresearch.com" or host.endswith(".nousresearch.com")


def _resolve_web_extract_auxiliary(model: Optional[str] = None) -> tuple[Optional[Any], Optional[str], Dict[str, Any]]:
    """Resolve the current web-extract auxiliary client, model, and extra body."""
    from agent.auxiliary_client import get_async_text_auxiliary_client

    client, default_model = get_async_text_auxiliary_client("web_extract")
    configured_model = os.getenv("AUXILIARY_WEB_EXTRACT_MODEL", "").strip()
    effective_model = model or configured_model or default_model

    extra_body: Dict[str, Any] = {}
    if client is not None and _is_nous_auxiliary_client(client):
        from agent.auxiliary_client import get_auxiliary_extra_body
        from agent.portal_tags import nous_portal_tags
        extra_body = get_auxiliary_extra_body() or {"tags": nous_portal_tags()}

    return client, effective_model, extra_body


def _get_default_summarizer_model() -> Optional[str]:
    """Return the current default model for web extraction summarization."""
    _, model, _ = _resolve_web_extract_auxiliary()
    return model


async def process_content_with_llm(
    raw_content: str,
    url: str,
    title: str,
    model: Optional[str],
    min_length: int = DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION,
) -> Optional[str]:
    """Legacy auxiliary summarization hook for oversized markdown/html extracts.

    Modern ``web`` calls rely on provider-native focused modes and
    truncate-and-store. Keep this hook present for compatibility and return
    ``None`` for short/no-content inputs so callers retain the original text.
    """
    if not raw_content or len(raw_content) < min_length:
        return None
    return None


# Hard ceiling on the full-text file written to cache/web. The truncate-store
# path otherwise calls path.write_text(content, encoding="utf-8") with no upper bound, so a
# multi-MB page (some backends return very large markdown) writes unbounded
# bytes to disk on every extract. Cap the stored copy; the model only ever
# sees char_limit anyway, and a 2MB page is already far more than any single
# read_file paging session needs. Mirrors the pre-truncate-store era's 2MB
# refusal ceiling, but stores (capped) instead of refusing.
MAX_STORED_TEXT_CHARS = 2_000_000

_debug = DebugSession("web_tools", env_var="WEB_TOOLS_DEBUG")


def _get_extract_char_limit() -> int:
    """Resolve the per-page char budget from config, clamped to a sane range."""
    try:
        configured = _load_web_config().get("extract_char_limit")
        if configured is not None:
            value = int(configured)
            # Floor at 2k (below that the footer dominates), no hard ceiling
            # beyond a generous guard so a typo can't blow up context.
            return max(2000, min(value, 500_000))
    except (TypeError, ValueError):
        pass
    return DEFAULT_EXTRACT_CHAR_LIMIT


def convert_base64_images_to_links(text: str) -> str:
    """Replace inline base64 image blobs with labeled markdown links.

    base64 image payloads are token bombs (a single inline PNG can be tens of
    thousands of characters), so we never send the raw bytes to the model. But
    we preserve the fact that an image was there, and its alt text, as an
    inspectable placeholder. Real (http/https) markdown image links are left
    untouched so the agent can ``web_extract`` / ``vision_analyze`` them.

    Transformations:
      ``![alt](data:image/png;base64,AAAA...)``  -> ``[IMAGE: alt](base64 image omitted)``
      ``(data:image/png;base64,AAAA...)``        -> ``[IMAGE]``
      bare ``data:image/...;base64,AAAA...``     -> ``[IMAGE]``
    """
    # 1. Markdown image with base64 source -> keep alt text, drop the blob.
    def _md_repl(m: "re.Match[str]") -> str:
        alt = (m.group("alt") or "").strip()
        return f"[IMAGE: {alt}]" if alt else "[IMAGE]"

    md_b64 = re.compile(
        r"!\[(?P<alt>[^\]]*)\]\(\s*data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+\)"
    )
    out = md_b64.sub(_md_repl, text)

    # 2. Parenthesised base64 (non-markdown) and 3. bare base64 -> [IMAGE].
    out = re.sub(r"\(\s*data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+\)", "[IMAGE]", out)
    out = re.sub(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", "[IMAGE]", out)
    return out


def _store_full_text(url: str, content: str) -> Optional[str]:
    """Write the full extracted page to cache/web and return its absolute path.

    The file is mounted read-only into remote backends (Docker/Modal/SSH) via
    credential_files._CACHE_DIRS, so the agent's terminal/read_file tools can
    page through the complete text on any backend. Returns None on failure
    (storage is best-effort; truncated content is still returned to the model).
    """
    try:
        import hashlib
        from urllib.parse import urlparse
        from hermes_constants import get_hermes_dir

        cache_dir = get_hermes_dir("cache/web", "web_cache")
        cache_dir.mkdir(parents=True, exist_ok=True)

        host = (urlparse(url).hostname or "page").replace(":", "_")
        slug = re.sub(r"[^A-Za-z0-9._-]", "-", host)[:60].strip("-") or "page"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        path = cache_dir / f"{slug}-{digest}.md"
        # Bound the stored copy so a pathologically large page can't write
        # unbounded bytes to disk. If capped, append a marker so a reader of
        # the file knows it isn't the literal complete page.
        if len(content) > MAX_STORED_TEXT_CHARS:
            content = (
                content[:MAX_STORED_TEXT_CHARS]
                + f"\n\n[... stored copy truncated at {MAX_STORED_TEXT_CHARS:,} chars "
                f"of {len(content):,}; re-extract a more specific URL for the rest ...]"
            )
        path.write_text(content, encoding="utf-8")
        return str(path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to store full web_extract text for %s: %s", url, exc)
        return None


def _truncate_with_footer(
    content: str,
    url: str,
    char_limit: int,
) -> tuple[str, bool]:
    """Return (model_text, was_truncated) for one page's clean content.

    Pages at or under ``char_limit`` are returned whole. Larger pages get a
    head+tail window (~75% head / ~25% tail) cut on a markdown line boundary
    where possible, plus an explicit footer telling the model exactly how much
    it is seeing, where the full text is stored, and which read_file call pages
    in the omitted middle. Deterministic — no model involvement.
    """
    if len(content) <= char_limit:
        return content, False

    head_budget = int(char_limit * 0.75)
    tail_budget = char_limit - head_budget

    head = content[:head_budget]
    tail = content[-tail_budget:]
    # Snap the head cut back to the last newline so we don't slice mid-line.
    nl = head.rfind("\n")
    if nl > head_budget * 0.5:
        head = head[:nl]
    # Snap the tail cut forward to the next newline for the same reason.
    nl = tail.find("\n")
    if 0 <= nl < tail_budget * 0.5:
        tail = tail[nl + 1:]

    total = len(content)
    stored_path = _store_full_text(url, content)
    shown = len(head) + len(tail)

    footer_lines = [
        "",
        "─" * 8 + " [TRUNCATED] " + "─" * 8,
        f"Showing {len(head):,} chars (head) + {len(tail):,} chars (tail) "
        f"of {total:,} total clean characters.",
    ]
    if stored_path:
        # The omitted middle begins right after the head we're showing. Give
        # the model a concrete starting line (head line count + 1) so its first
        # read_file lands in the gap instead of guessing <line>. read_file is
        # 1-indexed; +1 moves past the last head line we already showed.
        middle_start_line = head.count("\n") + 2
        footer_lines.append(f"Full text saved to: {stored_path}")
        footer_lines.append(
            f'To read the omitted middle: read_file path="{stored_path}" '
            f"offset={middle_start_line} limit=200  (the file is the complete page; "
            f"raise/lower offset to page through it)."
        )
    else:
        footer_lines.append(
            "Full text could not be stored; re-run web_extract on a more "
            "specific URL or use browser_navigate for the complete page."
        )
    footer_lines.append("─" * 29)

    model_text = head + "\n\n[... middle omitted — see footer ...]\n\n" + tail
    model_text += "\n" + "\n".join(footer_lines)
    return model_text, True



# ─── Exa / Parallel inline helpers — moved into plugins ──────────────────────
# After PR #25182, the exa client + search/extract and parallel client +
# search/extract helpers all live in their respective plugins:
#   - plugins/web/exa/provider.py
#   - plugins/web/parallel/provider.py
# Both plugins register through agent.web_search_registry and the
# dispatchers in this file resolve them via get_active_*_provider().


def _ensure_web_plugins_loaded() -> None:
    """Idempotently trigger plugin discovery so the web registry is populated.

    Every bundled web provider (brave-free, ddgs, searxng, exa, parallel,
    tavily, firecrawl) registers itself via ``plugins/web/<vendor>/__init__.py``
    during plugin discovery. Tool dispatch can be reached from contexts that
    haven't already triggered discovery — subprocess agent runs, delegate
    children, standalone scripts, certain test paths — and without it the
    registry is empty and ``get_provider('firecrawl')`` returns ``None`` even
    when the user has ``web.extract_backend: firecrawl`` configured and
    ``FIRECRAWL_API_KEY`` set. The symptom is a misleading "No web extract
    provider configured" error (issue #27580).

    Mirrors :func:`tools.browser_tool._ensure_browser_plugins_loaded` exactly:
    the underlying discovery call is idempotent and cheap on subsequent
    invocations.
    """
    try:
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
    except Exception as exc:  # noqa: BLE001
        # Warning, not debug: if a plugin import is genuinely broken the
        # user otherwise hits the misleading "No web extract provider
        # configured" error this helper is meant to eliminate, with no
        # clue in normal logs about the real cause.
        logger.warning("Web plugin discovery failed (non-fatal): %s", exc)


def web_search_tool(query: str, limit: int = 5) -> str:
    """
    Search the web for information using available search API backend.

    This function provides a generic interface for web search that can work
    with multiple backends (Parallel or Firecrawl).

    Note: This function returns search result metadata only (URLs, titles, descriptions).
    Use web_extract_tool to get full content from specific URLs.
    
    Args:
        query (str): The search query to look up
        limit (int): Maximum number of results to return (default: 5)
    
    Returns:
        str: JSON string containing search results with the following structure:
             {
                 "success": bool,
                 "data": {
                     "web": [
                         {
                             "title": str,
                             "url": str,
                             "description": str,
                             "position": int
                         },
                         ...
                     ]
                 }
             }
    
    Raises:
        Exception: If search fails or API key is not set
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 5
    limit = min(max(limit, 1), 100)

    debug_call_data = {
        "parameters": {
            "query": query,
            "limit": limit
        },
        "error": None,
        "results_count": 0,
        "original_response_size": 0,
        "final_response_size": 0
    }
    
    try:
        from tools.interrupt import is_interrupted
        if is_interrupted():
            return tool_error("Interrupted", success=False)

        # Dispatch through the web search registry. All 7 providers
        # (brave-free, ddgs, searxng, exa, parallel, tavily, firecrawl)
        # now live as plugins; the dispatcher is just a registry lookup +
        # delegation. Sync only — every provider's search() is sync.
        _ensure_web_plugins_loaded()
        from agent.web_search_registry import (
            get_active_search_provider,
            get_provider as _wsp_get_provider,
            _disabled_web_plugin_for,
        )

        backend = _get_search_backend()
        provider = _wsp_get_provider(backend) if backend else None
        if provider is None or not provider.supports_search():
            # Fall back to availability-walked active provider when the
            # configured backend isn't a registered search provider (typo,
            # uninstalled plugin, or capability mismatch).
            provider = get_active_search_provider()

        if provider is None:
            # A bundled web plugin the user explicitly disabled looks
            # identical to "no provider" here — point at the real cause
            # (re-enable the plugin) rather than a generic setup hint.
            disabled_key = _disabled_web_plugin_for(capability="search")
            if disabled_key:
                _vendor = disabled_key.split("/", 1)[-1]
                response_data = {
                    "success": False,
                    "error": (
                        f"web.search_backend is set to '{_vendor}', but its "
                        f"plugin ('{disabled_key}') is disabled in config. "
                        f"Re-enable it with `hermes plugins enable {disabled_key}` "
                        "(or remove it from plugins.disabled)."
                    ),
                }
            else:
                response_data = {
                    "success": False,
                    "error": (
                        "No web search provider configured. "
                        "Run `hermes tools` to set one up."
                    ),
                }
        else:
            logger.info(
                "Web search via %s: '%s' (limit: %d)",
                provider.name, query, limit,
            )
            response_data = provider.search(query, limit)

        debug_call_data["results_count"] = len(response_data.get("data", {}).get("web", []))
        result_json = json.dumps(response_data, indent=2, ensure_ascii=False)
        debug_call_data["final_response_size"] = len(result_json)
        _debug.log_call("web_search_tool", debug_call_data)
        _debug.save()
        return result_json

    except Exception as e:
        error_msg = f"Error searching web: {str(e)}"
        logger.debug("%s", error_msg)

        debug_call_data["error"] = error_msg
        _debug.log_call("web_search_tool", debug_call_data)
        _debug.save()

        return tool_error(error_msg)


async def web_extract_tool(
    urls: List[Any],
    format: str = None,
    use_llm_processing: bool = True,
    model: Optional[str] = None,
    min_length: int = DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION,
    mode: Optional[str] = None,
    question: Optional[str] = None,
    max_chars: Optional[int] = None,
    char_limit: Optional[int] = None,
    only_main_content: Optional[bool] = None,
    wait_for: Optional[int] = None,
    schema: Optional[dict] = None,
) -> str:
    """
    Extract content from specific web pages using the configured extraction backend.

    This function provides a generic interface for web content extraction that
    can work with multiple backends. Advanced modes (answer, summary, json,
    links) require Firecrawl; other backends support basic markdown/html. Pages over
    ``char_limit`` are head+tail truncated with an explicit footer; the full
    text is stored under cache/web and the footer tells the model how to
    read_file the omitted middle. Inline base64 images are replaced with
    ``[IMAGE: alt]`` placeholders (real image URLs are preserved as links).

    Args:
        urls (List[Any]): URL strings or search-result objects containing a
            string ``url`` or ``href`` field.
        format (str): Backward-compatible alias for mode.
        mode (Optional[str]): Extraction mode: markdown/html, or Firecrawl-only answer, summary, json, links.
        question (Optional[str]): Focused page question for Firecrawl mode="answer".
        max_chars (Optional[int]): Backward-compatible alias for char_limit.
        char_limit (Optional[int]): Per-page char budget sent to the model
            (default: web.extract_char_limit or 15000). Larger pages truncate.
        only_main_content (Optional[bool]): Prefer main article/body content when the backend supports it.
        wait_for (Optional[int]): Milliseconds to wait for JS-rendered content when the backend supports it.
        schema (Optional[dict]): Firecrawl-only JSON schema for mode="json".
        use_llm_processing (bool): Whether markdown/html content may be summarized with an auxiliary LLM (default: True)
        model (Optional[str]): The model to use for LLM processing (defaults to current auxiliary backend model)
        min_length (int): Minimum content length to trigger LLM processing (default: 5000)

    Security: URLs are checked for embedded secrets before fetching.

    Returns:
        str: JSON string with a ``results`` list; each entry has
             ``url``, ``title``, ``content``, ``error``. ``content`` is the
             (possibly truncated) clean page text.

    Raises:
        Exception: If extraction fails or API key is not set
    """
    # Block URLs containing embedded secrets (exfiltration prevention).
    # URL-decode first so percent-encoded secrets (%73k- = sk-) are caught.
    from agent.redact import _PREFIX_RE
    from urllib.parse import unquote
    normalized_urls: List[str] = []
    normalized_indices: List[int] = []
    invalid_urls: Dict[int, Dict[str, Any]] = {}
    for index, item in enumerate(urls):
        _url = _web_extract_url(item)
        if _url is None:
            invalid_urls[index] = {
                "url": "",
                "title": "",
                "content": "",
                "error": (
                    f"Invalid URL item at index {index}: expected a URL string "
                    "or an object with a string 'url' or 'href' field"
                ),
            }
            continue
        normalized_url = normalize_url_for_request(_url)
        if (
            _PREFIX_RE.search(_url)
            or _PREFIX_RE.search(unquote(_url))
            or _PREFIX_RE.search(normalized_url)
            or _PREFIX_RE.search(unquote(normalized_url))
        ):
            return json.dumps({
                "success": False,
                "error": "Blocked: URL contains what appears to be an API key or token. "
                         "Secrets must not be sent in URLs.",
            })
        sensitive_query_key = sensitive_query_param_name(normalized_url)
        if sensitive_query_key:
            return json.dumps({
                "success": False,
                "error": (
                    "Blocked: URL contains a credential-like query parameter "
                    f"({sensitive_query_key}). Web extract backends are third-party "
                    "readers; remove the sensitive query parameter or use a local "
                    "browser session when this access is explicitly required."
                ),
            })
        normalized_urls.append(normalized_url)
        normalized_indices.append(index)

    debug_call_data = {
        "parameters": {
            "urls": normalized_urls,
            "format": format,
            "mode": mode,
            "question": question,
            "max_chars": max_chars,
            "char_limit": char_limit,
            "only_main_content": only_main_content,
            "wait_for": wait_for,
            "schema": schema,
            "use_llm_processing": use_llm_processing,
            "model": model,
            "min_length": min_length
        },
        "error": None,
        "pages_extracted": 0,
        "pages_truncated": 0,
        "original_response_size": 0,
        "final_response_size": 0,
        "pages_processed_with_llm": 0,
        "compression_metrics": [],
        "truncation_metrics": [],
        "processing_applied": []
    }
    
    try:
        logger.info("Extracting content from %d URL(s)", len(normalized_urls))

        # ── SSRF protection — filter out private/internal URLs before any backend ──
        safe_urls = []
        safe_indices = []
        ssrf_blocked: Dict[int, Dict[str, Any]] = {}
        for index, url in zip(normalized_indices, normalized_urls):
            if not await _safe_url_allowed(url):
                ssrf_blocked[index] = {
                    "url": url, "title": "", "content": "",
                    "error": "Blocked: URL targets a private or internal network address",
                }
            else:
                safe_urls.append(url)
                safe_indices.append(index)

        # Dispatch only safe URLs. For default markdown fetches, try cheap
        # machine-readable/static extractors first (Shopify product JSON,
        # WordPress/WooCommerce REST, JSON-LD/OpenGraph) and fall back to the
        # configured provider only for misses.
        results: List[Dict[str, Any]] = []
        if safe_urls:
            fast_results, provider_urls = await try_fast_extract_urls(
                safe_urls,
                mode=mode or format or "markdown",
                format=format,
                only_main_content=only_main_content,
                wait_for=wait_for,
                question=question,
                schema=schema,
            )
            results.extend(fast_results)
            if fast_results:
                debug_call_data["processing_applied"].append("fast_extract")
                debug_call_data["fast_extract_count"] = len(fast_results)
            safe_urls = provider_urls

        if safe_urls:
            backend = _get_extract_backend()

            # All seven providers (brave-free, ddgs, searxng, exa, parallel,
            # tavily, firecrawl) now live as plugins. The dispatcher is a
            # registry lookup + delegation. Some providers' extract() is
            # async (parallel, firecrawl), others sync (exa, tavily) — we
            # detect coroutine functions and await; sync functions run
            # inline (the policy gate, SSRF re-check, etc. live inside the
            # provider itself for the firecrawl per-URL loop).
            _ensure_web_plugins_loaded()
            from agent.web_search_registry import (
                get_active_extract_provider,
                get_provider as _wsp_get_provider,
                _disabled_web_plugin_for,
            )

            provider = _wsp_get_provider(backend) if backend else None
            if provider is None or not provider.supports_extract():
                # When the configured name IS registered but doesn't support
                # extract (search-only providers like brave-free / ddgs /
                # searxng), surface that as a typed "search-only" error
                # rather than silently switching backends. When the name
                # isn't registered at all (typo / uninstalled plugin), fall
                # through to the active-provider walk.
                if provider is not None and not provider.supports_extract():
                    return json.dumps(
                        {
                            "success": False,
                            "error": (
                                f"{provider.display_name} is a search-only "
                                "backend and cannot extract URL content. "
                                "Set web.extract_backend to firecrawl, "
                                "tavily, exa, or parallel."
                            ),
                        },
                        ensure_ascii=False,
                    )
                provider = get_active_extract_provider()
                if provider is None:
                    # If the configured backend is a bundled web plugin the
                    # user explicitly disabled, the backend is set correctly
                    # and the real fix is to re-enable the plugin — say so
                    # instead of telling them to set web.extract_backend
                    # (which they already did). #40190 follow-up.
                    disabled_key = _disabled_web_plugin_for(capability="extract")
                    if disabled_key:
                        _vendor = disabled_key.split("/", 1)[-1]
                        return json.dumps(
                            {
                                "success": False,
                                "error": (
                                    f"web.extract_backend is set to '{_vendor}', "
                                    f"but its plugin ('{disabled_key}') is disabled "
                                    "in config. Re-enable it with "
                                    f"`hermes plugins enable {disabled_key}` "
                                    "(or remove it from plugins.disabled)."
                                ),
                            },
                            ensure_ascii=False,
                        )
                    return json.dumps(
                        {
                            "success": False,
                            "error": (
                                "No web extract provider configured. "
                                "Set web.extract_backend to firecrawl, "
                                "tavily, exa, or parallel."
                            ),
                        },
                        ensure_ascii=False,
                    )

            logger.info(
                "Web extract via %s: %d URL(s)", provider.name, len(safe_urls)
            )

            requested_mode = (mode or format or "markdown").lower()
            if requested_mode not in {"markdown", "html"} and provider.name != "firecrawl":
                return json.dumps(
                    {
                        "success": False,
                        "error": (
                            f"web_extract mode={requested_mode!r} requires Firecrawl. "
                            f"Configured extract backend {provider.display_name} supports "
                            "basic markdown/html extraction only."
                        ),
                    },
                    ensure_ascii=False,
                )

            # Async-or-sync dispatch: parallel + firecrawl have async
            # extract(); exa + tavily are sync.
            import inspect
            extract_kwargs = {
                "mode": mode,
                "question": question,
                "only_main_content": only_main_content,
                "wait_for": wait_for,
                "schema": schema,
            }
            if format is not None:
                extract_kwargs["format"] = format
            extract_kwargs = {k: v for k, v in extract_kwargs.items() if v is not None}
            if inspect.iscoroutinefunction(provider.extract):
                provider_results = await provider.extract(safe_urls, **extract_kwargs)
            else:
                # Run sync extract() in a thread so we don't block the
                # event loop on network I/O.
                provider_results = await asyncio.to_thread(
                    provider.extract, safe_urls, **extract_kwargs
                )
            for requested_url, item in zip(safe_urls, provider_results):
                if isinstance(item, dict):
                    item.setdefault("requested_url", requested_url)
            results.extend(provider_results)

        # Reconstruct original input order across invalid, blocked, fast-path,
        # and provider results. URL buckets avoid positional mismatches when the
        # fast extractor handles only a subset of safe URLs.
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for item in results:
            key = item.get("requested_url") or item.get("url")
            if key:
                buckets.setdefault(key, []).append(item)
        safe_results: Dict[int, Dict[str, Any]] = {}
        for index, url in zip(safe_indices, [normalized_urls[normalized_indices.index(i)] for i in safe_indices]):
            bucket = buckets.get(url) or []
            safe_results[index] = bucket.pop(0) if bucket else {
                "url": url,
                "title": "",
                "content": "",
                "error": "Extract backend returned no result for this URL",
            }
        by_index = {**safe_results, **ssrf_blocked, **invalid_urls}
        results = [by_index[index] for index in range(len(urls))]

        response = {"results": results}
        
        pages_extracted = len(response.get('results', []))
        logger.info("Extracted content from %d pages", pages_extracted)
        
        debug_call_data["pages_extracted"] = pages_extracted
        debug_call_data["original_response_size"] = len(json.dumps(response))
        effective_model = model or _get_default_summarizer_model()
        auxiliary_available = check_auxiliary_model()
        effective_use_llm_processing = use_llm_processing and not (
            (mode or format or "markdown").lower() in {"answer", "summary", "json", "links"}
        )
        
        # Process each result with LLM if enabled
        if effective_use_llm_processing and auxiliary_available:
            logger.info("Processing extracted content with LLM (parallel)...")
            debug_call_data["processing_applied"].append("llm_processing")
            
            # Prepare tasks for parallel processing
            async def process_single_result(result):
                """Process a single result with LLM and return updated result with metrics."""
                url = result.get('url', 'Unknown URL')
                title = result.get('title', '')
                raw_content = result.get('raw_content', '') or result.get('content', '')
                
                if not raw_content:
                    return result, None, "no_content"
                
                original_size = len(raw_content)
                
                # Process content with LLM
                processed = await process_content_with_llm(
                    raw_content, url, title, effective_model, min_length
                )
                
                if processed:
                    processed_size = len(processed)
                    compression_ratio = processed_size / original_size if original_size > 0 else 1.0
                    
                    # Update result with processed content
                    result['content'] = processed
                    result['raw_content'] = raw_content
                    
                    metrics = {
                        "url": url,
                        "original_size": original_size,
                        "processed_size": processed_size,
                        "compression_ratio": compression_ratio,
                        "model_used": effective_model
                    }
                    return result, metrics, "processed"
                else:
                    metrics = {
                        "url": url,
                        "original_size": original_size,
                        "processed_size": original_size,
                        "compression_ratio": 1.0,
                        "model_used": None,
                        "reason": "content_too_short"
                    }
                    return result, metrics, "too_short"
            
            # Run all LLM processing in parallel
            results_list = response.get('results', [])
            tasks = [process_single_result(result) for result in results_list]
            # Use return_exceptions=True so a single task failure does not
            # discard all other successfully processed results.
            processed_results = await asyncio.gather(*tasks, return_exceptions=True)
            # Collect metrics and print results
            for result_item in processed_results:
                if isinstance(result_item, BaseException):
                    logger.warning("Web result processing task failed: %s", result_item)
                    continue
                result, metrics, status = result_item
                url = result.get('url', 'Unknown URL')
                if status == "processed":
                    debug_call_data["compression_metrics"].append(metrics)
                    debug_call_data["pages_processed_with_llm"] += 1
                    logger.info("%s (processed)", url)
                elif status == "too_short":
                    debug_call_data["compression_metrics"].append(metrics)
                    logger.info("%s (no processing - content too short)", url)
                else:
                    logger.warning("%s (no content to process)", url)
        else:
            if effective_use_llm_processing and not auxiliary_available:
                logger.warning("LLM processing requested but no auxiliary model available, returning raw content")
                debug_call_data["processing_applied"].append("llm_processing_unavailable")
            # Print summary of extracted pages for debugging (original behavior)
            for result in response.get('results', []):
                url = result.get('url', 'Unknown URL')
                content_length = len(result.get('raw_content', ''))
                logger.info("%s (%d characters)", url, content_length)

        effective_char_limit = (
            char_limit
            if char_limit is not None
            else max_chars
            if max_chars is not None
            else _get_extract_char_limit()
        )
        try:
            effective_char_limit = max(2000, min(int(effective_char_limit), 500_000))
        except (TypeError, ValueError):
            effective_char_limit = DEFAULT_EXTRACT_CHAR_LIMIT

        # Truncate-and-store after optional backend/LLM processing. For each
        # result, convert inline base64 images to labeled placeholders (keeping
        # alt text + real image URLs), then return the clean content directly if
        # within budget, or a head+tail window plus a footer pointing at the
        # stored full text.
        debug_call_data["processing_applied"].append("truncate_and_store")
        for result in response.get("results", []):
            if result.get("error"):
                continue
            url = result.get("url", "")
            raw_content = result.get("content", "") or result.get("raw_content", "")
            if not raw_content:
                continue
            clean = convert_base64_images_to_links(raw_content)
            model_text, truncated = _truncate_with_footer(clean, url, effective_char_limit)
            result["content"] = model_text
            result["truncated"] = truncated
            if truncated:
                debug_call_data["pages_truncated"] += 1
                debug_call_data["truncation_metrics"].append({
                    "url": url,
                    "original_size": len(clean),
                    "sent_size": len(model_text),
                })
                logger.info("%s (truncated %d -> %d chars)", url, len(clean), len(model_text))
            else:
                logger.info("%s (%d chars, whole)", url, len(clean))

        # Trim output to minimal fields per entry: title, content, error
        trimmed_results = [
            {
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "content": r.get("content", ""),
                "error": r.get("error"),
                **({"truncated": r["truncated"]} if "truncated" in r else {}),
                **({"backend_used": r["backend_used"]} if "backend_used" in r else {}),
                **({"metadata": r["metadata"]} if "metadata" in r else {}),
                **({  "blocked_by_policy": r["blocked_by_policy"]} if "blocked_by_policy" in r else {}),
            }
            for r in response.get("results", [])
        ]
        trimmed_response = {"results": trimmed_results}

        if trimmed_response.get("results") == []:
            result_json = tool_error("Content was inaccessible or not found")
        else:
            result_json = json.dumps(trimmed_response, indent=2, ensure_ascii=False)

        # base64 images were already converted to placeholders per-result above;
        # this is a belt-and-suspenders sweep over the serialized JSON in case a
        # provider tucked a blob somewhere unexpected (e.g. metadata).
        cleaned_result = convert_base64_images_to_links(result_json)

        debug_call_data["final_response_size"] = len(cleaned_result)
        debug_call_data["processing_applied"].append("base64_image_conversion")
        
        # Log debug information
        _debug.log_call("web_extract_tool", debug_call_data)
        _debug.save()
        
        return cleaned_result
            
    except Exception as e:
        error_msg = f"Error extracting content: {str(e)}"
        logger.debug("%s", error_msg)
        
        debug_call_data["error"] = error_msg
        _debug.log_call("web_extract_tool", debug_call_data)
        _debug.save()
        
        return tool_error(error_msg)


# Convenience function to check Firecrawl credentials
def web_tools_registered() -> bool:
    """Registration probe for legacy web tools; availability is checked separately."""
    return True


def check_web_api_key() -> bool:
    """Usability probe: True when the selected web backends can service calls.

    Probes the backends that :func:`_get_search_backend` /
    :func:`_get_extract_backend` actually select, while also honoring
    plugin-registered providers. An explicit per-capability backend with missing
    credentials reports unusable instead of being masked by a shared fallback.
    Distinct from :func:`web_tools_registered` (always True — whether the tool
    is offered).
    """
    cfg = _load_web_config()
    search_specific = str(cfg.get("search_backend") or "").lower().strip()
    extract_specific = str(cfg.get("extract_backend") or "").lower().strip()
    if search_specific and not _is_backend_available(search_specific):
        return False
    if extract_specific and not _is_backend_available(extract_specific):
        return False
    if search_specific or extract_specific:
        return _backend_usable(_get_search_backend()) and _backend_usable(_get_extract_backend())

    configured = str(cfg.get("backend") or "").lower().strip()
    if configured:
        return _is_backend_available(configured)
    if any(_is_backend_available(backend) for backend in _LEGACY_WEB_BACKENDS):
        return True
    try:
        from agent.web_search_registry import (
            get_active_search_provider,
            get_active_extract_provider,
        )

        return (
            get_active_search_provider() is not None
            or get_active_extract_provider() is not None
        )
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("web provider registry availability check failed: %s", exc)
        return False


def check_auxiliary_model() -> bool:
    """Check if an auxiliary text model is available for LLM content processing."""
    client, _, _ = _resolve_web_extract_auxiliary()
    return client is not None


if __name__ == "__main__":
    """
    Simple test/demo when run directly
    """
    print("🌐 Standalone Web Tools Module")
    print("=" * 40)

    # Check if API keys are available
    web_available = check_web_api_key()
    tool_gateway_available = _is_tool_gateway_ready()
    from hermes_cli.config import get_env_value as _gev
    firecrawl_key_available = bool((_gev("FIRECRAWL_API_KEY") or "").strip())
    firecrawl_url_available = bool((_gev("FIRECRAWL_API_URL") or "").strip())

    if web_available:
        backend = _get_backend()
        print(f"✅ Web backend: {backend}")
        if backend == "exa":
            print("   Using Exa API (https://exa.ai)")
        elif backend == "parallel":
            print("   Using Parallel API (https://parallel.ai)")
        elif backend == "tavily":
            print("   Using Tavily API (https://tavily.com)")
        elif backend == "searxng":
            print(f"   Using SearXNG (search only): {_env_value('SEARXNG_URL')}")
        elif backend == "brave-free":
            print("   Using Brave Search free tier (search only)")
        elif backend == "ddgs":
            print("   Using DuckDuckGo via ddgs package (search only)")
        elif firecrawl_url_available:
            print(f"   Using self-hosted Firecrawl: {(_gev('FIRECRAWL_API_URL') or '').strip().rstrip('/')}")
        elif firecrawl_key_available:
            print("   Using direct Firecrawl cloud API")
        elif tool_gateway_available:
            print(f"   Using Firecrawl tool-gateway: {_get_firecrawl_gateway_url()}")
        else:
            print("   Firecrawl backend selected but not configured")
    else:
        print("❌ No web search backend configured")
        print(
            "Set EXA_API_KEY, PARALLEL_API_KEY, TAVILY_API_KEY, FIRECRAWL_API_KEY, FIRECRAWL_API_URL"
            f"{_firecrawl_backend_help_suffix()}"
        )

    if not web_available:
        sys.exit(1)

    print("🛠️  Web tools ready for use!")
    print(f"   Extract char limit: {_get_extract_char_limit()} chars "
          "(pages over this are truncated; full text stored in cache/web)")

    # Show debug mode status
    if _debug.active:
        print(f"🐛 Debug mode ENABLED - Session ID: {_debug.session_id}")
        print(f"   Debug logs will be saved to: {_debug.log_dir}/web_tools_debug_{_debug.session_id}.json")
    else:
        print("🐛 Debug mode disabled (set WEB_TOOLS_DEBUG=true to enable)")

    print("\nBasic usage:")
    print("  from web_tools import web_search_tool, web_extract_tool")
    print("  import asyncio")
    print("")
    print("  # Search (synchronous)")
    print("  results = web_search_tool('Python tutorials')")
    print("")
    print("  # Extract (asynchronous, no LLM — truncate-and-store)")
    print("  async def main():")
    print("      content = await web_extract_tool(['https://example.com'])")
    print("      # bigger budget for one call:")
    print("      content = await web_extract_tool(['https://docs.python.org'], char_limit=40000)")
    print("  asyncio.run(main())")

    print("\nDebug mode:")
    print("  export WEB_TOOLS_DEBUG=true")
    print("  # Logs saved to: ./logs/web_tools_debug_UUID.json")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
from tools.registry import registry, tool_error

WEB_SEARCH_SCHEMA = {
    "name": "web_search",
    "description": "Search the web for information. Returns up to 5 results by default with titles, URLs, and descriptions. The query is passed through to the configured backend, so operators such as site:domain, filetype:pdf, intitle:word, -term, and \"exact phrase\" may work when the backend supports them.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to look up on the web. You may include backend-supported operators such as site:example.com, filetype:pdf, intitle:word, -term, or \"exact phrase\"."
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return. Defaults to 5.",
                "minimum": 1,
                "maximum": 100,
                "default": 5
            }
        },
        "required": ["query"]
    }
}

WEB_EXTRACT_SCHEMA = {
    "name": "web_extract",
    "description": "Extract content from web page URLs using the configured extract backend. Default mode returns markdown/text. Also works with PDF URLs. Firecrawl adds mode=answer with question for focused page Q&A, mode=summary for compact summaries, mode=json with schema for structured extraction, and mode=links for link extraction. Pages within the char budget (default 15000) return whole; larger pages return a head+tail window with a footer telling you the full text's saved file path and read_file call. Inline images appear as [IMAGE: alt] placeholders; real image URLs are kept as links.",
    "parameters": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of URLs to extract content from (max 5 URLs per call)",
                "maxItems": 5
            },
            "mode": {
                "type": "string",
                "enum": ["markdown", "html", "answer", "summary", "json", "links"],
                "description": "Extraction mode. Defaults to markdown. answer/summary/json/links require Firecrawl."
            },
            "format": {
                "type": "string",
                "enum": ["markdown", "html"],
                "description": "Backward-compatible alias for mode. Prefer mode for new calls."
            },
            "question": {"type": "string", "description": "Focused question for Firecrawl mode=answer."},
            "max_chars": {"type": "integer", "description": "Backward-compatible alias for char_limit."},
            "char_limit": {
                "type": "integer",
                "description": "Optional per-page character budget sent back (default 15000). Pages larger than this are head+tail truncated with the full text stored to disk. Raise it when you need more of a long page inline.",
                "minimum": 2000
            },
            "only_main_content": {"type": "boolean", "description": "Prefer main article/body content when supported."},
            "wait_for": {"type": "integer", "description": "Milliseconds to wait for JS-rendered content when supported."},
            "schema": {"type": "object", "description": "Firecrawl-only JSON schema for mode=json."}
        },
        "required": ["urls"]
    }
}

WEB_SCHEMA = {
    "name": "web",
    "description": (
        "Web research wrapper. Search the web, fetch pages, ask focused page questions, "
        "summarize, extract structured JSON/links, or use curl.md fallback. Keep "
        "github_repo_brief separate for GitHub repositories."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "fetch", "answer", "summary", "json", "links", "curlmd"],
                "description": "Web operation to perform.",
            },
            "query": {"type": "string", "description": "For action='search': search query."},
            "limit": {"type": "integer", "description": "For action='search': max results.", "minimum": 1, "maximum": 100, "default": 5},
            "urls": {"type": "array", "items": {"type": "string"}, "description": "For fetch/answer/summary/json/links: URLs to extract (max 5).", "maxItems": 5},
            "url": {"type": "string", "description": "For action='curlmd': absolute http(s) URL to fetch."},
            "mode": {"type": "string", "enum": ["markdown", "html", "answer", "summary", "json", "links"], "description": "Optional extract mode override. Usually inferred from action."},
            "question": {"type": "string", "description": "Focused question for action='answer'."},
            "max_chars": {"type": "integer", "description": "Maximum characters returned after extraction/fetch."},
            "only_main_content": {"type": "boolean", "description": "Prefer main article/body content when supported."},
            "wait_for": {"type": "integer", "description": "Milliseconds to wait for JS-rendered content when supported."},
            "schema": {"type": "object", "description": "Firecrawl-only JSON schema for action='json'."},
            "objective": {"type": "string", "description": "For curlmd: focused extraction objective."},
            "keywords": {"description": "For curlmd: keyword prefilter; string or list of strings.", "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
            "curlmd_mode": {"type": "string", "enum": ["smart", "rush"], "description": "For curlmd: curl.md processing mode.", "default": "smart"},
            "fresh": {"type": "boolean", "description": "For curlmd: bypass curl.md cache.", "default": False},
            "retries": {"type": "integer", "description": "For curlmd: retry count.", "minimum": 0, "maximum": 4, "default": 2},
            "timeout_seconds": {"type": "integer", "description": "For curlmd: per-attempt timeout.", "minimum": 5, "maximum": 180, "default": 45},
            "fallback": {"type": "boolean", "description": "For fetch actions: allow explicit fallback when supported. Results must say fallback_used/reason.", "default": False},
            "fallback_to_curl": {"type": "boolean", "description": "For curlmd: allow plain curl fallback.", "default": True},
        },
        "required": ["action"],
    },
}

def _load_curlmd_tool_module():
    """Load the curl.md helper from the user plugin, with legacy fallback."""
    import importlib

    for module_name in (
        "hermes_plugins.local_tools.curlmd_tool",
        "plugins.local_tools.curlmd_tool",
        "tools.curlmd_tool",
    ):
        try:
            return importlib.import_module(module_name)
        except Exception:
            continue
    raise ModuleNotFoundError("curlmd_tool is not available from local-tools plugin or legacy tools package")


def _check_web_wrapper_available() -> bool:
    if check_web_api_key():
        return True
    try:
        return bool(_load_curlmd_tool_module().check_curlmd_available())
    except Exception:
        return False


async def _handle_web(args, **kw):
    action = args.get("action")
    if action == "search":
        if not check_web_api_key():
            return tool_error("web(action='search') requires a configured web search backend/API key. Configure web search, or use action='curlmd' with a specific URL.")
        if not args.get("query"):
            return tool_error("web(action='search') requires 'query'.")
        return web_search_tool(args.get("query", ""), limit=args.get("limit", 5))

    if action in {"fetch", "answer", "summary", "json", "links"}:
        urls = args.get("urls", [])[:5] if isinstance(args.get("urls"), list) else []
        if not urls:
            if args.get("url"):
                return tool_error("web(action='fetch'/'answer'/'summary'/'json'/'links') requires 'urls' as a list. Use action='curlmd' for a single 'url'.")
            return tool_error("web extract actions require 'urls' as a non-empty list.")
        mode = args.get("mode") or ("markdown" if action == "fetch" else action)
        return await web_extract_tool(
            urls,
            format=args.get("format"),
            mode=mode,
            question=args.get("question"),
            max_chars=args.get("max_chars"),
            char_limit=args.get("char_limit"),
            only_main_content=args.get("only_main_content"),
            wait_for=args.get("wait_for"),
            schema=args.get("schema"),
            use_llm_processing=mode not in {"answer", "summary", "json", "links"},
        )

    if action == "curlmd":
        curlmd_tool = _load_curlmd_tool_module()
        return curlmd_tool.curlmd_fetch_tool(
            url=args.get("url", ""),
            objective=args.get("objective"),
            keywords=args.get("keywords"),
            mode=args.get("curlmd_mode", "smart"),
            fresh=bool(args.get("fresh", False)),
            retries=args.get("retries", 2),
            timeout_seconds=args.get("timeout_seconds", 45),
            fallback_to_curl=bool(args.get("fallback_to_curl", True)),
            max_chars=args.get("max_chars", 50_000),
        )

    return tool_error("Unknown web action. Use one of: search, fetch, answer, summary, json, links, curlmd.")


registry.register(
    name="web",
    toolset="web",
    schema=WEB_SCHEMA,
    handler=_handle_web,
    check_fn=_check_web_wrapper_available,
    is_async=True,
    emoji="🌐",
    max_result_size_chars=100_000,
)
registry.register(
    name="web_search",
    toolset="web",
    schema=WEB_SEARCH_SCHEMA,
    handler=lambda args, **kw: web_search_tool(args.get("query", ""), limit=args.get("limit", 5)),
    check_fn=check_web_api_key,
    requires_env=_web_requires_env(),
    emoji="🔍",
    max_result_size_chars=100_000,
)
registry.register(
    name="web_extract",
    toolset="web_legacy",
    schema=WEB_EXTRACT_SCHEMA,
    handler=lambda args, **kw: web_extract_tool(
        args.get("urls", [])[:5] if isinstance(args.get("urls"), list) else [],
        format=args.get("format"),
        mode=args.get("mode") or args.get("format") or "markdown",
        question=args.get("question"),
        max_chars=args.get("max_chars"),
        char_limit=args.get("char_limit"),
        only_main_content=args.get("only_main_content"),
        wait_for=args.get("wait_for"),
        schema=args.get("schema"),
        use_llm_processing=(args.get("mode") or args.get("format") or "markdown") not in {"answer", "summary", "json", "links"},
    ),
    check_fn=web_tools_registered,
    requires_env=_web_requires_env(),
    is_async=True,
    emoji="📄",
    max_result_size_chars=100_000,
)
