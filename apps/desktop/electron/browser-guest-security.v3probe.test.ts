// V3 INDEPENDENT ADVERSARIAL SECURITY PROBES
//
// Authored by the mandatory independent security reviewer (V3 gate) to verify
// the guest-security controller's defenses beyond the maker's own suite. These
// probes attempt real exploits: privileged-IPC sender forgery, ownership-tuple
// field forgery before any CDP dispatch, and one-shot pixel-grant replay across
// a mismatched tuple. Each must be REJECTED with no debugger.sendCommand escape.
//
// The harness intentionally mirrors browser-guest-security.test.ts so the probe
// exercises the identical exported controller under identical wiring.

import { EventEmitter } from 'node:events'

import { describe, expect, it, vi } from 'vitest'

import {
  BROWSER_PARTITION,
  BrowserGuestSecurityController,
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
  url = 'about:blank'

  constructor(id: number, session: FakeSession) {
    super()
    this.id = id
    this.session = session
    this.debugger = Object.assign(new EventEmitter(), {
      attach: vi.fn(),
      isAttached: vi.fn(() => false),
      sendCommand: vi.fn(async () => ({}))
    })
  }

  close() { this.closed = true }
  getURL() { return this.url }
  getTitle() { return 'Authenticated account' }
  isDestroyed() { return this.closed }
  isCrashed() { return false }
  async loadURL(url: string) { this.url = url }
  setWindowOpenHandler() { /* captured elsewhere */ }
}

class FakeSession extends EventEmitter {
  permissionRequestHandler = vi.fn()
  permissionCheckHandler = vi.fn()
  devicePermissionHandler = vi.fn()
  beforeRequest = vi.fn()
  completedRequest = vi.fn()
  failedRequest = vi.fn()
  webRequest = {
    onBeforeRequest: (h: unknown) => this.beforeRequest(h),
    onCompleted: (h: unknown) => this.completedRequest(h),
    onErrorOccurred: (h: unknown) => this.failedRequest(h)
  }
  setPermissionRequestHandler(h: unknown) { this.permissionRequestHandler(h) }
  setPermissionCheckHandler(h: unknown) { this.permissionCheckHandler(h) }
  setDevicePermissionHandler(h: unknown) { this.devicePermissionHandler(h) }
}

function setup() {
  const app = new EventEmitter()
  const handlers = new Map<string, (event: { sender: FakeContents }, request: unknown) => unknown>()
  const sessions = new Map<string, FakeSession>()
  const sessionFromPartition = (partition: string) => {
    let value = sessions.get(partition)
    if (!value) { value = new FakeSession(); sessions.set(partition, value) }
    return value
  }

  const controller = new BrowserGuestSecurityController({
    app: app as never,
    chooseDownloadDestination: vi.fn(async () => '/tmp/x'),
    durablePermissionDecision: vi.fn(() => null),
    buildUploadConsent: () => ({}) as never,
    invalidateAssignedUploads: vi.fn(),
    ipcMain: { handle: (channel, handler) => handlers.set(channel, handler as never) } as never,
    launchExternal: vi.fn(async () => undefined),
    notifyConsentResolved: vi.fn(),
    notifyFreshSnapshot: vi.fn(),
    notifyRetired: vi.fn(),
    notifyUploadExpired: vi.fn(),
    presentConsent: (hostId: number, prompt: { consentId: string }) => {
      queueMicrotask(() => {
        void handlers.get('hermes:browser-consent:resolve')?.(
          { sender: { id: hostId } as FakeContents },
          { consentId: prompt.consentId, decision: 'allow' }
        )
      })
    },
    recordTransfer: vi.fn(),
    requestPixelConsent: undefined,
    saveAnnotationScreenshot: vi.fn(async () => 'saved' as const),
    sessionFromPartition: sessionFromPartition as never
  } as never)

  controller.install()
  const host = new FakeContents(11, new FakeSession())
  app.emit('web-contents-created', {}, host)
  controller.registerHost(host as never)
  return { app, controller, handlers, host, sessionFromPartition }
}

async function bindGuest(fixture: ReturnType<typeof setup>, opts: {
  tabId: string; surfaceEpoch: string; taskId: string; taskGeneration: number; guestId: number
}) {
  const partition = BROWSER_PARTITION
  const prepared = (await fixture.handlers.get('hermes:browser-guest:prepare')!(
    { sender: fixture.host },
    { partition, private: false, profile: 'default', surfaceEpoch: opts.surfaceEpoch, tabId: opts.tabId }
  )) as { attachmentUrl: string; generation: string }

  fixture.host.emit('will-attach-webview', { preventDefault: vi.fn() }, {}, { partition, src: prepared.attachmentUrl })
  const guest = new FakeContents(opts.guestId, fixture.sessionFromPartition(partition))
  guest.url = prepared.attachmentUrl
  fixture.host.emit('did-attach-webview', {}, guest)
  await vi.waitFor(() =>
    expect(guest.debugger.sendCommand).toHaveBeenCalledWith(
      'Page.setInterceptFileChooserDialog', { cancel: true, enabled: true }
    )
  )
  guest.url = 'https://example.test/account?private=value'

  const binding = {
    guestGeneration: prepared.generation,
    tabId: opts.tabId,
    taskGeneration: opts.taskGeneration,
    taskId: opts.taskId
  }
  expect(await fixture.handlers.get('hermes:browser-guest:bind-automation')!(
    { sender: fixture.host }, binding
  )).toMatchObject({ ok: true })
  guest.debugger.sendCommand.mockClear()
  return { binding, guest, generation: prepared.generation }
}

describe('V3 adversarial probes', () => {
  // CLASS 1: a non-host sender (a compromised/guest-like webContents id) must
  // not reach any privileged hermes:browser-* handler.
  it('rejects every privileged IPC handler invoked by a non-host sender id', async () => {
    const fixture = setup()
    const partition = BROWSER_PARTITION
    const attacker = { sender: { id: 999 } as FakeContents }

    expect(await fixture.handlers.get('hermes:browser-guest:prepare')!(
      attacker, { partition, private: false, profile: 'default', surfaceEpoch: 's', tabId: 't' }
    )).toEqual({ error: 'browser-host-not-authorized', ok: false })

    expect(await fixture.handlers.get('hermes:browser-guest:bind-automation')!(
      attacker, { guestGeneration: 'g', tabId: 't', taskGeneration: 1, taskId: 'k' }
    )).toEqual({ error: 'browser-automation-binding-invalid', ok: false })

    // report/release resolve to a no-op-or-denied outcome, never a live binding.
    expect(await fixture.handlers.get('hermes:browser-guest:report')!(
      attacker, { generation: 'g', tabId: 't', kind: 'viewport' }
    )).toEqual({ error: 'browser-report-denied', ok: false })
  })

  // CLASS 5: forge each ownership-tuple field independently and confirm every
  // mismatch is rejected BEFORE any debugger.sendCommand.
  it('rejects each forged ownership-tuple field before any CDP dispatch', async () => {
    const fixture = setup()
    const { binding, guest, generation } = await bindGuest(fixture, {
      tabId: 'tab-a', surfaceEpoch: 'surf-a', taskId: 'task-a', taskGeneration: 5, guestId: 41
    })

    const base = {
      ...binding,
      operationId: 'op-1',
      role: 'automation' as const,
      frame: { id: 1, method: 'Page.getFrameTree', params: {} }
    }

    const forgeries: Array<[string, Record<string, unknown>]> = [
      ['wrong taskId', { ...base, taskId: 'task-OTHER' }],
      ['wrong tabId', { ...base, tabId: 'tab-OTHER' }],
      ['wrong guestGeneration', { ...base, guestGeneration: 'guest-OTHER' }],
      ['bumped taskGeneration', { ...base, taskGeneration: 9999 }],
      ['dropped taskGeneration', { ...base, taskGeneration: 1 }],
      ['wrong role value', { ...base, role: 'not-a-role' }]
    ]

    for (const [label, req] of forgeries) {
      const result = await fixture.controller.dispatchAutomationCommand(req as never)
      expect(result, label).toMatchObject({ error: { data: { disposition: 'not_started' } } })
    }
    // Not a single forged tuple reached the debugger.
    expect(guest.debugger.sendCommand).not.toHaveBeenCalled()

    // Control: the exact tuple DOES dispatch, proving the rejections above were
    // the tuple check and not a universally-dead path.
    const ok = await fixture.controller.dispatchAutomationCommand(base as never)
    expect(ok).toMatchObject({ id: 1 })
    expect(guest.debugger.sendCommand).toHaveBeenCalledWith('Page.getFrameTree', {})
  })

  // CLASS 6: after unbind (takeover/teardown), a captured-but-stale lease must
  // not act, even replaying the exact prior tuple.
  it('fails a replayed dispatch after the lease is unbound', async () => {
    const fixture = setup()
    const { binding, guest } = await bindGuest(fixture, {
      tabId: 'tab-b', surfaceEpoch: 'surf-b', taskId: 'task-b', taskGeneration: 3, guestId: 42
    })
    const captured = {
      ...binding,
      operationId: 'op-replay',
      role: 'automation' as const,
      frame: { id: 7, method: 'Page.getFrameTree', params: {} }
    }
    expect(await fixture.handlers.get('hermes:browser-guest:unbind-automation')!(
      { sender: fixture.host }, binding
    )).toEqual({ ok: true })

    const replay = await fixture.controller.dispatchAutomationCommand(captured as never)
    expect(replay).toMatchObject({
      error: { data: { disposition: 'not_started', hermesCode: 'NAVIGATION_TARGET_STALE' } }
    })
    expect(guest.debugger.sendCommand).not.toHaveBeenCalled()
  })

  // CLASS 9: a minted one-shot pixel grant cannot be consumed under a mismatched
  // tuple, and cannot be replayed once consumed.
  it('binds the pixel grant to its exact tuple and forbids replay', async () => {
    const fixture = setup()
    const partition = BROWSER_PARTITION
    const prepared = (await fixture.handlers.get('hermes:browser-guest:prepare')!(
      { sender: fixture.host },
      { partition, private: false, profile: 'default', surfaceEpoch: 'surf-p', tabId: 'tab-p' }
    )) as { attachmentUrl: string; generation: string }
    fixture.host.emit('will-attach-webview', { preventDefault: vi.fn() }, {}, { partition, src: prepared.attachmentUrl })
    const guest = new FakeContents(43, fixture.sessionFromPartition(partition))
    guest.url = prepared.attachmentUrl
    fixture.host.emit('did-attach-webview', {}, guest)
    await vi.waitFor(() =>
      expect(guest.debugger.sendCommand).toHaveBeenCalledWith(
        'Page.setInterceptFileChooserDialog', { cancel: true, enabled: true }
      )
    )
    guest.url = 'https://example.test/account?private=value'

    const binding = { guestGeneration: prepared.generation, tabId: 'tab-p', taskGeneration: 7, taskId: 'task-p' }
    expect(await fixture.handlers.get('hermes:browser-guest:bind-automation')!(
      { sender: fixture.host }, binding
    )).toMatchObject({ ok: true })

    guest.debugger.sendCommand.mockImplementation(async (method: string) =>
      method === 'Page.captureScreenshot' ? { data: Buffer.from('private pixels').toString('base64') } : {}
    )

    const route = {
      ...binding,
      bindingGeneration: 11,
      capabilityGeneration: 9,
      connectionId: 'conn-p',
      profile: 'default',
      role: 'automation' as const
    }
    const scope = {
      binding_generation: 11,
      capability_generation: 9,
      connection_id: 'conn-p',
      document_generation: 3,
      guest_generation: prepared.generation,
      profile: 'default',
      tab_id: 'tab-p',
      task_generation: 7,
      task_id: 'task-p'
    }
    const captureParams = { captureBeyondViewport: false, format: 'png', fromSurface: true }
    const grantRes = (await fixture.controller.dispatchAutomationCommand({
      ...route,
      operationId: 'op-grant',
      frame: {
        id: 'c1',
        method: 'Hermes.requestPixelConsent',
        params: {
          captureParams,
          maxBytes: 8 * 1024 * 1024,
          purpose: 'Read the visible account status',
          recipient: 'strict-provider/vision',
          retention: 'memory-only-transient',
          scope
        }
      }
    } as never)) as { result?: { grantId?: string; granted?: boolean } }
    const grantId = grantRes.result?.grantId
    expect(grantRes.result?.granted).toBe(true)
    expect(typeof grantId).toBe('string')

    // Attempt 1: consume the grant under a FORGED tuple (wrong task_id in the
    // envelope scope + request). Must be rejected as consent-required.
    const forged = await fixture.controller.dispatchAutomationCommand({
      ...route,
      taskId: 'task-OTHER',
      operationId: 'op-forge',
      frame: {
        id: 'c2',
        method: 'Page.captureScreenshot',
        params: { ...captureParams, __hermesPixelConsent: {
          grantId, purpose: 'Read the visible account status',
          recipient: 'strict-provider/vision', scope
        } }
      }
    } as never)
    expect(forged).toMatchObject({ error: { data: { disposition: 'not_started' } } })

    // Attempt 2: legitimate consumption of the exact grant succeeds once.
    const first = (await fixture.controller.dispatchAutomationCommand({
      ...route,
      operationId: 'op-consume',
      frame: {
        id: 'c3',
        method: 'Page.captureScreenshot',
        params: { ...captureParams, __hermesPixelConsent: {
          grantId, purpose: 'Read the visible account status',
          recipient: 'strict-provider/vision', scope
        } }
      }
    } as never)) as { result?: { data?: string } }
    expect(typeof first.result?.data).toBe('string')

    // Attempt 3: replay the SAME grantId. One-shot consumption must reject.
    const replay = await fixture.controller.dispatchAutomationCommand({
      ...route,
      operationId: 'op-replay',
      frame: {
        id: 'c4',
        method: 'Page.captureScreenshot',
        params: { ...captureParams, __hermesPixelConsent: {
          grantId, purpose: 'Read the visible account status',
          recipient: 'strict-provider/vision', scope
        } }
      }
    } as never)
    expect(replay).toMatchObject({
      error: { data: { disposition: 'not_started', hermesCode: 'CAPTURE_CONSENT_REQUIRED' } }
    })
  })
})
