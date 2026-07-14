from __future__ import annotations
import json, sqlite3
from pathlib import Path
import pytest

from gateway.contact_memory.imessage_bootstrap import (
    authoritative_source_map, build_manifest, chunk_rows, iter_chat_rows, open_messages_readonly,
    resolve_one_to_one_chat, stable_semantic_source_id, validate_semantic_items,
)
from scripts.bootstrap_proactive_imessage import _operator_approval, main as bootstrap_main, run_semantic_workflow


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
    assert [r.author for r in rows]==['kosta-owner','stephen-lucier','stephen-lucier','stephen-lucier']
    manifest=build_manifest(chat,chunks,source_path=path)
    assert manifest['represented']+manifest['explicit_non_text']+manifest['rejected']==manifest['selected']==4
    assert manifest['rejection_reasons']=={'associated_message':1}
    assert manifest['directions']=={'kosta-owner':1,'stephen-lucier':2}
    assert 'likes cars' not in json.dumps(manifest)


def test_group_or_ambiguous_resolution_refused(tmp_path: Path):
    path=tmp_path/'chat.db'; db(path,group=True)
    with open_messages_readonly(path) as con:
        with pytest.raises(ValueError,match='exactly one'): resolve_one_to_one_chat(con,['+14015550100'])


def test_cross_speaker_and_sensitive_semantics(tmp_path: Path):
    from gateway.contact_memory.imessage_bootstrap import AuthoritativeSource
    sources={'g1': AuthoritativeSource('kosta-owner', __import__('hashlib').sha256(b'Kosta likes cars').hexdigest())}
    item={'kind':'fact','guid':'g1','author':'kosta-owner','text':'likes cars','predicate':'likes','confidence':.9}
    with pytest.raises(ValueError,match='cross-speaker'):
        validate_semantic_items([item],subject='stephen-lucier',sources=sources)
    sensitive={**item,'sensitive':True}
    parsed=validate_semantic_items([sensitive],subject='kosta-owner',sources=sources)
    assert parsed[0]['suppressed'] is True
    assert parsed[0]['source_id']==stable_semantic_source_id('g1','kosta-owner',sensitive)
    assert parsed[0]['source_id']==stable_semantic_source_id('g1','kosta-owner',sensitive)


def test_semantics_require_authoritative_source_author_and_hash():
    from gateway.contact_memory.imessage_bootstrap import AuthoritativeSource
    source = AuthoritativeSource('stephen-lucier', __import__('hashlib').sha256(b'evidence').hexdigest())
    forged={'kind':'fact','guid':'g2','author':'kosta-owner','text':'forged','confidence':.9}
    with pytest.raises(ValueError,match='cross-speaker'):
        validate_semantic_items([forged],subject='kosta-owner',sources={'g2':source})
    correct={**forged,'author':'stephen-lucier','source_content_hash':'bad'}
    with pytest.raises(ValueError,match='content hash'):
        validate_semantic_items([correct],subject='stephen-lucier',sources={'g2':source})
    with pytest.raises(ValueError,match='unknown source'):
        validate_semantic_items([{**correct,'guid':'invented','source_content_hash':''}],subject='stephen-lucier',sources={'g2':source})


def test_prompt_only_stages_private_sender_attributed_packets(tmp_path: Path):
    source=tmp_path/'chat.db'; staging=tmp_path/'private'; db(source)
    args=['--source-person','Stephen Lucier','--chat-db',str(source),'--handle','+14015550100',
          '--chunk-size','2','--staging-dir',str(staging),'--prompt-only']
    assert bootstrap_main(args)==0
    index=json.loads(next(staging.glob('*/packet-index.json')).read_text())
    packet=json.loads(Path(index[0]['path']).read_text())
    assert packet['provider']=='openai-codex' and packet['reasoning_effort']=='medium'
    assert [row['author'] for row in packet['rows']]==['kosta-owner','stephen-lucier']
    assert packet['rows'][0]['text']=='Kosta likes cars'
    assert len(packet['rows'][0]['source_content_hash'])==64
    assert not (staging.stat().st_mode & 0o077)
    assert bootstrap_main([*args,'--resume'])==0


def test_extraction_merge_coverage_and_review_execute_with_canonical_ids(tmp_path: Path):
    source=tmp_path/'chat.db'; db(source)
    with open_messages_readonly(source) as con:
        chat=resolve_one_to_one_chat(con,['+14015550100'])
        chunks=list(chunk_rows(iter_chat_rows(con,chat),chunk_size=2))
    sources=authoritative_source_map(chunks)
    manifest=build_manifest(chat,chunks,source_path=source)
    calls=[]

    def model(prompt):
        calls.append(prompt)
        if prompt.startswith('Untrusted iMessage rows'):
            rows=json.loads(prompt.split('\n',1)[1])
            return {'items': [
                {'kind':'fact','source_key':row['source'],'source_content_hash':sources[row['source']].content_hash,
                 'author':row['author'],'text':row['text'],'predicate':'context','confidence':.9}
                for row in rows if row['text']!='[NON_TEXT]'
            ]}
        if prompt.startswith('Merge these'):
            candidates=json.loads(prompt.split('\n',1)[1])
            return {'dossiers':{
                'kosta-owner':[item for item in candidates if item['author']=='kosta-owner'],
                'stephen-lucier':[item for item in candidates if item['author']=='stephen-lucier'],
            },'coverage':{'accounted_source_ids':[item['source_id'] for item in candidates]}}
        dossiers=json.loads(prompt.split('\n',1)[1])
        ids=[item['source_id'] for items in dossiers.values() for item in items]
        return {'accepted_source_ids':ids,'rejected_source_ids':[]}

    review=run_semantic_workflow(chunks,sources,manifest,tmp_path/'private',call_model=model)
    value=json.loads(review.read_text())
    assert value['coverage']=={'extracted':3,'merged':3,'reviewed':3}
    assert {item['author'] for items in value['dossiers'].values() for item in items}=={'kosta-owner','stephen-lucier'}
    assert len(calls)==4
    assert not (review.stat().st_mode & 0o077)


def test_semantic_validator_rejects_forged_derived_source_id():
    from gateway.contact_memory.imessage_bootstrap import AuthoritativeSource
    source=AuthoritativeSource('kosta-owner',__import__('hashlib').sha256(b'evidence').hexdigest())
    item={'kind':'fact','source_key':'g1','source_content_hash':source.content_hash,'source_id':'forged',
          'author':'kosta-owner','text':'evidence','predicate':'context','confidence':.9}
    with pytest.raises(ValueError,match='source ID'):
        validate_semantic_items([item],subject='kosta-owner',sources={'g1':source})


def test_guest_visibility_requires_operator_file_bound_to_review_bytes(tmp_path: Path):
    import hashlib
    review=tmp_path/'review.json'; review.write_text('{"dossiers":{}}')
    manifest={'rowset_sha256':'rows'}
    approval=tmp_path/'approval.json'
    approval.write_text(json.dumps({
        'operator_approved':True,'rowset_sha256':'rows',
        'review_sha256':hashlib.sha256(review.read_bytes()).hexdigest(),
        'guest_visible_source_ids':['approved-source'],
    }))
    assert _operator_approval(approval,review,manifest)=={'approved-source'}
    approval.chmod(0o620)
    with pytest.raises(ValueError,match='owned by the operator'):
        _operator_approval(approval,review,manifest)
    approval.chmod(0o600)
    review.write_text('{"dossiers":{"changed":true}}')
    with pytest.raises(ValueError,match='reviewed dossiers'):
        _operator_approval(approval,review,manifest)
