"""Deterministic, privacy-minimizing link signals from one approved iMessage chat.

The public output contains URL hashes and mechanical metadata, never message
bodies or full URLs.  No function in this module writes contact-memory state.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from typing import Any, Callable, Iterable, Iterator, Mapping

from .imessage_bootstrap import (
    ResolvedChat, _decode_attributed_body, _apple_timestamp,
)

_URL_RE = re.compile(r"https?://[^\s<>\"'\]\[{}]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}"
_TRACKING = frozenset({
    "fbclid", "gclid", "dclid", "msclkid", "igshid", "mc_cid", "mc_eid",
    "ref", "ref_src", "source", "spm", "si", "feature",
})
_POSITIVE_TAPBACKS = frozenset({2000, 2001, 2003, 2004})
_RULES: tuple[tuple[frozenset[str], str, str], ...] = (
    (frozenset({"instagram.com", "www.instagram.com"}), "social-video", "Instagram reels and posts"),
    (frozenset({"tiktok.com", "www.tiktok.com", "vm.tiktok.com"}), "social-video", "TikTok videos"),
    (frozenset({"youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com"}), "video", "YouTube videos"),
    (frozenset({"spotify.com", "open.spotify.com"}), "music", "Spotify music and podcasts"),
    (frozenset({"reddit.com", "www.reddit.com", "old.reddit.com"}), "discussion", "Reddit discussions"),
    (frozenset({"x.com", "www.x.com", "twitter.com", "www.twitter.com"}), "social-post", "X posts"),
)
_SENSITIVE = frozenset({
    "abortion", "addiction", "alcohol", "assault", "cancer", "crime", "death", "diagnosis",
    "disease", "drug", "drugs", "election", "erectile", "genocide", "gun", "health", "hospital",
    "immigration", "injury", "legal", "medicine", "murder", "nazi", "opioid", "politics",
    "prescription", "relationship", "sex", "sexual", "steroid", "suicide", "therapy", "trump",
    "vaccine", "violence", "war", "weed",
})
_WORD_RE = re.compile(r"[a-z0-9]+", re.I)


@dataclass(frozen=True)
class LinkSignal:
    canonical_url: str  # process-local only; omitted from manifests
    url_sha256: str
    message_guid: str
    created_at: float
    sender: str
    domain: str
    category: str | None
    topic: str | None
    positive_tapback: bool = False
    recipient_reply: bool = False


class FetchError(RuntimeError):
    """A bounded enrichment failed; callers may keep this only in local cache."""


def canonicalize_url(raw: str) -> str | None:
    """Return a stable public HTTP(S) URL without fragments/tracking fields."""
    candidate = raw.strip().rstrip(_TRAILING)
    try:
        parsed = urllib.parse.urlsplit(candidate)
        scheme = parsed.scheme.casefold()
        host = (parsed.hostname or "").rstrip(".").casefold()
        port = parsed.port
    except (ValueError, UnicodeError):
        return None
    if scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc += f":{port}"
    path = urllib.parse.quote(urllib.parse.unquote(parsed.path or "/"), safe="/%:@!$&'()*+,;=-._~")
    query = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        lower = key.casefold()
        if lower.startswith("utm_") or lower in _TRACKING:
            continue
        query.append((key, value))
    query.sort()
    return urllib.parse.urlunsplit((scheme, netloc, path, urllib.parse.urlencode(query, doseq=True), ""))


def extract_urls(text: str | None) -> list[str]:
    found: list[str] = []
    for match in _URL_RE.findall(text or ""):
        canonical = canonicalize_url(match)
        if canonical and canonical not in found:
            found.append(canonical)
    return found


def _row_urls(text: object, attributed_body: object) -> list[str]:
    """Recover only explicit URL runs from both message representations."""
    candidates = [str(text)] if text is not None else []
    decoded = _decode_attributed_body(attributed_body)
    if decoded:
        candidates.append(decoded)
    if isinstance(attributed_body, (bytes, bytearray, memoryview)):
        # Keyed archives often contain the URL as an intact UTF-8 run even if
        # the surrounding archive cannot be decoded. We parse only http(s)
        # tokens, never infer or concatenate fragments.
        candidates.append(bytes(attributed_body).decode("utf-8", "ignore"))
    urls: list[str] = []
    for candidate in candidates:
        for url in extract_urls(candidate):
            if url.casefold().startswith("http://www.apple.com/dtds/propertylist-"):
                continue
            if url not in urls:
                urls.append(url)
    return urls


def classify_url(url: str) -> tuple[str | None, str | None]:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").casefold()
    words = {word.casefold() for word in _WORD_RE.findall(host + " " + parsed.path)}
    if words & _SENSITIVE:
        return None, None
    for hosts, category, topic in _RULES:
        if host in hosts:
            return category, topic
    return None, None


def _columns(con: Any, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})")}


def _target_guid(value: object) -> str:
    text = str(value or "")
    # Apple's associated GUID is commonly p:0:<message-guid>.
    return text.split(":", 2)[-1] if text.startswith(("p:", "bp:")) else text


def iter_link_signals(con: Any, chat: ResolvedChat) -> Iterator[LinkSignal]:
    """Read link rows and mechanically attributable engagement in one snapshot."""
    columns = _columns(con, "message")
    attributed = "m.attributedBody" if "attributedBody" in columns else "NULL"
    associated_guid = "m.associated_message_guid" if "associated_message_guid" in columns else "NULL"
    thread_guid = "m.thread_originator_guid" if "thread_originator_guid" in columns else "NULL"
    sql = f"""SELECT m.ROWID,m.guid,m.date,m.is_from_me,m.text,{attributed} attributedBody,
                     COALESCE(m.associated_message_type,0) associated_type,
                     {associated_guid} associated_guid,{thread_guid} thread_guid
              FROM message m JOIN chat_message_join cmj ON cmj.message_id=m.ROWID
              WHERE cmj.chat_id=? ORDER BY m.date,m.ROWID"""
    rows = list(con.execute(sql, (chat.chat_id,)))
    directions: dict[str, bool] = {str(row["guid"] or ""): bool(row["is_from_me"]) for row in rows}
    tapbacks: set[str] = set()
    replies: set[str] = set()
    for row in rows:
        sender_is_me = bool(row["is_from_me"])
        target = _target_guid(row["associated_guid"])
        kind = int(row["associated_type"] or 0)
        if target in directions and directions[target] != sender_is_me and kind in _POSITIVE_TAPBACKS:
            tapbacks.add(target)
        thread = _target_guid(row["thread_guid"])
        if thread in directions and directions[thread] != sender_is_me and kind == 0:
            replies.add(thread)

    deduped: dict[tuple[str, str], LinkSignal] = {}
    for row in rows:
        if int(row["associated_type"] or 0) != 0:
            continue
        for url in _row_urls(row["text"], row["attributedBody"]):
            category, topic = classify_url(url)
            guid = str(row["guid"] or f"rowid:{int(row['ROWID'])}")
            signal = LinkSignal(
                canonical_url=url,
                url_sha256=hashlib.sha256(url.encode()).hexdigest(),
                message_guid=guid,
                created_at=_apple_timestamp(row["date"]),
                sender="kosta-owner" if bool(row["is_from_me"]) else "stephen-lucier",
                domain=(urllib.parse.urlsplit(url).hostname or "").casefold(),
                category=category,
                topic=topic,
                positive_tapback=guid in tapbacks,
                recipient_reply=guid in replies,
            )
            # A URL can be shared by each participant without losing sender
            # attribution. Repeats by one sender count once; prefer the
            # mechanically engaged occurrence when selecting its pointer.
            key = (signal.sender, url)
            previous = deduped.get(key)
            if previous is None or (
                (signal.positive_tapback or signal.recipient_reply)
                and not (previous.positive_tapback or previous.recipient_reply)
            ):
                deduped[key] = signal
    yield from deduped.values()


def build_review_manifest(chat: ResolvedChat, signals: Iterable[LinkSignal]) -> dict[str, Any]:
    """Create canonical review data. A topic needs at least two distinct links."""
    ordered = sorted(signals, key=lambda item: (item.sender, item.topic or "", item.created_at, item.url_sha256))
    groups: dict[tuple[str, str], list[LinkSignal]] = defaultdict(list)
    excluded: Counter[str] = Counter()
    for signal in ordered:
        if not signal.topic or not signal.category:
            excluded["unclassified_or_sensitive"] += 1
            continue
        groups[(signal.sender, signal.topic)].append(signal)
    candidates = []
    for (sender, topic), items in sorted(groups.items()):
        if len(items) < 2:
            excluded["single_link"] += 1
            continue
        candidates.append({
            "candidate_id": hashlib.sha256((sender + "\0" + topic).encode()).hexdigest(),
            "subject": sender,
            "topic": topic,
            "category": items[0].category,
            "distinct_links": len(items),
            "positive_tapbacks": sum(item.positive_tapback for item in items),
            "recipient_replies": sum(item.recipient_reply for item in items),
            "signals": [{
                "url_sha256": item.url_sha256,
                "message_guid": item.message_guid,
                "created_at": item.created_at,
                "sender": item.sender,
                "domain": item.domain,
                "positive_tapback": item.positive_tapback,
                "recipient_reply": item.recipient_reply,
            } for item in items],
        })
    manifest: dict[str, Any] = {
        "schema": 1,
        "kind": "imessage-link-interest-review",
        "apply_supported": False,
        "chat_guid_sha256": hashlib.sha256(chat.chat_guid.encode()).hexdigest(),
        "handle_sha256": hashlib.sha256(chat.handle.encode()).hexdigest(),
        "candidates": candidates,
        "counts": {
            "unique_links": len(ordered), "candidate_topics": len(candidates),
            "excluded": dict(sorted(excluded.items())),
        },
    }
    manifest["review_sha256"] = hashlib.sha256(_canonical_bytes(manifest)).hexdigest()
    return manifest


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def is_public_url(url: str, *, resolver: Callable[..., Any] = socket.getaddrinfo) -> bool:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return False
    try:
        addresses = resolver(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        ips = {ipaddress.ip_address(item[4][0]) for item in addresses}
    except (OSError, ValueError):
        return False
    return bool(ips) and all(ip.is_global for ip in ips)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def fetch_public_metadata(
    url: str, *, timeout: float = 5.0, max_bytes: int = 131_072, max_redirects: int = 3,
    resolver: Callable[..., Any] = socket.getaddrinfo,
    opener: Any | None = None,
) -> dict[str, str]:
    """Optionally fetch bounded public metadata, validating every redirect hop."""
    current = canonicalize_url(url)
    if not current:
        raise FetchError("invalid URL")
    http = opener or urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))
    for _ in range(max_redirects + 1):
        if not is_public_url(current, resolver=resolver):
            raise FetchError("non-public destination refused")
        parsed = urllib.parse.urlsplit(current)
        robots_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
        robots = urllib.robotparser.RobotFileParser(robots_url)
        try:
            robots_response = http.open(urllib.request.Request(
                robots_url, headers={"User-Agent": "HermesLinkReview/1.0"}), timeout=timeout,
            )
            with robots_response:
                robots_data = robots_response.read(min(max_bytes, 65_536) + 1)
            if len(robots_data) > min(max_bytes, 65_536):
                raise FetchError("robots response too large")
            robots.parse(robots_data.decode("utf-8", "replace").splitlines())
        except FetchError:
            raise
        except Exception:
            raise FetchError("robots unavailable") from None
        if not robots.can_fetch("HermesLinkReview/1.0", current):
            raise FetchError("robots disallow fetch")
        request = urllib.request.Request(current, headers={"User-Agent": "HermesLinkReview/1.0", "Accept": "text/html"})
        try:
            response = http.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308} and exc.headers.get("Location"):
                current = urllib.parse.urljoin(current, exc.headers["Location"])
                continue
            raise FetchError(f"HTTP {exc.code}") from None
        except (OSError, urllib.error.URLError) as exc:
            raise FetchError(type(exc).__name__) from None
        with response:
            if response.headers.get_content_type() != "text/html":
                raise FetchError("non-HTML response")
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise FetchError("response too large")
        html = data.decode("utf-8", "replace")
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        title = re.sub(r"\s+", " ", title_match.group(1)).strip()[:240] if title_match else ""
        return {"final_url_sha256": hashlib.sha256(current.encode()).hexdigest(), "title": title}
    raise FetchError("too many redirects")


__all__ = ["FetchError", "LinkSignal", "build_review_manifest", "canonicalize_url", "classify_url",
           "extract_urls", "fetch_public_metadata", "is_public_url", "iter_link_signals"]
