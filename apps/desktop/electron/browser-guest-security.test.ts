import { EventEmitter } from 'node:events'

import { describe, expect, it, vi } from 'vitest'

import { digestAnnotationText } from './browser-annotation-reporter'
import {
  ATTACH_PREFIX,
  BROWSER_PARTITION,
  BrowserGuestSecurityController,
  isAllowedBrowserNavigation,
  REPORTER_WORLD_ID
} from './browser-guest-security'

class FakeContents extends EventEmitter {
  id: number
  debugger: EventEmitter & {
    attach: ReturnType<typeof vi.fn>
    isAttached: ReturnType<typeof vi.fn>
    sendCommand: ReturnType<typeof vi.fn>
  }
  session: FakeSession
  closed = false
  loaded: string[] = []
  popupHandler: null | ((details?: { url?: string }) => { action: 'deny' }) = null
  url = 'about:blank'

  constructor(id: number, session: FakeSession) {
    super()
    this.id = id
    this.session = session
    this.debugger = Object.assign(new EventEmitter(), {
      attach: vi.fn(),
      isAttached: vi.fn(() => false),
      sendCommand: vi.fn(async method => {
        if (method === 'Page.getFrameTree') {
          return { frameTree: { frame: { id: 'frame-1' } } }
        }

        if (method === 'Page.createIsolatedWorld') {
          return { executionContextId: 7 }
        }

        if (method === 'Runtime.evaluate') {
          return { result: { objectId: 'reporter-1' } }
        }

        if (method === 'Runtime.callFunctionOn') {
          return { result: { value: { devicePixelRatio: 2, height: 600, width: 800 } } }
        }

        return {}
      })
    })
  }

  close() {
    this.closed = true
  }

  getURL() {
    return this.url
  }

  getTitle() {
    return 'Authenticated account'
  }

  isDestroyed() {
    return this.closed
  }

  isCrashed() {
    return false
  }

  async loadURL(url: string) {
    this.loaded.push(url)
    this.url = url
  }

  setWindowOpenHandler(handler: (details?: { url?: string }) => { action: 'deny' }) {
    this.popupHandler = handler
  }
}

class FakeSession extends EventEmitter {
  permissionRequestHandler = vi.fn()
  permissionCheckHandler = vi.fn()
  devicePermissionHandler = vi.fn()
  beforeRequest = vi.fn()
  completedRequest = vi.fn()
  failedRequest = vi.fn()
  resolveHost = vi.fn(async (hostname: string) => ({
    endpoints: [
      {
        address: hostname === 'localtest.me' ? '127.0.0.1' : '93.184.216.34',
        family: 'ipv4'
      }
    ]
  }))
  webRequest = {
    onBeforeRequest: (handler: unknown) => this.beforeRequest(handler),
    onCompleted: (handler: unknown) => this.completedRequest(handler),
    onErrorOccurred: (handler: unknown) => this.failedRequest(handler)
  }

  setPermissionRequestHandler(handler: unknown) {
    this.permissionRequestHandler(handler)
  }

  setPermissionCheckHandler(handler: unknown) {
    this.permissionCheckHandler(handler)
  }

  setDevicePermissionHandler(handler: unknown) {
    this.devicePermissionHandler(handler)
  }
}

function setup(
  requestPixelConsent?: (prompt: any) => Promise<boolean>,
  options: {
    autoResolveConsent?: boolean
    consentDecision?: 'allow' | 'deny'
    downloadPath?: string | null
    durablePermissionDecision?: 'allow' | 'deny' | null
    handleUploadChooser?: (chooser: any) => Promise<void> | void
    saveAnnotationScreenshot?: (hostId: number, png: Buffer, viewport: unknown, markers: unknown) => Promise<'canceled' | 'saved'>
  } = {}
) {
  const app = new EventEmitter()
  const handlers = new Map<string, (event: { sender: FakeContents }, request: unknown) => unknown>()
  const notifyRetired = vi.fn()
  const notifyFreshSnapshot = vi.fn()
  const notifyConsentResolved = vi.fn()
  const presentedConsents: any[] = []
  const launchExternal = vi.fn(async () => undefined)
  const chooseDownloadDestination = vi.fn(async () => options.downloadPath ?? '/tmp/approved-download')
  const recordTransfer = vi.fn()
  const invalidateAssignedUploads = vi.fn()
  const notifyUploadExpired = vi.fn()
  const durablePermissionDecision = vi.fn(() => options.durablePermissionDecision ?? null)
  const saveAnnotationScreenshot = vi.fn(options.saveAnnotationScreenshot ?? (async () => 'saved' as const))
  const sessions = new Map<string, FakeSession>()

  const sessionFromPartition = (partition: string) => {
    let value = sessions.get(partition)

    if (!value) {
      value = new FakeSession()
      sessions.set(partition, value)
    }

    return value
  }

  const controller = new BrowserGuestSecurityController({
    app: app as never,
    chooseDownloadDestination,
    durablePermissionDecision,
    buildUploadConsent: (chooser, files) => ({
      accept: chooser.accept,
      aggregateSize: files.reduce((sum, file) => sum + file.size, 0),
      destinationOrigin: chooser.origin,
      files: files.map(({ displayName, mimeType, originalDisplayName, size }) => ({
        displayName, mimeType, originalDisplayName, size
      })),
      formActionOrigin: chooser.formActionOrigin,
      formLabel: chooser.formLabel,
      formMethod: chooser.formMethod,
      immediateSubmissionPossible: true,
      inputLabel: chooser.inputLabel,
      mode: chooser.mode,
      source: 'studio-session-artifact'
    }),
    handleUploadChooser: options.handleUploadChooser,
    invalidateAssignedUploads,
    ipcMain: { handle: (channel, handler) => handlers.set(channel, handler as never) } as never,
    launchExternal,
    notifyConsentResolved,
    notifyFreshSnapshot,
    notifyRetired,
    notifyUploadExpired,
    presentConsent: (hostId, prompt) => {
      presentedConsents.push(prompt)

      if (options.autoResolveConsent === false) {return}
      queueMicrotask(() => {
        void handlers.get('hermes:browser-consent:resolve')?.(
          { sender: { id: hostId } as FakeContents },
          { consentId: prompt.consentId, decision: options.consentDecision ?? 'allow' }
        )
      })
    },
    recordTransfer,
    requestPixelConsent,
    saveAnnotationScreenshot,
    sessionFromPartition: sessionFromPartition as never
  })

  controller.install()

  const host = new FakeContents(11, new FakeSession())
  app.emit('web-contents-created', {}, host)
  controller.registerHost(host as never)

  return {
    chooseDownloadDestination,
    controller,
    durablePermissionDecision,
    handlers,
    host,
    invalidateAssignedUploads,
    launchExternal,
    notifyConsentResolved,
    notifyFreshSnapshot,
    notifyRetired,
    notifyUploadExpired,
    presentedConsents,
    recordTransfer,
    saveAnnotationScreenshot,
    sessionFromPartition
  }
}

function uploadInputDebuggerResult(
  method: string,
  params: Record<string, unknown> = {},
  multiple = false,
  overrides: Record<string, unknown> = {}
) {
  if (method === 'Page.createIsolatedWorld') {return { executionContextId: 44 }}
  if (method === 'DOM.resolveNode') {return { object: { objectId: 'input-object' } }}
  if (method === 'DOM.requestNode') {return { nodeId: 22 }}
  if (method === 'DOM.describeNode') {return { node: { backendNodeId: 88 } }}
  if (method === 'Runtime.callFunctionOn') {
    if (params.functionDeclaration === 'function () { return this.form }') {
      return { result: { objectId: 'form-object' } }
    }
    return { result: { value: {
      accept: multiple ? 'image/png,.pdf' : '',
      actionOrigin: 'https://uploads.example.test',
      actionUrl: 'https://uploads.example.test/submit',
      directory: false,
      formId: 'evidence-form',
      formLabel: 'Evidence upload',
      formMethod: 'post',
      formName: 'evidence',
      inputLabel: 'Attach evidence',
      inputName: multiple ? 'evidence' : '',
      multiple,
      ...overrides
    } } }
  }
  return {}
}

async function setupBoundAutomation(
  requestPixelConsent?: (prompt: any) => Promise<boolean>,
  options?: {
    autoResolveConsent?: boolean
    consentDecision?: 'allow' | 'deny'
    downloadPath?: string | null
    durablePermissionDecision?: 'allow' | 'deny' | null
    handleUploadChooser?: (chooser: any) => Promise<void> | void
  }
) {
  const fixture = setup(requestPixelConsent, options)
  const partition = BROWSER_PARTITION

  const prepared = (await fixture.handlers.get('hermes:browser-guest:prepare')!(
    { sender: fixture.host },
    { partition, private: false, profile: 'default', surfaceEpoch: 'surface-pixel', tabId: 'tab-pixel' }
  )) as { attachmentUrl: string; generation: string }

  fixture.host.emit(
    'will-attach-webview',
    { preventDefault: vi.fn() },
    {},
    { partition, src: prepared.attachmentUrl }
  )
  const guest = new FakeContents(91, fixture.sessionFromPartition(partition))
  guest.url = prepared.attachmentUrl
  fixture.host.emit('did-attach-webview', {}, guest)
  await vi.waitFor(() =>
    expect(guest.debugger.sendCommand).toHaveBeenCalledWith('Page.setInterceptFileChooserDialog', { cancel: true, enabled: true })
  )
  expect(guest.debugger.sendCommand).toHaveBeenCalledWith('Target.setAutoAttach', {
    autoAttach: true,
    flatten: true,
    waitForDebuggerOnStart: false
  })
  guest.url = 'https://example.test/account?private=value'

  const binding = {
    guestGeneration: prepared.generation,
    tabId: 'tab-pixel',
    taskGeneration: 7,
    taskId: 'task-pixel'
  }

  expect(await fixture.handlers.get('hermes:browser-guest:bind-automation')!({ sender: fixture.host }, binding)).toMatchObject({ ok: true })
  guest.debugger.sendCommand.mockClear()
  guest.debugger.sendCommand.mockImplementation(async method =>
    method === 'Page.captureScreenshot' ? { data: Buffer.from('private pixels').toString('base64') } : {}
  )

  const route = {
    ...binding,
    bindingGeneration: 11,
    capabilityGeneration: 9,
    connectionId: 'connection-pixel',
    profile: 'default',
    role: 'automation' as const
  }

  const scope = {
    binding_generation: 11,
    capability_generation: 9,
    connection_id: 'connection-pixel',
    document_generation: 3,
    guest_generation: prepared.generation,
    profile: 'default',
    tab_id: 'tab-pixel',
    task_generation: 7,
    task_id: 'task-pixel'
  }

  const purpose = 'Read the visible account status'
  const recipient = 'strict-provider/vision-model'
  const captureParams = { captureBeyondViewport: false, format: 'png', fromSurface: true }

  const requestGrant = (operationId?: string) => fixture.controller.dispatchAutomationCommand({
    ...route,
    ...(operationId ? { operationId } : {}),
    frame: {
      id: 'consent',
      method: 'Hermes.requestPixelConsent',
      params: {
        captureParams,
        maxBytes: 8 * 1024 * 1024,
        purpose,
        recipient,
        retention: 'memory-only-transient',
        scope
      }
    }
  })

  return { ...fixture, binding, captureParams, guest, purpose, recipient, requestGrant, route, scope }
}

async function setupBoundReport(tabId = 'tab-report') {
  const fixture = setup()
  const partition = BROWSER_PARTITION
  const prepared = (await fixture.handlers.get('hermes:browser-guest:prepare')!(
    { sender: fixture.host },
    {
      partition,
      private: false,
      profile: 'default',
      surfaceEpoch: 'surface-report',
      tabId,
      workspaceId: 'workspace-report'
    }
  )) as { attachmentUrl: string; generation: string }

  fixture.host.emit(
    'will-attach-webview',
    { preventDefault: vi.fn() },
    {},
    { partition, src: prepared.attachmentUrl }
  )
  const guest = new FakeContents(120, fixture.sessionFromPartition(partition))
  guest.url = prepared.attachmentUrl
  fixture.host.emit('did-attach-webview', {}, guest)
  await vi.waitFor(() =>
    expect(guest.debugger.sendCommand).toHaveBeenCalledWith('Page.setInterceptFileChooserDialog', { cancel: true, enabled: true })
  )

  return {
    ...fixture,
    guest,
    prepared,
    report: fixture.handlers.get('hermes:browser-guest:report')!,
    resolveAnnotations: fixture.handlers.get('hermes:browser-guest:resolve-annotations')!,
    exportAnnotationScreenshot: fixture.handlers.get('hermes:browser-guest:export-annotation-screenshot')!,
    tabId
  }
}

describe('browser guest security', () => {
  it('exports the app-global partition and denies unsafe navigation', () => {
    expect(BROWSER_PARTITION).toBe('persist:hermes-browser')
    expect(isAllowedBrowserNavigation('https://example.test/path')).toBe(false)

    for (const denied of [
      'file:///etc/passwd',
      'data:text/html,owned',
      'javascript:alert(1)',
      'chrome://settings',
      'http://localhost:8000/secret',
      'http://foo.localhost:8000/secret',
      'http://127.0.0.1:9000/secret',
      'http://127.0.0.2:9000/secret',
      'http://0.0.0.0:9000/secret',
      'http://[::]:9000/secret',
      'http://[::1]:9000/secret',
      'http://[::ffff:127.0.0.1]:9000/secret',
      'http://[::ffff:7f00:1]:9000/secret',
      'custom://authority'
    ]) {
      expect(isAllowedBrowserNavigation(denied), denied).toBe(false)
    }
  })

  it('rejects untrusted hosts and renderer partition aliases', async () => {
    const { handlers, host } = setup()
    const prepare = handlers.get('hermes:browser-guest:prepare')!

    const request = {
      partition: BROWSER_PARTITION,
      private: false,
      profile: 'coding',
      surfaceEpoch: 'surface-1',
      tabId: 'tab-1'
    }

    expect(await prepare({ sender: new FakeContents(99, new FakeSession()) }, request)).toEqual({
      error: 'browser-host-not-authorized',
      ok: false
    })
    expect(await prepare({ sender: host }, { ...request, partition: 'persist:hermes-preview' })).toEqual({
      error: 'browser-partition-mismatch',
      ok: false
    })
    expect(
      await prepare(
        { sender: host },
        { ...request, partition: 'persist:hermes-browser:v1:TikzYIaYz8WsHCfGsGIDLa' }
      )
    ).toEqual({
      error: 'browser-partition-mismatch',
      ok: false
    })
  })

  it('accepts a fresh private reconstruction identity without reusing the retired partition', async () => {
    const { handlers, host } = setup()
    const prepare = handlers.get('hermes:browser-guest:prepare')!

    const original = {
      partition: 'hermes-browser-private:v1:00000000-0000-4000-8000-000000000001',
      private: true,
      profile: 'default',
      surfaceEpoch: 'surface-private-1',
      tabId: 'tab-private-1'
    }

    const prepared = (await prepare({ sender: host }, original)) as { generation: string; ok: boolean }

    expect(prepared.ok).toBe(true)
    await handlers.get('hermes:browser-guest:release')!(
      { sender: host },
      { generation: prepared.generation, tabId: original.tabId }
    )

    const replacement = await prepare(
      { sender: host },
      {
        ...original,
        partition: 'hermes-browser-private:v1:00000000-0000-4000-8000-000000000002',
        surfaceEpoch: 'surface-private-2',
        tabId: 'tab-private-2'
      }
    )

    expect(replacement).toMatchObject({ ok: true })
  })

  it('waits for Electron 40 to expose the attachment URL before binding the guest', async () => {
    const { handlers, host, sessionFromPartition } = setup()
    const partition = BROWSER_PARTITION
    const tabId = 'tab-delayed-url'
    const prepared = (await handlers.get('hermes:browser-guest:prepare')!(
      { sender: host },
      { partition, private: false, profile: 'default', surfaceEpoch: 'surface-delayed', tabId }
    )) as { attachmentUrl: string; generation: string }

    host.emit(
      'will-attach-webview',
      { preventDefault: vi.fn() },
      {},
      { partition, src: prepared.attachmentUrl }
    )

    const guest = new FakeContents(40, sessionFromPartition(partition))
    guest.url = ''
    host.emit('did-attach-webview', {}, guest)

    expect(guest.closed).toBe(false)
    guest.url = prepared.attachmentUrl

    await vi.waitFor(() => expect(guest.debugger.sendCommand).toHaveBeenCalledWith('Page.enable'))
    const activated = await handlers.get('hermes:browser-guest:activate')!(
      { sender: host },
      { generation: prepared.generation, tabId, url: 'https://example.test' }
    )

    expect(activated).toEqual({ ok: true })
    expect(guest.loaded).toEqual(['https://example.test'])
  })

  it('destroys a delayed guest whose initial URL becomes unclaimed', async () => {
    const { handlers, host, sessionFromPartition } = setup()
    const partition = BROWSER_PARTITION
    const prepared = (await handlers.get('hermes:browser-guest:prepare')!(
      { sender: host },
      { partition, private: false, profile: 'default', surfaceEpoch: 'surface-unclaimed', tabId: 'tab-unclaimed' }
    )) as { attachmentUrl: string }

    host.emit(
      'will-attach-webview',
      { preventDefault: vi.fn() },
      {},
      { partition, src: prepared.attachmentUrl }
    )

    const guest = new FakeContents(43, sessionFromPartition(partition))
    guest.url = ''
    host.emit('did-attach-webview', {}, guest)
    guest.url = 'https://unclaimed.example.test'

    await vi.waitFor(() => expect(guest.closed).toBe(true))
    expect(guest.debugger.sendCommand).not.toHaveBeenCalled()
  })

  it('destroys a guest when Electron never exposes its attachment URL', async () => {
    vi.useFakeTimers()

    try {
      const { handlers, host, sessionFromPartition } = setup()
      const partition = BROWSER_PARTITION
      const prepared = (await handlers.get('hermes:browser-guest:prepare')!(
        { sender: host },
        { partition, private: false, profile: 'default', surfaceEpoch: 'surface-timeout', tabId: 'tab-timeout' }
      )) as { attachmentUrl: string }

      host.emit(
        'will-attach-webview',
        { preventDefault: vi.fn() },
        {},
        { partition, src: prepared.attachmentUrl }
      )

      const guest = new FakeContents(44, sessionFromPartition(partition))
      guest.url = ''
      host.emit('did-attach-webview', {}, guest)
      await vi.advanceTimersByTimeAsync(1_010)

      expect(guest.closed).toBe(true)
      expect(guest.debugger.sendCommand).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('correlates out-of-order same-profile guests by exact attachment token', async () => {
    const { handlers, host, sessionFromPartition } = setup()
    const partition = BROWSER_PARTITION
    const prepare = handlers.get('hermes:browser-guest:prepare')!
    const activate = handlers.get('hermes:browser-guest:activate')!
    const prepared = [] as Array<{ attachmentUrl: string; generation: string; tabId: string }>

    for (const tabId of ['tab-a', 'tab-b']) {
      const claim = (await prepare(
        { sender: host },
        { partition, private: false, profile: 'default', surfaceEpoch: 'surface-shared', tabId }
      )) as { attachmentUrl: string; generation: string }

      host.emit(
        'will-attach-webview',
        { preventDefault: vi.fn() },
        {},
        { partition, src: claim.attachmentUrl }
      )
      prepared.push({ ...claim, tabId })
    }

    const guestB = new FakeContents(42, sessionFromPartition(partition))
    guestB.url = prepared[1].attachmentUrl
    host.emit('did-attach-webview', {}, guestB)
    const guestA = new FakeContents(41, sessionFromPartition(partition))
    guestA.url = prepared[0].attachmentUrl
    host.emit('did-attach-webview', {}, guestA)

    await vi.waitFor(() => expect(guestA.debugger.sendCommand).toHaveBeenCalledWith('Page.enable'))
    await vi.waitFor(() => expect(guestB.debugger.sendCommand).toHaveBeenCalledWith('Page.enable'))

    await activate(
      { sender: host },
      { generation: prepared[0].generation, tabId: 'tab-a', url: 'https://a.example.test' }
    )
    await activate(
      { sender: host },
      { generation: prepared[1].generation, tabId: 'tab-b', url: 'https://b.example.test' }
    )

    expect(guestA.loaded).toEqual(['https://a.example.test'])
    expect(guestB.loaded).toEqual(['https://b.example.test'])
  })

  it('binds one claimed tab, pins preferences, denies native authority, and navigates through main', async () => {
    const { handlers, host, sessionFromPartition } = setup()
    const partition = BROWSER_PARTITION
    const prepare = handlers.get('hermes:browser-guest:prepare')!

    const prepared = (await prepare(
      { sender: host },
      {
        partition,
        private: false,
        profile: 'Default',
        surfaceEpoch: 'surface-1',
        tabId: 'tab-1'
      }
    )) as { attachmentUrl: string; generation: string; ok: boolean }

    expect(prepared.ok).toBe(true)
    expect(prepared.attachmentUrl.startsWith(ATTACH_PREFIX)).toBe(true)

    const attachEvent = { preventDefault: vi.fn() }

    const preferences = {
      allowRunningInsecureContent: true,
      contextIsolation: false,
      devTools: true,
      nodeIntegration: true,
      preload: '/hostile.js',
      sandbox: false,
      webSecurity: false
    }

    const params = { name: 'hostile', partition, preload: '/hostile.js', src: prepared.attachmentUrl }
    host.emit('will-attach-webview', attachEvent, preferences, params)

    expect(attachEvent.preventDefault).not.toHaveBeenCalled()
    expect(preferences).toMatchObject({
      allowRunningInsecureContent: false,
      contextIsolation: true,
      devTools: false,
      nodeIntegration: false,
      preload: undefined,
      sandbox: true,
      webSecurity: true
    })
    expect(params.preload).toBeUndefined()
    expect(params.name).toBe('')

    const guestSession = sessionFromPartition(partition)
    const guest = new FakeContents(22, guestSession)
    guest.url = prepared.attachmentUrl
    host.emit('did-attach-webview', {}, guest)

    await vi.waitFor(() => expect(guest.debugger.sendCommand).toHaveBeenCalledWith('Page.setInterceptFileChooserDialog', { cancel: true, enabled: true }))

    const activate = handlers.get('hermes:browser-guest:activate')!
    expect(
      await activate(
        { sender: host },
        { generation: prepared.generation, tabId: 'tab-1', url: 'https://example.test/' }
      )
    ).toEqual({ ok: true })
    expect(guest.loaded).toEqual(['https://example.test/'])
    const activationEvent = { preventDefault: vi.fn() }
    guest.emit('will-navigate', activationEvent, 'https://example.test/')
    expect(activationEvent.preventDefault).not.toHaveBeenCalled()
    const activationRedirect = { preventDefault: vi.fn() }
    guest.emit('will-redirect', activationRedirect, 'https://sub.example.test/continued')
    expect(activationRedirect.preventDefault).not.toHaveBeenCalled()
    expect(
      await activate(
        { sender: host },
        { generation: prepared.generation, tabId: 'tab-1', url: 'http://localtest.me:9000/secret' }
      )
    ).toEqual({ error: 'browser-navigation-denied', ok: false })
    expect(guest.closed).toBe(false)

    const beforeRequest = guestSession.beforeRequest.mock.calls[0][0]
    const requestCallback = vi.fn()
    beforeRequest({ url: 'http://localtest.me:9000/secret' }, requestCallback)
    await vi.waitFor(() => expect(requestCallback).toHaveBeenCalledWith({ cancel: true }))
    const publicRequestCallback = vi.fn()
    beforeRequest({ url: 'https://example.test/resource.js' }, publicRequestCallback)
    await vi.waitFor(() => expect(publicRequestCallback).toHaveBeenCalledWith({ cancel: false }))

    const mainEvent = { preventDefault: vi.fn() }
    const frameEvent = { preventDefault: vi.fn(), url: 'http://10.0.0.8/frame' }
    const redirectEvent = { preventDefault: vi.fn() }
    guest.emit('will-navigate', mainEvent, 'data:text/html,blocked')
    guest.emit('will-frame-navigate', frameEvent)
    guest.emit('will-redirect', redirectEvent, 'https://user:pass@example.test/')
    expect(mainEvent.preventDefault).toHaveBeenCalledOnce()
    expect(frameEvent.preventDefault).toHaveBeenCalledOnce()
    expect(redirectEvent.preventDefault).toHaveBeenCalledOnce()

    expect(guest.popupHandler?.()).toEqual({ action: 'deny' })

    const permissionHandler = guestSession.permissionRequestHandler.mock.calls[0][0]
    const permissionCallback = vi.fn()
    permissionHandler(guest, 'notifications', permissionCallback)
    expect(permissionCallback).toHaveBeenCalledWith(false)

    const downloadEvent = { preventDefault: vi.fn() }
    guestSession.emit('will-download', downloadEvent)
    expect(downloadEvent.preventDefault).toHaveBeenCalledOnce()
  })

  it('reports through a fixed debugger-created isolated world and rejects arbitrary debugger methods', async () => {
    const { controller, handlers, host, sessionFromPartition } = setup()
    const partition = BROWSER_PARTITION

    const prepared = (await handlers.get('hermes:browser-guest:prepare')!(
      { sender: host },
      { partition, private: false, profile: 'default', surfaceEpoch: 'surface-1', tabId: 'tab-1' }
    )) as { attachmentUrl: string; generation: string }

    host.emit(
      'will-attach-webview',
      { preventDefault: vi.fn() },
      {},
      { partition, src: prepared.attachmentUrl }
    )
    const guest = new FakeContents(22, sessionFromPartition(partition))
    guest.url = prepared.attachmentUrl
    host.emit('did-attach-webview', {}, guest)
    await vi.waitFor(() =>
      expect(guest.debugger.sendCommand).toHaveBeenCalledWith('Page.setInterceptFileChooserDialog', { cancel: true, enabled: true })
    )

    const report = await handlers.get('hermes:browser-guest:report')!(
      { sender: host },
      { generation: prepared.generation, kind: 'viewport', tabId: 'tab-1' }
    )

    expect(report).toEqual({
      documentGeneration: 1,
      ok: true,
      value: { devicePixelRatio: 2, height: 600, width: 800 }
    })
    expect(guest.debugger.sendCommand).toHaveBeenCalledWith(
      'Page.createIsolatedWorld',
      expect.objectContaining({ worldName: `hermes-browser-reporter-${REPORTER_WORLD_ID}` })
    )

    await expect(
      controller.sendInternalDebuggerCommand(
        { generation: prepared.generation, hostId: host.id, tabId: 'tab-1' },
        'Browser.getVersion'
      )
    ).rejects.toThrow('browser-debugger-method-denied')
  })

  it('keeps semantic candidates in main while passing only bounded values into the page', async () => {
    const {
      controller,
      exportAnnotationScreenshot,
      guest,
      host,
      prepared,
      report,
      resolveAnnotations,
      saveAnnotationScreenshot,
      tabId
    } = await setupBoundReport('tab-semantic')
    const semanticReport = {
      candidates: [{
        accessibleName: 'Checkout',
        ancestorTags: ['main'],
        attributes: { 'data-testid': 'checkout' },
        rects: [{ height: 24, width: 100, x: 12, y: 40 }],
        role: 'button',
        shadowHostTags: [],
        siblingOrdinal: 1,
        tag: 'button',
        text: 'Buy now',
        visible: true
      }],
      kind: 'semantic-candidates',
      viewport: { devicePixelRatio: 2, height: 600, width: 800 }
    }

    guest.debugger.sendCommand.mockImplementation(async method => {
      if (method === 'Page.getFrameTree') {
        return { frameTree: { frame: {
          id: 'frame-1', name: '', securityOrigin: 'https://example.test', url: 'https://example.test/page'
        } } }
      }
      if (method === 'Page.createIsolatedWorld') {return { executionContextId: 7 }}
      if (method === 'Runtime.evaluate') {return { result: { objectId: 'reporter-1' } }}
      if (method === 'Runtime.callFunctionOn') {return { result: { value: semanticReport } }}
      if (method === 'Page.captureScreenshot') {return { data: Buffer.from('trusted-pixels').toString('base64') }}

      return {}
    })

    const denied = await report(
      { sender: host },
      { documentGeneration: 1, generation: prepared.generation, kind: 'semantic-candidates', tabId, tags: ['button'] }
    )
    const result = await controller.collectSemanticCandidates(
      { generation: prepared.generation, hostId: host.id, tabId },
      1,
      ['button']
    )
    const trusted = await controller.collectTrustedAnnotationReport(
      { generation: prepared.generation, hostId: host.id, tabId },
      1,
      ['button']
    )

    expect(denied).toEqual({ error: 'browser-report-denied', ok: false })
    expect(result).toEqual({ documentGeneration: 1, ok: true, value: semanticReport })
    expect(trusted).toMatchObject({
      documentGeneration: 1,
      ok: true,
      value: {
        candidates: [{
          fingerprint: {
            accessibleNameDigest: expect.stringMatching(/^[0-9a-f]{64}$/),
            stableAttributes: { 'data-testid': expect.stringMatching(/^[0-9a-f]{64}$/) },
            textDigest: expect.stringMatching(/^[0-9a-f]{64}$/)
          },
          frameId: 'frame-1'
        }]
      }
    })
    expect(JSON.stringify(trusted)).not.toContain('Checkout')
    expect(JSON.stringify(trusted)).not.toContain('Buy now')
    const call = guest.debugger.sendCommand.mock.calls.find(([method]) => method === 'Runtime.callFunctionOn')
    expect(call?.[1]).toMatchObject({ arguments: [{ value: { kind: 'semantic-candidates', tags: ['button'] } }] })
    expect(JSON.stringify(call?.[1])).not.toContain(prepared.generation)
    expect(JSON.stringify(call?.[1])).not.toContain(tabId)

    const anchorFingerprint = {
      accessibleNameDigest: digestAnnotationText('Checkout'),
      ancestorDigests: [digestAnnotationText('main')],
      role: 'button',
      shadowHostDigests: [],
      siblingOrdinal: 1,
      stableAttributes: { 'data-testid': digestAnnotationText('checkout') },
      tag: 'button',
      textDigest: digestAnnotationText('Buy now')
    }
    const annotation = {
      annotationId: 'annotation-1',
      anchor: {
        fingerprint: anchorFingerprint,
        framePath: [{
          committedUrlEvidence: 'https://example.test/page',
          embeddingElementDigest: null,
          frameIdHint: 'frame-1',
          frameName: '',
          opaqueOrigin: false,
          origin: 'https://example.test'
        }],
        type: 'element'
      },
      capture: { observedTargetRects: [{ height: 24, width: 100, x: 12, y: 40 }] },
      schemaVersion: 1,
      scope: {
        browserWorkspaceId: 'workspace-report',
        documentGenerationId: '1',
        profileId: 'default',
        tabId
      }
    }
    const projection = await resolveAnnotations(
      { sender: host },
      { generation: prepared.generation, records: [annotation], tabId, workspaceId: 'workspace-report' }
    )
    expect(projection).toEqual({
      documentGeneration: 1,
      ok: true,
      projections: [{ annotationId: 'annotation-1', externalLabel: 1, health: 'resolved' }]
    })
    expect(JSON.stringify(projection)).not.toContain('https://')
    expect(JSON.stringify(projection)).not.toContain('Checkout')
    const exported = await exportAnnotationScreenshot(
      { sender: host },
      { generation: prepared.generation, records: [annotation], tabId, workspaceId: 'workspace-report' }
    )
    expect(exported).toEqual({ canceled: false, ok: true })
    expect(saveAnnotationScreenshot).toHaveBeenCalledWith(
      host.id,
      expect.any(Buffer),
      { height: 600, width: 800 },
      [{ externalLabel: 1, rects: [{ height: 24, width: 100, x: 12, y: 40 }] }]
    )
    expect(JSON.stringify(exported)).not.toContain('trusted-pixels')
    await expect(resolveAnnotations(
      { sender: host },
      { generation: prepared.generation, records: [annotation], tabId, workspaceId: 'other' }
    )).resolves.toEqual({ error: 'browser-annotation-scope-denied', ok: false })
  })

  it('refuses semantic reports with stale document generations after main-frame committed navigation', async () => {
    const { controller, guest, host, prepared, tabId } = await setupBoundReport('tab-stale-report')

    guest.emit('did-frame-navigate', {}, 'https://example.test/after', 200, 'OK', true)

    expect(
      await controller.collectSemanticCandidates(
        { generation: prepared.generation, hostId: host.id, tabId },
        1,
        ['button']
      )
    ).toEqual({ error: 'browser-report-stale', ok: false })
    expect(guest.debugger.sendCommand).not.toHaveBeenCalledWith('Runtime.callFunctionOn', expect.anything())
  })

  it('rejects in-flight report results when the exact guest is retired before collection returns', async () => {
    const { controller, guest, handlers, host, prepared, tabId } = await setupBoundReport('tab-retired-report')
    let finish!: (value: unknown) => void

    guest.debugger.sendCommand.mockImplementation(async method => {
      if (method === 'Page.getFrameTree') {return { frameTree: { frame: { id: 'frame-1' } } }}
      if (method === 'Page.createIsolatedWorld') {return { executionContextId: 7 }}
      if (method === 'Runtime.evaluate') {return { result: { objectId: 'reporter-1' } }}
      if (method === 'Runtime.callFunctionOn') {
        return new Promise(resolve => {
          finish = resolve
        })
      }

      return {}
    })

    const pending = controller.collectSemanticCandidates(
      { generation: prepared.generation, hostId: host.id, tabId },
      1,
      ['button']
    )

    await vi.waitFor(() => expect(guest.debugger.sendCommand).toHaveBeenCalledWith('Runtime.callFunctionOn', expect.anything()))
    await handlers.get('hermes:browser-guest:release')!({ sender: host }, { generation: prepared.generation, tabId })
    finish({
      result: {
        value: {
          candidates: [],
          kind: 'semantic-candidates',
          viewport: { devicePixelRatio: 2, height: 600, width: 800 }
        }
      }
    })

    expect(await pending).toEqual({ error: 'browser-report-stale', ok: false })
  })

  it('enforces a per-binding report in-flight bound before dispatching another isolated-world call', async () => {
    const { guest, host, prepared, report, tabId } = await setupBoundReport('tab-report-bound')
    const releases: Array<(value: unknown) => void> = []

    guest.debugger.sendCommand.mockImplementation(async method => {
      if (method === 'Page.getFrameTree') {return { frameTree: { frame: { id: 'frame-1' } } }}
      if (method === 'Page.createIsolatedWorld') {return { executionContextId: 7 }}
      if (method === 'Runtime.evaluate') {return { result: { objectId: `reporter-${releases.length + 1}` } }}
      if (method === 'Runtime.callFunctionOn') {
        return new Promise(resolve => releases.push(resolve))
      }

      return {}
    })

    const first = report({ sender: host }, { generation: prepared.generation, kind: 'viewport', tabId })
    const second = report({ sender: host }, { generation: prepared.generation, kind: 'viewport', tabId })

    await vi.waitFor(() => expect(releases).toHaveLength(2))
    expect(await report({ sender: host }, { generation: prepared.generation, kind: 'viewport', tabId })).toEqual({
      error: 'browser-report-rate-limited',
      ok: false
    })

    releases.forEach(resolve => resolve({ result: { value: { devicePixelRatio: 2, height: 600, width: 800 } } }))
    await expect(Promise.all([first, second])).resolves.toEqual([
      { documentGeneration: 1, ok: true, value: { devicePixelRatio: 2, height: 600, width: 800 } },
      { documentGeneration: 1, ok: true, value: { devicePixelRatio: 2, height: 600, width: 800 } }
    ])
  })

  it('enforces a per-binding report rate window after completed collections', async () => {
    const { guest, host, prepared, report, tabId } = await setupBoundReport('tab-report-rate')

    guest.debugger.sendCommand.mockClear()

    for (let index = 0; index < 12; index += 1) {
      expect(await report({ sender: host }, { generation: prepared.generation, kind: 'viewport', tabId })).toEqual({
        documentGeneration: 1,
        ok: true,
        value: { devicePixelRatio: 2, height: 600, width: 800 }
      })
    }

    expect(await report({ sender: host }, { generation: prepared.generation, kind: 'viewport', tabId })).toEqual({
      error: 'browser-report-rate-limited',
      ok: false
    })
    expect(guest.debugger.sendCommand.mock.calls.filter(([method]) => method === 'Runtime.callFunctionOn')).toHaveLength(12)
  })

  it('keeps the binding alive when a newer activation supersedes an aborted load', async () => {
    const { handlers, host, sessionFromPartition } = setup()
    const partition = BROWSER_PARTITION

    const request = {
      partition,
      private: false,
      profile: 'coding',
      surfaceEpoch: 'surface-concurrent',
      tabId: 'tab-concurrent'
    }

    const prepared = (await handlers.get('hermes:browser-guest:prepare')!({ sender: host }, request)) as {
      attachmentUrl: string
      generation: string
    }

    const params = { partition, src: prepared.attachmentUrl }
    host.emit('will-attach-webview', { preventDefault: vi.fn() }, {}, params)

    const guest = new FakeContents(23, sessionFromPartition(partition))
    guest.url = prepared.attachmentUrl
    let rejectFirst!: (error: unknown) => void
    let resolveSecond!: () => void

    const load = vi
      .spyOn(guest, 'loadURL')
      .mockImplementationOnce(() => new Promise((_resolve, reject) => (rejectFirst = reject)))
      .mockImplementationOnce(() => new Promise(resolve => (resolveSecond = resolve)))

    host.emit('did-attach-webview', {}, guest)
    await vi.waitFor(() =>
      expect(guest.debugger.sendCommand).toHaveBeenCalledWith('Page.setInterceptFileChooserDialog', { cancel: true, enabled: true })
    )

    const activate = handlers.get('hermes:browser-guest:activate')!

    const first = activate(
      { sender: host },
      { generation: prepared.generation, tabId: request.tabId, url: 'https://slow.example.test' }
    ) as Promise<unknown>

    await vi.waitFor(() => expect(load).toHaveBeenCalledTimes(1))

    const second = activate(
      { sender: host },
      { generation: prepared.generation, tabId: request.tabId, url: 'https://fast.example.test' }
    ) as Promise<unknown>

    await vi.waitFor(() => expect(load).toHaveBeenCalledTimes(2))
    rejectFirst(Object.assign(new Error('aborted'), { code: 'ERR_ABORTED' }))
    resolveSecond()

    expect(await Promise.all([first, second])).toEqual([
      { ok: true, superseded: true },
      { ok: true }
    ])
    expect(guest.closed).toBe(false)
  })

  it('fences automation dispatch, enforces method arguments before debugger, and forwards events', async () => {
    const { controller, handlers, host, sessionFromPartition } = setup()
    const partition = BROWSER_PARTITION

    const prepared = (await handlers.get('hermes:browser-guest:prepare')!(
      { sender: host },
      { partition, private: false, profile: 'default', surfaceEpoch: 'surface-cdp', tabId: 'tab-cdp' }
    )) as { attachmentUrl: string; generation: string }

    host.emit(
      'will-attach-webview',
      { preventDefault: vi.fn() },
      {},
      { partition, src: prepared.attachmentUrl }
    )
    const guest = new FakeContents(31, sessionFromPartition(partition))
    guest.url = prepared.attachmentUrl
    host.emit('did-attach-webview', {}, guest)
    await vi.waitFor(() =>
      expect(guest.debugger.sendCommand).toHaveBeenCalledWith('Page.setInterceptFileChooserDialog', { cancel: true, enabled: true })
    )

    const binding = {
      guestGeneration: prepared.generation,
      tabId: 'tab-cdp',
      taskGeneration: 4,
      taskId: 'task-cdp'
    }

    expect(await handlers.get('hermes:browser-guest:bind-automation')!({ sender: host }, binding)).toEqual({
      ok: true,
      roles: ['automation', 'raw-cdp']
    })
    guest.debugger.sendCommand.mockClear()

    const allowed = await controller.dispatchAutomationCommand({
      ...binding,
      operationId: 'operation-navigation-allowed',
      role: 'automation',
      frame: { id: 8, method: 'Page.navigate', params: { url: 'https://example.test/allowed' } }
    })

    expect(allowed).toEqual({ id: 8, result: {} })
    expect(guest.debugger.sendCommand).toHaveBeenCalledExactlyOnceWith('Page.navigate', {
      url: 'https://example.test/allowed'
    })

    guest.debugger.sendCommand.mockClear()

    guest.debugger.sendCommand.mockImplementation(() => new Promise(() => undefined))

    // The mock never resolves, so any positive deadline forces the timeout
    // path. Use a deadline comfortably larger than the pre-dispatch consent/
    // policy async work (a 1ms deadline races that work under parallel CPU load
    // and can expire before dispatch is reached, yielding 0 calls instead of 1).
    const expired = await controller.dispatchAutomationCommand({
      ...binding,
      remainingDurationMs: 50,
      role: 'automation',
      frame: { id: 80, method: 'Page.getFrameTree', params: {} }
    })

    expect(expired).toMatchObject({
      id: 80,
      error: { data: { disposition: 'outcome_unknown', hermesCode: 'BROWSER_OUTCOME_UNKNOWN' } }
    })
    expect(guest.debugger.sendCommand).toHaveBeenCalledExactlyOnceWith('Page.getFrameTree', {})
    guest.debugger.sendCommand.mockReset()

    const captureDenied = await controller.dispatchAutomationCommand({
      ...binding,
      role: 'automation',
      frame: { id: 82, method: 'Page.captureScreenshot', params: { format: 'png' } }
    })

    expect(captureDenied).toMatchObject({
      id: 82,
      error: { data: { disposition: 'not_started', hermesCode: 'CAPTURE_CONSENT_REQUIRED' } }
    })
    expect(guest.debugger.sendCommand).not.toHaveBeenCalled()

    const rawEvaluation = await controller.dispatchAutomationCommand({
      ...binding,
      role: 'raw-cdp',
      frame: { id: 81, method: 'Runtime.evaluate', params: { expression: 'document.cookie' } }
    })

    expect(rawEvaluation).toMatchObject({
      id: 81,
      error: { data: { disposition: 'not_started', hermesCode: 'RAW_CDP_METHOD_BLOCKED' } }
    })

    guest.debugger.sendCommand.mockClear()

    const blocked = await controller.dispatchAutomationCommand({
      ...binding,
      role: 'automation',
      frame: {
        id: 9,
        method: 'Page.navigate',
        params: { url: 'file:///etc/passwd', unexpected: true }
      }
    })

    expect(blocked).toMatchObject({
      id: 9,
      error: { data: { disposition: 'not_started', hermesCode: 'NAVIGATION_POLICY_BLOCKED' } }
    })
    expect(guest.debugger.sendCommand).not.toHaveBeenCalled()

    const stale = await controller.dispatchAutomationCommand({
      ...binding,
      taskGeneration: 3,
      role: 'automation',
      frame: { id: 10, method: 'Page.getFrameTree', params: {} }
    })

    expect(stale).toMatchObject({
      id: 10,
      error: { data: { disposition: 'not_started', hermesCode: 'NAVIGATION_TARGET_STALE' } }
    })
    expect(guest.debugger.sendCommand).not.toHaveBeenCalled()

    const frameSink = vi.fn()
    controller.setFrameSink(frameSink)
    guest.debugger.emit('message', {}, 'Page.loadEventFired', { timestamp: 2 })
    expect(frameSink).toHaveBeenCalledWith({
      ...binding,
      role: 'automation',
      frame: { method: 'Page.loadEventFired', params: { timestamp: 2 } }
    })

    frameSink.mockClear()
    const navigationEvent = { frame: { id: 'frame-safe', url: 'https://example.test/committed' } }

    guest.debugger.emit('message', {}, 'Page.frameNavigated', navigationEvent)
    await vi.waitFor(() => expect(frameSink).toHaveBeenCalledWith({
      ...binding,
      role: 'automation',
      frame: { method: 'Page.frameNavigated', params: navigationEvent }
    }))
    expect(await handlers.get('hermes:browser-guest:unbind-automation')!({ sender: host }, binding)).toEqual({
      ok: true
    })
    expect(await handlers.get('hermes:browser-guest:bind-automation')!({ sender: host }, binding)).toEqual({
      error: 'browser-automation-generation-stale',
      ok: false
    })
  })

  it('reclassifies every redirect hop and never widens a one-chain admission across site, scheme, or new hard signals', async () => {
    const fixture = await setupBoundAutomation()
    const navigation = await fixture.controller.dispatchAutomationCommand({
      ...fixture.route,
      operationId: 'redirect-chain-navigation',
      frame: { id: 'navigate', method: 'Page.navigate', params: { url: 'https://shop.example.test/start' } }
    })
    expect(navigation).toMatchObject({ result: {} })

    const sameSite = { preventDefault: vi.fn() }
    fixture.guest.emit('will-redirect', sameSite, 'https://checkout.example.test/continue')
    expect(sameSite.preventDefault).not.toHaveBeenCalled()

    const hardEscalation = { preventDefault: vi.fn() }
    fixture.guest.emit('will-redirect', hardEscalation, 'https://user:secret@checkout.example.test/continue')
    expect(hardEscalation.preventDefault).toHaveBeenCalledOnce()

    const crossSite = { preventDefault: vi.fn() }
    fixture.guest.emit('will-redirect', crossSite, 'https://other.example/continue')
    expect(crossSite.preventDefault).toHaveBeenCalledOnce()

    const changedScheme = { preventDefault: vi.fn() }
    fixture.guest.emit('will-redirect', changedScheme, 'http://checkout.example.test/continue')
    expect(changedScheme.preventDefault).toHaveBeenCalledOnce()
  })

  it('gates debugger actions and fails future upload assignment without an exact staged handle', async () => {
    const fixture = await setupBoundAutomation()
    fixture.guest.debugger.sendCommand.mockClear()

    const click = await fixture.controller.dispatchAutomationCommand({
      ...fixture.route,
      operationId: 'operation-destructive-click',
      frame: {
        id: 'click',
        method: 'Input.dispatchMouseEvent',
        params: { button: 'left', type: 'mousePressed', x: 12, y: 14 }
      }
    })

    expect(click).toEqual({ id: 'click', result: {} })
    expect(fixture.presentedConsents.at(-1)).toMatchObject({
      category: 'destructive-action',
      guestGeneration: fixture.binding.guestGeneration,
      operationId: 'operation-destructive-click',
      profile: 'default',
      tabId: fixture.binding.tabId,
      taskGeneration: fixture.binding.taskGeneration,
      taskId: fixture.binding.taskId
    })

    const submit = await fixture.controller.dispatchAutomationCommand({
      ...fixture.route,
      operationId: 'operation-submit-enter',
      frame: {
        id: 'submit',
        method: 'Input.dispatchKeyEvent',
        params: { key: 'Enter', type: 'keyDown' }
      }
    })

    expect(submit).toEqual({ id: 'submit', result: {} })
    expect(fixture.presentedConsents.at(-1)).toMatchObject({
      category: 'website-submission',
      operationId: 'operation-submit-enter'
    })

    const inserted = await fixture.controller.dispatchAutomationCommand({
      ...fixture.route,
      operationId: 'operation-insert-text',
      frame: { id: 'insert', method: 'Input.insertText', params: { text: 'sensitive value' } }
    })

    expect(inserted).toEqual({ id: 'insert', result: {} })
    expect(fixture.presentedConsents.at(-1)).toMatchObject({
      category: 'destructive-action',
      operationId: 'operation-insert-text'
    })

    const released = await fixture.controller.dispatchAutomationCommand({
      ...fixture.route,
      operationId: 'operation-mouse-release',
      frame: {
        id: 'release',
        method: 'Input.dispatchMouseEvent',
        params: { button: 'left', type: 'mouseReleased', x: 12, y: 14 }
      }
    })

    expect(released).toEqual({ id: 'release', result: {} })
    expect(fixture.presentedConsents.at(-1)).toMatchObject({
      category: 'destructive-action',
      operationId: 'operation-mouse-release'
    })

    const upload = await fixture.controller.dispatchAutomationCommand({
      ...fixture.route,
      operationId: 'operation-upload',
      frame: {
        id: 'upload',
        method: 'DOM.setFileInputFiles',
        params: { files: ['/tmp/not-a-staged-handle'] }
      }
    })

    expect(upload).toMatchObject({
      error: { data: { disposition: 'not_started', hermesCode: 'UPLOAD_UNSUPPORTED' } },
      id: 'upload'
    })
  })

  it.each([
    ['dispatchEvent click', "button.dispatchEvent(new MouseEvent('click', { bubbles: true }))"],
    ['submit.call', 'HTMLFormElement.prototype.submit.call(form)'],
    ['sendBeacon', "navigator.sendBeacon('/collect', secret)"],
    ['Image.src exfiltration', "new Image().src = '/collect?secret=' + token"],
    ['computed fetch', "globalThis['fe' + 'tch']('/submit', { method: 'POST' })"],
    ['checked assignment', 'checkbox.checked = true']
  ])('fails closed for Runtime.evaluate %s', async (_label, expression) => {
    const fixture = await setupBoundAutomation(undefined, { consentDecision: 'deny' })
    fixture.guest.debugger.sendCommand.mockClear()

    const result = await fixture.controller.dispatchAutomationCommand({
      ...fixture.route,
      operationId: 'operation-runtime-fail-closed',
      frame: { id: expression, method: 'Runtime.evaluate', params: { expression } }
    })

    expect(result).toMatchObject({
      id: expression,
      error: { data: { disposition: 'not_started', hermesCode: 'CONSENT_REQUIRED' } }
    })
    expect(fixture.presentedConsents).toHaveLength(1)
    expect(fixture.presentedConsents[0]).toMatchObject({ category: 'destructive-action' })
    expect(fixture.guest.debugger.sendCommand).not.toHaveBeenCalled()
  })

  it('dismisses an exact pending consent on takeover and rejects a late allow', async () => {
    const fixture = await setupBoundAutomation(undefined, { autoResolveConsent: false })

    fixture.guest.debugger.sendCommand.mockClear()

    const dispatch = fixture.controller.dispatchAutomationCommand({
      ...fixture.route,
      operationId: 'operation-revoked-before-input',
      frame: { id: 'pending-input', method: 'Input.insertText', params: { text: 'never sent' } }
    })

    await vi.waitFor(() => expect(fixture.presentedConsents).toHaveLength(1))
    const consentId = fixture.presentedConsents[0].consentId

    expect(
      await fixture.handlers.get('hermes:browser-guest:revoke-local')!({ sender: fixture.host }, fixture.binding)
    ).toEqual({ ok: true, retired: true })
    expect(fixture.notifyConsentResolved).toHaveBeenCalledExactlyOnceWith({
      consentId,
      hostId: fixture.host.id,
      reason: 'revoked'
    })
    expect(
      await fixture.handlers.get('hermes:browser-consent:resolve')!(
        { sender: fixture.host },
        { consentId, decision: 'allow' }
      )
    ).toEqual({ error: 'browser-consent-stale', ok: false })
    await expect(dispatch).resolves.toMatchObject({
      error: { data: { disposition: 'not_started', hermesCode: 'CONSENT_REQUIRED' } }
    })
    expect(fixture.guest.debugger.sendCommand).not.toHaveBeenCalled()
  })

  it('keeps task-bound native permission and download authority behind exact consent even with a durable grant', async () => {
    const fixture = await setupBoundAutomation(undefined, { durablePermissionDecision: 'allow' })
    const permissionCallback = vi.fn()
    const permissionHandler = fixture.guest.session.permissionRequestHandler.mock.calls[0][0]
    permissionHandler(fixture.guest, 'notifications', permissionCallback, { requestingUrl: 'https://frame.example/request' })
    await vi.waitFor(() => expect(permissionCallback).toHaveBeenCalledWith(true))
    expect(fixture.durablePermissionDecision).toHaveBeenCalledWith('default', 'https://frame.example', 'notifications')
    expect(fixture.presentedConsents.at(-1)).toMatchObject({
      category: 'permission',
      permission: 'notifications',
      site: 'https://example.test'
    })

    const item = Object.assign(new EventEmitter(), {
      cancel: vi.fn(),
      getFilename: vi.fn(() => 'report.pdf'),
      getTotalBytes: vi.fn(() => 42),
      pause: vi.fn(),
      resume: vi.fn(),
      setSavePath: vi.fn()
    })

    fixture.guest.session.emit('will-download', { preventDefault: vi.fn() }, item, fixture.guest)
    await vi.waitFor(() => expect(item.resume).toHaveBeenCalledTimes(1))
    expect(item.pause).toHaveBeenCalledTimes(1)
    expect(item.setSavePath).toHaveBeenCalledWith('/tmp/approved-download')
    expect(fixture.presentedConsents.at(-1)).toMatchObject({ category: 'download', filename: 'report.pdf' })
    fixture.guest.url = 'https://navigated.example/after-download'
    fixture.guest.close()
    item.emit('done', {}, 'completed')
    expect(fixture.recordTransfer).toHaveBeenCalledWith('default', expect.objectContaining({
      origin: 'https://example.test', outcome: 'completed', redactedName: 'report.pdf'
    }))

    expect(fixture.guest.popupHandler?.({ url: 'mailto:person@example.com' })).toEqual({ action: 'deny' })
    await vi.waitFor(() => expect(fixture.launchExternal).toHaveBeenCalledWith('mailto:person@example.com'))
    expect(fixture.presentedConsents.at(-1)).toMatchObject({
      category: 'external-handler',
      scheme: 'mailto',
      site: 'mailto:'
    })
  })

  it('keeps a handed-back generation read-only until a fresh accessibility snapshot completes', async () => {
    const fixture = await setupBoundAutomation()

    expect(
      await fixture.handlers.get('hermes:browser-guest:revoke-local')!({ sender: fixture.host }, fixture.binding)
    ).toEqual({ ok: true, retired: true })

    const successor = {
      ...fixture.binding,
      requireFreshSnapshot: true,
      taskGeneration: fixture.binding.taskGeneration + 1
    }

    expect(
      await fixture.handlers.get('hermes:browser-guest:bind-automation')!({ sender: fixture.host }, successor)
    ).toMatchObject({ ok: true })
    fixture.guest.debugger.sendCommand.mockImplementation(async method =>
      method === 'Accessibility.getFullAXTree' ? { nodes: [] } : {}
    )

    const dispatch = (role: 'automation' | 'raw-cdp', method: string) =>
      fixture.controller.dispatchAutomationCommand({
        ...fixture.route,
        ...successor,
        ...(method === 'Page.navigate' ? { operationId: 'hand-back-navigation' } : {}),
        role,
        frame: { id: method, method, params: method === 'Page.navigate' ? { url: 'https://example.test/next' } : {} }
      })

    await expect(dispatch('automation', 'Page.navigate')).resolves.toMatchObject({
      error: { data: { disposition: 'not_started', hermesCode: 'HAND_BACK_SNAPSHOT_REQUIRED' } }
    })
    await expect(dispatch('raw-cdp', 'Page.getFrameTree')).resolves.toMatchObject({
      error: { data: { disposition: 'not_started', hermesCode: 'HAND_BACK_SNAPSHOT_REQUIRED' } }
    })
    expect(fixture.guest.debugger.sendCommand).not.toHaveBeenCalled()

    await expect(dispatch('automation', 'Page.getFrameTree')).resolves.toMatchObject({ result: {} })
    expect(fixture.notifyFreshSnapshot).not.toHaveBeenCalled()
    await expect(dispatch('automation', 'Accessibility.getFullAXTree')).resolves.toMatchObject({ result: { nodes: [] } })
    expect(fixture.notifyFreshSnapshot).toHaveBeenCalledWith({
      guestGeneration: successor.guestGeneration,
      hostId: fixture.host.id,
      surfaceEpoch: 'surface-pixel',
      tabId: successor.tabId,
      taskGeneration: successor.taskGeneration,
      taskId: successor.taskId
    })
    await expect(dispatch('automation', 'Page.navigate')).resolves.toMatchObject({ result: {} })
  })

  it('retires exact local authority before acknowledgement and stops only the exact guest generation', async () => {
    const fixture = await setupBoundAutomation()
    const lifecycle = vi.fn()
    fixture.controller.setTaskLifecycleSink(lifecycle)

    expect(
      await fixture.handlers.get('hermes:browser-guest:revoke-local')!({ sender: fixture.host }, fixture.binding)
    ).toEqual({ ok: true, retired: true })
    expect(lifecycle).toHaveBeenCalledWith({ ...fixture.binding, profile: 'default', type: 'unbind' })
    expect(
      await fixture.controller.dispatchAutomationCommand({
        ...fixture.route,
        frame: { id: 'after-revoke', method: 'Page.getFrameTree', params: {} }
      })
    ).toMatchObject({
      error: { data: { disposition: 'not_started', hermesCode: 'NAVIGATION_TARGET_STALE' } }
    })
    expect(
      await fixture.handlers.get('hermes:browser-guest:revoke-local')!({ sender: fixture.host }, fixture.binding)
    ).toEqual({ ok: true, retired: false })

    const next = { ...fixture.binding, taskGeneration: fixture.binding.taskGeneration + 1 }
    expect(await fixture.handlers.get('hermes:browser-guest:bind-automation')!({ sender: fixture.host }, next)).toMatchObject({ ok: true })
    expect(
      await fixture.handlers.get('hermes:browser-guest:stop-and-close')!({ sender: fixture.host }, fixture.binding)
    ).toEqual({ ok: true, retired: false })
    expect(fixture.guest.closed).toBe(false)
    expect(
      await fixture.handlers.get('hermes:browser-guest:stop-and-close')!({ sender: fixture.host }, next)
    ).toEqual({ ok: true, retired: true })
    expect(fixture.guest.closed).toBe(true)
  })

  it.each([
    ['did-start-navigation', ['http://localtest.me/private']],
    ['did-frame-navigate', ['http://10.0.0.9/frame']],
    ['debugger-frame', []]
  ])('retires the complete binding when %s observes a forbidden committed destination', async (source, eventArgs) => {
    const { controller, handlers, host, sessionFromPartition } = setup()
    const partition = BROWSER_PARTITION
    const tabId = `tab-${source}`

    const prepared = (await handlers.get('hermes:browser-guest:prepare')!(
      { sender: host },
      { partition, private: false, profile: `post-${source}`, surfaceEpoch: 'surface-post', tabId }
    )) as { attachmentUrl: string; generation: string }

    host.emit(
      'will-attach-webview',
      { preventDefault: vi.fn() },
      {},
      { partition, src: prepared.attachmentUrl }
    )
    const guest = new FakeContents(80 + String(source).length, sessionFromPartition(partition))
    guest.url = prepared.attachmentUrl
    host.emit('did-attach-webview', {}, guest)
    await vi.waitFor(() =>
      expect(guest.debugger.sendCommand).toHaveBeenCalledWith('Page.setInterceptFileChooserDialog', { cancel: true, enabled: true })
    )

    const binding = {
      guestGeneration: prepared.generation,
      tabId,
      taskGeneration: 1,
      taskId: `task-${source}`
    }

    expect(await handlers.get('hermes:browser-guest:bind-automation')!({ sender: host }, binding)).toMatchObject({ ok: true })
    const frameSink = vi.fn()

    controller.setFrameSink(frameSink)

    if (source === 'debugger-frame') {
      guest.debugger.emit('message', {}, 'Page.frameNavigated', { frame: { id: 'bad', url: 'file:///etc/passwd' } })
    } else {
      guest.emit(source, {}, ...eventArgs)
    }

    await vi.waitFor(() => expect(guest.closed).toBe(true))
    expect(frameSink).not.toHaveBeenCalled()
    await expect(controller.dispatchAutomationCommand({
      ...binding,
      role: 'automation',
      frame: { id: 1, method: 'Page.getFrameTree', params: {} }
    })).resolves.toMatchObject({
      error: { data: { disposition: 'not_started', hermesCode: 'NAVIGATION_TARGET_STALE' } },
      id: 1
    })
  })

  it('discloses exact viewport document scope and memory-only retention to trusted chrome', async () => {
    const fixture = await setupBoundAutomation()
    const granted = await fixture.requestGrant('operation-pixel-disclosure')

    expect(granted).toMatchObject({ result: { granted: true } })
    expect(fixture.presentedConsents.at(-1)).toMatchObject({
      captureScope: 'viewport',
      category: 'outbound-pixels',
      documentGeneration: fixture.scope.document_generation,
      operationId: 'operation-pixel-disclosure',
      purpose: fixture.purpose,
      recipient: fixture.recipient,
      retention: 'memory-only-transient',
      site: 'https://example.test'
    })
  })

  it('mints in trusted chrome and atomically consumes exactly one transient viewport grant', async () => {
    const consent = vi.fn(async () => true)
    const fixture = await setupBoundAutomation(consent)
    const granted = await fixture.requestGrant()
    const grantId = (granted.result as { grantId: string }).grantId

    expect(consent).toHaveBeenCalledWith(expect.objectContaining({
      captureKind: 'viewport-screenshot',
      origin: 'https://example.test',
      purpose: fixture.purpose,
      recipient: fixture.recipient,
      retention: 'memory-only-transient',
      scope: fixture.scope,
      tabTitle: 'Authenticated account'
    }))
    expect(grantId).toMatch(/^[A-Za-z0-9_-]{43}$/)

    const capture = {
      ...fixture.route,
      frame: {
        id: 'capture',
        method: 'Page.captureScreenshot',
        params: {
          ...fixture.captureParams,
          __hermesPixelConsent: {
            grantId,
            purpose: fixture.purpose,
            recipient: fixture.recipient,
            scope: fixture.scope
          }
        }
      }
    }

    const [first, racedReplay] = await Promise.all([
      fixture.controller.dispatchAutomationCommand(capture),
      fixture.controller.dispatchAutomationCommand(capture)
    ])

    expect([first, racedReplay].filter(row => 'result' in row)).toHaveLength(1)
    expect([first, racedReplay].filter(row => 'error' in row)[0]).toMatchObject({
      error: { data: { disposition: 'not_started', hermesCode: 'CAPTURE_CONSENT_REQUIRED' } }
    })
    expect(fixture.guest.debugger.sendCommand).toHaveBeenCalledExactlyOnceWith(
      'Page.captureScreenshot',
      fixture.captureParams
    )

    const replay = await fixture.controller.dispatchAutomationCommand(capture)
    expect(replay).toMatchObject({ error: { data: { hermesCode: 'CAPTURE_CONSENT_REQUIRED' } } })
    expect(fixture.guest.debugger.sendCommand).toHaveBeenCalledTimes(1)
  })

  it('does not let raw CDP, omitted trusted chrome, or forged params mint a grant', async () => {
    const prompt = vi.fn(async () => true)
    const fixture = await setupBoundAutomation(prompt)

    const rawRequest = await fixture.controller.dispatchAutomationCommand({
      ...fixture.route,
      role: 'raw-cdp',
      frame: {
        id: 'raw-consent',
        method: 'Hermes.requestPixelConsent',
        params: {
          captureParams: fixture.captureParams,
          maxBytes: 1024,
          purpose: fixture.purpose,
          recipient: fixture.recipient,
          retention: 'memory-only-transient',
          scope: fixture.scope
        }
      }
    })

    expect(rawRequest).toMatchObject({ error: { data: { hermesCode: 'CAPTURE_CONSENT_REQUIRED' } } })
    expect(prompt).not.toHaveBeenCalled()

    const forged = await fixture.controller.dispatchAutomationCommand({
      ...fixture.route,
      frame: {
        id: 'forged',
        method: 'Page.captureScreenshot',
        params: {
          ...fixture.captureParams,
          __hermesPixelConsent: {
            grantId: 'F'.repeat(43),
            purpose: fixture.purpose,
            recipient: fixture.recipient,
            scope: fixture.scope
          }
        }
      }
    })

    expect(forged).toMatchObject({ error: { data: { hermesCode: 'CAPTURE_CONSENT_REQUIRED' } } })
    expect(fixture.guest.debugger.sendCommand).not.toHaveBeenCalled()

    const noTrustedChrome = await setupBoundAutomation()
    await expect(noTrustedChrome.requestGrant()).resolves.toMatchObject({
      result: { error: 'CAPTURE_DENIED', granted: false }
    })
    expect(noTrustedChrome.guest.debugger.sendCommand).not.toHaveBeenCalled()
  })

  it.each([
    ['recipient', (fixture: any) => ({ recipient: 'wrong/provider' })],
    ['purpose', (fixture: any) => ({ purpose: 'A different purpose' })],
    ['scope', (fixture: any) => ({ scope: { ...fixture.scope, document_generation: 4 } })],
    ['capture params', () => ({ extra: true })]
  ])('consumes but never dispatches a grant presented with wrong %s', async (_name, mutate) => {
    const fixture = await setupBoundAutomation(async () => true)
    const granted = await fixture.requestGrant()
    const grantId = (granted.result as { grantId: string }).grantId
    const changed = mutate(fixture)
    const envelopeChange = 'extra' in changed ? {} : changed
    const paramsChange = 'extra' in changed ? changed : {}

    const denied = await fixture.controller.dispatchAutomationCommand({
      ...fixture.route,
      frame: {
        id: 'wrong',
        method: 'Page.captureScreenshot',
        params: {
          ...fixture.captureParams,
          ...paramsChange,
          __hermesPixelConsent: {
            grantId,
            purpose: fixture.purpose,
            recipient: fixture.recipient,
            scope: fixture.scope,
            ...envelopeChange
          }
        }
      }
    })

    expect(denied).toMatchObject({ error: { data: { hermesCode: 'CAPTURE_CONSENT_REQUIRED' } } })
    expect(fixture.guest.debugger.sendCommand).not.toHaveBeenCalled()
  })

  it('expires grants and invalidates them on navigation and task lifecycle changes', async () => {
    const fixture = await setupBoundAutomation(async () => true)

    const makeCapture = (grantId: string) => ({
      ...fixture.route,
      frame: {
        id: 'stale',
        method: 'Page.captureScreenshot',
        params: {
          ...fixture.captureParams,
          __hermesPixelConsent: {
            grantId,
            purpose: fixture.purpose,
            recipient: fixture.recipient,
            scope: fixture.scope
          }
        }
      }
    })

    const now = vi.spyOn(Date, 'now').mockReturnValue(1_000)
    const expiring = await fixture.requestGrant()
    now.mockReturnValue(61_001)
    await expect(
      fixture.controller.dispatchAutomationCommand(makeCapture((expiring.result as any).grantId))
    ).resolves.toMatchObject({ error: { data: { hermesCode: 'CAPTURE_CONSENT_REQUIRED' } } })

    now.mockReturnValue(70_000)
    const navigated = await fixture.requestGrant()
    fixture.guest.emit('did-start-navigation', {}, 'https://example.test/next')
    await expect(
      fixture.controller.dispatchAutomationCommand(makeCapture((navigated.result as any).grantId))
    ).resolves.toMatchObject({ error: { data: { hermesCode: 'CAPTURE_CONSENT_REQUIRED' } } })

    const unbound = await fixture.requestGrant()
    await fixture.handlers.get('hermes:browser-guest:unbind-automation')!({ sender: fixture.host }, fixture.binding)
    await expect(
      fixture.controller.dispatchAutomationCommand(makeCapture((unbound.result as any).grantId))
    ).resolves.toMatchObject({ error: { data: { hermesCode: 'NAVIGATION_TARGET_STALE' } } })
    expect(fixture.guest.debugger.sendCommand).not.toHaveBeenCalled()
    now.mockRestore()
  })

  it('reports outcome unknown when a lease retires after debugger dispatch', async () => {
    const fixture = await setupBoundAutomation()
    let finish!: (value: Record<string, unknown>) => void

    fixture.guest.debugger.sendCommand.mockImplementation(
      () => new Promise<Record<string, unknown>>(resolve => {finish = resolve})
    )

    const pending = fixture.controller.dispatchAutomationCommand({
      ...fixture.route,
      frame: { id: 91, method: 'Page.getFrameTree', params: {} }
    })

    await vi.waitFor(() => expect(finish).toBeTypeOf('function'))
    await fixture.handlers.get('hermes:browser-guest:unbind-automation')!({
      sender: fixture.host
    }, fixture.binding)
    finish({ frameTree: {} })

    await expect(pending).resolves.toMatchObject({
      id: 91,
      error: { data: { disposition: 'outcome_unknown', hermesCode: 'BROWSER_OUTCOME_UNKNOWN' } }
    })
  })

  it('retires a crashed guest and reports an in-flight debugger outcome as unknown', async () => {
    const fixture = await setupBoundAutomation()
    const lifecycle = vi.fn()
    let rejectDispatch!: (reason?: unknown) => void

    fixture.controller.setTaskLifecycleSink(lifecycle)
    fixture.guest.debugger.sendCommand.mockImplementation(
      () => new Promise((_resolve, reject) => {rejectDispatch = reject})
    )

    const pending = fixture.controller.dispatchAutomationCommand({
      ...fixture.route,
      frame: { id: 'crash-race', method: 'Page.getFrameTree', params: {} }
    })

    await vi.waitFor(() => expect(rejectDispatch).toBeTypeOf('function'))
    fixture.guest.emit('render-process-gone', {}, { reason: 'crashed' })
    rejectDispatch(new Error('Render frame was disposed'))

    await expect(pending).resolves.toMatchObject({
      id: 'crash-race',
      error: {
        code: -32002,
        data: { disposition: 'outcome_unknown', hermesCode: 'BROWSER_OUTCOME_UNKNOWN' }
      }
    })
    expect(fixture.guest.closed).toBe(true)
    expect(fixture.notifyRetired).toHaveBeenCalledTimes(1)
    expect(fixture.notifyRetired).toHaveBeenCalledWith({
      guestGeneration: fixture.binding.guestGeneration,
      hostId: fixture.host.id,
      reason: 'crashed',
      tabId: fixture.binding.tabId
    })
    expect(lifecycle).toHaveBeenCalledTimes(1)
    expect(lifecycle).toHaveBeenCalledWith({
      ...fixture.binding,
      profile: 'default',
      type: 'unbind'
    })
    expect(
      await fixture.handlers.get('hermes:browser-guest:bind-automation')!(
        { sender: fixture.host },
        fixture.binding
      )
    ).toEqual({ error: 'browser-automation-guest-stale', ok: false })
  })

  it('does not mint when the exact binding retires while trusted chrome is deciding', async () => {
    let decide!: (approved: boolean) => void

    const fixture = await setupBoundAutomation(
      () => new Promise<boolean>(resolve => {decide = resolve})
    )

    const pending = fixture.requestGrant()

    await vi.waitFor(() => expect(decide).toBeTypeOf('function'))
    await fixture.handlers.get('hermes:browser-guest:unbind-automation')!({ sender: fixture.host }, fixture.binding)
    decide(true)
    await expect(pending).resolves.toMatchObject({
      result: { error: 'CAPTURE_SCOPE_STALE', granted: false }
    })
    expect(fixture.guest.debugger.sendCommand).not.toHaveBeenCalled()
  })

  it('auto-cancels intercepted chooser events when upload handling is unavailable', async () => {
    const fixture = await setupBoundAutomation()
    const frameSink = vi.fn()

    fixture.controller.setFrameSink(frameSink)
    fixture.guest.debugger.emit('message', {}, 'Page.fileChooserOpened', {
      frameId: 'frame-upload',
      mode: 'selectSingle'
    })

    await new Promise(resolve => setTimeout(resolve, 0))
    expect(fixture.guest.debugger.sendCommand).not.toHaveBeenCalledWith(
      'DOM.setFileInputFiles',
      expect.anything(),
      expect.anything()
    )
    expect(frameSink).not.toHaveBeenCalled()
  })

  it('binds an intercepted chooser to the exact live task and cancels it on navigation', async () => {
    const received = vi.fn(async (_chooser: any) => new Promise<void>(() => undefined))
    const fixture = await setupBoundAutomation(undefined, { handleUploadChooser: received })
    const frameSink = vi.fn()
    let frameSecurityOrigin = 'https://uploads.example.test'
    let directory = false

    fixture.controller.setFrameSink(frameSink)
    fixture.guest.debugger.sendCommand.mockImplementation(async (method, params) =>
      method === 'Page.getFrameTree'
        ? { frameTree: { frame: {
            id: 'frame-upload',
            securityOrigin: frameSecurityOrigin,
            url: 'about:blank'
          } } }
        : uploadInputDebuggerResult(method, params, true, { directory })
    )
    fixture.guest.debugger.emit('message', {}, 'Target.attachedToTarget', {
      sessionId: 'session-upload',
      targetInfo: { targetId: 'frame-upload', type: 'iframe' }
    })
    fixture.guest.debugger.emit('message', {}, 'Page.fileChooserOpened', {
      backendNodeId: 77,
      frameId: 'frame-upload',
      mode: 'selectMultiple'
    })
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(received).not.toHaveBeenCalled()
    fixture.guest.debugger.emit('message', {}, 'Page.fileChooserOpened', {
      backendNodeId: 77,
      frameId: 'frame-upload',
      mode: 'selectMultiple'
    }, 'session-upload')

    await vi.waitFor(() => expect(received).toHaveBeenCalledTimes(1))
    expect(received.mock.calls[0][0]).toMatchObject({
      accept: 'image/png,.pdf',
      backendNodeId: 77,
      documentGeneration: 1,
      frameId: 'frame-upload',
      guestGeneration: fixture.binding.guestGeneration,
      hostId: fixture.host.id,
      inputName: 'evidence',
      mode: 'selectMultiple',
      origin: 'https://uploads.example.test',
      profile: 'default',
      tabId: fixture.binding.tabId,
      taskGeneration: fixture.binding.taskGeneration,
      taskId: fixture.binding.taskId
    })
    expect(received.mock.calls[0][0].chooserId).toMatch(/^[A-Za-z0-9_-]{43}$/)
    expect(received.mock.calls[0][0].formFingerprint).toMatch(/^[A-Za-z0-9_-]{43}$/)
    expect(received.mock.calls[0][0]).toMatchObject({
      directory: false,
      formActionOrigin: 'https://uploads.example.test',
      formLabel: 'Evidence upload',
      formMethod: 'post',
      inputLabel: 'Attach evidence'
    })
    expect(fixture.guest.debugger.sendCommand).toHaveBeenCalledWith(
      'Page.createIsolatedWorld',
      { frameId: 'frame-upload', grantUniveralAccess: false, worldName: 'hermes-upload-descriptor-v1' },
      'session-upload'
    )
    expect(fixture.guest.debugger.sendCommand).toHaveBeenCalledWith(
      'DOM.resolveNode',
      { backendNodeId: 77, executionContextId: 44 },
      'session-upload'
    )
    expect(received.mock.calls[0][0].signal.aborted).toBe(false)
    expect(frameSink).not.toHaveBeenCalled()
    expect(fixture.guest.debugger.sendCommand).toHaveBeenCalledWith('Page.enable', undefined, 'session-upload')
    expect(fixture.guest.debugger.sendCommand).toHaveBeenCalledWith(
      'Page.setInterceptFileChooserDialog',
      { cancel: true, enabled: true },
      'session-upload'
    )

    directory = true
    fixture.guest.debugger.emit('message', {}, 'Page.fileChooserOpened', {
      backendNodeId: 78,
      frameId: 'frame-upload',
      mode: 'selectMultiple'
    }, 'session-upload')
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(received).toHaveBeenCalledTimes(1)

    fixture.guest.emit('did-start-navigation', {}, 'https://example.test/next')
    await vi.waitFor(() => expect(fixture.guest.debugger.sendCommand).toHaveBeenCalledWith(
      'DOM.setFileInputFiles',
      { backendNodeId: 77, files: [] },
      'session-upload'
    ))
    expect(received.mock.calls[0][0].signal.aborted).toBe(true)
    expect(fixture.invalidateAssignedUploads).toHaveBeenCalledWith(expect.objectContaining({
      documentGeneration: 1,
      guestGeneration: fixture.binding.guestGeneration,
      tabId: fixture.binding.tabId
    }))

    frameSecurityOrigin = '://'
    fixture.guest.debugger.emit('message', {}, 'Page.fileChooserOpened', {
      backendNodeId: 78,
      frameId: 'frame-upload',
      mode: 'selectSingle'
    }, 'session-upload')
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(received).toHaveBeenCalledTimes(1)
  })

  it('aborts exact chooser work on task-generation replacement and target detachment', async () => {
    const received = vi.fn(async (_chooser: any) => new Promise<void>(() => undefined))
    const fixture = await setupBoundAutomation(undefined, { handleUploadChooser: received })

    fixture.guest.debugger.sendCommand.mockImplementation(async (method, params) =>
      method === 'Page.getFrameTree'
        ? { frameTree: { frame: {
            id: 'frame-upload',
            securityOrigin: 'https://uploads.example.test',
            url: 'https://uploads.example.test/form'
          } } }
        : uploadInputDebuggerResult(method, params)
    )
    const openChooser = async (backendNodeId: number, sessionId: string) => {
      fixture.guest.debugger.emit('message', {}, 'Target.attachedToTarget', {
        sessionId,
        targetInfo: { targetId: 'frame-upload', type: 'iframe' }
      })
      await vi.waitFor(() => expect(fixture.guest.debugger.sendCommand).toHaveBeenCalledWith(
        'Page.setInterceptFileChooserDialog',
        { cancel: true, enabled: true },
        sessionId
      ))
      fixture.guest.debugger.emit('message', {}, 'Page.fileChooserOpened', {
        backendNodeId,
        frameId: 'frame-upload',
        mode: 'selectSingle'
      }, sessionId)
      await vi.waitFor(() => expect(received).toHaveBeenCalledTimes(backendNodeId === 81 ? 1 : 2))
      return received.mock.calls.at(-1)![0]
    }

    const replaced = await openChooser(81, 'session-old')
    await fixture.handlers.get('hermes:browser-guest:bind-automation')!({ sender: fixture.host }, {
      ...fixture.binding,
      taskGeneration: fixture.binding.taskGeneration + 1
    })
    await vi.waitFor(() => expect(replaced.signal.aborted).toBe(true))
    expect(fixture.invalidateAssignedUploads).toHaveBeenCalledWith(expect.objectContaining({
      taskGeneration: fixture.binding.taskGeneration,
      taskId: fixture.binding.taskId
    }))
    expect(fixture.guest.debugger.sendCommand).toHaveBeenCalledWith(
      'DOM.setFileInputFiles',
      { backendNodeId: 81, files: [] },
      'session-old'
    )

    const detached = await openChooser(82, 'session-new')
    fixture.guest.debugger.emit('message', {}, 'Target.detachedFromTarget', { sessionId: 'session-new' })
    await vi.waitFor(() => expect(detached.signal.aborted).toBe(true))
    expect(fixture.invalidateAssignedUploads).toHaveBeenCalledWith(expect.objectContaining({ frameId: 'frame-upload' }))
    expect(fixture.guest.debugger.sendCommand).toHaveBeenCalledWith(
      'DOM.setFileInputFiles',
      { backendNodeId: 82, files: [] },
      'session-new'
    )
  })

  it('aborts a dispatched chooser before discovering its successor and rejects stale discovery', async () => {
    const received = vi.fn(async (_chooser: any) => new Promise<void>(() => undefined))
    const fixture = await setupBoundAutomation(undefined, { handleUploadChooser: received })
    const requests: Array<{ resolve: (value: unknown) => void }> = []

    fixture.guest.debugger.sendCommand.mockImplementation(async (method, params) => {
      if (method !== 'Page.getFrameTree') {return uploadInputDebuggerResult(method, params)}
      return new Promise(resolve => requests.push({ resolve }))
    })
    const emitChooser = (backendNodeId: number) => fixture.guest.debugger.emit(
      'message',
      {},
      'Page.fileChooserOpened',
      { backendNodeId, frameId: 'frame-upload', mode: 'selectSingle' }
    )
    const frameTree = {
      frameTree: { frame: {
            id: 'frame-upload',
            securityOrigin: 'https://uploads.example.test',
            url: 'https://uploads.example.test/form'
          } }
    }

    emitChooser(90)
    await vi.waitFor(() => expect(requests).toHaveLength(1))
    requests[0].resolve(frameTree)
    await vi.waitFor(() => expect(received).toHaveBeenCalledTimes(1))
    const superseded = received.mock.calls[0][0]

    emitChooser(91)
    await vi.waitFor(() => expect(requests).toHaveLength(2))
    expect(superseded.signal.aborted).toBe(true)
    expect(fixture.guest.debugger.sendCommand).toHaveBeenCalledWith(
      'DOM.setFileInputFiles',
      { backendNodeId: 90, files: [] },
      undefined
    )

    emitChooser(91)
    await vi.waitFor(() => expect(requests).toHaveLength(3))
    requests[2].resolve(frameTree)
    await vi.waitFor(() => expect(received).toHaveBeenCalledTimes(2))
    const current = received.mock.calls[1][0]
    expect(current.backendNodeId).toBe(91)
    expect(current.signal.aborted).toBe(false)

    requests[1].resolve(frameTree)
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(received).toHaveBeenCalledTimes(2)
    expect(current.signal.aborted).toBe(false)
    expect(fixture.guest.debugger.sendCommand).not.toHaveBeenCalledWith(
      'DOM.setFileInputFiles',
      { backendNodeId: 91, files: [] },
      undefined
    )

    emitChooser(93)
    await vi.waitFor(() => expect(requests).toHaveLength(4))
    fixture.guest.debugger.emit('message', {}, 'Page.frameDetached', { frameId: 'frame-upload' })
    requests[3].resolve(frameTree)
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(received).toHaveBeenCalledTimes(2)
  })

  it('revalidates exact form state after consent and assigns staged paths once from main', async () => {
    const received = vi.fn(async (_chooser: any) => new Promise<void>(() => undefined))
    const fixture = await setupBoundAutomation(undefined, { handleUploadChooser: received })
    fixture.guest.debugger.sendCommand.mockImplementation(async (method, params) =>
      method === 'Page.getFrameTree'
        ? { frameTree: { frame: {
            id: 'frame-upload',
            securityOrigin: 'https://uploads.example.test',
            url: 'https://uploads.example.test/form'
          } } }
        : uploadInputDebuggerResult(method, params)
    )
    fixture.guest.debugger.emit('message', {}, 'Target.attachedToTarget', {
      sessionId: 'session-upload',
      targetInfo: { targetId: 'frame-upload', type: 'iframe' }
    })
    await vi.waitFor(() => expect(fixture.guest.debugger.sendCommand).toHaveBeenCalledWith(
      'Page.setInterceptFileChooserDialog',
      { cancel: true, enabled: true },
      'session-upload'
    ))
    fixture.guest.debugger.emit('message', {}, 'Page.fileChooserOpened', {
      backendNodeId: 77,
      frameId: 'frame-upload',
      mode: 'selectSingle'
    }, 'session-upload')
    await vi.waitFor(() => expect(received).toHaveBeenCalledTimes(1))
    const chooser = received.mock.calls[0][0]
    const consume = vi.fn(async () => ['/trusted/staging/report.pdf'])
    const settled = vi.fn(async () => undefined)
    const assignmentFile = {
      displayName: 'report.pdf',
      mimeType: 'application/pdf',
      sha256: 'a'.repeat(64),
      size: 123
    }
    await expect(fixture.controller.assignPendingUpload(chooser.chooserId, {
      consume,
      files: Array.from({ length: 21 }, () => assignmentFile)
    })).resolves.toBe('not_started')
    expect(fixture.presentedConsents).toHaveLength(0)
    expect(consume).not.toHaveBeenCalled()

    const outcome = await fixture.controller.assignPendingUpload(chooser.chooserId, {
      consume,
      files: [assignmentFile],
      settled
    })

    expect(outcome).toBe('completed')
    expect(fixture.presentedConsents.at(-1)).toMatchObject({
      category: 'upload-assignment',
      documentGeneration: 1,
      site: 'https://uploads.example.test',
      upload: {
        aggregateSize: 123,
        destinationOrigin: 'https://uploads.example.test',
        files: [{ displayName: 'report.pdf', mimeType: 'application/pdf', size: 123 }],
        source: 'studio-session-artifact'
      }
    })
    expect(fixture.presentedConsents.at(-1)).not.toHaveProperty('detail')
    expect(JSON.stringify(fixture.presentedConsents.at(-1))).not.toContain('a'.repeat(64))
    expect(consume).toHaveBeenCalledTimes(1)
    expect(fixture.guest.debugger.sendCommand).toHaveBeenCalledWith(
      'DOM.setFileInputFiles',
      { backendNodeId: 77, files: ['/trusted/staging/report.pdf'] },
      'session-upload'
    )
    expect(fixture.guest.debugger.sendCommand.mock.calls.filter(
      ([method, params]) => method === 'DOM.setFileInputFiles' && (params as any).files.length > 0
    )).toHaveLength(1)
    const browserSession = fixture.sessionFromPartition(BROWSER_PARTITION)
    const before = browserSession.beforeRequest.mock.calls[0][0] as (details: any, callback: (result: any) => void) => void
    const completed = browserSession.completedRequest.mock.calls[0][0] as (details: any) => void
    before({
      id: 42, method: 'POST', uploadData: [{ file: '/trusted/staging/report.pdf' }],
      url: 'https://uploads.example.test/submit', webContentsId: fixture.guest.id
    }, vi.fn())
    completed({ id: 42, webContentsId: fixture.guest.id })
    await vi.waitFor(() => expect(settled).toHaveBeenCalledWith('completed'))
    fixture.guest.debugger.emit('message', {}, 'Page.navigatedWithinDocument', {
      frameId: 'frame-upload', url: 'https://uploads.example.test/form#submitted'
    })
    expect(fixture.invalidateAssignedUploads).toHaveBeenCalledWith(expect.objectContaining({ frameId: 'frame-upload' }))
    await expect(fixture.controller.assignPendingUpload(chooser.chooserId, {
      consume,
      files: [{ displayName: 'report.pdf', mimeType: 'application/pdf', sha256: 'a'.repeat(64), size: 123 }]
    })).resolves.toBe('not_started')
  })

  it('ignores false observer matches and settles a matched request failure only once', async () => {
    const received = vi.fn(async (_chooser: any) => new Promise<void>(() => undefined))
    const fixture = await setupBoundAutomation(undefined, { handleUploadChooser: received })
    fixture.guest.debugger.sendCommand.mockImplementation(async (method, params) =>
      method === 'Page.getFrameTree'
        ? { frameTree: { frame: { id: 'frame-upload', securityOrigin: 'https://uploads.example.test' } } }
        : uploadInputDebuggerResult(method, params)
    )
    fixture.guest.debugger.emit('message', {}, 'Page.fileChooserOpened', {
      backendNodeId: 77, frameId: 'frame-upload', mode: 'selectSingle'
    })
    await vi.waitFor(() => expect(received).toHaveBeenCalledOnce())
    const settled = vi.fn(async () => undefined)
    await fixture.controller.assignPendingUpload(received.mock.calls[0][0].chooserId, {
      consume: async () => ['/trusted/staging/report.pdf'],
      files: [{ displayName: 'report.pdf', mimeType: 'application/pdf', sha256: 'a'.repeat(64), size: 123 }],
      settled
    })
    const browserSession = fixture.sessionFromPartition(BROWSER_PARTITION)
    const before = browserSession.beforeRequest.mock.calls[0][0] as (details: any, callback: (result: any) => void) => void
    const completed = browserSession.completedRequest.mock.calls[0][0] as (details: any) => void
    const failed = browserSession.failedRequest.mock.calls[0][0] as (details: any) => void
    const base = { method: 'POST', url: 'https://uploads.example.test/submit', webContentsId: fixture.guest.id }

    before({ ...base, id: 50, uploadData: [{ file: '/unrelated/report.pdf' }] }, vi.fn())
    completed({ id: 50, webContentsId: fixture.guest.id })
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(settled).not.toHaveBeenCalled()

    before({ ...base, id: 51, uploadData: [{ file: '/trusted/staging/report.pdf' }] }, vi.fn())
    failed({ id: 51, webContentsId: fixture.guest.id })
    completed({ id: 51, webContentsId: fixture.guest.id })
    await vi.waitFor(() => expect(settled).toHaveBeenCalledExactlyOnceWith('failed'))
  })

  it('settles every assigned input included in the same upload request', async () => {
    const received = vi.fn(async (_chooser: any) => new Promise<void>(() => undefined))
    const fixture = await setupBoundAutomation(undefined, { handleUploadChooser: received })
    fixture.guest.debugger.sendCommand.mockImplementation(async (method, params) =>
      method === 'Page.getFrameTree'
        ? { frameTree: { frame: { id: 'frame-upload', securityOrigin: 'https://uploads.example.test' } } }
        : uploadInputDebuggerResult(method, params)
    )
    const assignInput = async (backendNodeId: number, path: string) => {
      fixture.guest.debugger.emit('message', {}, 'Page.fileChooserOpened', {
        backendNodeId, frameId: 'frame-upload', mode: 'selectSingle'
      })
      await vi.waitFor(() => expect(received).toHaveBeenCalledTimes(backendNodeId === 77 ? 1 : 2))
      const settled = vi.fn(async () => undefined)
      await fixture.controller.assignPendingUpload(received.mock.calls.at(-1)![0].chooserId, {
        consume: async () => [path],
        files: [{ displayName: path.split('/').at(-1)!, mimeType: 'application/pdf', sha256: 'a'.repeat(64), size: 123 }],
        settled
      })
      return settled
    }

    const reportSettled = await assignInput(77, '/trusted/staging/report.pdf')
    const appendixSettled = await assignInput(78, '/trusted/staging/appendix.pdf')
    const browserSession = fixture.sessionFromPartition(BROWSER_PARTITION)
    const before = browserSession.beforeRequest.mock.calls[0][0] as (details: any, callback: (result: any) => void) => void
    const completed = browserSession.completedRequest.mock.calls[0][0] as (details: any) => void

    before({
      id: 53,
      method: 'POST',
      uploadData: [
        { file: '/trusted/staging/report.pdf' },
        { file: '/trusted/staging/appendix.pdf' }
      ],
      url: 'https://uploads.example.test/submit',
      webContentsId: fixture.guest.id
    }, vi.fn())
    completed({ id: 53, webContentsId: fixture.guest.id })

    await vi.waitFor(() => {
      expect(reportSettled).toHaveBeenCalledExactlyOnceWith('completed')
      expect(appendixSettled).toHaveBeenCalledExactlyOnceWith('completed')
    })
  })

  it('settles colliding request ids only for the exact web contents in the app-global session', async () => {
    const received = vi.fn(async (_chooser: any) => new Promise<void>(() => undefined))
    const fixture = await setupBoundAutomation(undefined, { handleUploadChooser: received })
    const debuggerResult = async (method: string, params: Record<string, unknown>) =>
      method === 'Page.getFrameTree'
        ? { frameTree: { frame: { id: 'frame-upload', securityOrigin: 'https://uploads.example.test' } } }
        : uploadInputDebuggerResult(method, params)
    fixture.guest.debugger.sendCommand.mockImplementation(debuggerResult)
    fixture.guest.debugger.emit('message', {}, 'Page.fileChooserOpened', {
      backendNodeId: 77, frameId: 'frame-upload', mode: 'selectSingle'
    })
    await vi.waitFor(() => expect(received).toHaveBeenCalledOnce())
    const defaultSettled = vi.fn(async () => undefined)
    await fixture.controller.assignPendingUpload(received.mock.calls[0][0].chooserId, {
      consume: async () => ['/trusted/staging/report.pdf'],
      files: [{ displayName: 'report.pdf', mimeType: 'application/pdf', sha256: 'a'.repeat(64), size: 123 }],
      settled: defaultSettled
    })

    const otherProfile = 'collision-profile'
    const otherPrepared = (await fixture.handlers.get('hermes:browser-guest:prepare')!(
      { sender: fixture.host },
      {
        partition: BROWSER_PARTITION,
        private: false,
        profile: otherProfile,
        surfaceEpoch: 'surface-collision',
        tabId: 'tab-collision'
      }
    )) as { attachmentUrl: string; generation: string }
    fixture.host.emit(
      'will-attach-webview',
      { preventDefault: vi.fn() },
      {},
      { partition: BROWSER_PARTITION, src: otherPrepared.attachmentUrl }
    )
    const otherGuest = new FakeContents(92, fixture.sessionFromPartition(BROWSER_PARTITION))
    otherGuest.url = otherPrepared.attachmentUrl
    fixture.host.emit('did-attach-webview', {}, otherGuest)
    await vi.waitFor(() =>
      expect(otherGuest.debugger.sendCommand).toHaveBeenCalledWith(
        'Page.setInterceptFileChooserDialog', { cancel: true, enabled: true }
      )
    )
    expect(await fixture.handlers.get('hermes:browser-guest:bind-automation')!(
      { sender: fixture.host },
      {
        guestGeneration: otherPrepared.generation,
        tabId: 'tab-collision',
        taskGeneration: 1,
        taskId: 'task-collision'
      }
    )).toMatchObject({ ok: true })
    otherGuest.debugger.sendCommand.mockImplementation(debuggerResult)
    otherGuest.debugger.emit('message', {}, 'Page.fileChooserOpened', {
      backendNodeId: 77, frameId: 'frame-upload', mode: 'selectSingle'
    })
    await vi.waitFor(() => expect(received).toHaveBeenCalledTimes(2))
    const otherSettled = vi.fn(async () => undefined)
    await fixture.controller.assignPendingUpload(received.mock.calls[1][0].chooserId, {
      consume: async () => ['/trusted/staging/report.pdf'],
      files: [{ displayName: 'report.pdf', mimeType: 'application/pdf', sha256: 'a'.repeat(64), size: 123 }],
      settled: otherSettled
    })

    const browserSession = fixture.sessionFromPartition(BROWSER_PARTITION)
    const before = browserSession.beforeRequest.mock.calls[0][0] as (details: any, callback: (result: any) => void) => void
    const completed = browserSession.completedRequest.mock.calls[0][0] as (details: any) => void
    const upload = {
      id: 61,
      method: 'POST',
      uploadData: [{ file: '/trusted/staging/report.pdf' }],
      url: 'https://uploads.example.test/submit'
    }
    before({ ...upload, webContentsId: fixture.guest.id }, vi.fn())
    before({ ...upload, webContentsId: otherGuest.id }, vi.fn())

    completed({ id: 61, webContentsId: 999 })
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(defaultSettled).not.toHaveBeenCalled()
    expect(otherSettled).not.toHaveBeenCalled()

    completed({ id: 61, webContentsId: otherGuest.id })
    await vi.waitFor(() => expect(otherSettled).toHaveBeenCalledExactlyOnceWith('completed'))
    expect(defaultSettled).not.toHaveBeenCalled()

    completed({ id: 61, webContentsId: fixture.guest.id })
    await vi.waitFor(() => expect(defaultSettled).toHaveBeenCalledExactlyOnceWith('completed'))
  })

  it('removes exact observer state synchronously on lifecycle invalidation', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const received = vi.fn(async (_chooser: any) => new Promise<void>(() => undefined))
      const fixture = await setupBoundAutomation(undefined, { handleUploadChooser: received })
      fixture.guest.debugger.sendCommand.mockImplementation(async (method, params) =>
        method === 'Page.getFrameTree'
          ? { frameTree: { frame: { id: 'frame-upload', securityOrigin: 'https://uploads.example.test' } } }
          : uploadInputDebuggerResult(method, params)
      )
      fixture.guest.debugger.emit('message', {}, 'Page.fileChooserOpened', {
        backendNodeId: 77, frameId: 'frame-upload', mode: 'selectSingle'
      })
      await vi.waitFor(() => expect(received).toHaveBeenCalledOnce())
      const settled = vi.fn(async () => undefined)
      await fixture.controller.assignPendingUpload(received.mock.calls[0][0].chooserId, {
        consume: async () => ['/trusted/staging/report.pdf'],
        files: [{ displayName: 'report.pdf', mimeType: 'application/pdf', sha256: 'a'.repeat(64), size: 123 }],
        settled
      })
      const browserSession = fixture.sessionFromPartition(BROWSER_PARTITION)
      const before = browserSession.beforeRequest.mock.calls[0][0] as (details: any, callback: (result: any) => void) => void
      const completed = browserSession.completedRequest.mock.calls[0][0] as (details: any) => void
      before({
        id: 52, method: 'POST', uploadData: [{ file: '/trusted/staging/report.pdf' }],
        url: 'https://uploads.example.test/submit', webContentsId: fixture.guest.id
      }, vi.fn())

      fixture.guest.debugger.emit('message', {}, 'Page.navigatedWithinDocument', {
        frameId: 'frame-upload', url: 'https://uploads.example.test/form#new'
      })
      completed({ id: 52, webContentsId: fixture.guest.id })
      await vi.advanceTimersByTimeAsync(30 * 60_000)
      expect(fixture.invalidateAssignedUploads).toHaveBeenCalledWith(expect.objectContaining({ frameId: 'frame-upload' }))
      expect(settled).not.toHaveBeenCalled()
      expect(fixture.notifyUploadExpired).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('clears and expires an assigned upload at the hard 30-minute boundary', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const received = vi.fn(async (_chooser: any) => new Promise<void>(() => undefined))
      const fixture = await setupBoundAutomation(undefined, { handleUploadChooser: received })
      fixture.guest.debugger.sendCommand.mockImplementation(async (method, params) =>
        method === 'Page.getFrameTree'
          ? { frameTree: { frame: { id: 'frame-upload', securityOrigin: 'https://uploads.example.test' } } }
          : uploadInputDebuggerResult(method, params)
      )
      fixture.guest.debugger.emit('message', {}, 'Page.fileChooserOpened', {
        backendNodeId: 77, frameId: 'frame-upload', mode: 'selectSingle'
      })
      await vi.waitFor(() => expect(received).toHaveBeenCalledOnce())
      const chooser = received.mock.calls[0][0]
      const settled = vi.fn(async () => undefined)
      await fixture.controller.assignPendingUpload(chooser.chooserId, {
        consume: async () => ['/trusted/staging/report.pdf'],
        files: [{ displayName: 'report.pdf', mimeType: 'application/pdf', sha256: 'a'.repeat(64), size: 123 }],
        settled
      })
      fixture.guest.debugger.sendCommand.mockClear()

      await vi.advanceTimersByTimeAsync(30 * 60_000)
      await vi.waitFor(() => expect(settled).toHaveBeenCalledExactlyOnceWith('expired'))
      expect(fixture.notifyUploadExpired).toHaveBeenCalledWith(chooser)
      expect(fixture.guest.debugger.sendCommand).toHaveBeenCalledWith(
        'DOM.setFileInputFiles', { backendNodeId: 77, files: [] }, undefined
      )
    } finally {
      vi.useRealTimers()
    }
  })

  it('refuses staged bytes when the live form fingerprint changes during consent', async () => {
    const received = vi.fn(async (_chooser: any) => new Promise<void>(() => undefined))
    const fixture = await setupBoundAutomation(undefined, { handleUploadChooser: received })
    let formLabel = 'Evidence upload'
    fixture.guest.debugger.sendCommand.mockImplementation(async (method, params) =>
      method === 'Page.getFrameTree'
        ? { frameTree: { frame: { id: 'frame-upload', securityOrigin: 'https://uploads.example.test' } } }
        : uploadInputDebuggerResult(method, params, false, { formLabel })
    )
    fixture.guest.debugger.emit('message', {}, 'Page.fileChooserOpened', {
      backendNodeId: 77,
      frameId: 'frame-upload',
      mode: 'selectSingle'
    })
    await vi.waitFor(() => expect(received).toHaveBeenCalledTimes(1))
    formLabel = 'Changed upload target'
    const consume = vi.fn(async () => ['/trusted/staging/report.pdf'])

    await expect(fixture.controller.assignPendingUpload(received.mock.calls[0][0].chooserId, {
      consume,
      files: [{ displayName: 'report.pdf', mimeType: 'application/pdf', sha256: 'a'.repeat(64), size: 123 }]
    })).resolves.toBe('not_started')
    expect(consume).not.toHaveBeenCalled()
    expect(fixture.guest.debugger.sendCommand).not.toHaveBeenCalledWith(
      'DOM.setFileInputFiles',
      expect.objectContaining({ files: ['/trusted/staging/report.pdf'] }),
      expect.anything()
    )
  })
})
