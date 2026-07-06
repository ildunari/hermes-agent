"""Client helpers for the codex_subtask supervisor."""
from __future__ import annotations
import json, os, socket, subprocess, sys, time
from pathlib import Path
from typing import Any
DEFAULT_SOCKET=Path('~/.hermes/shared/codex-subtask.sock').expanduser(); DEFAULT_LOG=Path('~/.hermes/logs/codex-subtask-supervisor.log').expanduser(); LAUNCHD_LABEL='ai.hermes.codex-subtask-supervisor'; PLIST_PATH=Path('~/Library/LaunchAgents/ai.hermes.codex-subtask-supervisor.plist').expanduser()
class CodexSubtaskClientError(RuntimeError): pass
def _codex_path() -> str:
    parts = [
        str(Path('~/.npm-global/bin').expanduser()),
        str(Path('~/.local/bin').expanduser()),
        '/opt/homebrew/bin',
        '/usr/local/bin',
        os.environ.get('PATH') or os.defpath,
    ]
    seen=[]
    for chunk in parts:
        for part in str(chunk).split(os.pathsep):
            if part and part not in seen:
                seen.append(part)
    return os.pathsep.join(seen)

def install_launchd_plist(python_executable=None, repo_root=None):
    python_executable=python_executable or sys.executable; repo_root=repo_root or str(Path(__file__).resolve().parents[2]); PLIST_PATH.parent.mkdir(parents=True, exist_ok=True); DEFAULT_LOG.parent.mkdir(parents=True, exist_ok=True)
    plist = f'''<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>\n<key>Label</key><string>{LAUNCHD_LABEL}</string>\n<key>ProgramArguments</key><array><string>{python_executable}</string><string>-m</string><string>agent.codex_subtask.supervisor</string></array>\n<key>WorkingDirectory</key><string>{repo_root}</string>\n<key>EnvironmentVariables</key><dict><key>HOME</key><string>{str(Path('~').expanduser())}</string><key>USER</key><string>{os.environ.get('USER','Kosta')}</string><key>PYTHONPATH</key><string>{repo_root}</string><key>CODEX_HOME</key><string>{str(Path('~/.codex').expanduser())}</string><key>PATH</key><string>{_codex_path()}</string></dict>\n<key>KeepAlive</key><true/><key>RunAtLoad</key><true/><key>ThrottleInterval</key><integer>5</integer>\n<key>StandardOutPath</key><string>{str(DEFAULT_LOG)}</string><key>StandardErrorPath</key><string>{str(DEFAULT_LOG)}</string>\n</dict></plist>\n'''
    PLIST_PATH.write_text(plist); return PLIST_PATH
def ping(timeout=0.25):
    try:
        if not DEFAULT_SOCKET.exists(): return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout); s.connect(str(DEFAULT_SOCKET)); s.sendall(b'{"action":"ping"}\n'); return bool(s.recv(1024))
    except OSError: return False
def _bootstrap_launchd():
    if os.uname().sysname!='Darwin': return
    plist=install_launchd_plist(); uid=os.getuid(); subprocess.run(['launchctl','bootstrap',f'user/{uid}',str(plist)],capture_output=True,text=True,timeout=10); subprocess.run(['launchctl','kickstart','-k',f'user/{uid}/{LAUNCHD_LABEL}'],capture_output=True,text=True,timeout=10)
def ensure_supervisor(timeout=2.0):
    if ping(): return
    _bootstrap_launchd(); deadline=time.time()+timeout
    while time.time()<deadline:
        if ping(): return
        time.sleep(0.1)
    DEFAULT_LOG.parent.mkdir(parents=True, exist_ok=True); log=open(DEFAULT_LOG,'ab'); subprocess.Popen([sys.executable,'-m','agent.codex_subtask.supervisor'], cwd=str(Path(__file__).resolve().parents[2]), stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    deadline=time.time()+timeout
    while time.time()<deadline:
        if ping(): return
        time.sleep(0.1)
    raise CodexSubtaskClientError(f'codex_subtask supervisor unavailable at {DEFAULT_SOCKET}')
def request(action, ensure=True, **params:Any):
    socket_timeout=float(params.pop('_socket_timeout',30))
    if ensure: ensure_supervisor()
    payload={'action':action, **params}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(socket_timeout); s.connect(str(DEFAULT_SOCKET)); s.sendall((json.dumps(payload, ensure_ascii=False)+'\n').encode()); data=b''
            while not data.endswith(b'\n'):
                chunk=s.recv(65536)
                if not chunk: break
                data+=chunk
    except OSError as exc: raise CodexSubtaskClientError(str(exc)) from exc
    if not data: raise CodexSubtaskClientError('empty response from supervisor')
    return json.loads(data.decode())
