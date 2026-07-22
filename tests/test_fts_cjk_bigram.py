"""Focused lifecycle coverage for the compact v23 trigram search index."""

import sqlite3

import pytest

from hermes_state import FTS_STORAGE_VERSION, SCHEMA_VERSION, SessionDB


def _sqlite_supports_trigram() -> bool:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE t USING fts5(content, tokenize='trigram')"
        )
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


pytestmark = pytest.mark.skipif(
    not _sqlite_supports_trigram(),
    reason="SQLite FTS5 trigram tokenizer is not available",
)


def test_schema23_marker1_missing_trigram_rebuilds_on_open(tmp_path):
    """A compact marker must not hide an empty recreated trigram index."""
    db_path = tmp_path / "state.db"
    seeded = SessionDB(db_path=db_path)
    try:
        seeded.create_session(session_id="s1", source="cli")
        seeded.append_message(
            "s1", role="user", content="missing-table needle 大别山项目"
        )
        # Reproduce the exact live state: schema v23 plus the independent
        # compact-layout marker already stamped at version 1.
        seeded.set_meta("fts_storage_version", str(FTS_STORAGE_VERSION))
        assert seeded.get_meta("fts_storage_version") == str(FTS_STORAGE_VERSION)
        assert seeded._conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0] == SCHEMA_VERSION
    finally:
        seeded.close()

    # External sync triggers are attached to messages, so they survive this
    # table loss. That is the live failure shape: trigger count still looked
    # complete and the recreated virtual table was previously left empty.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TABLE messages_fts_trigram")
        surviving = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' "
            "AND name LIKE 'messages_fts_trigram_%'"
        ).fetchone()[0]
        assert surviving == 3
        conn.commit()
    finally:
        conn.close()

    healed = SessionDB(db_path=db_path)
    try:
        assert healed.get_meta("fts_storage_version") == str(FTS_STORAGE_VERSION)
        assert healed.fts_optimize_available() is False
        assert healed._trigram_available is True
        assert healed._conn.execute(
            "SELECT COUNT(*) FROM messages_fts_trigram"
        ).fetchone()[0] == 1
        assert healed._conn.execute(
            "SELECT COUNT(*) FROM messages_fts_trigram "
            "WHERE messages_fts_trigram MATCH ?",
            ('"\u5927\u522b\u5c71\u9879\u76ee"',),
        ).fetchone()[0] == 1
        healed._conn.execute(
            "INSERT INTO messages_fts_trigram(messages_fts_trigram, rank) "
            "VALUES('integrity-check', 1)"
        )
    finally:
        healed.close()
