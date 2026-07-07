"""Unix-socket supervisor for Codex-native subtasks."""
from __future__ import annotations
import argparse, json, os, socketserver, threading, time
from pathlib import Path
from agent.codex_subtask.registry import TERMINAL_STATUSES, JobRegistry, now_ms, utc_iso
from agent.transports.codex_app_server_session import CodexAppServerSession, _ServerRequestRouting
DEFAULT_SOCKET=Path('~/.hermes/shared/codex-subtask.sock').expanduser(); DEFAULT_DB=Path('~/.hermes/shared/codex_subtask_jobs.db').expanduser(); SYNC_TIMEOUT_DEFAULT=600; SYNC_TIMEOUT_CAP=600; ASYNC_TIMEOUT_CAP=86400; STARTUP_TIMEOUT_SECONDS=90
def _build_prompt(prompt, context_files, cwd):
    if not context_files: return prompt
    base=Path(cwd).expanduser().resolve(); parts=[prompt,'\n\n---\nContext files inlined by Hermes codex_subtask:']
    for raw in list(context_files)[:20]:
        try:
            p=Path(raw).expanduser(); p=(base/p if not p.is_absolute() else p).resolve(); text=p.read_text(encoding='utf-8', errors='replace')[:50000]; parts.append(f'\n\n### {p}\n```\n{text}\n```')
        except Exception as exc: parts.append(f'\n\n### {raw}\n<failed to read: {type(exc).__name__}: {exc}>')
    return ''.join(parts)
def _overrides(model, reasoning_effort, sandbox_mode, allow_plugins, deny_plugins, skills):
    out=[]
    if model: out.append('model='+json.dumps(model))
    # Hermes tools expose the OpenAI-style `minimal` enum, but current Codex
    # GPT-5.5 profiles reject `minimal` and accept `low`. Normalize here so a
    # codex_subtask does not fail before the prompt runs. Minimal reasoning is
    # also incompatible with some Codex tools, so disable those if a stale caller
    # still passes it through.
    if reasoning_effort == 'minimal':
        reasoning_effort = 'low'
        out.append('web_search='+json.dumps('disabled'))
        out.append('features.image_generation=false')
    if reasoning_effort: out.append('model_reasoning_effort='+json.dumps(reasoning_effort))
    if sandbox_mode: out.append('sandbox_mode='+json.dumps(sandbox_mode))
    if allow_plugins: out.append('plugins.allow='+json.dumps(allow_plugins))
    if deny_plugins: out.append('plugins.deny='+json.dumps(deny_plugins))
    if skills: out.append('skills='+json.dumps(skills))
    return out
class Worker:
    def __init__(self, registry, job_id, **opts): self.registry=registry; self.job_id=job_id; self.opts=opts; self.session=None; self.cancel_requested=False; self.lock=threading.RLock(); self.thread=threading.Thread(target=self._run, daemon=True)
    def start(self): self.thread.start()
    def _event(self,event): self.registry.append_transcript(self.job_id, {'event':event})
    def _run(self):
        rec=self.registry.get(self.job_id)
        if not rec: return
        self.registry.update(self.job_id, status='starting', started_at=now_ms())
        try:
            self.session=CodexAppServerSession(cwd=rec.cwd, codex_profile=None, codex_config_overrides=_overrides(self.opts.get('model'), self.opts.get('reasoning_effort'), self.opts.get('sandbox_mode'), self.opts.get('allow_plugins'), self.opts.get('deny_plugins'), self.opts.get('skills')), request_routing=_ServerRequestRouting(auto_approve_exec=True, auto_approve_apply_patch=True), on_event=self._event, startup_timeout_seconds=STARTUP_TIMEOUT_SECONDS)
            tid=self.session.ensure_started(); self.registry.update(self.job_id, status='running', codex_thread_id=tid); self.registry.append_transcript(self.job_id, {'status':'running','codex_thread_id':tid})
            result=self.session.run_turn(rec.prompt, turn_timeout=float(rec.timeout_seconds or ASYNC_TIMEOUT_CAP))
            status='completed'
            if self.cancel_requested: status='cancelled'
            elif result.error and 'timed out' in result.error: status='timed_out'
            elif result.error: status='error'
            elif result.interrupted: status='interrupted'
            self.registry.update(self.job_id, status=status, completed_at=now_ms(), final_text=result.final_text or '', error_text=result.error, tool_iterations=result.tool_iterations, codex_thread_id=result.thread_id or tid)
            self.registry.append_transcript(self.job_id, {'status':status,'final_text':result.final_text,'error':result.error,'tool_iterations':result.tool_iterations,'transcript':result.projected_messages})
        except Exception as exc:
            self.registry.update(self.job_id, status='error', completed_at=now_ms(), error_text=f'{type(exc).__name__}: {exc}'); self.registry.append_transcript(self.job_id, {'status':'error','error':f'{type(exc).__name__}: {exc}'})
        finally:
            if self.session:
                try: self.session.close()
                except Exception: pass
    def cancel(self):
        with self.lock:
            self.cancel_requested=True
            if self.session: self.session.request_interrupt()
    def send(self,message,timeout=None):
        with self.lock:
            rec=self.registry.get(self.job_id)
            if not rec or rec.status not in {'running','awaiting_approval'}: return {'status':'error','error':'job is not running'}
            if not self.session: return {'status':'error','error':'job session unavailable'}
            result=self.session.run_turn(message, turn_timeout=float(timeout or SYNC_TIMEOUT_DEFAULT)); self.registry.append_transcript(self.job_id, {'followup':message,'final_text':result.final_text,'error':result.error,'transcript':result.projected_messages})
            return {'status':'completed' if not result.error else 'error','final_text':result.final_text,'error_text':result.error,'tool_iterations':result.tool_iterations}
class Supervisor:
    def __init__(self, db_path=DEFAULT_DB): self.registry=JobRegistry(db_path); self.workers={}; self.lock=threading.RLock(); self.registry.mark_active_interrupted_on_startup(); self.registry.gc_expired()
    def dispatch(self,req):
        try:
            a=req.get('action')
            if a=='submit': return self._submit(req)
            if a=='status': return self._status(req['job_id'])
            if a=='await': return self._await(req['job_id'], req.get('timeout_seconds'))
            if a=='cancel': return self._cancel(req['job_id'])
            if a=='send': return self._send(req['job_id'], req.get('message') or '', req.get('timeout_seconds'))
            if a=='list': return self._list(req)
            if a=='logs': return self._logs(req['job_id'], int(req.get('since') or 0), int(req.get('limit') or 200))
            if a=='ping': return {'status':'ok','now':utc_iso()}
            return {'status':'error','error':f'unknown action: {a}'}
        except Exception as exc: return {'status':'error','error':f'{type(exc).__name__}: {exc}'}
    def _submit(self,req):
        prompt=req.get('prompt') or ''
        if not prompt.strip(): return {'status':'error','error':'prompt is required'}
        mode=req.get('mode') or 'sync'; cwd=str(Path(req.get('cwd') or os.getcwd()).expanduser().resolve()); profile=req.get('profile') or 'gpt'; timeout=req.get('timeout_seconds'); message=None
        if mode=='sync':
            original=timeout; timeout=SYNC_TIMEOUT_DEFAULT if timeout in (None,'') else int(timeout)
            if timeout>SYNC_TIMEOUT_CAP: timeout=SYNC_TIMEOUT_CAP; message=f'timeout clamped from {original} to {SYNC_TIMEOUT_CAP} seconds'
            if timeout<=0: return {'status':'error','error':'timeout_seconds must be positive'}
        else:
            if timeout in (None,''): timeout=None
            else:
                original=int(timeout); timeout=max(1,min(original,ASYNC_TIMEOUT_CAP))
                if timeout!=original: message=f'timeout clamped from {original} to {ASYNC_TIMEOUT_CAP} seconds'
        rec=self.registry.create_job(prompt=_build_prompt(prompt, req.get('context_files'), cwd), cwd=cwd, profile=profile, model=req.get('model'), reasoning_effort=req.get('reasoning_effort'), sandbox_mode=req.get('sandbox_mode'), timeout_seconds=timeout, hermes_session_id=req.get('hermes_session_id') or 'default')
        w=Worker(self.registry, rec.job_id, model=req.get('model'), reasoning_effort=req.get('reasoning_effort'), sandbox_mode=req.get('sandbox_mode'), allow_plugins=req.get('allow_plugins'), deny_plugins=req.get('deny_plugins'), skills=req.get('skills'))
        with self.lock: self.workers[rec.job_id]=w
        w.start()
        if mode=='async': return {'status':'queued','job_id':rec.job_id,'codex_thread_id':None,'created_at':utc_iso(rec.created_at),'timeout_seconds':timeout,'supervisor_socket':str(DEFAULT_SOCKET),'message':message or ('started with no soft timeout' if timeout is None else f'started with {timeout}s timeout')}
        return self._await(rec.job_id, timeout+5, extra={'message':message,'timeout_seconds':timeout})
    def _status(self,job_id):
        rec=self.registry.get(job_id); return {'status':'error','error':'unknown job_id'} if not rec else rec.to_public_dict(False)
    def _await(self,job_id,timeout_seconds=None,extra=None):
        deadline=time.time()+float(timeout_seconds or ASYNC_TIMEOUT_CAP)
        while time.time()<deadline:
            rec=self.registry.get(job_id)
            if not rec: return {'status':'error','error':'unknown job_id'}
            if rec.status in TERMINAL_STATUSES:
                d=rec.to_public_dict(False); d['final_text']=rec.final_text or ''; d['transcript'],d['transcript_cursor']=self.registry.read_transcript(job_id, limit=200)
                if extra: d.update({k:v for k,v in extra.items() if v is not None})
                return d
            time.sleep(0.1)
        return {'status':'timed_out','job_id':job_id,'error_text':f'await timed out after {timeout_seconds}s'}
    def _cancel(self,job_id):
        with self.lock: w=self.workers.get(job_id)
        if w: w.cancel()
        rec=self.registry.get(job_id)
        if rec and rec.status in {'queued','starting'}: self.registry.update(job_id,status='cancelled',completed_at=now_ms(),error_text='cancelled before running')
        return self._status(job_id)
    def _send(self,job_id,message,timeout):
        with self.lock: w=self.workers.get(job_id)
        return w.send(message, timeout) if w else {'status':'error','error':'job is not attached to this supervisor'}
    def _list(self,req): return {'status':'ok','jobs':[j.to_public_dict(False) for j in self.registry.list(hermes_session_id=req.get('hermes_session_id'), status=req.get('filter') or req.get('status'), limit=int(req.get('limit') or 20))]}
    def _logs(self,job_id,since,limit):
        events,cursor=self.registry.read_transcript(job_id,since=since,limit=limit); return {'status':'ok','job_id':job_id,'events':events,'cursor':cursor}
class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line=self.rfile.readline(2_000_000)
        try:
            req=json.loads(line.decode()) if line else {}
        except Exception as exc:
            response={'status':'error','error':f'bad json: {exc}'}
        else:
            response=getattr(self.server, 'supervisor').dispatch(req)
        try:
            self.wfile.write((json.dumps(response, ensure_ascii=False)+'\n').encode())
        except (BrokenPipeError, ConnectionResetError):
            return
class UnixServer(socketserver.ThreadingUnixStreamServer): daemon_threads=True; allow_reuse_address=True
def serve(socket_path=DEFAULT_SOCKET, db_path=DEFAULT_DB):
    socket_path=Path(socket_path).expanduser(); socket_path.parent.mkdir(parents=True, exist_ok=True)
    try: socket_path.unlink()
    except FileNotFoundError: pass
    srv=UnixServer(str(socket_path), Handler); srv.supervisor=Supervisor(Path(db_path).expanduser()); os.chmod(socket_path,0o600); srv.serve_forever()
def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument('--socket',default=str(DEFAULT_SOCKET)); p.add_argument('--db',default=str(DEFAULT_DB)); a=p.parse_args(argv); serve(a.socket,a.db); return 0
if __name__=='__main__': raise SystemExit(main())
