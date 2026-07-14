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


@dataclass(frozen=True)
class BootstrapChunk:
    index: int
    rows: tuple[IMessageRow, ...]
    chunk_hash: str

    def prompt_rows(self) -> list[dict[str, object]]:
        return [
            {"source": row.source_key, "author": row.author, "created_at": row.created_at,
             "text": row.text if row.text is not None else "[NON_TEXT]"}
            for row in self.rows
        ]


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


def resolve_one_to_one_chat(con: sqlite3.Connection, approved_handles: Sequence[str]) -> ResolvedChat:
    approved = {_normalize_handle(item) for item in approved_handles if _normalize_handle(item)}
    if not approved:
        raise ValueError("at least one explicitly approved Stephen handle is required")
    rows = con.execute(
        """SELECT c.ROWID chat_id,c.guid chat_guid,h.ROWID handle_id,h.id handle,
                  count(DISTINCT chj2.handle_id) participants,
                  count(DISTINCT cmj.message_id) message_count
           FROM chat c JOIN chat_handle_join chj ON chj.chat_id=c.ROWID
           JOIN handle h ON h.ROWID=chj.handle_id
           JOIN chat_handle_join chj2 ON chj2.chat_id=c.ROWID
           LEFT JOIN chat_message_join cmj ON cmj.chat_id=c.ROWID
           GROUP BY c.ROWID,h.ROWID"""
    ).fetchall()
    matches = [row for row in rows if int(row["participants"]) == 1 and _normalize_handle(row["handle"]) in approved]
    if len(matches) != 1:
        candidates = sorted({(_normalize_handle(row["handle"]), int(row["message_count"])) for row in matches})
        raise ValueError(f"strict 1:1 resolution requires exactly one chat; matched={candidates}")
    row = matches[0]
    return ResolvedChat(int(row["chat_id"]), str(row["chat_guid"] or ""), int(row["handle_id"]),
                        _normalize_handle(row["handle"]), int(row["message_count"]))


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
        useful = [s for s in strings if not s.startswith(("__k", "NS"))]
        if useful:
            return max(useful, key=len)
    except Exception:
        pass
    # NSAttributedString keyed archives contain readable UTF-8 runs.  This is a
    # conservative recovery fallback; undecodable blobs are explicit non-text.
    runs = [part.decode("utf-8", "ignore").strip() for part in re.findall(rb"[\x20-\x7e\xc2-\xf4]{2,}", data)]
    runs = [r for r in runs if r and not r.startswith(("streamtyped", "NS", "__k"))]
    return max(runs, key=len) if runs else None


def _apple_timestamp(raw: object) -> float:
    value = float(raw or 0)
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
        # Tapbacks/reactions are not authored prose and are rejected explicitly.
        if int(row["associated_type"] or 0) != 0:
            continue
        text = str(row["text"]).strip() if row["text"] is not None else ""
        if not text:
            text = _decode_attributed_body(row["attributedBody"]) or ""
        yield IMessageRow(
            rowid=int(row["rowid"]), guid=str(row["guid"] or ""),
            created_at=_apple_timestamp(row["date"]),
            author="kosta-owner" if bool(row["is_from_me"]) else "stephen-lucier",
            text=text or None, non_text=not bool(text),
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
    identity = "\n".join(f"{r.rowid}\0{r.guid}\0{r.author}" for r in rows)
    return BootstrapChunk(index, tuple(rows), hashlib.sha256(identity.encode()).hexdigest())


def build_manifest(chat: ResolvedChat, chunks: Sequence[BootstrapChunk], *, source_path: str | Path) -> dict[str, Any]:
    rows = [row for chunk in chunks for row in chunk.rows]
    represented = sum(row.text is not None for row in rows)
    non_text = sum(row.non_text for row in rows)
    directions = {author: sum(row.author == author for row in rows) for author in sorted(CANONICAL_AUTHORS)}
    row_identity = "\n".join(f"{row.rowid}\0{row.guid}" for row in rows)
    manifest = {
        "schema": 1, "source_sha256": hashlib.sha256(str(Path(source_path).resolve()).encode()).hexdigest(),
        "chat_guid_sha256": hashlib.sha256(chat.chat_guid.encode()).hexdigest(),
        "handle_sha256": hashlib.sha256(chat.handle.encode()).hexdigest(),
        "selected": len(rows), "represented": represented, "explicit_non_text": non_text,
        "rejected": 0, "directions": directions, "chunks": [
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
    canonical = json.dumps(dict(item), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return f"imessage:{guid}:{author}:{digest}"


def validate_semantic_items(items: object, *, subject: str) -> list[dict[str, Any]]:
    if subject not in CANONICAL_AUTHORS or not isinstance(items, list):
        raise ValueError("invalid semantic subject or output")
    allowed = {"kind", "guid", "author", "predicate", "text", "topic", "signal_type", "valence",
               "confidence", "sensitive", "third_party", "audience", "guest_reviewed", "created_at"}
    result: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, Mapping) or not set(raw) <= allowed:
            raise ValueError("semantic item has invalid schema")
        item = dict(raw)
        if item.get("author") != subject:
            raise ValueError("cross-speaker semantic evidence rejected")
        if item.get("kind") not in {"fact", "interest"} or not str(item.get("guid") or "").strip():
            raise ValueError("semantic item lacks kind/guid")
        confidence = float(item.get("confidence", 0))
        if confidence < 0.7 or confidence > 1:
            raise ValueError("semantic confidence outside accepted range")
        if item["kind"] == "fact" and not str(item.get("text") or "").strip():
            raise ValueError("fact text is required")
        if item["kind"] == "interest" and not str(item.get("topic") or "").strip():
            raise ValueError("interest topic is required")
        item["source_id"] = stable_semantic_source_id(str(item["guid"]), subject, item)
        if item.get("sensitive") or item.get("third_party") or str(item.get("valence")) == "negative":
            # Negative/sensitive evidence is retained only as suppression context,
            # never promoted into proactive positive-interest candidates.
            item["suppressed"] = True
        result.append(item)
    return result


__all__ = ["BootstrapChunk", "CANONICAL_AUTHORS", "IMessageRow", "ResolvedChat", "build_manifest",
           "chunk_rows", "iter_chat_rows", "open_messages_readonly", "resolve_one_to_one_chat",
           "stable_semantic_source_id", "validate_semantic_items"]
