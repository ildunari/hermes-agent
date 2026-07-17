"""Privacy-bounded link fetching, metadata extraction, and public corroboration.

Exact URLs are accepted only by the pinned fetch boundary. They are never logged,
placed in search queries, or returned in canonical projection payloads.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from abc import ABC, abstractmethod
import hashlib
import hmac
import http.client
import ipaddress
import json
import re
import socket
import ssl
from typing import Any, Callable, Mapping, Protocol, Sequence
import urllib.parse

from .imessage_link_review import _entropy
from .phase_e_taxonomy import PLATFORM_ENTITY_DENYLIST, PUBLIC_ENTITIES, TAXONOMY

RESEARCH_VERSION = "phase-e-link-research-v4.0.0"
_ALLOWED_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml", "application/json", "application/ld+json"})
_TOKEN_KEY = re.compile(r"(?:^|[_-])(?:access[_-]?token|token|signature|sig|auth|api[_-]?key|secret|code|invite|unsubscribe|action|session|jwt|expires?)(?:$|[_-])", re.I)
_ACTION_PATH = re.compile(r"/(?:unsubscribe|verify|activate|confirm|accept|invite|reset|login|oauth|checkout)(?:/|$)", re.I)
_PUBLIC_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .&+'-]{0,78}[A-Za-z0-9]$")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Admission:
    allowed: bool
    code: str


@dataclass(frozen=True)
class FetchedPage:
    status: int
    content_type: str
    body: bytes
    headers: Mapping[str, str]
    peer_ip: str | None = None


@dataclass(frozen=True)
class LinkResearchRequest:
    evidence_id: str
    url: str
    platform: str
    shared_by: str
    occurred_at: float
    positive_reactors: tuple[str, ...] = ()
    replied_by: tuple[str, ...] = ()
    repeated_shares: int = 1
    distinct_days: int = 1


@dataclass(frozen=True)
class ResearchSource:
    support_id: str
    source_quality: str
    recency_band: str
    entity_type: str | None = None
    canonical_label: str | None = None
    ontology_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class LinkResearchResult:
    evidence_id: str
    status: str
    ontology_ids: tuple[str, ...] = ()
    public_entities: tuple[tuple[str, str], ...] = ()
    source_quality: str | None = None
    recency_band: str | None = None
    support_ids: tuple[str, ...] = ()
    sources: tuple[ResearchSource, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict, compare=False, repr=False)
    provider_commitment: str | None = None

    @classmethod
    def failure(cls, evidence_id: str, code: str) -> "LinkResearchResult":
        return cls(evidence_id=evidence_id, status=code)


class LinkResearchProvider(ABC):
    @abstractmethod
    def research(self, request: LinkResearchRequest) -> LinkResearchResult:
        raise NotImplementedError


class PublicSearchProvider(Protocol):
    name: str

    def search_public(self, terms: tuple[str, ...], *, limit: int) -> Sequence[ResearchSource]: ...


class Fetcher(Protocol):
    def fetch(self, url: str) -> FetchedPage: ...


def _fully_unquote(value: str) -> str:
    current = str(value)
    for _ in range(5):
        decoded = urllib.parse.unquote(current)
        if decoded == current:
            return decoded
        current = decoded
    return current


class SafeFetchPolicy:
    """Strict Phase E admission independent of generic private-URL overrides."""

    def admit(self, url: str) -> Admission:
        if any(ord(character) < 32 or ord(character) == 127 for character in str(url)):
            return Admission(False, "blocked_url")
        try:
            parsed = urllib.parse.urlsplit(url)
            host = (parsed.hostname or "").rstrip(".").casefold().encode("idna").decode("ascii")
            port = parsed.port
        except (ValueError, UnicodeError):
            return Admission(False, "blocked_url")
        if parsed.scheme.casefold() != "https":
            return Admission(False, "blocked_scheme")
        if parsed.username or parsed.password:
            return Admission(False, "blocked_credentials")
        if not host or host == "localhost" or host.endswith((".local", ".internal")):
            return Admission(False, "blocked_host")
        if port not in {None, 443}:
            return Admission(False, "blocked_port")
        decoded_path = _fully_unquote(parsed.path)
        if _ACTION_PATH.search(decoded_path):
            return Admission(False, "blocked_action")
        for segment in parsed.path.split("/"):
            value = _fully_unquote(segment)
            if len(value) >= 40 and _entropy(value) >= 4.0:
                return Admission(False, "blocked_high_entropy")
        for component in parsed.query.split("&") if parsed.query else ():
            key, _, value = component.partition("=")
            decoded_key = _fully_unquote(key)
            separated_key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", decoded_key)
            compact_key = re.sub(r"[^a-z0-9]", "", separated_key.casefold())
            sensitive_suffix = (
                "token", "signature", "secret", "invite", "unsubscribe", "action",
                "session", "jwt", "auth", "apikey", "verificationcode", "resetcode",
            )
            if _TOKEN_KEY.search(separated_key) or compact_key.endswith(sensitive_suffix):
                return Admission(False, "blocked_token")
            decoded = _fully_unquote(value)
            if len(decoded) >= 32 and _entropy(decoded) >= 4.0:
                return Admission(False, "blocked_high_entropy")
        literal = self.validate_addresses((host,), allow_hostname=True)
        return literal if not literal.allowed else Admission(True, "ok")

    def validate_addresses(self, addresses: Sequence[str], *, allow_hostname: bool = False) -> Admission:
        if not addresses:
            return Admission(False, "blocked_dns")
        for raw in addresses:
            try:
                address = ipaddress.ip_address(raw)
            except ValueError:
                if allow_hostname:
                    continue
                return Admission(False, "blocked_dns")
            if (
                not address.is_global or address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_unspecified or address.is_reserved
                or str(address) == "169.254.169.254"
            ):
                return Admission(False, "blocked_address")
        return Admission(True, "ok")


class _PinnedConnection:
    def __init__(self, host: str, address: str, timeout: float):
        self._host = host
        self._address = address
        self._timeout = timeout
        self._sock: ssl.SSLSocket | None = None

    @property
    def peer(self) -> str:
        if self._sock is None:
            raise RuntimeError("connection_not_open")
        return str(self._sock.getpeername()[0])

    def request(self, host: str, target: str, *, headers: dict[str, str]) -> FetchedPage:
        raw = socket.create_connection((self._address, 443), timeout=self._timeout)
        context = ssl.create_default_context()
        self._sock = context.wrap_socket(raw, server_hostname=host)
        response = http.client.HTTPResponse(self._sock)
        request = f"GET {target} HTTP/1.1\r\n" + "\r\n".join(
            f"{key}: {value}" for key, value in headers.items()
        ) + "\r\n\r\n"
        self._sock.sendall(request.encode("ascii"))
        response.begin()
        body = response.read(1_048_577)
        return FetchedPage(
            status=response.status,
            content_type=response.getheader("Content-Type", ""),
            body=body,
            headers={key.casefold(): value for key, value in response.getheaders()},
            peer_ip=self.peer,
        )

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()


class PinnedHttpsTransport:
    """Resolve, validate, pin, and peer-check every HTTPS redirect hop."""

    def __init__(
        self,
        *,
        policy: SafeFetchPolicy | None = None,
        resolver: Callable[[str], Sequence[str]] | None = None,
        connector: Callable[[str, str, float], object] | None = None,
        timeout: float = 8.0,
        max_redirects: int = 4,
        max_bytes: int = 1_048_576,
    ) -> None:
        self.policy = policy or SafeFetchPolicy()
        self.resolver = resolver or self._resolve
        self.connector = connector or (lambda host, address, timeout: _PinnedConnection(host, address, timeout))
        self.timeout = min(max(float(timeout), 0.5), 15.0)
        self.max_redirects = min(max(int(max_redirects), 0), 5)
        self.max_bytes = min(max(int(max_bytes), 1024), 2_097_152)

    @staticmethod
    def _resolve(host: str) -> tuple[str, ...]:
        return tuple(sorted({item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}))

    def fetch(self, url: str) -> FetchedPage:
        current = url
        for hop in range(self.max_redirects + 1):
            admission = self.policy.admit(current)
            if not admission.allowed:
                raise RuntimeError(admission.code)
            parsed = urllib.parse.urlsplit(current)
            try:
                host = str(parsed.hostname).rstrip(".").casefold().encode("idna").decode("ascii")
            except UnicodeError as exc:
                raise RuntimeError("blocked_url") from exc
            try:
                addresses = tuple(self.resolver(host))
            except (OSError, socket.gaierror) as exc:
                raise RuntimeError("blocked_dns") from exc
            address_check = self.policy.validate_addresses(addresses)
            if not address_check.allowed:
                raise RuntimeError(address_check.code)
            address = sorted(addresses, key=lambda value: (":" in value, value))[0]
            connection = self.connector(host, address, self.timeout)
            try:
                path = urllib.parse.quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
                query = urllib.parse.quote(parsed.query, safe="%&=:@!$'()*+,;/?-._~")
                target = urllib.parse.urlunsplit(("", "", path, query, ""))
                page = connection.request(host, target, headers={
                    "Host": host,
                    "User-Agent": "Hermes-LinkResearch/1",
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.8",
                    "Connection": "close",
                })
                peer = page.peer_ip or str(getattr(connection, "peer", ""))
                peer_check = self.policy.validate_addresses((peer,))
                if not peer_check.allowed or peer not in addresses:
                    raise RuntimeError("blocked_peer")
            finally:
                connection.close()
            if page.status in {301, 302, 303, 307, 308}:
                if hop >= self.max_redirects:
                    raise RuntimeError("redirect_limit")
                location = page.headers.get("location")
                if not location:
                    raise RuntimeError("redirect_missing_location")
                current = urllib.parse.urljoin(current, location)
                continue
            if page.status != 200:
                raise RuntimeError(_status_code(page.status))
            content_type = page.content_type.partition(";")[0].strip().casefold()
            if content_type not in _ALLOWED_CONTENT_TYPES:
                raise RuntimeError("unsupported_content")
            if len(page.body) > self.max_bytes:
                raise RuntimeError("response_too_large")
            return page
        raise RuntimeError("redirect_limit")


def _status_code(status: int) -> str:
    if status == 429:
        return "rate_limited"
    if 500 <= status <= 599:
        return "server_error"
    return "http_error"


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.body: list[str] = []
        self.meta: dict[str, str] = {}
        self.schema: list[str] = []
        self._title = False
        self._body = False
        self._json_ld = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        self._title = self._title or tag.casefold() == "title"
        self._body = self._body or tag.casefold() == "body"
        if tag.casefold() == "meta":
            key = (values.get("property") or values.get("name") or "").casefold()
            if key in {"og:title", "og:description", "og:type", "description", "author"}:
                self.meta[key] = values.get("content", "")
        if tag.casefold() == "script" and values.get("type", "").casefold() == "application/ld+json":
            self._json_ld = True

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "title":
            self._title = False
        elif tag.casefold() == "body":
            self._body = False
        elif tag.casefold() == "script":
            self._json_ld = False

    def handle_data(self, data: str) -> None:
        if self._title:
            self.title.append(data)
        elif self._json_ld:
            self.schema.append(data)
        elif self._body and len(" ".join(self.body)) < 2000:
            self.body.append(data)


def _clean(value: object, limit: int) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    text = re.sub(r"<[^>]{0,200}>", " ", text)
    return _SPACE.sub(" ", text).strip()[:limit]


def extract_public_metadata(page: FetchedPage) -> dict[str, str]:
    content_type = page.content_type.partition(";")[0].strip().casefold()
    result: dict[str, str] = {}
    if content_type in {"application/json", "application/ld+json"}:
        try:
            value = json.loads(page.body.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return {}
        if isinstance(value, Mapping):
            for source, target in (("name", "title"), ("description", "description"), ("@type", "schema_type"), ("author", "author_name")):
                if isinstance(value.get(source), (str, int, float)):
                    result[target] = _clean(value[source], 500)
        return result
    parser = _MetadataParser()
    parser.feed(page.body.decode("utf-8", "replace"))
    result["title"] = _clean(parser.meta.get("og:title") or " ".join(parser.title), 500)
    result["description"] = _clean(parser.meta.get("og:description") or parser.meta.get("description"), 500)
    result["author_name"] = _clean(parser.meta.get("author"), 200)
    result["type"] = _clean(parser.meta.get("og:type"), 100)
    result["body_summary"] = _clean(" ".join(parser.body), 1000)
    for payload in parser.schema[:4]:
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if not isinstance(item, Mapping):
                continue
            if item.get("@type"):
                result["schema_type"] = _clean(item["@type"], 100)
            if item.get("name"):
                result["schema_name"] = _clean(item["name"], 300)
            break
    return {key: value for key, value in result.items() if value}


def _closed_semantics(metadata: Mapping[str, str]) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    text = " ".join(metadata.values()).casefold()
    ontology = []
    for rule in TAXONOMY:
        if any(re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) for phrase in rule.phrases):
            ontology.append(rule.ontology_id)
    entities = []
    for item in PUBLIC_ENTITIES:
        if item.canonical_label.casefold() in PLATFORM_ENTITY_DENYLIST:
            continue
        if any(re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", text) for alias in item.aliases):
            entities.append((item.entity_type, item.canonical_label))
    return tuple(sorted(set(ontology))), tuple(sorted(set(entities)))


def _public_terms(metadata: Mapping[str, str]) -> tuple[str, ...]:
    """Return only closed-world public labels, never arbitrary fetched-page text."""
    ontology, entities = _closed_semantics(metadata)
    by_ontology = {rule.ontology_id: rule.label for rule in TAXONOMY}
    labels = [by_ontology[item] for item in ontology if item in by_ontology]
    labels.extend(label for _entity_type, label in entities)
    return tuple(dict.fromkeys(label for label in labels if _PUBLIC_TOKEN.fullmatch(label)))[:12]


class HermesWebSearchAdapter:
    """Expose a configured Hermes provider without leaking exact private URLs."""

    def __init__(self, provider: Any, *, secret: bytes) -> None:
        if len(secret) < 16:
            raise ValueError("search commitment secret must be at least 16 bytes")
        self._provider = provider
        self._secret = bytes(secret)
        self.name = f"hermes:{getattr(provider, 'name', 'unknown')}"

    def search_public(self, terms: tuple[str, ...], *, limit: int) -> Sequence[ResearchSource]:
        clean = tuple(term for term in terms[:12] if _PUBLIC_TOKEN.fullmatch(term))
        if not clean:
            return ()
        try:
            response = self._provider.search(
                " ".join(clean)[:500], limit=min(max(int(limit), 1), 5),
            )
        except Exception:
            return ()
        if not isinstance(response, Mapping) or not response.get("success"):
            return ()
        data = response.get("data")
        rows = data.get("web") if isinstance(data, Mapping) else None
        if not isinstance(rows, list):
            return ()
        result: list[ResearchSource] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            url = str(row.get("url") or "")
            title = _clean(row.get("title"), 200)
            description = _clean(row.get("description"), 500)
            if not title or not SafeFetchPolicy().admit(url).allowed:
                continue
            ontology, entities = _closed_semantics({"title": title, "description": description})
            support_id = hmac.new(
                self._secret, ("phase-e-public-source-v1\0" + url).encode(), hashlib.sha256,
            ).hexdigest()
            host = (urllib.parse.urlsplit(url).hostname or "").casefold()
            quality = (
                "primary" if host.endswith((".gov", ".edu"))
                else "established" if any(part in host for part in ("reuters", "apnews", "bbc", "nytimes"))
                else "public-web"
            )
            first_entity = entities[0] if entities else (None, None)
            result.append(ResearchSource(
                support_id=support_id, source_quality=quality, recency_band="unknown",
                entity_type=first_entity[0], canonical_label=first_entity[1],
                ontology_ids=ontology,
            ))
        return tuple(result)


class MetadataSearchResearchProvider(LinkResearchProvider):
    """Fetch privately, then optionally corroborate only sanitized public terms."""

    def __init__(self, *, fetcher: Fetcher, search_provider: PublicSearchProvider | None = None) -> None:
        self.fetcher = fetcher
        self.search_provider = search_provider

    def research(self, request: LinkResearchRequest) -> LinkResearchResult:
        try:
            page = self.fetcher.fetch(request.url)
            metadata = extract_public_metadata(page)
        except RuntimeError as exc:
            code = str(exc) if re.fullmatch(r"[a-z_]+", str(exc)) else "fetch_failure"
            if code == "timeout":
                code = "fetch_timeout"
            return LinkResearchResult.failure(request.evidence_id, code)
        except (OSError, TimeoutError):
            return LinkResearchResult.failure(request.evidence_id, "fetch_timeout")
        if not metadata:
            return LinkResearchResult.failure(request.evidence_id, "metadata_empty")
        ontology, entities = _closed_semantics(metadata)
        sources: tuple[ResearchSource, ...] = ()
        terms = _public_terms(metadata)
        if self.search_provider is not None and terms:
            try:
                sources = tuple(self.search_provider.search_public(terms, limit=5))
            except Exception:
                sources = ()
            fetched_ontology = set(ontology)
            fetched_entities = set(entities)
            sources = tuple(
                source for source in sources
                if fetched_ontology.intersection(source.ontology_ids)
                or (source.entity_type, source.canonical_label) in fetched_entities
            )
        commitment_payload = {
            "version": RESEARCH_VERSION,
            "provider": getattr(self.search_provider, "name", "private-fetch-only"),
            "ontology_ids": ontology,
            "public_entities": entities,
            "support_ids": tuple(sorted(source.support_id for source in sources)),
        }
        commitment = hashlib.sha256(json.dumps(commitment_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return LinkResearchResult(
            evidence_id=request.evidence_id,
            status="ok",
            ontology_ids=ontology,
            public_entities=entities,
            source_quality=(sources[0].source_quality if sources else "fetched-primary"),
            recency_band=(sources[0].recency_band if sources else "unknown"),
            support_ids=tuple(sorted(source.support_id for source in sources)),
            sources=sources,
            metadata=metadata,
            provider_commitment=commitment,
        )


__all__ = [
    "Admission", "FetchedPage", "HermesWebSearchAdapter", "LinkResearchProvider", "LinkResearchRequest", "LinkResearchResult",
    "MetadataSearchResearchProvider", "PinnedHttpsTransport", "PublicSearchProvider", "RESEARCH_VERSION",
    "ResearchSource", "SafeFetchPolicy", "extract_public_metadata",
]
