"""Privacy-minimizing iMessage link-interest review primitives.

The review manifest is aggregate-only.  Exact URLs, GUIDs and timestamps are
confined to a separately permissioned evidence map/fetch queue.  This module
never performs network I/O: a trusted, DNS-pinning web tool may consume the
bounded queue and return a private metadata cache for a later offline pass.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import math
import plistlib
import re
import urllib.parse
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .imessage_bootstrap import ResolvedChat, _apple_timestamp

_URL_RE = re.compile(r"https?://[^\s<>\"'\]\[{}]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}"
_TRACKING = frozenset({"fbclid", "gclid", "dclid", "msclkid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src", "source", "spm", "si", "feature"})
_POSITIVE_ADDS = frozenset({2000, 2001, 2003, 2004})
_POSITIVE_REMOVALS = frozenset(value + 1000 for value in _POSITIVE_ADDS)
_PLATFORM: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"instagram.com", "www.instagram.com"}), "instagram"),
    (frozenset({"tiktok.com", "www.tiktok.com", "vm.tiktok.com"}), "tiktok"),
    (frozenset({"youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com"}), "youtube"),
    (frozenset({"spotify.com", "open.spotify.com"}), "spotify"),
    (frozenset({"x.com", "www.x.com", "twitter.com", "www.twitter.com"}), "x"),
)
_RISK_KEYS = re.compile(r"(?:^|[_-])(token|signature|sig|auth|key|secret|code|invite|unsubscribe|action|session|jwt|expires?)(?:$|[_-])", re.I)
_ACTION_PATH = re.compile(r"/(?:unsubscribe|verify|activate|confirm|accept|invite|reset|login|oauth|checkout)(?:/|$)", re.I)
_WORD_RE = re.compile(r"[a-z0-9]+", re.I)

# Deliberately small, deterministic and normalized.  Titles never leave the
# private metadata cache; only these labels can reach a review manifest.
_TAXONOMY: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("music", "electronic dance music", frozenset({"techno", "house", "edm", "trance", "rave", "dj"})),
    ("music", "music", frozenset({"music", "song", "album", "artist", "band", "concert", "playlist", "track", "singer", "rapper"})),
    ("pop-culture-film-tv", "film and television", frozenset({"movie", "film", "cinema", "actor", "actress", "series", "television", "tv", "netflix", "trailer"})),
    ("bodybuilding-fitness", "bodybuilding and strength training", frozenset({"bodybuilding", "bodybuilder", "muscle", "hypertrophy", "workout", "fitness", "gym", "lifting", "powerlifting", "strongman"})),
    ("nightlife-parties-festivals", "nightlife, parties, and festivals", frozenset({"nightlife", "party", "club", "festival", "rave", "dancefloor"})),
    ("humor-memes", "humor and memes", frozenset({"funny", "humor", "comedy", "comedian", "meme", "satire", "joke"})),
    ("food", "food and dining", frozenset({"food", "restaurant", "recipe", "cooking", "chef", "dinner", "lunch", "breakfast", "cuisine"})),
    ("technology", "technology", frozenset({"technology", "software", "computer", "iphone", "android", "gadget", "coding", "programming", "robot", "ai"})),
    ("animals", "animals and pets", frozenset({"animal", "dog", "cat", "pet", "puppy", "kitten", "wildlife"})),
    ("vehicles", "vehicles", frozenset({"car", "cars", "vehicle", "tesla", "motorcycle", "truck", "automotive"})),
    ("travel", "travel", frozenset({"travel", "trip", "vacation", "flight", "hotel", "tourism", "destination", "beach"})),
)
LINK_INTEREST_TAXONOMY = frozenset(
    (category, topic) for category, topic, _keywords in _TAXONOMY
)
_METADATA_FIELDS = frozenset({"title", "description", "author_name", "provider_name", "type", "platform"})


@dataclass(frozen=True)
class LinkSignal:
    identity_url: str
    fetch_url: str
    message_guid: str
    created_at: float
    shared_by: str
    domain: str
    platform: str | None
    positive_reactors: tuple[str, ...] = ()
    replied_by: tuple[str, ...] = ()  # neutral engagement only

    @property
    def canonical_url(self) -> str:  # compatibility for private callers
        return self.identity_url


class FetchError(RuntimeError):
    """Direct fetching is intentionally unavailable without pinned transport."""


def canonicalize_url(raw: str) -> str | None:
    """Conservative identity URL: preserve escapes and non-tracker query order."""
    candidate = raw.strip().rstrip(_TRAILING)
    try:
        parsed = urllib.parse.urlsplit(candidate)
        scheme = parsed.scheme.casefold()
        host = (parsed.hostname or "").rstrip(".").casefold().encode("idna").decode("ascii")
        port = parsed.port
    except (ValueError, UnicodeError):
        return None
    if scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc += f":{port}"
    kept: list[str] = []
    for component in parsed.query.split("&") if parsed.query else ():
        encoded_key = component.partition("=")[0]
        try:
            key = urllib.parse.unquote(encoded_key).casefold()
        except Exception:
            key = encoded_key.casefold()
        if key.startswith("utm_") or key in _TRACKING:
            continue
        kept.append(component)
    # Never unquote reserved path escapes and never sort/re-encode query data.
    return urllib.parse.urlunsplit((scheme, netloc, parsed.path or "/", "&".join(kept), ""))


def extract_urls(text: str | None) -> list[str]:
    result: list[str] = []
    for match in _URL_RE.findall(text or ""):
        value = canonicalize_url(match)
        if value and value not in result:
            result.append(value)
    return result


def _attributed_visible_string(blob: object) -> str | None:
    """Decode only an explicit attributed string, never arbitrary archive runs."""
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        return None
    try:
        value = plistlib.loads(bytes(blob))
    except Exception:
        return None
    if not isinstance(value, Mapping):
        return None
    # Simple archived fixtures and some legacy messages expose NSString at the
    # attributed-string root.  Do not recursively choose a longest string:
    # sibling preview/CDN metadata is not visible message text.
    for key in ("NSString", "string", "NS.string"):
        text = value.get(key)
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


def _row_urls(text: object, attributed_body: object) -> list[str]:
    # A populated message.text is authoritative.  The attributed archive must
    # not add hidden rich-preview URLs alongside it.
    if text is not None and str(text).strip():
        return extract_urls(str(text))
    return extract_urls(_attributed_visible_string(attributed_body))


def classify_url(url: str) -> tuple[str | None, str | None]:
    host = (urllib.parse.urlsplit(url).hostname or "").casefold()
    for hosts, platform in _PLATFORM:
        if host in hosts:
            return platform, None
    return None, None


def _columns(con: Any, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})")}


def _target_guid(value: object) -> str:
    text = str(value or "")
    match = re.match(r"^(?:bp|p):\d+(?::|/)(.+)$", text)
    return match.group(1) if match else text


def iter_link_signals(con: Any, chat: ResolvedChat) -> Iterator[LinkSignal]:
    """Extract visible links after validating every incoming handle id."""
    columns = _columns(con, "message")
    attributed = "m.attributedBody" if "attributedBody" in columns else "NULL"
    associated_guid = "m.associated_message_guid" if "associated_message_guid" in columns else "NULL"
    thread_guid = "m.thread_originator_guid" if "thread_originator_guid" in columns else "NULL"
    handle_id = "m.handle_id" if "handle_id" in columns else "NULL"
    sql = f"""SELECT m.ROWID,m.guid,m.date,m.is_from_me,m.text,{attributed} attributedBody,
                     COALESCE(m.associated_message_type,0) associated_type,
                     {associated_guid} associated_guid,{thread_guid} thread_guid,
                     {handle_id} handle_id
              FROM message m JOIN chat_message_join cmj ON cmj.message_id=m.ROWID
              WHERE cmj.chat_id=? ORDER BY m.date,m.ROWID"""
    rows = list(con.execute(sql, (chat.chat_id,)))
    approved_ids = set(chat.approved_handle_ids or (chat.handle_id,))
    validated_rows = []
    for row in rows:
        if bool(row["is_from_me"]):
            validated_rows.append(row)
            continue
        if "handle_id" not in columns:
            raise ValueError("message.handle_id is required for incoming identity validation")
        incoming_id = int(row["handle_id"] or 0)
        if incoming_id == 0:
            # Modern Messages contains a small number of service/sync rows with
            # no sender identity even inside direct chats. They cannot be
            # attributed, so omit them rather than poisoning the whole review.
            continue
        if incoming_id not in approved_ids:
            raise ValueError("incoming message has an unapproved handle_id")
        validated_rows.append(row)
    rows = validated_rows

    directions = {str(row["guid"] or ""): bool(row["is_from_me"]) for row in rows}
    reaction_state: dict[tuple[str, str], bool] = {}
    replies: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        actor = "kosta-owner" if bool(row["is_from_me"]) else "stephen-lucier"
        target = _target_guid(row["associated_guid"])
        kind = int(row["associated_type"] or 0)
        if target in directions and directions[target] != bool(row["is_from_me"]):
            if kind in _POSITIVE_ADDS:
                reaction_state[(target, actor)] = True
            elif kind in _POSITIVE_REMOVALS:
                reaction_state[(target, actor)] = False
        thread = _target_guid(row["thread_guid"])
        if kind == 0 and thread in directions and directions[thread] != bool(row["is_from_me"]):
            replies[thread].add(actor)

    for row in rows:
        if int(row["associated_type"] or 0) != 0:
            continue
        guid = str(row["guid"] or f"rowid:{int(row['ROWID'])}")
        shared_by = "kosta-owner" if bool(row["is_from_me"]) else "stephen-lucier"
        reactors = tuple(sorted(actor for (target, actor), active in reaction_state.items() if target == guid and active))
        for url in _row_urls(row["text"], row["attributedBody"]):
            platform, _ = classify_url(url)
            yield LinkSignal(
                identity_url=url, fetch_url=url, message_guid=guid,
                created_at=_apple_timestamp(row["date"]), shared_by=shared_by,
                domain=(urllib.parse.urlsplit(url).hostname or "").casefold(), platform=platform,
                positive_reactors=reactors, replied_by=tuple(sorted(replies.get(guid, ()))),
            )


def evidence_id(secret: bytes, namespace: str, value: str) -> str:
    if len(secret) < 16:
        raise ValueError("HMAC secret must be at least 16 bytes")
    return hmac.new(secret, (namespace + "\0" + value).encode(), hashlib.sha256).hexdigest()


def verify_review_id(manifest: Mapping[str, Any], secret: bytes) -> bool:
    """Verify the HMAC binding for an aggregate schema-2 review manifest."""
    supplied = manifest.get("review_id")
    if not isinstance(supplied, str) or not re.fullmatch(r"[0-9a-f]{64}", supplied):
        return False
    unsigned = dict(manifest)
    unsigned.pop("review_id", None)
    payload = json.dumps(unsigned, sort_keys=True, separators=(",", ":"))
    expected = evidence_id(secret, "review", payload)
    return hmac.compare_digest(supplied, expected)


def _link_id(secret: bytes, signal: LinkSignal) -> str:
    return evidence_id(secret, "url", signal.identity_url)


def is_safe_fetch_candidate(url: str) -> bool:
    """Reject action-bearing, signed/tokenized, local and high-entropy URLs."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not host or host in {"localhost"} or host.endswith((".local", ".internal")):
        return False
    try:
        if not ipaddress.ip_address(host).is_global:
            return False
    except ValueError:
        pass
    try:
        if parsed.port is not None and parsed.port not in {80, 443}:
            return False
    except ValueError:
        return False
    if _ACTION_PATH.search(parsed.path):
        return False
    for segment in parsed.path.split("/"):
        decoded_segment = urllib.parse.unquote(segment)
        if len(decoded_segment) >= 40 and _entropy(decoded_segment) >= 4.0:
            return False
    for component in parsed.query.split("&") if parsed.query else ():
        key, _, value = component.partition("=")
        if _RISK_KEYS.search(urllib.parse.unquote(key)):
            return False
        decoded = urllib.parse.unquote(value)
        if len(decoded) >= 32 and _entropy(decoded) >= 4.0:
            return False
    return True


def _entropy(value: str) -> float:
    counts = Counter(value)
    length = len(value) or 1
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def select_enrichment_queue(signals: Sequence[LinkSignal], secret: bytes, *, max_requests: int = 50) -> dict[str, Any]:
    """Select and rank before any trusted network tool receives full URLs."""
    if not 0 <= max_requests <= 50:
        raise ValueError("max_requests must be between 0 and 50")
    grouped: dict[str, list[LinkSignal]] = defaultdict(list)
    for signal in signals:
        if is_safe_fetch_candidate(signal.fetch_url):
            grouped[signal.identity_url].append(signal)
    ranked = sorted(grouped.items(), key=lambda pair: (
        -int(any(item.platform for item in pair[1])),
        -int(any(item.shared_by == "stephen-lucier" for item in pair[1])),
        -int(any(item.positive_reactors for item in pair[1])),
        -len(pair[1]), -max(item.created_at for item in pair[1]), pair[0],
    ))[:max_requests]
    return {"schema": 1, "kind": "private-link-metadata-fetch-queue", "max_requests": max_requests,
            "requests": [{"evidence_id": evidence_id(secret, "url", url), "url": items[0].fetch_url,
                          "platform": items[0].platform or "public-page",
                          "metadata_strategy": (
                              "oembed" if items[0].platform in {"youtube", "spotify"}
                              else "platform-public-metadata" if items[0].platform
                              else "opengraph-title"
                          )} for url, items in ranked]}


def sanitize_metadata_cache(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, Mapping):
        return {}
    source = value.get("metadata", value)
    if not isinstance(source, Mapping):
        return {}
    result: dict[str, dict[str, str]] = {}
    for key, raw in source.items():
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key) or not isinstance(raw, Mapping):
            continue
        clean: dict[str, str] = {}
        for field in _METADATA_FIELDS:
            item = raw.get(field)
            if isinstance(item, str):
                item = re.sub(r"[\x00-\x1f\x7f]+", " ", item)
                item = re.sub(r"<[^>]{0,200}>", " ", item)
                clean[field] = " ".join(item.split())[:500]
        result[key] = clean
    return result


def _metadata_topics(metadata: Mapping[str, str]) -> list[tuple[str, str]]:
    words = set(_WORD_RE.findall(" ".join(metadata.values()).casefold()))
    matched: list[tuple[str, str]] = []
    for category, topic, keywords in _TAXONOMY:
        if words & keywords:
            matched.append((category, topic))
    return matched


def build_evidence_map(chat: ResolvedChat, signals: Sequence[LinkSignal], secret: bytes) -> dict[str, Any]:
    rows = []
    for signal in signals:
        rows.append({"evidence_id": _link_id(secret, signal), "url": signal.identity_url,
                     "message_guid": signal.message_guid, "created_at": signal.created_at,
                     "shared_by": signal.shared_by, "positive_reactors": list(signal.positive_reactors),
                     "replied_by": list(signal.replied_by)})
    return {"schema": 1, "kind": "private-link-review-evidence-map", "chat_id": chat.chat_id,
            "chat_guid": chat.chat_guid, "approved_handle_ids": list(chat.approved_handle_ids), "signals": rows}


def build_review_manifest(chat: ResolvedChat, signals: Iterable[LinkSignal], *, secret: bytes,
                          metadata_cache: object | None = None) -> dict[str, Any]:
    """Derive aggregate actor topics from repeated or positively-engaged metadata."""
    items = list(signals)
    metadata = sanitize_metadata_cache(metadata_cache or {})
    by_url: dict[str, list[LinkSignal]] = defaultdict(list)
    for signal in items:
        by_url[signal.identity_url].append(signal)

    # First require repeated distinct links for a keyword topic, or one share
    # with an explicit positive reaction. Replies are intentionally neutral.
    topic_urls: dict[tuple[str, str], set[str]] = defaultdict(set)
    url_topics: dict[str, list[tuple[str, str]]] = {}
    for url, occurrences in by_url.items():
        eid = evidence_id(secret, "url", url)
        topics = _metadata_topics(metadata.get(eid, {}))
        url_topics[url] = topics
        for category_topic in topics:
            topic_urls[category_topic].add(url)

    actor_edges: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    actor_positive: Counter[tuple[str, str, str]] = Counter()
    months: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    excluded = Counter()
    for url, occurrences in by_url.items():
        topics = url_topics.get(url, [])
        for category, topic in topics:
            engaged = any(item.positive_reactors for item in occurrences)
            if len(topic_urls[(category, topic)]) < 2 and not engaged:
                excluded["insufficient_topic_evidence"] += 1
                continue
            eid = evidence_id(secret, "url", url)
            for item in occurrences:
                actors = {item.shared_by, *item.positive_reactors}
                month = datetime.fromtimestamp(item.created_at, timezone.utc).strftime("%Y-%m")
                for actor in actors:
                    key = (actor, category, topic)
                    actor_edges[key].add(eid)
                    months[key].add(month)
                    if actor in item.positive_reactors:
                        actor_positive[key] += 1

    candidates = []
    for (actor, category, topic), ids in sorted(actor_edges.items()):
        positive = actor_positive[(actor, category, topic)]
        if len(ids) < 2 and positive <= 0:
            excluded["insufficient_actor_evidence"] += 1
            continue
        candidates.append({"candidate_id": evidence_id(secret, "candidate", actor + "\0" + topic),
                           "subject": actor, "category": category, "topic": topic,
                           "distinct_evidence": len(ids), "positive_reactions": positive,
                           "month_buckets": sorted(months[(actor, category, topic)]),
                           "evidence_ids": sorted(ids)})
    manifest: dict[str, Any] = {"schema": 2, "kind": "imessage-link-interest-review",
        "apply_supported": False, "candidates": candidates,
        "counts": {"link_occurrences": len(items), "distinct_links": len(by_url),
                   "metadata_records": len(metadata), "candidate_topics": len(candidates),
                   "excluded": dict(sorted(excluded.items()))}}
    manifest["review_id"] = evidence_id(secret, "review", json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    return manifest


def fetch_public_metadata(*args: Any, **kwargs: Any) -> dict[str, str]:
    raise FetchError("direct network fetch disabled: export a bounded private queue to a trusted pinned transport")


def is_public_url(*args: Any, **kwargs: Any) -> bool:
    # Kept as a fail-closed compatibility surface. DNS preflight is not a safe
    # transport because it cannot prevent rebinding between lookup and connect.
    return False


__all__ = ["FetchError", "LINK_INTEREST_TAXONOMY", "LinkSignal", "build_evidence_map", "build_review_manifest", "canonicalize_url",
           "classify_url", "evidence_id", "extract_urls", "fetch_public_metadata", "is_public_url",
           "is_safe_fetch_candidate", "iter_link_signals", "sanitize_metadata_cache", "select_enrichment_queue",
           "verify_review_id"]
