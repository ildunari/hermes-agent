"""Persistent SQLite registry for codex_subtask jobs."""
from __future__ import annotations
import gzip, hashlib, json, sqlite3, time, secrets, threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional
TERMINAL_STATUSES={"completed","error","cancelled","timed_out","interrupted","interrupted_by_restart"}
DEFAULT_RETENTION_DAYS=30
def now_ms(): return int(time.time()*1000)
def utc_iso(ms=None):
    import datetime as dt
    ms=now_ms() if ms is None else ms
    return dt.datetime.fromtimestamp(ms/1000, tz=dt.timezone.utc).isoformat().replace('+00:00','Z')
def new_job_id(): return f"{int(time.time()*1000):013x}{secrets.token_hex(8)}"
@dataclass
class JobRecord:
    job_id:str; hermes_session_id:str; profile:str; model:Optional[str]; reasoning_effort:Optional[str]; sandbox_mode:Optional[str]
    prompt:str; prompt_sha256:str; cwd:str; status:str; codex_thread_id:Optional[str]; created_at:int; started_at:Optional[int]
    completed_at:Optional[int]; timeout_seconds:Optional[int]; final_text:Optional[str]; error_text:Optional[str]
    tool_iterations:int; approvals_handled:int; expires_at:int; transcript_path:Optional[str]
    def to_public_dict(self, include_prompt=False):
        d=asdict(self); d['created_at_iso']=utc_iso(self.created_at); d['started_at_iso']=utc_iso(self.started_at) if self.started_at else None; d['completed_at_iso']=utc_iso(self.completed_at) if self.completed_at else None; d['expires_at_iso']=utc_iso(self.expires_at); d['timed_out']=self.status=='timed_out'
        if not include_prompt: d.pop('prompt',None)
        return d
SCHEMA="""
CREATE TABLE IF NOT EXISTS jobs (job_id TEXT PRIMARY KEY, hermes_session_id TEXT NOT NULL, profile TEXT NOT NULL, model TEXT, reasoning_effort TEXT, sandbox_mode TEXT, prompt TEXT NOT NULL, prompt_sha256 TEXT NOT NULL, cwd TEXT NOT NULL, status TEXT NOT NULL, codex_thread_id TEXT, created_at INTEGER NOT NULL, started_at INTEGER, completed_at INTEGER, timeout_seconds INTEGER, final_text TEXT, error_text TEXT, tool_iterations INTEGER NOT NULL DEFAULT 0, approvals_handled INTEGER NOT NULL DEFAULT 0, expires_at INTEGER NOT NULL, transcript_path TEXT);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_expires ON jobs(expires_at);
CREATE INDEX IF NOT EXISTS jobs_session ON jobs(hermes_session_id);
"""
class JobRegistry:
    def __init__(self, db_path, transcript_dir=None):
        self.db_path=Path(db_path).expanduser(); self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.transcript_dir=Path(transcript_dir).expanduser() if transcript_dir else self.db_path.parent/'codex_subtask_transcripts'; self.transcript_dir.mkdir(parents=True, exist_ok=True)
        self._lock=threading.RLock()
        self._transcript_locks: dict[str, threading.RLock] = {}
        self._conn=sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30); self._conn.row_factory=sqlite3.Row
        with self._lock:
            self._conn.execute('PRAGMA busy_timeout=30000')
            self._conn.executescript(SCHEMA)
            self._conn.commit()
    def close(self):
        with self._lock: self._conn.close()
    def create_job(self, *, prompt, cwd, profile, model, reasoning_effort, sandbox_mode, timeout_seconds, hermes_session_id='default'):
        jid=new_job_id(); created=now_ms(); rec=JobRecord(jid, hermes_session_id, profile, model, reasoning_effort, sandbox_mode, prompt, hashlib.sha256(prompt.encode()).hexdigest(), str(Path(cwd).expanduser().resolve()), 'queued', None, created, None, None, timeout_seconds, None, None, 0, 0, created+DEFAULT_RETENTION_DAYS*86400*1000, str(self.transcript_dir/f'{jid}.jsonl.gz'))
        cols=list(asdict(rec).keys())
        with self._lock:
            self._conn.execute(f"INSERT INTO jobs ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", [getattr(rec,c) for c in cols])
            self._conn.commit()
        return rec
    def _row(self,row): return JobRecord(**{k:row[k] for k in row.keys()})
    def get(self, job_id):
        with self._lock:
            r=self._conn.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone()
        return self._row(r) if r else None
    def list(self, *, hermes_session_id=None, status=None, limit=20):
        sql='SELECT * FROM jobs'; vals=[]; clauses=[]
        if hermes_session_id: clauses.append('hermes_session_id=?'); vals.append(hermes_session_id)
        if status: clauses.append('status=?'); vals.append(status)
        if clauses: sql+=' WHERE '+' AND '.join(clauses)
        sql+=' ORDER BY created_at DESC LIMIT ?'; vals.append(max(1,min(int(limit),200)))
        with self._lock:
            rows=self._conn.execute(sql, vals).fetchall()
        return [self._row(r) for r in rows]
    def update(self, job_id, **fields):
        allowed=set(JobRecord.__dataclass_fields__.keys())-{'job_id'}; fields={k:v for k,v in fields.items() if k in allowed}
        with self._lock:
            if fields:
                self._conn.execute('UPDATE jobs SET '+', '.join(f'{k}=?' for k in fields)+' WHERE job_id=?', list(fields.values())+[job_id])
                self._conn.commit()
            r=self._conn.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone()
        return self._row(r) if r else None
    def mark_active_interrupted_on_startup(self):
        with self._lock:
            cur=self._conn.execute("UPDATE jobs SET status='interrupted_by_restart', error_text='supervisor restarted', completed_at=? WHERE status IN ('starting','running','awaiting_approval')",(now_ms(),))
            self._conn.commit()
            return cur.rowcount
    def gc_expired(self):
        with self._lock:
            rows=self._conn.execute('SELECT transcript_path FROM jobs WHERE expires_at < ?',(now_ms(),)).fetchall()
            self._conn.execute('DELETE FROM jobs WHERE expires_at < ?',(now_ms(),))
            self._conn.commit()
        for r in rows:
            try: Path(r['transcript_path']).unlink(missing_ok=True)
            except Exception: pass
        return len(rows)
    def _transcript_lock(self, path):
        key=str(path)
        with self._lock:
            lock=self._transcript_locks.get(key)
            if lock is None:
                lock=threading.RLock(); self._transcript_locks[key]=lock
            return lock
    def append_transcript(self, job_id, event):
        rec=self.get(job_id)
        if not rec or not rec.transcript_path: return
        p=Path(rec.transcript_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with self._transcript_lock(p):
            with gzip.open(p,'at',encoding='utf-8') as f: f.write(json.dumps({'ts':now_ms(), **event}, ensure_ascii=False)+'\n')
    def read_transcript(self, job_id, *, since=0, limit=200):
        rec=self.get(job_id); p=Path(rec.transcript_path) if rec and rec.transcript_path else None
        if not p or not p.exists(): return [], since
        out=[]; cursor=0
        with self._transcript_lock(p):
            with gzip.open(p,'rt',encoding='utf-8') as f:
                for idx,line in enumerate(f,1):
                    cursor=idx
                    if idx<=since: continue
                    if len(out)>=limit: break
                    try: out.append(json.loads(line))
                    except Exception: out.append({'raw':line.rstrip()})
        return out,cursor
