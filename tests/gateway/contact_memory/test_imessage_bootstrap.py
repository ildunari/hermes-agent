from __future__ import annotations
import json, sqlite3
from pathlib import Path
import threading, time
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
    digest=__import__('hashlib').sha256(b'Kosta likes cars').hexdigest()
    sources={'g1': AuthoritativeSource('kosta-owner', digest, 'Kosta likes cars')}
    item={'kind':'fact','guid':'g1','source_content_hash':digest,'author':'kosta-owner','text':'likes cars',
          'predicate':'likes','confidence':.9,'evidence_quote':'Kosta likes cars','evidence_start':0,'evidence_end':16}
    with pytest.raises(ValueError,match='cross-speaker'):
        validate_semantic_items([item],subject='stephen-lucier',sources=sources)
    sensitive={**item,'sensitive':True}
    parsed=validate_semantic_items([sensitive],subject='kosta-owner',sources=sources)
    assert parsed[0]['suppressed'] is True
    assert parsed[0]['source_id']==stable_semantic_source_id('g1','kosta-owner',sensitive)
    assert parsed[0]['source_id']==stable_semantic_source_id('g1','kosta-owner',sensitive)


def test_semantics_require_authoritative_source_author_and_hash():
    from gateway.contact_memory.imessage_bootstrap import AuthoritativeSource
    source = AuthoritativeSource('stephen-lucier', __import__('hashlib').sha256(b'evidence').hexdigest(), 'evidence')
    forged={'kind':'fact','guid':'g2','author':'kosta-owner','text':'forged','confidence':.9}
    with pytest.raises(ValueError,match='cross-speaker'):
        validate_semantic_items([forged],subject='kosta-owner',sources={'g2':source})
    correct={**forged,'author':'stephen-lucier','source_content_hash':'bad',
             'evidence_quote':'evidence','evidence_start':0,'evidence_end':8}
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
                 'author':row['author'],'text':row['text'],'predicate':'context','confidence':.9,
                 'evidence_quote':row['text'],'evidence_start':0,'evidence_end':len(row['text'])}
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
    source=AuthoritativeSource('kosta-owner',__import__('hashlib').sha256(b'evidence').hexdigest(),'evidence')
    item={'kind':'fact','source_key':'g1','source_content_hash':source.content_hash,'source_id':'forged',
          'author':'kosta-owner','text':'evidence','predicate':'context','confidence':.9,
          'evidence_quote':'evidence','evidence_start':0,'evidence_end':8}
    with pytest.raises(ValueError,match='source ID'):
        validate_semantic_items([item],subject='kosta-owner',sources={'g1':source})


def test_same_speaker_source_cannot_launder_unsupported_abstraction():
    import hashlib
    from gateway.contact_memory.imessage_bootstrap import AuthoritativeSource
    text='Stephen likes jazz'
    source=AuthoritativeSource('stephen-lucier',hashlib.sha256(text.encode()).hexdigest(),text)
    forged={'kind':'fact','source_key':'g2','source_content_hash':source.content_hash,
            'author':'stephen-lucier','text':'Kosta private fact','predicate':'context','confidence':.9,
            'evidence_quote':text,'evidence_start':0,'evidence_end':len(text)}
    with pytest.raises(ValueError,match='needs operator review'):
        validate_semantic_items([forged],subject='stephen-lucier',sources={'g2':source})


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


def semantic_fixture(tmp_path: Path, *, chunk_size=1, count=None):
    source=tmp_path/'chat.db'; db(source)
    with open_messages_readonly(source) as con:
        chat=resolve_one_to_one_chat(con,['(401) 555-0100'])
        chunks=list(chunk_rows(iter_chat_rows(con,chat),chunk_size=chunk_size))
    if count is not None:
        chunks=chunks[:count]
    return chunks, authoritative_source_map(chunks), build_manifest(chat,chunks,source_path=source)


def valid_items_from_prompt(prompt, sources):
    payload=json.loads(prompt.split('\n',1)[1])
    rows=payload['canonical_rows'] if isinstance(payload,dict) else payload
    return {'items':[
        {'kind':'fact','source_key':row['source'],'source_content_hash':sources[row['source']].content_hash,
         'author':row['author'],'text':row['text'],'predicate':'context','confidence':.9,
         'evidence_quote':row['text'],'evidence_start':0,'evidence_end':len(row['text'])}
        for row in rows if row['text']!='[NON_TEXT]'
    ]}


def merge_or_review(prompt):
    if prompt.startswith('Merge these'):
        candidates=json.loads(prompt.split('\n',1)[1])
        return {'dossiers':{
            'kosta-owner':[item for item in candidates if item['author']=='kosta-owner'],
            'stephen-lucier':[item for item in candidates if item['author']=='stephen-lucier'],
        },'coverage':{'accounted_source_ids':[item['source_id'] for item in candidates]}}
    dossiers=json.loads(prompt.split('\n',1)[1])
    ids=[item['source_id'] for items in dossiers.values() for item in items]
    return {'accepted_source_ids':ids,'rejected_source_ids':[]}


def test_malformed_chunk_is_repaired_without_replaying_bad_output(tmp_path: Path):
    chunks,sources,manifest=semantic_fixture(tmp_path,chunk_size=10)
    extraction_prompts=[]

    def model(prompt):
        if prompt.startswith('Untrusted iMessage rows'):
            extraction_prompts.append(prompt)
            if len(extraction_prompts)==1:
                row=json.loads(prompt.split('\n',1)[1])[0]
                return {'items':[{'kind':'fact','source_key':row['source'],'author':row['author'],
                                  'text':'DO_NOT_REPLAY','confidence':.9}]}
            return valid_items_from_prompt(prompt,sources)
        return merge_or_review(prompt)

    review=run_semantic_workflow(chunks,sources,manifest,tmp_path/'private',call_model=model)
    assert review.is_file()
    assert len(extraction_prompts)==2
    assert 'DO_NOT_REPLAY' not in extraction_prompts[1]
    repair=json.loads(extraction_prompts[1].split('\n',1)[1])
    assert 'schema_error' in repair and repair['canonical_rows']==chunks[0].prompt_rows()
    attempts=sorted((review.parent/'attempts'/'chunk-000000').glob('*.json'))
    assert [json.loads(path.read_text())['status'] for path in attempts]==['error','validated']


def test_permanent_malformed_fails_all_chunks_with_resumable_manifest(tmp_path: Path):
    chunks,sources,manifest=semantic_fixture(tmp_path,count=2)
    extraction_calls=0
    merge_called=False
    lock=threading.Lock()

    def model(prompt):
        nonlocal extraction_calls,merge_called
        if prompt.startswith('Untrusted iMessage rows'):
            with lock: extraction_calls+=1
            return {'items':[{'author':'not-canonical'}]}
        merge_called=True
        return {}

    with pytest.raises(RuntimeError,match='resumable failure manifest'):
        run_semantic_workflow(chunks,sources,manifest,tmp_path/'private',call_model=model)
    failure=json.loads((tmp_path/'private'/manifest['rowset_sha256']/'semantic-failure-manifest.json').read_text())
    assert extraction_calls==len(chunks)*3
    assert merge_called is False
    assert failure['resumable'] is True
    assert [item['index'] for item in failure['failed_chunks']]==[0,1]
    assert not (tmp_path/'private'/manifest['rowset_sha256']/'review-manifest.json').exists()


def test_resume_reuses_valid_chunk_checkpoint_and_finishes_failed_chunk(tmp_path: Path):
    chunks,sources,manifest=semantic_fixture(tmp_path,count=2)

    def first_model(prompt):
        if prompt.startswith('Untrusted iMessage rows'):
            payload=json.loads(prompt.split('\n',1)[1])
            rows=payload['canonical_rows'] if isinstance(payload,dict) else payload
            if rows[0]['source']=='g2':
                return {'items':[{'author':'bad'}]}
            return valid_items_from_prompt(prompt,sources)
        pytest.fail('merge/review must not run after a partial extraction failure')

    staging=tmp_path/'private'
    with pytest.raises(RuntimeError):
        run_semantic_workflow(chunks,sources,manifest,staging,call_model=first_model)
    second_extractions=[]

    def second_model(prompt):
        if prompt.startswith('Untrusted iMessage rows'):
            second_extractions.append(prompt)
            return valid_items_from_prompt(prompt,sources)
        return merge_or_review(prompt)

    review=run_semantic_workflow(chunks,sources,manifest,staging,call_model=second_model)
    assert len(second_extractions)==1 and 'g2' in second_extractions[0]
    assert review.is_file()
    assert not (review.parent/'semantic-failure-manifest.json').exists()


def test_concurrency_is_bounded_and_merge_order_is_deterministic(tmp_path: Path):
    chunks,sources,manifest=semantic_fixture(tmp_path,count=4)
    active=maximum=0
    lock=threading.Lock()
    merge_orders=[]

    def model(prompt):
        nonlocal active,maximum
        if prompt.startswith('Untrusted iMessage rows'):
            payload=json.loads(prompt.split('\n',1)[1])
            rows=payload['canonical_rows'] if isinstance(payload,dict) else payload
            with lock:
                active+=1; maximum=max(maximum,active)
            # Force completion in reverse chunk order.
            source_key=rows[0]['source'] if rows else 'tap'
            time.sleep({'g1':.08,'g2':.06,'g3':.04}.get(source_key,.02))
            try: return valid_items_from_prompt(prompt,sources)
            finally:
                with lock: active-=1
        if prompt.startswith('Merge these'):
            candidates=json.loads(prompt.split('\n',1)[1])
            merge_orders.append([item['source_key'] for item in candidates])
            # Deliberately scramble model order; workflow final order is canonical.
            candidates=list(reversed(candidates))
            return {'dossiers':{
                'kosta-owner':[item for item in candidates if item['author']=='kosta-owner'],
                'stephen-lucier':[item for item in candidates if item['author']=='stephen-lucier'],
            },'coverage':{'accounted_source_ids':[item['source_id'] for item in candidates]}}
        return merge_or_review(prompt)

    review=run_semantic_workflow(chunks,sources,manifest,tmp_path/'private',call_model=model,semantic_concurrency=4)
    value=json.loads(review.read_text())
    assert maximum==4
    assert merge_orders==[['g1','g2','g3']]
    assert value['coverage']=={'extracted':3,'merged':3,'reviewed':3}
    for items in value['dossiers'].values():
        assert [item['source_id'] for item in items]==sorted(item['source_id'] for item in items)
    with pytest.raises(ValueError,match='between 1 and 4'):
        run_semantic_workflow(chunks,sources,manifest,tmp_path/'other',call_model=model,semantic_concurrency=5)
    with pytest.raises(SystemExit):
        bootstrap_main(['--source-person','Stephen Lucier','--handle','+140****0100','--semantic-concurrency','5'])
