"""Memory-only scoped grants for hostile browser artifacts and live previews.

The gateway never returns a filesystem path or upstream URL from this module.
The opaque public reference is deliberately distinct from the delivery secret so
an internal URL copied from a browser guest is not, by itself, authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import ipaddress
import mimetypes
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from typing import Callable, Literal, Mapping
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit


_GRANT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")
_SCOPE_VALUE_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
_SENSITIVE_QUERY_RE = re.compile(
    r"(?:^|[-_.])(auth|authorization|bearer|credential|key|password|secret|session|signature|ticket|token)(?:$|[-_.])",
    re.IGNORECASE,
)
_INLINE_MIME_BY_SUFFIX = {
    ".css": "text/css; charset=utf-8",
    ".gif": "image/gif",
    ".htm": "text/html; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".webp": "image/webp",
}
_MAX_TTL_SECONDS = 10 * 60
_DEFAULT_TTL_SECONDS = 2 * 60
_MAX_ACTIVE_GRANTS = 512
_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024


class BrowserGrantError(Exception):
    """A typed, disclosure-safe grant failure."""

    def __init__(self, code: str, status_code: int = 400):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class BrowserGrantScope:
    recipient: str
    profile: str
    connection_id: str
    tab_id: str
    guest_generation: str
    source_session_id: str

    def validated(self) -> "BrowserGrantScope":
        values = (
            self.recipient,
            self.profile,
            self.connection_id,
            self.tab_id,
            self.guest_generation,
            self.source_session_id,
        )
        if not all(isinstance(value, str) and _SCOPE_VALUE_RE.fullmatch(value) for value in values):
            raise BrowserGrantError("invalid_scope")
        return self


@dataclass(frozen=True)
class BrowserArtifactGrant:
    ref: str
    credential: str
    scope: BrowserGrantScope
    target: Path
    root: Path
    display_name: str
    mime_type: str
    size: int
    device: int
    inode: int
    modified_ns: int
    expires_at: float
    kind: Literal["artifact"] = "artifact"


@dataclass(frozen=True)
class BrowserPreviewGrant:
    ref: str
    credential: str
    scope: BrowserGrantScope
    scheme: Literal["http", "https"]
    host: str
    port: int
    initial_path: str
    initial_query: str
    expires_at: float
    kind: Literal["preview"] = "preview"

    @property
    def origin(self) -> str:
        bracketed = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{bracketed}:{self.port}"


BrowserGrant = BrowserArtifactGrant | BrowserPreviewGrant


@dataclass(frozen=True)
class ArtifactRead:
    path: Path
    mime_type: str
    size: int
    start: int
    end: int
    status_code: int
    device: int
    inode: int
    modified_ns: int


class BrowserResourceGrantAuthority:
    """Thread-safe, process-memory authority for exact browser resource grants."""

    def __init__(
        self,
        *,
        workspace_root: Callable[[str, str], Path | None],
        clock: Callable[[], float] = time.monotonic,
        token: Callable[[int], str] = secrets.token_urlsafe,
    ) -> None:
        self._workspace_root = workspace_root
        self._clock = clock
        self._token = token
        self._lock = threading.RLock()
        self._grants: dict[str, BrowserGrant] = {}

    def _new_secret(self) -> str:
        # token_urlsafe(24) is exactly 32 URL-safe characters (192 bits).
        value = self._token(24)
        if not _GRANT_ID_RE.fullmatch(value):
            raise BrowserGrantError("entropy_unavailable", 503)
        return value

    def _expiry(self, ttl_seconds: int) -> float:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise BrowserGrantError("invalid_expiry")
        if ttl_seconds < 1 or ttl_seconds > _MAX_TTL_SECONDS:
            raise BrowserGrantError("invalid_expiry")
        return self._clock() + ttl_seconds

    def _insert(self, grant: BrowserGrant) -> BrowserGrant:
        with self._lock:
            self._purge_expired_locked()
            if len(self._grants) >= _MAX_ACTIVE_GRANTS:
                raise BrowserGrantError("grant_capacity", 429)
            self._grants[grant.ref] = grant
        return grant

    def mint_artifact(
        self,
        scope: BrowserGrantScope,
        candidate: str,
        *,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> BrowserArtifactGrant:
        scope = scope.validated()
        root = self._workspace_root(scope.profile, scope.source_session_id)
        if root is None:
            raise BrowserGrantError("source_session_unavailable", 404)
        try:
            root = Path(root).expanduser().resolve(strict=True)
            target = Path(candidate).expanduser()
            if ".." in target.parts:
                raise ValueError("lexical traversal")
            if not target.is_absolute():
                target = root / target
            target = target.resolve(strict=True)
            target.relative_to(root)
            info = target.stat()
        except (FileNotFoundError, NotADirectoryError):
            raise BrowserGrantError("artifact_unavailable", 404) from None
        except (OSError, RuntimeError, ValueError):
            raise BrowserGrantError("artifact_out_of_scope", 403) from None
        if not root.is_dir() or not stat.S_ISREG(info.st_mode):
            raise BrowserGrantError("artifact_unsupported", 415)
        if info.st_size > _MAX_ARTIFACT_BYTES:
            raise BrowserGrantError("artifact_too_large", 413)

        # Reuse the canonical file-read deny policy. A model-emitted path is
        # evidence to validate, never authority to reveal credentials.
        from agent.file_safety import get_read_block_error

        if get_read_block_error(str(target)) is not None:
            raise BrowserGrantError("artifact_sensitive", 403)
        mime_type = _INLINE_MIME_BY_SUFFIX.get(target.suffix.lower())
        if mime_type is None:
            guessed, _ = mimetypes.guess_type(target.name)
            if guessed not in {"application/pdf"} and not (guessed or "").startswith("image/"):
                raise BrowserGrantError("artifact_unsupported", 415)
            mime_type = guessed or "application/octet-stream"

        grant = BrowserArtifactGrant(
            ref=self._new_secret(),
            credential=self._new_secret(),
            scope=scope,
            target=target,
            root=root,
            display_name=target.name[:255] or "artifact",
            mime_type=mime_type,
            size=info.st_size,
            device=info.st_dev,
            inode=info.st_ino,
            modified_ns=info.st_mtime_ns,
            expires_at=self._expiry(ttl_seconds),
        )
        return self._insert(grant)  # type: ignore[return-value]

    def mint_preview(
        self,
        scope: BrowserGrantScope,
        upstream_url: str,
        *,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> BrowserPreviewGrant:
        scope = scope.validated()
        try:
            parsed = urlsplit(upstream_url)
            port = parsed.port
            host = parsed.hostname or ""
            address = ipaddress.ip_address(host)
        except (TypeError, ValueError):
            raise BrowserGrantError("preview_invalid") from None
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or port is None:
            raise BrowserGrantError("preview_invalid")
        mapped = getattr(address, "ipv4_mapped", None)
        if not (address.is_loopback or (mapped is not None and mapped.is_loopback)):
            raise BrowserGrantError("preview_not_loopback", 403)
        if any(_SENSITIVE_QUERY_RE.search(key) for key, _value in parse_qsl(parsed.query, keep_blank_values=True)):
            raise BrowserGrantError("preview_sensitive_url", 403)
        initial_path = parsed.path or "/"
        if not initial_path.startswith("/") or "\\" in initial_path:
            raise BrowserGrantError("preview_invalid")

        grant = BrowserPreviewGrant(
            ref=self._new_secret(),
            credential=self._new_secret(),
            scope=scope,
            scheme=parsed.scheme,  # type: ignore[arg-type]
            host=address.compressed,
            port=port,
            initial_path=initial_path,
            initial_query=parsed.query,
            expires_at=self._expiry(ttl_seconds),
        )
        return self._insert(grant)  # type: ignore[return-value]

    def _purge_expired_locked(self) -> None:
        now = self._clock()
        for ref, grant in list(self._grants.items()):
            if grant.expires_at <= now:
                del self._grants[ref]

    def _authorize(
        self,
        ref: str,
        credential: str,
        scope: BrowserGrantScope,
        kind: Literal["artifact", "preview"],
    ) -> BrowserGrant:
        scope = scope.validated()
        if not _GRANT_ID_RE.fullmatch(ref or "") or not _GRANT_ID_RE.fullmatch(credential or ""):
            raise BrowserGrantError("grant_invalid", 401)
        with self._lock:
            self._purge_expired_locked()
            grant = self._grants.get(ref)
            if grant is None or grant.kind != kind:
                raise BrowserGrantError("grant_unavailable", 404)
            if not hmac.compare_digest(grant.credential, credential):
                raise BrowserGrantError("grant_invalid", 401)
            if grant.scope != scope:
                # A valid secret presented under the wrong lifecycle tuple is a
                # real stale/cross-scope use. Revoke rather than leave it reusable.
                del self._grants[ref]
                raise BrowserGrantError("grant_scope_mismatch", 403)
            return grant

    def authorize_artifact(
        self, ref: str, credential: str, scope: BrowserGrantScope, range_header: str | None = None
    ) -> ArtifactRead:
        grant = self._authorize(ref, credential, scope, "artifact")
        assert isinstance(grant, BrowserArtifactGrant)
        try:
            current = grant.target.resolve(strict=True)
            current.relative_to(grant.root)
            info = current.stat()
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            self.revoke_ref(ref)
            raise BrowserGrantError("artifact_unavailable", 404) from None
        if (
            current != grant.target
            or not stat.S_ISREG(info.st_mode)
            or info.st_size != grant.size
            or info.st_dev != grant.device
            or info.st_ino != grant.inode
            or info.st_mtime_ns != grant.modified_ns
        ):
            self.revoke_ref(ref)
            raise BrowserGrantError("artifact_changed", 409)
        start, end, status_code = _parse_single_range(range_header, grant.size)
        return ArtifactRead(
            current,
            grant.mime_type,
            grant.size,
            start,
            end,
            status_code,
            grant.device,
            grant.inode,
            grant.modified_ns,
        )

    def authorize_preview(
        self, ref: str, credential: str, scope: BrowserGrantScope
    ) -> BrowserPreviewGrant:
        grant = self._authorize(ref, credential, scope, "preview")
        assert isinstance(grant, BrowserPreviewGrant)
        return grant

    def preview_target(self, grant: BrowserPreviewGrant, tail: str, query: str) -> str:
        clean_tail = tail.lstrip("/")
        path = f"/{clean_tail}" if clean_tail else grant.initial_path
        effective_query = query if query else (grant.initial_query if path == grant.initial_path else "")
        if "\\" in path or any(_SENSITIVE_QUERY_RE.search(key) for key, _ in parse_qsl(effective_query, keep_blank_values=True)):
            raise BrowserGrantError("preview_target_invalid", 403)
        return urlunsplit((grant.scheme, _netloc(grant.host, grant.port), path, effective_query, ""))

    def rewrite_preview_redirect(self, grant: BrowserPreviewGrant, current: str, location: str) -> str:
        try:
            target = urlsplit(urljoin(current, location))
            address = ipaddress.ip_address(target.hostname or "")
        except (TypeError, ValueError):
            raise BrowserGrantError("preview_redirect_denied", 502) from None
        if (
            target.scheme != grant.scheme
            or address.compressed != grant.host
            or target.port != grant.port
            or target.username
            or target.password
            or any(_SENSITIVE_QUERY_RE.search(key) for key, _ in parse_qsl(target.query, keep_blank_values=True))
        ):
            raise BrowserGrantError("preview_redirect_denied", 502)
        safe_path = "/".join(quote(segment, safe="-._~") for segment in target.path.split("/"))
        query = urlencode(parse_qsl(target.query, keep_blank_values=True))
        return f"/api/browser/preview/{grant.ref}/{safe_path.lstrip('/')}" + (f"?{query}" if query else "")

    def revoke_ref(self, ref: str) -> bool:
        with self._lock:
            return self._grants.pop(ref, None) is not None

    def revoke_scope(self, scope: BrowserGrantScope) -> int:
        scope = scope.validated()
        with self._lock:
            refs = [ref for ref, grant in self._grants.items() if grant.scope == scope]
            for ref in refs:
                del self._grants[ref]
            return len(refs)

    def revoke_where(self, **parts: str) -> int:
        allowed = set(BrowserGrantScope.__dataclass_fields__)
        if not parts or not set(parts).issubset(allowed):
            raise BrowserGrantError("invalid_scope")
        if not all(isinstance(value, str) and _SCOPE_VALUE_RE.fullmatch(value) for value in parts.values()):
            raise BrowserGrantError("invalid_scope")
        with self._lock:
            refs = [
                ref
                for ref, grant in self._grants.items()
                if all(getattr(grant.scope, key) == value for key, value in parts.items())
            ]
            for ref in refs:
                del self._grants[ref]
            return len(refs)

    def revoke_all(self) -> int:
        with self._lock:
            count = len(self._grants)
            self._grants.clear()
            return count


def _netloc(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _parse_single_range(value: str | None, size: int) -> tuple[int, int, int]:
    if size < 0:
        raise BrowserGrantError("artifact_unavailable", 404)
    if not value:
        return 0, size - 1, 200
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match or size == 0:
        raise BrowserGrantError("range_invalid", 416)
    first, last = match.groups()
    if not first and not last:
        raise BrowserGrantError("range_invalid", 416)
    if not first:
        length = int(last)
        if length <= 0:
            raise BrowserGrantError("range_invalid", 416)
        start = max(0, size - length)
        end = size - 1
    else:
        start = int(first)
        end = min(size - 1, int(last) if last else size - 1)
    if start >= size or start > end:
        raise BrowserGrantError("range_invalid", 416)
    return start, end, 206


def scope_from_headers(headers: Mapping[str, str]) -> BrowserGrantScope:
    return BrowserGrantScope(
        recipient=headers.get("x-hermes-browser-recipient", ""),
        profile=headers.get("x-hermes-browser-profile", ""),
        connection_id=headers.get("x-hermes-browser-connection", ""),
        tab_id=headers.get("x-hermes-browser-tab", ""),
        guest_generation=headers.get("x-hermes-browser-generation", ""),
        source_session_id=headers.get("x-hermes-browser-source-session", ""),
    ).validated()
