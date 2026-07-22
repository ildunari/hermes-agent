import { app, BrowserWindow, protocol, session } from 'electron'
import { execFileSync } from 'node:child_process'
import { aggregateVerdict } from './verdicts.mjs'
import fs from 'node:fs'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
process.stderr.write('[browser-e0] fixture main loaded\n')
const ROOT = path.resolve(HERE, '../../../../..')
const outputArg = process.argv.find(value => value.startsWith('--output='))
const outputPath = outputArg ? path.resolve(process.cwd(), outputArg.slice('--output='.length)) : null
const unsafe = process.argv.includes('--unsafe-policy')
const mutateDevToolsContinuity = process.argv.includes('--mutate-devtools-continuity')
const profileDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-browser-e0-'))
const fileCanary = path.join(profileDir, 'forbidden-file-canary.html')
fs.writeFileSync(fileCanary, '<title>FORBIDDEN_FILE_CANARY</title>')

app.setPath('userData', path.join(profileDir, 'user-data'))
app.commandLine.appendSwitch('site-per-process')
app.commandLine.appendSwitch('host-resolver-rules', 'MAP a.test 127.0.0.1, MAP b.test 127.0.0.1')

const WEBVIEW_SETTINGS = Object.freeze({
  hostWebPreferences: {
    webviewTag: true,
    contextIsolation: true,
    sandbox: true,
    nodeIntegration: false,
    backgroundThrottling: false
  },
  partition: 'persist:hermes-preview',
  webpreferences: 'contextIsolation=yes,nodeIntegration=no,sandbox=yes'
})

const results = []
const traces = []
let canaryReads = 0
let artifactReads = 0
let guestClicks = 0
let cleanupComplete = false
let win
let guest
let originA
let originB
let canaryOrigin

function record(id, pass, observation, fatal = true) {
  results.push({ id, outcome: pass ? 'pass' : 'fail', observation })
  process.stderr.write(`[browser-e0] ${pass ? 'PASS' : 'FAIL'} ${id}\n`)
  if (!pass && fatal) throw new Error(`${id}: ${observation}`)
}

const prediction = (id, pass, observation) => record(id, pass, observation, false)

function trace(kind, detail = {}) {
  traces.push({ sequence: traces.length + 1, kind, ...detail })
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

function onceWithTimeout(emitter, eventName, timeoutMs = 8000, predicate = () => true) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      emitter.removeListener(eventName, listener)
      reject(new Error(`timed out waiting for ${eventName}`))
    }, timeoutMs)
    const listener = (...args) => {
      if (!predicate(...args)) return
      clearTimeout(timeout)
      emitter.removeListener(eventName, listener)
      resolve(args)
    }
    emitter.on(eventName, listener)
  })
}

function startServer(handler) {
  const server = http.createServer(handler)
  return new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => resolve(server))
  })
}

function portOf(server) {
  return server.address().port
}

function guestPage({ iframe = '' } = {}) {
  return `<!doctype html><meta charset="utf-8"><style>
    html,body{margin:0;min-height:1200px;background:#004d5c}canvas{display:block;width:100%;height:500px}
  </style><canvas id="canvas" width="900" height="500"></canvas>${iframe}<script>
    const c=document.querySelector('#canvas').getContext('2d');c.fillStyle='rgb(0,128,160)';c.fillRect(0,0,900,500)
    document.body.addEventListener('click',()=>fetch('/clicked').catch(()=>{}))
  </script>`
}

async function createOriginFarm() {
  let serverA
  let serverB
  let canaryServer
  const handler = label => (req, res) => {
    const url = new URL(req.url, 'http://fixture.invalid')
    if (url.pathname === '/clicked') {
      guestClicks += 1
      res.writeHead(204).end()
      return
    }
    if (url.pathname === '/redirect') {
      res.writeHead(302, { location: url.searchParams.get('to') || '/' }).end()
      return
    }
    if (url.pathname === '/oopif') {
      const other = label === 'a' ? originB : originA
      res.writeHead(200, { 'content-type': 'text/html', 'cache-control': 'no-store' })
      res.end(guestPage({ iframe: `<iframe id="oopif" src="${other}/page" style="width:300px;height:180px"></iframe>` }))
      return
    }
    res.writeHead(200, { 'content-type': 'text/html', 'cache-control': 'no-store' })
    res.end(guestPage())
  }
  serverA = await startServer(handler('a'))
  serverB = await startServer(handler('b'))
  canaryServer = await startServer((_req, res) => {
    canaryReads += 1
    res.writeHead(200, { 'content-type': 'text/html' }).end('FORBIDDEN_LOOPBACK_CANARY')
  })
  originA = `http://localhost:${portOf(serverA)}`
  originB = `http://127.0.0.1:${portOf(serverB)}`
  canaryOrigin = `http://127.0.0.1:${portOf(canaryServer)}`
  return [serverA, serverB, canaryServer]
}

async function loadGuest(url) {
  const loaded = onceWithTimeout(guest, 'did-frame-finish-load', 12000, (_event, isMainFrame) => isMainFrame)
  void guest.loadURL(url).catch(error => trace('load-error', { name: error?.name || 'Error' }))
  await loaded
}

async function hostEval(source) {
  return win.webContents.executeJavaScript(source, true)
}

async function guestEval(source) {
  return guest.executeJavaScript(source, true)
}

async function pixelAt(x, y) {
  const image = await win.webContents.capturePage()
  if (process.env.HERMES_E0_DEBUG_CAPTURE) fs.writeFileSync(process.env.HERMES_E0_DEBUG_CAPTURE, image.toPNG())
  const pixel = image.crop({ x, y, width: 1, height: 1 }).toBitmap({ scaleFactor: 1 })
  return { b: pixel[0], g: pixel[1], r: pixel[2], a: pixel[3] }
}

function isMagenta(pixel) {
  return pixel.r > 230 && pixel.b > 230 && pixel.g < 30
}

async function clickHost(x, y) {
  win.webContents.sendInputEvent({ type: 'mouseMove', x, y })
  win.webContents.sendInputEvent({ type: 'mouseDown', x, y, button: 'left', clickCount: 1 })
  win.webContents.sendInputEvent({ type: 'mouseUp', x, y, button: 'left', clickCount: 1 })
  await sleep(250)
}

async function runS1() {
  await loadGuest(`${originA}/page`)
  win.show()
  await sleep(500)
  const localPixel = await pixelAt(250, 200)
  prediction('S-1/P1-local', isMagenta(localPixel), `host capture sampled overlay pixel ${JSON.stringify(localPixel)}`)

  await hostEval("window.e0.setOverlayPointerEvents('none')")
  const beforeGuest = guestClicks
  await clickHost(250, 200)
  prediction('S-1/P2', guestClicks === beforeGuest + 1, 'host mouse input crossed pointer-events:none overlay and reached guest')

  await hostEval("window.e0.setOverlayPointerEvents('auto')")
  const beforeOverlay = await hostEval('window.e0.overlayClicks()')
  const beforeBlockedGuest = guestClicks
  await clickHost(250, 200)
  const afterOverlay = await hostEval('window.e0.overlayClicks()')
  prediction('S-1/P3', afterOverlay === beforeOverlay + 1 && guestClicks === beforeBlockedGuest, 'pointer-events:auto captured host mouse input without guest delivery')

  await hostEval("window.e0.setOverlayPointerEvents('none')")
  await guestEval('scrollTo(0, 500)')
  const scrollPixel = await pixelAt(250, 200)
  prediction('S-1/P4', isMagenta(scrollPixel), 'overlay pixel stayed aligned after guest-initiated scrollTo')

  await loadGuest(`${originB}/page`)
  const crossOriginPixel = await pixelAt(250, 200)
  prediction('S-1/P1-cross-origin', isMagenta(crossOriginPixel), `cross-origin guest remained below overlay ${JSON.stringify(crossOriginPixel)}`)
  prediction('S-1/P5', isMagenta(crossOriginPixel), 'overlay survived cross-origin guest navigation without z-order inversion')
}

async function attachDebugger() {
  if (!guest.debugger.isAttached()) guest.debugger.attach('1.3')
  await guest.debugger.sendCommand('Page.enable')
  await guest.debugger.sendCommand('Runtime.enable')
  await guest.debugger.sendCommand('Target.setDiscoverTargets', { discover: true })
  await guest.debugger.sendCommand('Target.setAutoAttach', {
    autoAttach: true,
    waitForDebuggerOnStart: false,
    flatten: true
  })
}

async function runS2() {
  const methods = []
  const attachedTargets = []
  const fetchPauses = []
  const unexpectedDetaches = []
  const onMessage = async (_event, method, params, sessionId) => {
    methods.push(method)
    if (method === 'Target.attachedToTarget') attachedTargets.push(params.targetInfo)
    if (method === 'Fetch.requestPaused' && params.request?.url?.startsWith('http://dialog.invalid/')) {
      fetchPauses.push({ kind: params.request.url.split('/').pop(), sessionId: sessionId || null })
      try {
        const command = ['Fetch.failRequest', { requestId: params.requestId, errorReason: 'Aborted' }]
        if (sessionId) await guest.debugger.sendCommand(...command, sessionId)
        else await guest.debugger.sendCommand(...command)
      } catch (error) {
        trace('dialog-bridge-release-error', { name: error?.name || 'Error' })
      }
    }
  }
  const onDetach = (_event, reason) => unexpectedDetaches.push(reason)
  guest.debugger.on('message', onMessage)
  guest.debugger.on('detach', onDetach)
  await attachDebugger()
  await guest.debugger.sendCommand('Page.addScriptToEvaluateOnNewDocument', {
    source: `for (const kind of ['alert','confirm','prompt']) Object.defineProperty(window, kind, { value: function(){ const x=new XMLHttpRequest(); x.open('GET','http://dialog.invalid/'+kind,false); try{x.send()}catch{}; return kind==='confirm' ? true : kind==='prompt' ? '' : undefined }, configurable:false })`
  })
  await guest.debugger.sendCommand('Fetch.enable', {
    patterns: [{ urlPattern: 'http://dialog.invalid/*', requestStage: 'Request' }]
  })

  methods.length = 0
  await loadGuest(`${originA}/page`)
  prediction('S-2/P1', methods.includes('Page.frameNavigated'), 'Page.frameNavigated arrived for initial same-origin fixture navigation')

  methods.length = 0
  await loadGuest(`${originB}/page`)
  prediction('S-2/P2', methods.includes('Page.frameNavigated') && guest.debugger.isAttached(), 'Page events continued after cross-origin RFH swap')
  prediction('S-2/P3', unexpectedDetaches.length === 0, 'RFH swap caused no silent or observable debugger detach')

  fetchPauses.length = 0
  await guestEval("setTimeout(()=>alert('bridge-canary'),0)")
  for (let i = 0; i < 30 && fetchPauses.length === 0; i += 1) await sleep(100)
  prediction('S-2/P4', fetchPauses.some(item => item.kind === 'alert'), 'dialog bridge Fetch.requestPaused arrived after RFH swap and was released without a native modal')

  attachedTargets.length = 0
  await guestEval(`{const f=document.createElement('iframe');f.id='oopif';f.src=${JSON.stringify(`${originA}/page`)};f.style='width:300px;height:180px';document.body.append(f)}`)
  for (let i = 0; i < 40 && !attachedTargets.some(target => target.type === 'iframe'); i += 1) await sleep(100)
  prediction('S-2/P5', attachedTargets.some(target => target.type === 'iframe'), 'Target.setAutoAttach observed cross-origin OOPIF target')

  const detachesBeforeDevTools = unexpectedDetaches.length
  const devToolsOpened = onceWithTimeout(guest, 'devtools-opened', 8000).catch(() => null)
  guest.openDevTools({ mode: 'detach', activate: false })
  const openedEvent = await devToolsOpened
  if (mutateDevToolsContinuity && guest.debugger.isAttached()) {
    guest.debugger.removeListener('detach', onDetach)
    guest.debugger.detach()
    guest.debugger.on('detach', onDetach)
  }
  const detachObserved = unexpectedDetaches.length > detachesBeforeDevTools
  const stillAttached = guest.debugger.isAttached()
  let commandResponsive = false
  if (stillAttached) {
    try {
      const response = await guest.debugger.sendCommand('Target.getTargetInfo')
      commandResponsive = Boolean(response?.targetInfo?.targetId)
    } catch (error) {
      trace('devtools-continuity-error', { name: error?.name || 'Error' })
    }
  }
  const devToolsLifecycleSafe = Boolean(openedEvent) && (
    (stillAttached && commandResponsive) || (!stillAttached && detachObserved)
  )
  const devToolsObservation = stillAttached
    ? `DevTools coexisted with an attached, command-responsive debugger client${detachObserved ? ' after an unexpected detach event' : ''}`
    : `DevTools terminated the debugger client; detach event ${detachObserved ? 'was observed' : 'was not observed'}`
  const recordP6 = mutateDevToolsContinuity ? record : prediction
  recordP6('S-2/P6', devToolsLifecycleSafe, devToolsObservation)
  guest.closeDevTools()
  await sleep(250)
  guest.debugger.removeListener('message', onMessage)
  guest.debugger.removeListener('detach', onDetach)
  await attachDebugger()
}

const hardForbiddenSchemes = new Set([
  'file:', 'data:', 'blob:', 'javascript:', 'hermes-artifact:', 'custom:',
  'about:', 'devtools:', 'chrome:', 'chrome-extension:', 'filesystem:', 'view-source:'
])

function policyDecision(method, params = {}) {
  if (unsafe) return { allow: true, code: 'UNSAFE_FIXTURE_MODE' }
  if (method === 'Page.navigate') {
    let parsed
    try { parsed = new URL(params.url) } catch { return { allow: false, code: 'NAVIGATION_POLICY_BLOCKED', reason: 'malformed' } }
    if (hardForbiddenSchemes.has(parsed.protocol)) return { allow: false, code: 'NAVIGATION_POLICY_BLOCKED', reason: parsed.protocol }
    if ((parsed.protocol === 'http:' || parsed.protocol === 'https:') && ![new URL(originA).origin, new URL(originB).origin].includes(parsed.origin)) {
      return { allow: false, code: 'NAVIGATION_GRANT_REQUIRED', reason: 'ungranted-loopback-or-network' }
    }
    return { allow: true, code: 'ALLOW' }
  }
  if (method === 'Page.navigateToHistoryEntry') return { allow: false, code: 'NAVIGATION_POLICY_BLOCKED', reason: 'history-entry' }
  if (method === 'Page.setDocumentContent' || method.startsWith('Runtime.') || /^DOM\.(set|remove|request|resolve|focus)/.test(method)) {
    return { allow: false, code: 'NAVIGATION_POLICY_BLOCKED', reason: 'raw-mutation-role' }
  }
  return { allow: true, code: 'ALLOW' }
}

async function guardedSend(method, params = {}) {
  const decision = policyDecision(method, params)
  trace('pre-dispatch', { method, decision: decision.allow ? 'allow' : 'block', code: decision.code, reason: decision.reason || null })
  if (!decision.allow) return { dispatched: false, error: { code: -32000, hermesCode: decision.code, disposition: 'not_started' } }
  await guest.debugger.sendCommand(method, params)
  return { dispatched: true }
}

function navigationUrlFromArgs(args) {
  for (const arg of args) {
    if (typeof arg === 'string' && /^[a-z][a-z0-9+.-]*:/i.test(arg)) return arg
    if (arg && typeof arg === 'object' && typeof arg.url === 'string') return arg.url
  }
  return ''
}

function allowedNavigation(url) {
  if (unsafe) return true
  try {
    const parsed = new URL(url)
    return (parsed.protocol === 'http:' || parsed.protocol === 'https:') && [new URL(originA).origin, new URL(originB).origin].includes(parsed.origin)
  } catch {
    return false
  }
}

function installNavigationGuards() {
  for (const eventName of ['will-navigate', 'will-frame-navigate', 'will-redirect']) {
    guest.on(eventName, (...args) => {
      const event = args[0]
      const url = navigationUrlFromArgs(args.slice(1))
      const allow = allowedNavigation(url)
      trace('electron-navigation-event', { event: eventName, scheme: (() => { try { return new URL(url).protocol } catch { return 'malformed' } })(), decision: allow ? 'allow' : 'block' })
      if (!allow) event.preventDefault()
    })
  }
  guest.setWindowOpenHandler(details => {
    const allow = allowedNavigation(details.url)
    trace('window-open', { decision: allow ? 'deny-owned-routing' : 'block' })
    return { action: 'deny' }
  })
  guest.session.webRequest.onBeforeRequest({ urls: ['*://*/*', 'file://*/*', 'data:*', 'blob:*'] }, (details, callback) => {
    if (details.resourceType !== 'mainFrame' && details.resourceType !== 'subFrame') return callback({})
    const allow = allowedNavigation(details.url)
    trace('before-request', { resourceType: details.resourceType, decision: allow ? 'allow' : 'block' })
    callback({ cancel: !allow })
  })
}

async function assertNoForbiddenCommit(id, action) {
  const beforeReads = canaryReads + artifactReads
  await action()
  await sleep(400)
  const current = guest.getURL()
  const marker = await guestEval("document.documentElement?.textContent?.includes('FORBIDDEN_') || false").catch(() => false)
  record(id, allowedNavigation(current) && !marker && canaryReads + artifactReads === beforeReads, 'attempt produced zero forbidden canary reads and no forbidden document commit')
}

async function runS2N() {
  installNavigationGuards()
  await loadGuest(`${originA}/page`)

  const directCases = [
    ['file', `file://${fileCanary}`],
    ['data', 'data:text/html,FORBIDDEN_DATA_CANARY'],
    ['blob', 'blob:http://a.test/00000000-0000-0000-0000-000000000000'],
    ['javascript', 'javascript:document.body.textContent="FORBIDDEN_JS_CANARY"'],
    ['custom', 'custom:FORBIDDEN_CUSTOM_CANARY'],
    ['loopback', `${canaryOrigin}/canary`],
    ['artifact', 'hermes-artifact://forged-id'],
    ['malformed', 'not a url']
  ]
  for (const [name, url] of directCases) {
    const result = await guardedSend('Page.navigate', { url })
    record(`S-2N/direct-${name}`, !result.dispatched && result.error?.disposition === 'not_started', 'policy refused Page.navigate before sendCommand with typed not_started error')
  }

  for (const method of ['Runtime.evaluate', 'Runtime.callFunctionOn', 'Runtime.runScript', 'DOM.setAttributeValue', 'DOM.setOuterHTML', 'Page.setDocumentContent', 'Page.navigateToHistoryEntry']) {
    const result = await guardedSend(method, {})
    record(`S-2N/method-${method}`, !result.dispatched, `${method} refused at the raw role before debugger dispatch`)
  }

  await assertNoForbiddenCommit('S-2N/runtime-location', () => guestEval(`location.href=${JSON.stringify(`${canaryOrigin}/runtime`)}`))
  await assertNoForbiddenCommit('S-2N/window-open', () => guestEval(`window.open(${JSON.stringify(`${canaryOrigin}/popup`)})`))
  await assertNoForbiddenCommit('S-2N/anchor', () => guestEval(`{const a=document.createElement('a');a.href=${JSON.stringify(`${canaryOrigin}/anchor`)};document.body.append(a);a.click()}`))
  await assertNoForbiddenCommit('S-2N/form', () => guestEval(`{const f=document.createElement('form');f.action=${JSON.stringify(`${canaryOrigin}/form`)};document.body.append(f);f.submit()}`))
  await assertNoForbiddenCommit('S-2N/meta-refresh', () => guestEval(`{const m=document.createElement('meta');m.httpEquiv='refresh';m.content='0;url=${canaryOrigin}/meta';document.head.append(m)}`))
  await assertNoForbiddenCommit('S-2N/subframe', () => guestEval(`{const f=document.createElement('iframe');f.src=${JSON.stringify(`${canaryOrigin}/frame`)};document.body.append(f)}`))

  await assertNoForbiddenCommit('S-2N/redirect', () => loadGuest(`${originA}/redirect?to=${encodeURIComponent(`${canaryOrigin}/redirect`)}`).catch(() => {}))
  if (!allowedNavigation(guest.getURL())) await loadGuest(`${originA}/page`)

  await loadGuest(`${originA}/page`)
  await guestEval(`{const f=document.createElement('iframe');f.id='oopif';f.src=${JSON.stringify(`${originB}/page`)};document.body.append(f)}`)
  await sleep(500)
  await assertNoForbiddenCommit('S-2N/oopif', () => guestEval(`document.querySelector('#oopif').src=${JSON.stringify(`${canaryOrigin}/oopif`)}`))

  await loadGuest(`${originB}/page`)
  await assertNoForbiddenCommit('S-2N/rfh-swap', () => guestEval(`location.assign(${JSON.stringify(`${canaryOrigin}/post-rfh`)})`))

  const sameDocumentBefore = guest.getURL()
  const sameDocumentResult = await guestEval(`(()=>{history.pushState({},'', '#allowed');try{history.pushState({},'',${JSON.stringify(`${canaryOrigin}/history`)})}catch(e){return e.name}return 'unexpected'})()`)
  record('S-2N/same-document', sameDocumentResult === 'SecurityError' && guest.getURL().startsWith(sameDocumentBefore.split('#')[0]), 'same-document history was observed without acquiring a new origin or scheme')

  record('S-2N/no-read-commit', canaryReads === 0 && artifactReads === 0, 'all forbidden routes completed with zero loopback/artifact canary reads')
  record('S-2N/other-surface', !win.isDestroyed(), 'blocked attempts left the trusted host surface intact')
}

function gitValue(args) {
  try { return execFileSync('git', args, { cwd: ROOT, encoding: 'utf8' }).trim() } catch { return 'unknown' }
}

function macOSVersion() {
  try {
    const product = execFileSync('sw_vers', ['-productVersion'], { encoding: 'utf8' }).trim()
    const build = execFileSync('sw_vers', ['-buildVersion'], { encoding: 'utf8' }).trim()
    return `macOS ${product} (${build})`
  } catch {
    return `${os.type()} ${os.release()}`
  }
}

async function writeEvidence(error = null) {
  const manifest = {
    schema: 'hermes-desktop-browser-e0/v1',
    generatedAt: new Date().toISOString(),
    fixture: {
      commit: gitValue(['rev-parse', 'HEAD']),
      dirty: Boolean(gitValue(['status', '--porcelain', '--', 'apps/desktop/electron/fixtures/browser-e0', 'apps/desktop/package.json'])),
      unsafePolicyMode: unsafe,
      devToolsContinuityMutation: mutateDevToolsContinuity
    },
    provenance: {
      host: os.hostname(),
      os: macOSVersion(),
      arch: os.arch(),
      electron: process.versions.electron,
      chromium: process.versions.chrome,
      node: process.versions.node,
      settings: WEBVIEW_SETTINGS,
      profile: 'temporary-disposable',
      network: 'local synthetic origins only',
      sourceContentRetained: false
    },
    verdicts: {
      s1: results.every(row => !row.id.startsWith('S-1/') || row.outcome === 'pass') ? 'OVERLAY_COMPOSITES' : 'INJECTION_REQUIRED',
      s2: aggregateVerdict(results, 'S-2/'),
      s2n: aggregateVerdict(results, 'S-2N/')
    },
    results,
    trace: traces,
    redaction: {
      rawUrls: false,
      filesystemPaths: false,
      canaryContent: false,
      secrets: false,
      retainedFields: 'scenario ids, schemes/reasons, event classes, typed outcomes, versions, settings'
    },
    cleanupComplete,
    error: error ? { name: error.name, message: error.message } : null
  }
  const serialized = `${JSON.stringify(manifest, null, 2)}\n`
  if (outputPath) {
    fs.mkdirSync(path.dirname(outputPath), { recursive: true })
    fs.writeFileSync(outputPath, serialized)
  }
  process.stdout.write(serialized)
}

async function run() {
  const servers = []
  let failure = null
  try {
  process.stderr.write('[browser-e0] waiting for app ready\n')
  await app.whenReady()
  process.stderr.write('[browser-e0] app ready\n')
  protocol.handle('hermes-artifact', () => {
    artifactReads += 1
    return new Response('FORBIDDEN_ARTIFACT_CANARY')
  })
  servers.push(...await createOriginFarm())
  process.stderr.write('[browser-e0] origin farm ready\n')
  const guestCreated = new Promise(resolve => {
    const listener = (_event, contents) => {
      if (contents.getType() === 'webview') {
        app.removeListener('web-contents-created', listener)
        resolve(contents)
      }
    }
    app.on('web-contents-created', listener)
  })
  win = new BrowserWindow({
    show: true,
    width: 700,
    height: 480,
    useContentSize: true,
    webPreferences: { ...WEBVIEW_SETTINGS.hostWebPreferences }
  })
  process.stderr.write('[browser-e0] browser window created\n')
  await win.loadFile(path.join(HERE, 'host.html'))
  process.stderr.write('[browser-e0] host loaded\n')
  await hostEval(`window.e0.load(${JSON.stringify(`${originA}/page`)})`)
  guest = await guestCreated
  await onceWithTimeout(guest, 'did-stop-loading', 12000).catch(() => {})

  record('fixture/settings', guest.session === session.fromPartition(WEBVIEW_SETTINGS.partition), 'real webview uses persist:hermes-preview temporary-profile session')
  await runS1()
  await runS2()
  await runS2N()
  } catch (error) {
    failure = error
  } finally {
    try { if (guest?.debugger?.isAttached()) guest.debugger.detach() } catch { /* best-effort teardown */ }
    try { win?.destroy() } catch { /* best-effort teardown */ }
    for (const server of servers) {
      server.closeAllConnections?.()
      server.close()
    }
    try { fs.rmSync(profileDir, { recursive: true, force: true }); cleanupComplete = !fs.existsSync(profileDir) } catch { /* reported by cleanupComplete */ }
    await writeEvidence(failure)
    app.exit(failure ? 1 : 0)
  }
}

void run()
