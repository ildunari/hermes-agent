"""Mandatory projection for authenticated in-app browser output.

The Authenticated Browser Projection (ABP) is deliberately separate from the
ordinary Hermes secret redactor.  Existing browser backends may return usable
OAuth and pre-signed URLs; an authenticated Desktop partition may not release
those credentials to a model, transcript, log, or persistence sink.

Raw values are accepted only with an immutable authenticated scope.  URL
capabilities and correlation keys are memory-only, and all registries are
closed/versioned so a newly admitted tool or CDP method cannot silently inherit
a permissive recursive-redaction fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlsplit

PROJECTOR_VERSION = "abp-v1"
MAX_PROJECTABLE_BYTES = 8 * 1024 * 1024
URL_CAPABILITY_TTL_SECONDS = 15 * 60.0

_URL_REF_RE = re.compile(r"^@url([1-9][0-9]*)$")
_ANY_URL_REF_RE = re.compile(r"(?<![A-Za-z0-9_])@url[1-9][0-9]*\b")
# Deliberately conservative tokenization. Dedicated URL-valued fields do not
# depend on this regex; it is defense for URLs embedded in console/AX prose.
_EMBEDDED_URL_RE = re.compile(
    r"(?P<url>data:⟦[^⟧]{0,256}; payload withheld; bytes=[0-9]+⟧|"
    r"(?:(?:https?|wss?|ftp)://|(?:data|blob|file|javascript):)"
    r"[^\s<>\"'`\]\[{}]+)",
    re.IGNORECASE,
)
_BASE64_PAYLOAD_RE = re.compile(r"[A-Za-z0-9+/]{252,}={0,2}")
_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_URL_FIELD_NAMES = frozenset({
    "url",
    "uri",
    "href",
    "src",
    "action",
    "poster",
    "documenturl",
    "baseurl",
    "unreachableurl",
    "securityorigin",
    "origin",
    "location",
})
_NON_ACTIONABLE_SECRET_FIELDS = frozenset({
    "authorization",
    "proxyauthorization",
    "cookie",
    "cookies",
    "setcookie",
    "password",
    "passwd",
    "secret",
    "token",
    "postdata",
    "requestbody",
    "responsebody",
    "body",
    "websocketpayload",
    "payloaddata",
    "storagevalue",
})
_IDENTITY_FIELDS = frozenset({"sessionid", "objectid", "loaderid"})
_SAFE_HEADER_VALUES = frozenset({"content-type", "content-length"})


class AuthenticatedBrowserProjectionError(ValueError):
    """A typed content-free failure at the authenticated output boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def payload(self) -> dict[str, Any]:
        return {
            "success": False,
            "error": self.code,
            "message": self.message,
            "projector_version": PROJECTOR_VERSION,
        }


@dataclass(frozen=True, repr=False)
class AuthenticatedBrowserScope:
    """Complete authority tuple for one authenticated document generation."""

    profile: str = field(repr=False)
    connection_id: str = field(repr=False)
    capability_generation: int
    tab_id: str = field(repr=False)
    binding_generation: int
    document_generation: int
    task_id: str = field(repr=False)
    guest_generation: str = field(repr=False)
    task_generation: int

    def __post_init__(self) -> None:
        for value, name in (
            (self.profile, "profile"),
            (self.connection_id, "connection_id"),
            (self.tab_id, "tab_id"),
            (self.task_id, "task_id"),
            (self.guest_generation, "guest_generation"),
        ):
            if not isinstance(value, str) or not value or len(value) > 512:
                raise AuthenticatedBrowserProjectionError(
                    "AUTHENTICATED_SCOPE_INVALID", f"{name} is invalid"
                )
        for value, name in (
            (self.capability_generation, "capability_generation"),
            (self.binding_generation, "binding_generation"),
            (self.document_generation, "document_generation"),
            (self.task_generation, "task_generation"),
        ):
            if type(value) is not int or value <= 0:
                raise AuthenticatedBrowserProjectionError(
                    "AUTHENTICATED_SCOPE_INVALID", f"{name} is invalid"
                )

    def __repr__(self) -> str:
        return (
            "AuthenticatedBrowserScope("
            f"capability_generation={self.capability_generation}, "
            f"binding_generation={self.binding_generation}, "
            f"document_generation={self.document_generation}, "
            f"task_generation={self.task_generation})"
        )


@dataclass(repr=False)
class _UrlCapability:
    raw_url: str = field(repr=False)
    last_used: float
    projected_display: str | None = field(default=None, repr=False)


_lock = threading.RLock()
_url_capabilities: dict[AuthenticatedBrowserScope, dict[str, _UrlCapability]] = {}
_next_url_number: dict[tuple[str, str], int] = {}
_runtime_digest_key = secrets.token_bytes(32)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=lambda _value: "<unserializable>",
        ).encode("utf-8", "replace")
    except Exception:
        return b"<unserializable>"


def correlation_digest(scope: AuthenticatedBrowserScope, value: Any) -> str:
    scope_key = "\0".join((
        scope.profile,
        scope.connection_id,
        str(scope.capability_generation),
        scope.tab_id,
        str(scope.binding_generation),
        str(scope.document_generation),
    )).encode("utf-8")
    scoped_key = hmac.new(_runtime_digest_key, scope_key, hashlib.sha256).digest()
    return hmac.new(scoped_key, _canonical_bytes(value), hashlib.sha256).hexdigest()[
        :16
    ]


def _withheld(
    scope: AuthenticatedBrowserScope, value: Any, data_class: str
) -> dict[str, Any]:
    raw = _canonical_bytes(value)
    return {
        "withheld": data_class,
        "byte_count": len(raw),
        "digest": correlation_digest(scope, value),
        "projector_version": PROJECTOR_VERSION,
    }


def _prune_scope_locked(scope: AuthenticatedBrowserScope, now: float) -> None:
    entries = _url_capabilities.get(scope)
    if not entries:
        return
    for ref, entry in tuple(entries.items()):
        if now - entry.last_used > URL_CAPABILITY_TTL_SECONDS:
            entries.pop(ref, None)
    if not entries:
        _url_capabilities.pop(scope, None)


def mint_url_reference(
    scope: AuthenticatedBrowserScope,
    raw_url: str,
    projected_display: str | None = None,
) -> str:
    if not isinstance(raw_url, str) or not raw_url:
        raise AuthenticatedBrowserProjectionError(
            "OUTPUT_PROJECTION_FAILED", "URL capability source is invalid"
        )
    now = time.monotonic()
    with _lock:
        _prune_scope_locked(scope, now)
        entries = _url_capabilities.setdefault(scope, {})
        for ref, entry in entries.items():
            if hmac.compare_digest(
                entry.raw_url.encode("utf-8"), raw_url.encode("utf-8")
            ):
                entry.last_used = now
                if projected_display is not None:
                    entry.projected_display = projected_display
                return ref
        counter_key = (scope.profile, scope.connection_id)
        next_number = _next_url_number.get(counter_key, 0) + 1
        _next_url_number[counter_key] = next_number
        ref = f"@url{next_number}"
        entries[ref] = _UrlCapability(
            raw_url=raw_url,
            projected_display=projected_display,
            last_used=now,
        )
        return ref


def resolve_url_reference(scope: AuthenticatedBrowserScope, value: str) -> str:
    """Resolve only an exact whole-argument URL capability."""

    if not isinstance(value, str) or not _URL_REF_RE.fullmatch(value):
        raise AuthenticatedBrowserProjectionError(
            "URL_REFERENCE_INVALID",
            "URL reference must be the complete navigation argument",
        )
    now = time.monotonic()
    with _lock:
        _prune_scope_locked(scope, now)
        entry = (_url_capabilities.get(scope) or {}).get(value)
        if entry is None:
            raise AuthenticatedBrowserProjectionError(
                "URL_REFERENCE_EXPIRED",
                "URL reference is stale, unknown, or outside this scope",
            )
        entry.last_used = now
        return entry.raw_url


def contains_url_reference(value: Any) -> bool:
    if isinstance(value, str):
        return _ANY_URL_REF_RE.search(value) is not None
    if isinstance(value, Mapping):
        return any(
            contains_url_reference(key) or contains_url_reference(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_url_reference(item) for item in value)
    return False


def invalidate_scope(scope: AuthenticatedBrowserScope) -> None:
    with _lock:
        _url_capabilities.pop(scope, None)


def retire_task(task_id: str) -> None:
    with _lock:
        for scope in tuple(_url_capabilities):
            if scope.task_id == task_id:
                _url_capabilities.pop(scope, None)


def _force_redact(value: str) -> str:
    from agent.redact import redact_sensitive_text

    return redact_sensitive_text(value, force=True)


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _validate_percent_encoding(value: str) -> bool:
    return _PERCENT_ESCAPE_RE.search(value) is None


def _project_path(path: str) -> str:
    projected: list[str] = []
    for segment in path.split("/"):
        decoded = unquote(segment)
        redacted = _force_redact(decoded)
        if redacted != decoded:
            projected.append("⟦secret-path-segment⟧")
        else:
            projected.append(_force_redact(segment))
    return "/".join(projected)


def _project_network_url(scope: AuthenticatedBrowserScope, raw_url: str) -> str:
    try:
        if _has_control(raw_url) or not _validate_percent_encoding(raw_url):
            raise ValueError("invalid URL encoding")
        parsed = urlsplit(raw_url)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https", "ws", "wss", "ftp"}:
            raise ValueError("unsupported network scheme")
        if not parsed.hostname:
            raise ValueError("host is absent")
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid port") from exc
        default_port = {"http": 80, "https": 443, "ws": 80, "wss": 443, "ftp": 21}[
            scheme
        ]
        authority = hostname + (
            f":{port}" if port is not None and port != default_port else ""
        )
        if parsed.username is not None or parsed.password is not None:
            authority = f"⟦userinfo withheld⟧@{authority}"
        path = _project_path(parsed.path or "")
        query = ""
        if parsed.query:
            rows: list[str] = []
            for component in parsed.query.split("&"):
                name, separator, _value = component.partition("=")
                safe_name = _force_redact(name)
                rows.append(
                    f"{safe_name}=⟦withheld⟧"
                    if separator
                    else f"{safe_name}=⟦withheld⟧"
                )
            query = "?" + "&".join(rows)
        fragment = "#⟦withheld⟧" if parsed.fragment else ""
        display = f"{scheme}://{authority}{path}{query}{fragment}"
    except Exception as exc:
        raise AuthenticatedBrowserProjectionError(
            "MALFORMED_URL_WITHHELD", "malformed authenticated URL was withheld"
        ) from exc
    return f"{display} {mint_url_reference(scope, raw_url, display)}"


def project_url(scope: AuthenticatedBrowserScope, raw_url: str) -> str:
    if not isinstance(raw_url, str) or not raw_url or len(raw_url) > 1024 * 1024:
        raise AuthenticatedBrowserProjectionError(
            "MALFORMED_URL_WITHHELD", "malformed authenticated URL was withheld"
        )
    if _has_control(raw_url):
        return f"⟦malformed-url:{correlation_digest(scope, raw_url)}⟧"
    try:
        parsed = urlsplit(raw_url)
    except Exception:
        return f"⟦malformed-url:{correlation_digest(scope, raw_url)}⟧"
    scheme = parsed.scheme.lower()
    if scheme in {"http", "https", "ws", "wss", "ftp"}:
        try:
            return _project_network_url(scope, raw_url)
        except AuthenticatedBrowserProjectionError:
            return f"⟦malformed-url:{correlation_digest(scope, raw_url)}⟧"
    if scheme == "data":
        media = raw_url[5:].partition(",")[0].partition(";")[0] or "text/plain"
        return f"data:⟦{_force_redact(media)}; payload withheld; bytes={len(raw_url.encode('utf-8'))}⟧"
    if scheme == "blob":
        creator = raw_url[5:].rsplit("/", 1)[0]
        projected_creator = (
            project_url(scope, creator) if "://" in creator else "⟦creator withheld⟧"
        )
        display = f"blob:⟦{projected_creator}; object withheld⟧"
        return f"{display} {mint_url_reference(scope, raw_url, display)}"
    if scheme:
        display = f"⟦{_force_redact(scheme)} URL withheld⟧"
        return f"{display} {mint_url_reference(scope, raw_url, display)}"
    # Relative URLs require a trusted browser-held base. This projector has no
    # raw base by design, so fail toward withholding and mint no capability.
    return f"⟦relative-url:{correlation_digest(scope, raw_url)}⟧"


def _active_ref(scope: AuthenticatedBrowserScope, ref: str) -> bool:
    now = time.monotonic()
    with _lock:
        _prune_scope_locked(scope, now)
        return ref in (_url_capabilities.get(scope) or {})


def _is_trusted_projected_pair(
    scope: AuthenticatedBrowserScope,
    display: str,
    following_text: str,
) -> bool:
    match = re.match(r"\s+(@url[1-9][0-9]*)\b", following_text)
    if match is None:
        return False
    ref = match.group(1)
    now = time.monotonic()
    with _lock:
        _prune_scope_locked(scope, now)
        entry = (_url_capabilities.get(scope) or {}).get(ref)
        return entry is not None and entry.projected_display == display


def _escape_page_refs(scope: AuthenticatedBrowserScope, text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        ref = match.group(0)
        return ref if _active_ref(scope, ref) else ref.replace("@", "@\u200b", 1)

    return _ANY_URL_REF_RE.sub(replace, text)


def project_text(scope: AuthenticatedBrowserScope, text: str) -> str:
    if not isinstance(text, str):
        raise AuthenticatedBrowserProjectionError(
            "OUTPUT_PROJECTION_FAILED", "projected text is malformed"
        )
    if len(text.encode("utf-8", "replace")) > MAX_PROJECTABLE_BYTES:
        raise AuthenticatedBrowserProjectionError(
            "OUTPUT_PROJECTION_FAILED", "projected text exceeds the safety limit"
        )
    if _BASE64_PAYLOAD_RE.fullmatch(text.strip()):
        return project_diagnostic(scope, text, "ENCODED_BINARY_PAYLOAD")
    redacted = _force_redact(text)

    def replace(match: re.Match[str]) -> str:
        token = match.group("url")
        # Trim common prose punctuation while preserving it after projection.
        suffix = ""
        while token and token[-1] in ".,;:)":
            suffix = token[-1] + suffix
            token = token[:-1]
        if _is_trusted_projected_pair(scope, token, redacted[match.end() :]):
            return token + suffix
        if re.fullmatch(r"data:⟦[^⟧]{0,256}; payload withheld; bytes=[0-9]+⟧", token):
            return token + suffix
        return project_url(scope, token) + suffix

    projected = _EMBEDDED_URL_RE.sub(replace, redacted)
    return _escape_page_refs(scope, projected)


def project_diagnostic(
    scope: AuthenticatedBrowserScope, value: Any, data_class: str
) -> str:
    """Return a bounded diagnostic with no browser-originated body or repr."""

    raw = _canonical_bytes(value)
    return (
        f"⟦{data_class} withheld; bytes={len(raw)}; "
        f"digest={correlation_digest(scope, value)}; projector={PROJECTOR_VERSION}⟧"
    )


def _normalized_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _project_headers(scope: AuthenticatedBrowserScope, value: Any) -> Any:
    if isinstance(value, Mapping):
        rows = []
        for name, raw_value in value.items():
            safe_name = _force_redact(str(name))
            if safe_name.lower() in _SAFE_HEADER_VALUES:
                rows.append({
                    "name": safe_name,
                    "value": project_text(scope, str(raw_value)),
                })
            else:
                rows.append({"name": safe_name, "value": "⟦withheld⟧"})
        return rows
    if isinstance(value, list):
        rows = []
        for row in value:
            if not isinstance(row, Mapping):
                rows.append(_withheld(scope, row, "HEADER_VALUE"))
                continue
            name = _force_redact(str(row.get("name", "")))
            raw_value = row.get("value", "")
            rows.append({
                "name": name,
                "value": project_text(scope, str(raw_value))
                if name.lower() in _SAFE_HEADER_VALUES
                else "⟦withheld⟧",
            })
        return rows
    return _withheld(scope, value, "HEADER_MAP")


def _project_value(
    scope: AuthenticatedBrowserScope,
    value: Any,
    *,
    path: tuple[str, ...] = (),
) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return (
            value
            if value == value and value not in {float("inf"), float("-inf")}
            else None
        )
    if isinstance(value, str):
        return project_text(scope, value)
    if isinstance(value, (list, tuple)):
        if len(value) >= 64 and all(
            type(item) is int and 0 <= item <= 255 for item in value
        ):
            return _withheld(scope, value, "BINARY_BYTE_ARRAY")
        if (
            len(value) >= 2
            and all(isinstance(item, str) for item in value)
            and sum(len(item) for item in value) >= 256
            and all(re.fullmatch(r"[A-Za-z0-9+/=]+", item) for item in value)
        ):
            return _withheld(scope, value, "ENCODED_BINARY_CHUNKS")
        return [_project_value(scope, item, path=path + ("[]",)) for item in value]
    if not isinstance(value, Mapping):
        raise AuthenticatedBrowserProjectionError(
            "OUTPUT_PROJECTION_FAILED", "result contains an unsupported value class"
        )
    output: dict[str, Any] = {}
    for raw_key, item in value.items():
        key = str(raw_key)
        normalized = _normalized_key(key)
        child_path = path + (key,)
        if normalized in _URL_FIELD_NAMES and isinstance(item, str):
            output[key] = project_url(scope, item)
        elif normalized in {"headers", "requestheaders", "responseheaders"}:
            output[key] = _project_headers(scope, item)
        elif normalized in _NON_ACTIONABLE_SECRET_FIELDS or normalized.endswith(
            "password"
        ):
            output[key] = _withheld(scope, item, "SECRET_VALUE")
        elif normalized in _IDENTITY_FIELDS and item not in (None, ""):
            output[key] = f"@id:{correlation_digest(scope, item)}"
        else:
            output[key] = _project_value(scope, item, path=child_path)
    return output


ToolProjector = Callable[[AuthenticatedBrowserScope, Any], Any]
CdpProjector = Callable[[AuthenticatedBrowserScope, Any], Any]


def _generic_projector(scope: AuthenticatedBrowserScope, value: Any) -> Any:
    return _project_value(scope, value)


def _cookie_projector(scope: AuthenticatedBrowserScope, value: Any) -> Any:
    projected = _project_value(scope, value)
    # _project_value already withholds keys named cookies wholesale. CDP cookie
    # inventories need names and structural flags, so reconstruct a restrictive
    # inventory without any replay-enabling values.
    if not isinstance(value, Mapping):
        raise AuthenticatedBrowserProjectionError(
            "OUTPUT_PROJECTION_FAILED", "cookie result must be an object"
        )
    cookies = value.get("cookies")
    if not isinstance(cookies, list):
        return projected
    rows = []
    for cookie in cookies:
        if not isinstance(cookie, Mapping):
            rows.append(_withheld(scope, cookie, "COOKIE_RECORD"))
            continue
        row: dict[str, Any] = {}
        if "name" in cookie:
            row["name"] = project_text(scope, str(cookie.get("name", "")))
        for key in ("domain", "path", "secure", "httpOnly", "sameSite", "session"):
            if key in cookie:
                row[key] = _project_value(scope, cookie[key], path=("cookies", key))
        row["value"] = _withheld(scope, cookie.get("value"), "COOKIE_VALUE")
        rows.append(row)
    return {"cookies": rows}


_TOOL_PROJECTORS = MappingProxyType({
    name: _generic_projector
    for name in (
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
    )
})

# This is intentionally exact rather than prefix-based. Adding an Electron CDP
# method is incomplete until its output disposition is selected here.
_CDP_PROJECTORS = MappingProxyType({
    **{
        method: _generic_projector
        for method in (
            "Accessibility.disable",
            "Accessibility.enable",
            "Accessibility.getFullAXTree",
            "Browser.getVersion",
            "DOM.describeNode",
            "DOM.disable",
            "DOM.enable",
            "DOM.getBoxModel",
            "DOM.getDocument",
            "DOM.querySelector",
            "Emulation.setDeviceMetricsOverride",
            "Input.dispatchKeyEvent",
            "Input.dispatchMouseEvent",
            "Input.insertText",
            "Network.disable",
            "Network.enable",
            "Page.disable",
            "Page.enable",
            "Page.getFrameTree",
            "Page.getLayoutMetrics",
            "Page.navigate",
            "Page.reload",
            "Page.stopLoading",
            "Runtime.disable",
            "Runtime.enable",
            "Runtime.evaluate",
            "Target.attachToTarget",
            "Target.detachFromTarget",
            "Target.getTargets",
        )
    },
    "Network.getCookies": _cookie_projector,
    "Network.getAllCookies": _cookie_projector,
})

PIXEL_CDP_METHODS = frozenset({
    "Page.captureScreenshot",
    "Page.startScreencast",
    "Page.printToPDF",
    "HeadlessExperimental.beginFrame",
})


def tool_projector_names() -> frozenset[str]:
    return frozenset(_TOOL_PROJECTORS)


def cdp_projector_names() -> frozenset[str]:
    return frozenset(_CDP_PROJECTORS)


def require_cdp_projector(method: str) -> None:
    if method in PIXEL_CDP_METHODS:
        raise AuthenticatedBrowserProjectionError(
            "CAPTURE_CONSENT_REQUIRED",
            "ordinary browser_cdp pixel routes cannot mint or consume trusted grants",
        )
    if method not in _CDP_PROJECTORS:
        raise AuthenticatedBrowserProjectionError(
            "UNPROJECTED_CDP_METHOD",
            "CDP method has no authenticated result projector",
        )


def _check_size(value: Any) -> None:
    if len(_canonical_bytes(value)) > MAX_PROJECTABLE_BYTES:
        raise AuthenticatedBrowserProjectionError(
            "OUTPUT_PROJECTION_FAILED",
            "result exceeds the authenticated projection limit",
        )


def project_tool_result(
    scope: AuthenticatedBrowserScope,
    tool_name: str,
    value: Any,
) -> Any:
    projector = _TOOL_PROJECTORS.get(tool_name)
    if projector is None:
        raise AuthenticatedBrowserProjectionError(
            "UNPROJECTED_BROWSER_TOOL", "browser tool has no authenticated projector"
        )
    _check_size(value)
    try:
        return projector(scope, value)
    except AuthenticatedBrowserProjectionError:
        raise
    except Exception as exc:
        raise AuthenticatedBrowserProjectionError(
            "OUTPUT_PROJECTION_FAILED", "authenticated browser output was withheld"
        ) from exc


def project_cdp_result(
    scope: AuthenticatedBrowserScope,
    method: str,
    value: Any,
) -> Any:
    require_cdp_projector(method)
    projector = _CDP_PROJECTORS[method]
    if not isinstance(value, Mapping):
        raise AuthenticatedBrowserProjectionError(
            "OUTPUT_PROJECTION_FAILED", "CDP result is malformed"
        )
    _check_size(value)
    try:
        return projector(scope, value)
    except AuthenticatedBrowserProjectionError:
        raise
    except Exception as exc:
        raise AuthenticatedBrowserProjectionError(
            "OUTPUT_PROJECTION_FAILED", "CDP projector failed closed"
        ) from exc


def serialize_projected_tool_result(
    scope: AuthenticatedBrowserScope,
    tool_name: str,
    raw_result: Any,
) -> Any:
    """Project a JSON-string or structured tool result without raw fallback."""

    structured = raw_result
    was_json = False
    if isinstance(raw_result, str):
        try:
            structured = json.loads(raw_result)
            was_json = True
        except (TypeError, ValueError):
            structured = raw_result
    projected = project_tool_result(scope, tool_name, structured)
    if was_json:
        return json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    return projected


def _reset_for_tests() -> None:
    global _runtime_digest_key
    with _lock:
        _url_capabilities.clear()
        _next_url_number.clear()
        _runtime_digest_key = b"A" * 32


__all__ = [
    "AuthenticatedBrowserProjectionError",
    "AuthenticatedBrowserScope",
    "MAX_PROJECTABLE_BYTES",
    "PIXEL_CDP_METHODS",
    "PROJECTOR_VERSION",
    "cdp_projector_names",
    "contains_url_reference",
    "correlation_digest",
    "invalidate_scope",
    "mint_url_reference",
    "project_cdp_result",
    "project_diagnostic",
    "project_text",
    "project_tool_result",
    "project_url",
    "require_cdp_projector",
    "resolve_url_reference",
    "retire_task",
    "serialize_projected_tool_result",
    "tool_projector_names",
]
