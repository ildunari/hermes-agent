#!/usr/bin/env node
/**
 * Cross-machine Browser Dev coordinator.
 *
 * The Studio serves the renderer directly through Vite over Tailscale. The
 * MacBook runs only an isolated Electron shell, so React/CSS changes arrive via
 * HMR while Electron/preload changes are rsynced, rebuilt, and relaunched.
 */
import { spawn, spawnSync } from 'node:child_process'
import { existsSync, realpathSync, watch } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { buildConnectionBootstrapScript, shellQuote } from './browser-dev-connection.mjs'

const here = dirname(fileURLToPath(import.meta.url))
export const desktopRoot = resolve(here, '..')
export const repoRoot = resolve(desktopRoot, '../..')

export const defaults = Object.freeze({
  port: Number(process.env.HERMES_BROWSER_DEV_PORT || 5174),
  remoteDebugPort: Number(process.env.HERMES_BROWSER_DEV_CDP_PORT || 9231),
  sshTarget: process.env.HERMES_BROWSER_DEV_SSH_TARGET || 'kosta@kostas-macbook-pro.tailf7342a.ts.net',
  remoteRoot: process.env.HERMES_BROWSER_DEV_REMOTE_ROOT || '/Users/kosta/.cache/hermes-browser-dev/hermes-agent',
  remoteModules:
    process.env.HERMES_BROWSER_DEV_REMOTE_MODULES || '/Users/kosta/.cache/hermes-browser-dev/hermes-agent/node_modules',
  userData: process.env.HERMES_BROWSER_DEV_USER_DATA || '/Users/kosta/Library/Application Support/Hermes Browser Dev',
  backendHost: process.env.HERMES_BROWSER_DEV_BACKEND_HOST || 'macstudio.tailf7342a.ts.net',
  backendUser: process.env.HERMES_BROWSER_DEV_BACKEND_USER || 'Kosta',
  backendPort: Number(process.env.HERMES_BROWSER_DEV_BACKEND_PORT || 22),
  backendKeyPath: process.env.HERMES_BROWSER_DEV_BACKEND_KEY_PATH || '',
  backendHermesPath: process.env.HERMES_BROWSER_DEV_BACKEND_HERMES_PATH || '',
  backendProfile: process.env.HERMES_BROWSER_DEV_PROFILE || ''
})

const sshArgs = [
  '-o',
  'BatchMode=yes',
  '-o',
  'ConnectTimeout=8',
  '-o',
  'IdentitiesOnly=yes',
  '-o',
  'IdentityAgent=none',
  '-F',
  '/dev/null',
  '-i',
  process.env.HERMES_BROWSER_DEV_SSH_KEY || `${process.env.HOME}/.ssh/termius_key`
]

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { encoding: 'utf8', stdio: options.capture ? 'pipe' : 'inherit', ...options })
  if (result.status !== 0) {
    const detail = options.capture ? `\n${result.stderr || result.stdout}` : ''
    throw new Error(`${command} exited ${result.status}${detail}`)
  }
  return result.stdout?.trim() || ''
}

function ssh(script, { capture = false } = {}) {
  return run('ssh', [...sshArgs, defaults.sshTarget, script], { capture })
}

function startReverseTunnel(port) {
  return spawn(
    'ssh',
    [
      ...sshArgs,
      '-o',
      'ExitOnForwardFailure=yes',
      '-o',
      'ServerAliveInterval=15',
      '-N',
      '-R',
      `${port}:127.0.0.1:${port}`,
      defaults.sshTarget
    ],
    { stdio: 'inherit' }
  )
}

function rsync(source, destination, extra = []) {
  run('rsync', [
    '-az',
    '--delete',
    '--exclude',
    'node_modules',
    '--exclude',
    'dist',
    '--exclude',
    'release',
    '--exclude',
    'test-results',
    '-e',
    `ssh ${sshArgs.map(shellQuote).join(' ')}`,
    ...extra,
    source,
    `${defaults.sshTarget}:${destination}`
  ])
}

export function syncRemoteSource() {
  const root = defaults.remoteRoot
  ssh(`mkdir -p ${shellQuote(`${root}/apps`)} ${shellQuote(`${root}/hermes_cli`)}`)
  rsync(`${desktopRoot}/`, `${root}/apps/desktop/`)
  rsync(`${join(repoRoot, 'apps/shared')}/`, `${root}/apps/shared/`)
  run('rsync', [
    '-az',
    '-e',
    `ssh ${sshArgs.map(shellQuote).join(' ')}`,
    join(repoRoot, 'hermes_cli/browser_wire_v1.json'),
    `${defaults.sshTarget}:${root}/hermes_cli/`
  ])
  run('rsync', [
    '-az',
    '-e',
    `ssh ${sshArgs.map(shellQuote).join(' ')}`,
    join(repoRoot, 'package.json'),
    join(repoRoot, 'package-lock.json'),
    `${defaults.sshTarget}:${root}/`
  ])
  ssh(`
set -eu
ROOT=${shellQuote(root)}
cd "$ROOT"
LOCK_HASH=$(shasum -a 256 package-lock.json | cut -d ' ' -f 1)
MARKER=.browser-dev-deps.sha256
ELECTRON=node_modules/electron/dist/Electron.app/Contents/MacOS/Electron
if [ ! -f "$MARKER" ] || [ "$(cat "$MARKER" 2>/dev/null || true)" != "$LOCK_HASH" ] || [ ! -x "$ELECTRON" ]; then
  rm -rf apps/desktop/node_modules
  npm install --workspace apps/desktop --include-workspace-root
  [ -x "$ELECTRON" ] || node node_modules/electron/install.js
  node node_modules/esbuild/install.js
  if [ -f node_modules/node-pty/scripts/prebuild.js ]; then
    node node_modules/node-pty/scripts/prebuild.js
  fi
  printf '%s\n' "$LOCK_HASH" > "$MARKER"
fi
cd apps/desktop
node scripts/bundle-electron-main.mjs --dev
`)
}

export function stopRemote({ quiet = false } = {}) {
  const pidFile = `${defaults.userData}/browser-dev.pid`
  const script = `
PID_FILE=${shellQuote(pidFile)}
PID=$(lsof -tiTCP:${defaults.remoteDebugPort} -sTCP:LISTEN 2>/dev/null | head -n 1 || true)
if [ -z "$PID" ] && [ -f "$PID_FILE" ]; then PID=$(cat "$PID_FILE" 2>/dev/null || true); fi
if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then
  rm -f "$PID_FILE"
  ${quiet ? 'exit 0' : 'echo "Hermes Browser Dev is not running"; exit 0'}
fi
CMD=$(ps -p "$PID" -o command= 2>/dev/null || true)
case "$CMD" in
  *${defaults.remoteRoot.replaceAll('~', '$HOME')}*) kill -TERM "$PID" 2>/dev/null || true ;;
  *) echo "Refusing to stop PID $PID: identity mismatch ($CMD)" >&2; exit 1 ;;
esac
for _ in $(seq 1 20); do kill -0 "$PID" 2>/dev/null || break; sleep 0.25; done
if kill -0 "$PID" 2>/dev/null; then kill -KILL "$PID" 2>/dev/null || true; fi
for _ in $(seq 1 40); do curl -fsS http://127.0.0.1:${defaults.remoteDebugPort}/json/version >/dev/null 2>&1 || break; sleep 0.25; done
if curl -fsS http://127.0.0.1:${defaults.remoteDebugPort}/json/version >/dev/null 2>&1; then
  echo "Browser Dev CDP port ${defaults.remoteDebugPort} did not close after stopping PID $PID" >&2
  exit 1
fi
rm -f "$PID_FILE"
${quiet ? '' : 'echo "Hermes Browser Dev stopped"'}
`
  try {
    ssh(script)
  } catch (error) {
    if (!quiet) throw error
  }
}

async function terminateProcess(pid, { graceMs = 2000, label = 'process' } = {}) {
  if (!pid || isNaN(pid)) return
  try {
    process.kill(pid, 0)
  } catch {
    return
  }
  try {
    process.kill(pid, 'SIGTERM')
  } catch (err) {
    console.warn(`[browser-dev] Failed to send SIGTERM to ${label} (PID ${pid}): ${err.message}`)
    return
  }

  const deadline = Date.now() + graceMs
  while (Date.now() < deadline) {
    try {
      process.kill(pid, 0)
    } catch {
      return
    }
    await new Promise(resolve => setTimeout(resolve, 100))
  }

  try {
    process.kill(pid, 0)
    process.kill(pid, 'SIGKILL')
  } catch {
    // Process exited before or during SIGKILL
  }
}

function getLocalPortListenerPids(port) {
  const result = spawnSync('lsof', ['-tiTCP:' + port, '-sTCP:LISTEN'], { encoding: 'utf8' })
  if (result.status !== 0 || !result.stdout) return []
  const pids = []
  for (const rawLine of result.stdout.trim().split('\n')) {
    const pid = Number(rawLine.trim())
    if (!pid || isNaN(pid) || pid === process.pid) continue
    const psRes = spawnSync('ps', ['-p', String(pid), '-o', 'command='], { encoding: 'utf8' })
    if (psRes.status !== 0 || !psRes.stdout) continue
    const cmd = psRes.stdout.trim()
    if (cmd.includes('vite') && cmd.includes(repoRoot)) {
      pids.push(pid)
    } else {
      console.warn(`[browser-dev] Refusing to kill PID ${pid} holding local port ${port}: identity mismatch (${cmd})`)
    }
  }
  return pids
}

function getLocalTunnelPids(port) {
  const result = spawnSync('ps', ['-A', '-o', 'pid=,command='], { encoding: 'utf8' })
  if (result.status !== 0 || !result.stdout) return []
  const expectedPattern = `-R ${port}:127.0.0.1:${port}`
  const pids = []
  for (const line of result.stdout.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed) continue
    const match = trimmed.match(/^(\d+)\s+(.+)$/)
    if (!match) continue
    const [, pidStr, cmd] = match
    const pid = Number(pidStr)
    if (!pid || isNaN(pid) || pid === process.pid) continue
    if (cmd.includes(expectedPattern) && (cmd.includes('ssh ') || cmd.startsWith('ssh '))) {
      pids.push(pid)
    }
  }
  return pids
}

export async function reapOrphans({ port = defaults.port, quiet = false } = {}) {
  try {
    // 1. Clear remote port forwarding listener on MacBook if present
    const remoteScript = `
PORT=${port}
PIDS=$(lsof -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null || true)
if [ -n "$PIDS" ]; then
  for PID in $PIDS; do
    CMD=$(ps -p "$PID" -o command= 2>/dev/null || true)
    case "$CMD" in
      *sshd*)
        kill -TERM "$PID" 2>/dev/null || true
        ;;
      *)
        echo "Refusing to stop remote port $PORT listener PID $PID: identity mismatch ($CMD)" >&2
        ;;
    esac
  done
  for _ in $(seq 1 20); do
    STALE=$(lsof -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null || true)
    if [ -z "$STALE" ]; then break; fi
    sleep 0.1
  done
  STALE=$(lsof -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null || true)
  if [ -n "$STALE" ]; then
    for PID in $STALE; do
      CMD=$(ps -p "$PID" -o command= 2>/dev/null || true)
      case "$CMD" in
        *sshd*) kill -KILL "$PID" 2>/dev/null || true ;;
      esac
    done
  fi
fi
`
    try {
      ssh(remoteScript)
    } catch (err) {
      if (!quiet) console.warn(`[browser-dev] Remote orphan cleanup warning for port ${port}: ${err.message}`)
    }

    // 2. Kill local processes listening on port
    const localPortPids = getLocalPortListenerPids(port)
    for (const pid of localPortPids) {
      if (!quiet) console.log(`[browser-dev] Reaping stale local port ${port} listener PID ${pid}`)
      await terminateProcess(pid, { label: `local port ${port} listener` })
    }

    // 3. Kill local ssh reverse tunnel processes matching port
    const localTunnelPids = getLocalTunnelPids(port)
    for (const pid of localTunnelPids) {
      if (!quiet) console.log(`[browser-dev] Reaping stale local SSH tunnel PID ${pid}`)
      await terminateProcess(pid, { label: `local SSH tunnel (port ${port})` })
    }
  } catch (err) {
    if (!quiet) console.warn(`[browser-dev] Local orphan cleanup warning: ${err.message}`)
  }
}

export async function stopAll({ quiet = false } = {}) {
  stopRemote({ quiet })
  await reapOrphans({ quiet })
}

export function startRemote(devServerUrl) {
  stopRemote({ quiet: true })
  const root = defaults.remoteRoot
  const userData = defaults.userData
  const electron = `${defaults.remoteModules}/electron/dist/Electron.app/Contents/MacOS/Electron`
  const log = `${userData}/browser-dev.log`
  const pidFile = `${userData}/browser-dev.pid`
  const connectionBootstrap = buildConnectionBootstrapScript({
    activeProfile: defaults.backendProfile,
    backend: {
      host: defaults.backendHost,
      user: defaults.backendUser,
      port: defaults.backendPort,
      keyPath: defaults.backendKeyPath,
      remoteHermesPath: defaults.backendHermesPath
    },
    userData
  })
  const script = `
set -eu
ROOT=${shellQuote(root)}
USER_DATA=${shellQuote(userData)}
mkdir -p "$USER_DATA"
${connectionBootstrap}
cd "$ROOT/apps/desktop"
nohup env \
  HERMES_DESKTOP_APP_NAME='Hermes Browser Dev' \
  HERMES_DESKTOP_DEV_SERVER=${shellQuote(devServerUrl)} \
  HERMES_DESKTOP_USER_DATA_DIR="$USER_DATA" \
  HERMES_DESKTOP_HERMES_ROOT="$HOME/.hermes/hermes-agent" \
  ${shellQuote(electron)} --remote-debugging-port=${defaults.remoteDebugPort} . \
  >${shellQuote(log)} 2>&1 </dev/null &
echo $! >${shellQuote(pidFile)}
sleep 2
PID=$(cat ${shellQuote(pidFile)})
kill -0 "$PID"
echo "Hermes Browser Dev started: pid=$PID log=$USER_DATA/browser-dev.log"
`
  ssh(script)
}

export function remoteStatus() {
  const pidFile = `${defaults.userData}/browser-dev.pid`
  return ssh(
    `
if [ ! -f ${shellQuote(pidFile)} ]; then echo stopped; exit 1; fi
PID=$(cat ${shellQuote(pidFile)})
if ! kill -0 "$PID" 2>/dev/null; then echo stale-pid:$PID; exit 1; fi
printf 'running pid=%s command=' "$PID"
ps -p "$PID" -o command=
`,
    { capture: true }
  )
}

async function waitForVite(url, child) {
  const deadline = Date.now() + 30_000
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`Vite exited early with code ${child.exitCode}`)
    try {
      const response = await fetch(url)
      if (response.ok) return
    } catch {
      // Vite is still starting; retry until the deadline.
    }
    await new Promise(resolvePromise => setTimeout(resolvePromise, 250))
  }
  throw new Error(`Timed out waiting for ${url}`)
}

function watchElectronSources(onChange) {
  const watchers = []
  let timer = null
  const schedule = filename => {
    clearTimeout(timer)
    timer = setTimeout(() => onChange(filename), 350)
  }
  watchers.push(watch(join(desktopRoot, 'electron'), { recursive: true }, (_event, filename) => schedule(filename)))
  watchers.push(watch(join(desktopRoot, 'scripts/bundle-electron-main.mjs'), (_event, filename) => schedule(filename)))
  return () => watchers.forEach(watcher => watcher.close())
}

async function start() {
  const viteBin = join(repoRoot, 'node_modules/.bin/vite')
  if (!existsSync(viteBin)) {
    throw new Error('Dependencies are missing. Run npm install at the repository root.')
  }

  await reapOrphans({ port: defaults.port, quiet: true })

  const host = '127.0.0.1'
  const url = `http://${host}:${defaults.port}`
  const vite = spawn(viteBin, ['--host', host, '--port', String(defaults.port), '--strictPort'], {
    cwd: desktopRoot,
    env: { ...process.env, VITE_HERMES_BROWSER_DEV: '1' },
    stdio: 'inherit'
  })

  let syncing = false
  let rerun = false
  const rebuildRemote = async reason => {
    if (syncing) {
      rerun = true
      return
    }
    syncing = true
    try {
      console.log(
        `\n[browser-dev] Electron source changed (${reason || 'unknown'}); syncing and relaunching MacBook shell…`
      )
      syncRemoteSource()
      startRemote(url)
    } catch (error) {
      console.error(`[browser-dev] remote rebuild failed: ${error.message}`)
    } finally {
      syncing = false
      if (rerun) {
        rerun = false
        void rebuildRemote('coalesced follow-up')
      }
    }
  }

  const tunnel = startReverseTunnel(defaults.port)
  try {
    await waitForVite(url, vite)
    await new Promise(resolvePromise => setTimeout(resolvePromise, 500))
    if (tunnel.exitCode !== null) throw new Error(`Tailscale SSH tunnel exited with code ${tunnel.exitCode}`)
    ssh(`curl -fsS ${shellQuote(url)} >/dev/null`)
    console.log(`[browser-dev] Vite is tunneled to the MacBook at ${url}; staging the isolated shell…`)
    syncRemoteSource()
    startRemote(url)
  } catch (error) {
    tunnel.kill('SIGTERM')
    vite.kill('SIGTERM')
    throw error
  }
  console.log(
    '[browser-dev] React/CSS saves now hot-reload directly. Electron/preload saves rebuild and relaunch the dev shell.'
  )

  const closeWatcher = watchElectronSources(filename => void rebuildRemote(filename))
  const shutdown = () => {
    closeWatcher()
    tunnel.kill('SIGTERM')
    vite.kill('SIGTERM')
    try {
      stopRemote({ quiet: true })
    } catch (error) {
      console.error(`[browser-dev] remote stop failed: ${error.message}`)
    }
  }
  process.once('SIGINT', () => {
    shutdown()
    process.exit(130)
  })
  process.once('SIGTERM', () => {
    shutdown()
    process.exit(143)
  })
  vite.once('exit', code => {
    closeWatcher()
    tunnel.kill('SIGTERM')
    try {
      stopRemote({ quiet: true })
    } catch {
      // The Vite exit code remains the primary failure for this cleanup path.
    }
    process.exit(code || 0)
  })
}

// Only run the CLI when this file is executed directly. Importing it (tests,
// tooling, `node -e "import(...)"`) must never start or relaunch the dev shell.
const invokedDirectly = process.argv[1] && realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)

if (invokedDirectly) {
  const action = process.argv[2] || 'start'
  try {
    if (action === 'start') await start()
    else if (action === 'sync') syncRemoteSource()
    else if (action === 'stop') await stopAll()
    else if (action === 'status') console.log(remoteStatus())
    else if (action === 'print-config') console.log(JSON.stringify(defaults, null, 2))
    else throw new Error(`Unknown action: ${action}`)
  } catch (error) {
    console.error(`[browser-dev] ${error.message}`)
    process.exitCode = 1
  }
}
