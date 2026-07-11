"""Global canonical-entity registry. It intentionally stores no fact text."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
from typing import Iterable

from .schema import REGISTRY_SCHEMA_SQL, SCHEMA_VERSION


class EntityRegistry:
    def __init__(self, root: str | Path):
        self.path = Path(root) / "registry.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as con:
            con.executescript(REGISTRY_SCHEMA_SQL)
            con.execute(
                "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
        try:
            self.path.parent.chmod(0o700)
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA secure_delete=ON")
        return con

    def upsert(self, entity_id: str, entity_type: str, canonical_label: str, aliases: Iterable[str] = ()) -> None:
        if entity_type not in {"person", "place", "organization", "thing", "event"}:
            raise ValueError("invalid entity type")
        if not entity_id.strip() or not canonical_label.strip():
            raise ValueError("entity_id and canonical_label are required")
        now = time.time()
        normalized_aliases = sorted({str(alias).strip() for alias in aliases if str(alias).strip()})
        with self._connect() as con:
            con.execute(
                """INSERT INTO entity(entity_id,entity_type,canonical_label,aliases_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(entity_id) DO UPDATE SET entity_type=excluded.entity_type,
                   canonical_label=excluded.canonical_label,aliases_json=excluded.aliases_json,updated_at=excluded.updated_at""",
                (entity_id, entity_type, canonical_label, json.dumps(normalized_aliases, ensure_ascii=False), now, now),
            )

    def get(self, entity_id: str) -> dict[str, object] | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM entity WHERE entity_id=?", (entity_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["aliases"] = json.loads(result.pop("aliases_json"))
        return result
