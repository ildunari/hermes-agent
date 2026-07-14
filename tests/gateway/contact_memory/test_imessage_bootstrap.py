from __future__ import annotations
import json, sqlite3
from pathlib import Path
import pytest

from gateway.contact_memory.imessage_bootstrap import (
    build_manifest, chunk_rows, iter_chat_rows, open_messages_readonly,
    resolve_one_to_one_chat, stable_semantic_source_id, validate_semantic_items,
)


def db(path: Path, *, group=False):
    con=sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE chat(ROWID INTEGER PRIMARY KEY,guid TEXT);
    CREATE TABLE handle(ROWID INTEGER PRIMARY KEY,id TEXT);
    CREATE TABLE chat_handle_join(chat_id INTEGER,handle_id INTEGER);
    CREATE TABLE message(ROWID INTEGER PRIMARY KEY,guid TEXT,date INTEGER,is_from_me INTEGER,text TEXT,attributedBody BLOB,associated_message_type INTEGER DEFAULT 0);
    CREATE TABLE chat_message_join(chat_id INTEGER,message_id INTEGER);
    """)
    con.execute("INSERT INTO chat VALUES(1,'iMessage;-;approved')")
    con.execute("INSERT INTO handle VALUES(1,'+14015550100')")
    con.execute("INSERT INTO chat_handle_join VALUES(1,1)")
    if group:
        con.execute("INSERT INTO handle VALUES(2,'+14015550200')"); con.execute("INSERT INTO chat_handle_join VALUES(1,2)")
    rows=[(1,'g1',1_000_000_000,1,'Kosta likes cars',None,0),(2,'g2',2_000_000_000,0,'Stephen likes music',None,0),(3,'g3',3_000_000_000,0,None,b'not decodable',0),(4,'tap',4_000_000_000,0,'liked',None,2000)]
    con.executemany("INSERT INTO message VALUES(?,?,?,?,?,?,?)",rows)
    con.executemany("INSERT INTO chat_message_join VALUES(1,?)",[(1,),(2,),(3,),(4,)])
    con.commit(); con.close()


def test_readonly_strict_attribution_no_drop_and_manifest_has_no_text(tmp_path: Path):
    path=tmp_path/'chat.db'; db(path)
    with open_messages_readonly(path) as con:
        chat=resolve_one_to_one_chat(con,['(401) 555-0100'])
        chunks=list(chunk_rows(iter_chat_rows(con,chat,limit=0),chunk_size=2))
        with pytest.raises(sqlite3.OperationalError): con.execute("DELETE FROM message")
    rows=[r for c in chunks for r in c.rows]
    assert [r.author for r in rows]==['kosta-owner','stephen-lucier','stephen-lucier']
    manifest=build_manifest(chat,chunks,source_path=path)
    assert manifest['represented']+manifest['explicit_non_text']+manifest['rejected']==manifest['selected']==3
    assert manifest['directions']=={'kosta-owner':1,'stephen-lucier':2}
    assert 'likes cars' not in json.dumps(manifest)


def test_group_or_ambiguous_resolution_refused(tmp_path: Path):
    path=tmp_path/'chat.db'; db(path,group=True)
    with open_messages_readonly(path) as con:
        with pytest.raises(ValueError,match='exactly one'): resolve_one_to_one_chat(con,['+14015550100'])


def test_cross_speaker_and_sensitive_semantics(tmp_path: Path):
    item={'kind':'fact','guid':'g1','author':'kosta-owner','text':'likes cars','predicate':'likes','confidence':.9}
    with pytest.raises(ValueError,match='cross-speaker'):
        validate_semantic_items([item],subject='stephen-lucier')
    sensitive={**item,'sensitive':True}
    parsed=validate_semantic_items([sensitive],subject='kosta-owner')
    assert parsed[0]['suppressed'] is True
    assert parsed[0]['source_id']==stable_semantic_source_id('g1','kosta-owner',sensitive)
    assert parsed[0]['source_id']==stable_semantic_source_id('g1','kosta-owner',sensitive)
