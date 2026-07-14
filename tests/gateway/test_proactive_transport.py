from __future__ import annotations
import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace
import pytest
from gateway.contact_memory.store import ContactMemoryStore
from gateway.platforms.base import SendResult
from gateway.proactive_scheduler import ContactRoute, ProactiveConfig, ProactiveMode, ProactiveOwnershipRegistry, ProactiveScheduler, ProactiveStateStore
from gateway.proactive_transport import BlueBubblesProactiveDelivery, deliver_prepared_exactly_once

NOW=1_800_000_000.0
ROUTE=ContactRoute('kosta-owner','poke','UTC','owner','dm','existing','owner','parent')
ALLOW=(('poke','kosta-owner','owner'),('guest','stephen-lucier','guest'))

class Adapter:
    platform=SimpleNamespace(value='bluebubbles')
    def __init__(self,result): self.result=result; self.calls=0; self.texts=[]; self.auth_requests=[]
    async def resolve_authenticated_existing_dm(self,chat_id,user_id):
        self.auth_requests.append((chat_id,user_id))
        return ('iMessage;-;existing','fingerprint') if chat_id and user_id else None
    async def send(self,*args,**kwargs): self.calls+=1; self.texts.append(args[1]); await asyncio.sleep(0); return self.result


def setup(tmp_path: Path):
    state=ProactiveStateStore(tmp_path/'state.db')
    route={'platform':'bluebubbles','chat_type':'dm','chat_id':'existing','user_id':'owner','session_id':'parent'}
    for i in range(5): state.register_inbound(profile='poke',contact_id='kosta-owner',route=route,timezone_name='UTC',source_id=f'm{i}',received_at=NOW-100+i)
    cfg=ProactiveConfig(enabled=True,dry_run=False,mode=ProactiveMode.LIVE,allowed_contacts=ALLOW,
                        active_start='00:00',active_end='23:59',alarm_sink_configured=True)
    scheduler=ProactiveScheduler(state_db_path=tmp_path/'state.db',profile_home=tmp_path,profile_name='poke',config=cfg,
                                 ownership_registry_path=tmp_path/'ownership.db')
    slot=scheduler.arm_slot(ROUTE,kind='checkin',fire_at=NOW-1,now=NOW-2)
    claim=scheduler.claim_due(worker_id='x',now=NOW)[0]
    return scheduler,claim,slot

def transport(adapter, scheduler):
    scheduler.ownership_registry.acquire_transport('runner','adapter',now=NOW)
    return BlueBubblesProactiveDelivery(adapter,ownership_registry=scheduler.ownership_registry,
                                        runner_id='runner',adapter_id='adapter',
                                        participant_identities={('poke','kosta-owner'):frozenset({'owner'})},
                                        scheduler=scheduler)

@pytest.mark.asyncio
async def test_exactly_once_concurrent_delivery(tmp_path: Path):
    scheduler,claim,slot=setup(tmp_path); adapter=Adapter(SendResult(True,message_id='guid'))
    delivery=transport(adapter,scheduler)
    results=await asyncio.gather(*[deliver_prepared_exactly_once(scheduler=scheduler,delivery=delivery,route=ROUTE,claim=claim,text='one',correlation_id=str(i),now=NOW) for i in range(2)])
    assert adapter.calls==1 and 'sent' in results
    with scheduler._connect() as con:
        row=con.execute('SELECT state,attempt_count,transport_message_id FROM proactive_delivery WHERE slot_id=?',(slot,)).fetchone()
    assert tuple(row)==('sent',1,'guid')
    projected=ContactMemoryStore(tmp_path/'contact-memory','kosta-owner').get_proactive_send(slot)
    assert projected is not None and projected.sent_at==NOW
    circuit=scheduler.ownership_registry.global_send_status(now=NOW)
    assert circuit['circuit_state']=='open' and circuit['circuit_reason']=='send_rate_above_40_percent'

@pytest.mark.asyncio
@pytest.mark.parametrize(('send_result','expected','calls'),[(SendResult(False,error='timeout'), 'delivery_unknown',1),(SendResult(False,error='connect',retryable=True),'retry_wait',1),(SendResult(False,error='partial',raw_response={'partial_delivery':True}),'partial_delivery',1)])
async def test_delivery_uncertainty_and_retry_classification(tmp_path: Path,send_result,expected,calls):
    scheduler,claim,_=setup(tmp_path); adapter=Adapter(send_result)
    result=await deliver_prepared_exactly_once(scheduler=scheduler,delivery=transport(adapter,scheduler),route=ROUTE,claim=claim,text='one',correlation_id='c',now=NOW)
    assert result==expected and adapter.calls==calls

@pytest.mark.asyncio
async def test_nonallowlisted_refused_before_adapter(tmp_path: Path):
    scheduler,claim,_=setup(tmp_path); adapter=Adapter(SendResult(True,message_id='x'))
    bad=ContactRoute('mom','poke','UTC','owner','dm','existing')
    with pytest.raises(ValueError,match='non-allowlisted'):
        await transport(adapter,scheduler).deliver(route=bad,text='x',slot_id=claim.slot_id,correlation_id='c')
    assert adapter.calls==0


@pytest.mark.asyncio
async def test_final_participant_auth_uses_operator_registry_not_stored_route_user(tmp_path: Path):
    scheduler,claim,_=setup(tmp_path); adapter=Adapter(SendResult(True,message_id='sent'))
    stale=dataclasses.replace(ROUTE,user_id='stale-or-corrupt-route-user')
    result=await deliver_prepared_exactly_once(
        scheduler=scheduler,delivery=transport(adapter,scheduler),route=stale,claim=claim,
        text='one',correlation_id='registry',now=NOW,
    )
    assert result=='sent'
    assert adapter.auth_requests==[('existing',frozenset({'owner'}))]


@pytest.mark.asyncio
async def test_missing_operator_participant_registry_fails_closed(tmp_path: Path):
    scheduler,claim,_=setup(tmp_path); adapter=Adapter(SendResult(True,message_id='sent'))
    scheduler.ownership_registry.acquire_transport('runner','adapter',now=NOW)
    delivery=BlueBubblesProactiveDelivery(
        adapter,ownership_registry=scheduler.ownership_registry,runner_id='runner',adapter_id='adapter',
        scheduler=scheduler,
    )
    result = await deliver_prepared_exactly_once(
        scheduler=scheduler,delivery=delivery,route=ROUTE,claim=claim,
        text='one',correlation_id='missing',now=NOW,
    )
    assert result == 'failed'
    assert adapter.calls==0
    with scheduler._connect() as con:
        row = con.execute(
            'SELECT state,last_error_class FROM proactive_delivery WHERE slot_id=?',
            (claim.slot_id,),
        ).fetchone()
    assert tuple(row) == ('failed', 'pre_send_invariant:RuntimeError')


def test_final_check_refuses_outside_active_hours(tmp_path: Path):
    scheduler,claim,_=setup(tmp_path)
    scheduler.config=dataclasses.replace(scheduler.config,active_start='09:00',active_end='09:01')
    scheduler.reserve_delivery(claim,'one',now=NOW)
    assert scheduler.final_delivery_check(ROUTE,claim,now=NOW)=='outside_active_hours'


def test_circuit_breaker_and_sprawl_cleanup(tmp_path: Path):
    scheduler,claim,_=setup(tmp_path)
    with scheduler._connect() as con:
        for index in range(3):
            slot=f'old-{index}'
            con.execute("INSERT INTO proactive_slot(slot_id,contact_hash,kind,payload_json,status,fire_at,inbound_version,reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (slot,claim.contact_hash,'checkin','{}','suppressed',NOW-1,claim.inbound_version,'failed',NOW-1,NOW-1))
            con.execute("INSERT INTO proactive_delivery(slot_id,attempt_count,state,payload_hash,last_error_class,updated_at) VALUES(?,?,?,?,?,?)",
                        (slot,1,'failed','hash','connect',NOW-1))
        con.execute("INSERT INTO proactive_inbound(contact_hash,message_id,received_at) VALUES(?,?,?)",
                    (claim.contact_hash,'ancient',NOW-366*86400))
    assert scheduler.final_delivery_check(ROUTE,claim,now=NOW)=='transport_circuit_open'
    cleaned=scheduler.cleanup_sprawl(now=NOW)
    assert cleaned['inbound']==1


@pytest.mark.asyncio
async def test_retry_replays_immutable_payload_without_recomposition(tmp_path: Path):
    scheduler,claim,_=setup(tmp_path)
    first=Adapter(SendResult(False,error='connect',retryable=True))
    assert await deliver_prepared_exactly_once(scheduler=scheduler,delivery=transport(first,scheduler),route=ROUTE,claim=claim,text='original',correlation_id='one',now=NOW)=='retry_wait'
    retry=scheduler.claim_due(worker_id='retry',now=NOW+301)[0]
    second=Adapter(SendResult(True,message_id='sent'))
    assert await deliver_prepared_exactly_once(scheduler=scheduler,delivery=transport(second,scheduler),route=ROUTE,claim=retry,text='recomposed',correlation_id='two',now=NOW+301)=='sent'
    assert first.texts==['original'] and second.texts==['original']


@pytest.mark.asyncio
async def test_global_spacing_contention_rearms_without_attempt_or_payload_loss(tmp_path: Path):
    scheduler,claim,slot=setup(tmp_path)
    scheduler.ownership_registry.finish_global_send('prior',sent=True,now=NOW-1)
    # Seed the singleton as a recent visible send without touching this slot.
    with scheduler.ownership_registry._connect() as con:
        con.execute("INSERT INTO proactive_global_send VALUES(1,NULL,NULL,?,?) ON CONFLICT(singleton) DO UPDATE SET slot_id=NULL,reserved_until=NULL,last_visible_at=excluded.last_visible_at,updated_at=excluded.updated_at",(NOW-1,NOW-1))
    adapter=Adapter(SendResult(True,message_id='must-not-send'))
    result=await deliver_prepared_exactly_once(
        scheduler=scheduler,delivery=transport(adapter,scheduler),route=ROUTE,claim=claim,
        text='immutable',correlation_id='spacing',now=NOW,
    )
    assert result=='retry_wait' and adapter.calls==0
    with scheduler._connect() as con:
        row=con.execute('SELECT state,attempt_count,prepared_payload FROM proactive_delivery WHERE slot_id=?',(slot,)).fetchone()
    assert tuple(row)==('retry_wait',0,'immutable')


def test_global_send_lease_and_operator_reset_are_durable(tmp_path: Path):
    registry=ProactiveOwnershipRegistry(tmp_path/'ownership.db')
    assert registry.reserve_global_send('one',now=NOW)
    assert not registry.reserve_global_send('one',now=NOW+1)
    assert not registry.reserve_global_send('two',now=NOW+1)
    registry.finish_global_send('one',sent=True,now=NOW+2)
    assert not registry.reserve_global_send('two',now=NOW+301)
    assert registry.reserve_global_send('two',now=NOW+302)
    registry.open_circuit('operator_test',now=NOW+303)
    assert not ProactiveOwnershipRegistry(tmp_path/'ownership.db').reserve_global_send('three',now=NOW+500)
    with pytest.raises(PermissionError):
        registry.operator_reset_circuit(confirmed=False,now=NOW+501)
    registry.operator_reset_circuit(confirmed=True,now=NOW+502)
    assert ProactiveOwnershipRegistry(tmp_path/'ownership.db').reserve_global_send('three',now=NOW+503)


def test_transport_owner_conflict_opens_durable_global_circuit(tmp_path: Path):
    registry=ProactiveOwnershipRegistry(tmp_path/'ownership.db')
    registry.acquire_transport('runner-a','adapter-a',now=NOW)
    with pytest.raises(RuntimeError,match='owner lease conflict'):
        registry.acquire_transport('runner-b','adapter-b',now=NOW+1)
    assert not ProactiveOwnershipRegistry(tmp_path/'ownership.db').reserve_global_send('slot',now=NOW+2)


def test_authenticated_route_fingerprint_is_immutable(tmp_path: Path):
    scheduler,_,_=setup(tmp_path)
    assert scheduler.bind_route_fingerprint(ROUTE,'fingerprint-one')
    assert scheduler.bind_route_fingerprint(ROUTE,'fingerprint-one')
    assert not scheduler.bind_route_fingerprint(ROUTE,'fingerprint-two')
