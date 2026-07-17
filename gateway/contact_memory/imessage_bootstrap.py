"""Read-only, sender-attributed iMessage bootstrap primitives.

Raw message text is yielded only to the caller's protected staging process.  The
manifest contains hashes and counts, never transcript text.  Resolution is
strictly limited to one existing 1:1 chat with one explicitly approved handle.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import plistlib
import re
import sqlite3
import unicodedata
from typing import Any, Iterable, Iterator, Mapping, Sequence

CANONICAL_AUTHORS = frozenset({"kosta-owner", "stephen-lucier"})
APPLE_EPOCH = 978307200.0


@dataclass(frozen=True)
class IMessageRow:
    rowid: int
    guid: str
    created_at: float
    author: str
    text: str | None
    non_text: bool
    rejection_reason: str | None = None

    @property
    def source_key(self) -> str:
        return self.guid or f"rowid:{self.rowid}"


@dataclass(frozen=True)
class ResolvedChat:
    chat_id: int
    chat_guid: str
    handle_id: int
    handle: str
    message_count: int
    # Every incoming row must carry one of these handle ids.  Keeping the
    # approved database ids on the resolution result prevents callers from
    # inferring identity merely from ``is_from_me == 0``.
    approved_handle_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class BootstrapChunk:
    index: int
    rows: tuple[IMessageRow, ...]
    chunk_hash: str

    def prompt_rows(self) -> list[dict[str, object]]:
        return [
            {"source": row.source_key, "author": row.author, "created_at": row.created_at,
             "source_content_hash": content_hash(row),
             "text": row.text if row.text is not None else "[NON_TEXT]"}
            for row in self.rows if row.rejection_reason is None
        ]


@dataclass(frozen=True)
class AuthoritativeSource:
    canonical_author: str
    content_hash: str
    canonical_text: str


def _normalize_evidence(value: object) -> str:
    """Normalize only representation differences, never paraphrase semantics."""
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _normalized_contains(haystack: object, needle: object) -> bool:
    normalized_haystack = _normalize_evidence(haystack)
    normalized_needle = _normalize_evidence(needle)
    if not normalized_needle:
        return False
    return re.search(r"(?<!\w)" + re.escape(normalized_needle) + r"(?!\w)", normalized_haystack) is not None


def _normalize_handle(value: str) -> str:
    raw = str(value).strip().lower()
    if "@" in raw:
        return raw
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits if raw.startswith("+") and digits else raw


def open_messages_readonly(path: str | Path) -> sqlite3.Connection:
    source = Path(path).expanduser().resolve(strict=True)
    uri = f"file:{source.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def resolve_one_to_one_chat(
    con: sqlite3.Connection,
    approved_handles: Sequence[str],
    *,
    chat_id: int | None = None,
) -> ResolvedChat:
    approved = {_normalize_handle(item) for item in approved_handles if _normalize_handle(item)}
    if not approved:
        raise ValueError("at least one explicitly approved Stephen handle is required")
    chat_columns = {str(row[1]) for row in con.execute("PRAGMA table_info(chat)")}
    style = "c.style" if "style" in chat_columns else "NULL"
    display_name = "c.display_name" if "display_name" in chat_columns else "NULL"
    group_id = "c.group_id" if "group_id" in chat_columns else "NULL"
    rows = con.execute(
        f"""SELECT c.ROWID chat_id,c.guid chat_guid,h.ROWID handle_id,h.id handle,
                  count(DISTINCT chj2.handle_id) participants,
                  count(DISTINCT cmj.message_id) message_count,
                  {style} style,{display_name} display_name,{group_id} group_id
           FROM chat c JOIN chat_handle_join chj ON chj.chat_id=c.ROWID
           JOIN handle h ON h.ROWID=chj.handle_id
           JOIN chat_handle_join chj2 ON chj2.chat_id=c.ROWID
           LEFT JOIN chat_message_join cmj ON cmj.chat_id=c.ROWID
           GROUP BY c.ROWID,h.ROWID"""
    ).fetchall()
    def direct(row: sqlite3.Row) -> bool:
        guid = str(row["chat_guid"] or "")
        # Apple's ``;-;`` GUID marker plus exactly one participant is the
        # authoritative direct-chat shape. Modern Messages may still populate
        # ``group_id`` and use style 45 for a direct conversation, so those
        # overloaded fields cannot be privacy boundaries. Explicit ``;+;``
        # group GUIDs and named conversations remain rejected.
        if ";-;" not in guid or ";+;" in guid or row["display_name"]:
            return False
        if row["style"] is not None and int(row["style"]) not in {0, 43, 45}:
            return False
        return int(row["participants"]) == 1

    matches = [row for row in rows if direct(row) and _normalize_handle(row["handle"]) in approved]
    if chat_id is not None:
        matches = [row for row in matches if int(row["chat_id"]) == int(chat_id)]
    if len(matches) != 1:
        raise ValueError(
            "strict 1:1 resolution requires exactly one chat (use chat_id to disambiguate)"
        )
    row = matches[0]
    # Approval is scoped to the selected chat participant, never to aliases
    # attached only to another chat.
    approved_ids = (int(row["handle_id"]),)
    return ResolvedChat(int(row["chat_id"]), str(row["chat_guid"] or ""), int(row["handle_id"]),
                        _normalize_handle(row["handle"]), int(row["message_count"]), approved_ids)


def _decode_attributed_body(blob: object) -> str | None:
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        return None
    data = bytes(blob)
    try:
        value = plistlib.loads(data)
        strings: list[str] = []
        def visit(item: object) -> None:
            if isinstance(item, str) and item.strip():
                strings.append(item.strip())
            elif isinstance(item, Mapping):
                for child in item.values(): visit(child)
            elif isinstance(item, (list, tuple)):
                for child in item: visit(child)
        visit(value)
        useful: list[str] = [s for s in strings if not s.startswith(("__k", "NS"))]
        if useful:
            useful.sort(key=lambda value: len(value))
            return useful[-1]
    except Exception:
        pass
    # NSAttributedString keyed archives contain readable UTF-8 runs.  This is a
    # conservative recovery fallback; undecodable blobs are explicit non-text.
    runs: list[str] = [part.decode("utf-8", "ignore").strip() for part in re.findall(rb"[\x20-\x7e\xc2-\xf4]{2,}", data)]
    runs = [r for r in runs if r and not r.startswith(("streamtyped", "NS", "__k"))]
    runs.sort(key=lambda value: len(value))
    return runs[-1] if runs else None


def _apple_timestamp(raw: object) -> float:
    value = float(raw) if isinstance(raw, (int, float, str)) and raw else 0.0
    # Modern chat.db stores nanoseconds since 2001; older DBs store seconds.
    if abs(value) > 10_000_000_000:
        value /= 1_000_000_000.0
    return APPLE_EPOCH + value


def iter_chat_rows(con: sqlite3.Connection, chat: ResolvedChat, *, limit: int = 0) -> Iterator[IMessageRow]:
    sql = """SELECT m.ROWID rowid,m.guid,m.date,m.is_from_me,m.text,m.attributedBody,
                    COALESCE(m.associated_message_type,0) associated_type
             FROM message m JOIN chat_message_join cmj ON cmj.message_id=m.ROWID
             WHERE cmj.chat_id=? ORDER BY m.date,m.ROWID"""
    params: list[object] = [chat.chat_id]
    if limit > 0:
        sql += " LIMIT ?"
        params.append(int(limit))
    for row in con.execute(sql, params):
        text = str(row["text"]).strip() if row["text"] is not None else ""
        if not text:
            text = _decode_attributed_body(row["attributedBody"]) or ""
        rejection_reason = (
            "associated_message" if int(row["associated_type"] or 0) != 0 else None
        )
        yield IMessageRow(
            rowid=int(row["rowid"]), guid=str(row["guid"] or ""),
            created_at=_apple_timestamp(row["date"]),
            author="kosta-owner" if bool(row["is_from_me"]) else "stephen-lucier",
            text=text or None, non_text=not bool(text),
            rejection_reason=rejection_reason,
        )


def chunk_rows(rows: Iterable[IMessageRow], *, chunk_size: int = 200) -> Iterator[BootstrapChunk]:
    if not 1 <= int(chunk_size) <= 1000:
        raise ValueError("chunk_size must be between 1 and 1000")
    batch: list[IMessageRow] = []
    index = 0
    for row in rows:
        batch.append(row)
        if len(batch) >= chunk_size:
            yield _make_chunk(index, batch); index += 1; batch = []
    if batch:
        yield _make_chunk(index, batch)


def _make_chunk(index: int, rows: Sequence[IMessageRow]) -> BootstrapChunk:
    identity = "\n".join(
        f"{r.rowid}\0{r.guid}\0{r.author}\0{content_hash(r)}\0{r.rejection_reason or ''}"
        for r in rows
    )
    return BootstrapChunk(index, tuple(rows), hashlib.sha256(identity.encode()).hexdigest())


def content_hash(row: IMessageRow) -> str:
    return hashlib.sha256((row.text or "").encode("utf-8")).hexdigest()


def authoritative_source_map(
    chunks: Sequence[BootstrapChunk],
) -> dict[str, AuthoritativeSource]:
    result: dict[str, AuthoritativeSource] = {}
    for chunk in chunks:
        for row in chunk.rows:
            if row.rejection_reason is not None:
                continue
            source = AuthoritativeSource(row.author, content_hash(row), row.text or "")
            previous = result.setdefault(row.source_key, source)
            if previous != source:
                raise ValueError(f"conflicting source identity: {row.source_key}")
    return result


def build_manifest(chat: ResolvedChat, chunks: Sequence[BootstrapChunk], *, source_path: str | Path) -> dict[str, Any]:
    rows = [row for chunk in chunks for row in chunk.rows]
    accepted = [row for row in rows if row.rejection_reason is None]
    represented = sum(row.text is not None for row in accepted)
    non_text = sum(row.non_text for row in accepted)
    rejected = len(rows) - len(accepted)
    directions = {author: sum(row.author == author for row in accepted) for author in sorted(CANONICAL_AUTHORS)}
    row_identity = "\n".join(f"{row.rowid}\0{row.guid}" for row in rows)
    manifest = {
        "schema": 1, "source_sha256": hashlib.sha256(str(Path(source_path).resolve()).encode()).hexdigest(),
        "chat_guid_sha256": hashlib.sha256(chat.chat_guid.encode()).hexdigest(),
        "handle_sha256": hashlib.sha256(chat.handle.encode()).hexdigest(),
        "selected": len(rows), "represented": represented, "explicit_non_text": non_text,
        "rejected": rejected,
        "rejection_reasons": {
            reason: sum(row.rejection_reason == reason for row in rows)
            for reason in sorted({row.rejection_reason for row in rows if row.rejection_reason})
        },
        "directions": directions, "chunks": [
            {"index": c.index, "rows": len(c.rows), "chunk_hash": c.chunk_hash} for c in chunks
        ],
        "rowset_sha256": hashlib.sha256(row_identity.encode()).hexdigest(),
        "date_min": min((row.created_at for row in rows), default=None),
        "date_max": max((row.created_at for row in rows), default=None),
    }
    if represented + non_text + manifest["rejected"] != manifest["selected"]:
        raise AssertionError("no-drop accounting failed")
    return manifest


def stable_semantic_source_id(guid: str, author: str, item: Mapping[str, object]) -> str:
    if author not in CANONICAL_AUTHORS:
        raise ValueError("unknown canonical author")
    canonical_item = {
        key: value for key, value in item.items()
        if key not in {"source_id", "source_key", "source_content_hash", "suppressed"}
    }
    canonical = json.dumps(canonical_item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return f"imessage:{guid}:{author}:{digest}"


def validate_semantic_items(
    items: object,
    *,
    subject: str,
    sources: Mapping[str, AuthoritativeSource],
) -> list[dict[str, Any]]:
    if subject not in CANONICAL_AUTHORS or not isinstance(items, list):
        raise ValueError("invalid semantic subject or output")
    allowed = {"kind", "guid", "source_key", "source_content_hash", "author", "predicate", "text",
               "topic", "signal_type", "valence", "confidence", "sensitive", "third_party",
               "audience", "created_at", "source_id", "suppressed", "evidence_quote", "evidence_start", "evidence_end"}
    result: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, Mapping) or not set(raw) <= allowed:
            raise ValueError("semantic item has invalid schema")
        item = dict(raw)
        source_key = str(item.get("source_key") or item.get("guid") or "").strip()
        source = sources.get(source_key)
        if source is None:
            raise ValueError("semantic evidence references an unknown source row")
        if item.get("author") != subject or source.canonical_author != subject:
            raise ValueError("cross-speaker semantic evidence rejected")
        supplied_hash = str(item.get("source_content_hash") or "").strip()
        if not supplied_hash or supplied_hash != source.content_hash:
            raise ValueError("semantic evidence content hash mismatch")
        quote = item.get("evidence_quote")
        start, end = item.get("evidence_start"), item.get("evidence_end")
        if not isinstance(quote, str) or not quote or len(quote) > 500:
            raise ValueError("semantic evidence requires a bounded verbatim quote")
        if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool):
            raise ValueError("semantic evidence requires integer quote bounds")
        if start < 0 or end <= start or end > len(source.canonical_text):
            raise ValueError("semantic evidence quote bounds are invalid")
        if source.canonical_text[start:end] != quote:
            raise ValueError("semantic evidence quote is not verbatim canonical source text")
        if item.get("kind") not in {"fact", "interest"}:
            raise ValueError("semantic item lacks kind/guid")
        item["guid"] = source_key
        item["source_key"] = source_key
        item["source_content_hash"] = source.content_hash
        confidence = float(item.get("confidence", 0))
        if confidence < 0.7 or confidence > 1:
            raise ValueError("semantic confidence outside accepted range")
        if item["kind"] == "fact" and not str(item.get("text") or "").strip():
            raise ValueError("fact text is required")
        if item["kind"] == "interest" and not str(item.get("topic") or "").strip():
            raise ValueError("interest topic is required")
        claim = item.get("text") if item["kind"] == "fact" else item.get("topic")
        if not _normalized_contains(quote, claim):
            raise ValueError("semantic abstraction needs operator review: claim is not deterministically grounded")
        canonical_source_id = stable_semantic_source_id(str(item["guid"]), subject, item)
        supplied_source_id = str(item.get("source_id") or "").strip()
        if supplied_source_id and supplied_source_id != canonical_source_id:
            raise ValueError("semantic evidence source ID mismatch")
        item["source_id"] = canonical_source_id
        if item.get("sensitive") or item.get("third_party") or str(item.get("valence")) == "negative":
            # Negative/sensitive evidence is retained only as suppression context,
            # never promoted into proactive positive-interest candidates.
            item["suppressed"] = True
        result.append(item)
    return result


__all__ = ["AuthoritativeSource", "BootstrapChunk", "CANONICAL_AUTHORS", "IMessageRow", "ResolvedChat",
           "authoritative_source_map", "build_manifest", "content_hash",
           "chunk_rows", "iter_chat_rows", "open_messages_readonly", "resolve_one_to_one_chat",
           "stable_semantic_source_id", "validate_semantic_items"]
