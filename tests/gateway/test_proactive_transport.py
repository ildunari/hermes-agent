from __future__ import annotations
import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest
from gateway.platforms.base import SendResult
from gateway.proactive_scheduler import ContactRoute, ProactiveConfig, ProactiveMode, ProactiveScheduler, ProactiveStateStore
from gateway.proactive_transport import BlueBubblesProactiveDelivery, deliver_prepared_exactly_once

NOW=1_800_000_000.0
ROUTE=ContactRoute('kosta-owner','poke','UTC','owner','dm','existing','owner','parent')
ALLOW=(('poke','kosta-owner','owner'),('guest','stephen-lucier','guest'))

class Adapter:
    platform=SimpleNamespace(value='bluebubbles')
    def __init__(self,result): self.result=result; self.calls=0
    async def _resolve_chat_guid(self,_): return 'iMessage;-;existing'
    async def send(self,*args,**kwargs): self.calls+=1; await asyncio.sleep(0); return self.result


def setup(tmp_path: Path):
    state=ProactiveStateStore(tmp_path/'state.db')
    route={'platform':'bluebubbles','chat_type':'dm','chat_id':'existing','user_id':'owner','session_id':'parent'}
    for i in range(5): state.register_inbound(profile='poke',contact_id='kosta-owner',route=route,timezone_name='UTC',source_id=f'm{i}',received_at=NOW-100+i)
    cfg=ProactiveConfig(enabled=True,dry_run=False,mode=ProactiveMode.LIVE,allowed_contacts=ALLOW)
    scheduler=ProactiveScheduler(state_db_path=tmp_path/'state.db',profile_home=tmp_path,profile_name='poke',config=cfg)
    slot=scheduler.arm_slot(ROUTE,kind='checkin',fire_at=NOW-1,now=NOW-2)
    claim=scheduler.claim_due(worker_id='x',now=NOW)[0]
    return scheduler,claim,slot

@pytest.mark.asyncio
async def test_exactly_once_concurrent_delivery(tmp_path: Path):
    scheduler,claim,slot=setup(tmp_path); adapter=Adapter(SendResult(True,message_id='guid'))
    delivery=BlueBubblesProactiveDelivery(adapter)
    results=await asyncio.gather(*[deliver_prepared_exactly_once(scheduler=scheduler,delivery=delivery,route=ROUTE,claim=claim,text='one',correlation_id=str(i),now=NOW) for i in range(2)])
    assert adapter.calls==1 and 'sent' in results
    with scheduler._connect() as con:
        row=con.execute('SELECT state,attempt_count,transport_message_id FROM proactive_delivery WHERE slot_id=?',(slot,)).fetchone()
    assert tuple(row)==('sent',1,'guid')

@pytest.mark.asyncio
@pytest.mark.parametrize(('send_result','expected','calls'),[(SendResult(False,error='timeout'), 'delivery_unknown',1),(SendResult(False,error='connect',retryable=True),'retry_wait',1),(SendResult(False,error='partial',raw_response={'partial_delivery':True}),'partial_delivery',1)])
async def test_delivery_uncertainty_and_retry_classification(tmp_path: Path,send_result,expected,calls):
    scheduler,claim,_=setup(tmp_path); adapter=Adapter(send_result)
    result=await deliver_prepared_exactly_once(scheduler=scheduler,delivery=BlueBubblesProactiveDelivery(adapter),route=ROUTE,claim=claim,text='one',correlation_id='c',now=NOW)
    assert result==expected and adapter.calls==calls

@pytest.mark.asyncio
async def test_nonallowlisted_refused_before_adapter(tmp_path: Path):
    scheduler,claim,_=setup(tmp_path); adapter=Adapter(SendResult(True,message_id='x'))
    bad=ContactRoute('mom','poke','UTC','owner','dm','existing')
    with pytest.raises(ValueError,match='non-allowlisted'):
        await BlueBubblesProactiveDelivery(adapter).deliver(route=bad,text='x',slot_id=claim.slot_id,correlation_id='c')
    assert adapter.calls==0
