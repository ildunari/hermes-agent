import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  __resetBrowserPersistenceForTests,
  clearBrowserSiteData,
  hydrateBrowserProfile,
  reseedBrowserPersistence,
  resetBrowserWorkspace,
  setBrowserRestoreEnabled,
  syncBrowserPersistence
} from './browser-persistence'
import {
  $browserTabs,
  $foregroundBrowserTabId,
  bindAutomationTask,
  clearBrowserTabs,
  createBrowserTab
} from './browser-store'

const geometry = { height: 600, width: 900, x: 0, y: 0 }

function installBridge(descriptors: BrowserRestoreDescriptor[] = []) {
  const state = {
    clearSiteData: vi.fn(async () => ({ ok: true })),
    history: vi.fn(async () => ({ degraded: false, rows: [] })),
    remove: vi.fn(async () => ({ ok: true })),
    resetWorkspace: vi.fn(async () => ({ epoch: 'epoch-after-reset', ok: true })),
    select: vi.fn(async () => ({ ok: true })),
    setRestoreEnabled: vi.fn(async () => ({ ok: true })),
    snapshot: vi.fn(async () => ({
      degraded: false,
      descriptors,
      epoch: 'epoch-current',
      restoreEnabled: true,
      selectedRestoreId: descriptors[0]?.restoreId ?? null
    })),
    upsert: vi.fn(async request => ({ ok: true, persisted: request.descriptor }))
  }

  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: { browserState: state }
  })
  return state
}

beforeEach(() => {
  clearBrowserTabs()
  __resetBrowserPersistenceForTests()
})

afterEach(() => {
  clearBrowserTabs()
  __resetBrowserPersistenceForTests()
  Reflect.deleteProperty(window, 'hermesDesktop')
})

describe('browser persistence coordinator', () => {
  it('hydrates safe descriptors into fresh tabs without restoring automation', async () => {
    const old = 'browser:old-live-id'
    installBridge([{
      createdAt: 1,
      ordinal: 0,
      pinned: false,
      restoreId: 'restore-one',
      restoredFromTabId: old,
      title: 'Restored',
      updatedAt: 2,
      url: 'https://restore.test/',
      workspaceId: 'workspace-one'
    }])

    await hydrateBrowserProfile('coding')
    const [restored] = $browserTabs.get()
    expect(restored.id).not.toBe(old)
    expect(restored.restoreId).toBe('restore-one')
    expect($foregroundBrowserTabId.get()).toBe(restored.id)
    expect(() => bindAutomationTask('new-explicit-task', restored.id)).not.toThrow()
  })

  it('writes ordinary tabs only and never writes private, resource, or credential URLs', async () => {
    const state = installBridge()
    await hydrateBrowserProfile('default')
    const ordinary = createBrowserTab({ geometry, profile: 'default', url: 'https://safe.test/path', workspaceId: 'workspace' })
    createBrowserTab({ geometry, private: true, profile: 'default', url: 'https://private.test/', workspaceId: 'private' })
    createBrowserTab({
      geometry,
      profile: 'default',
      resource: { kind: 'artifact', sourceSessionId: 'session', target: '/tmp/report' },
      url: 'hermes-artifact://g-local/report',
      workspaceId: 'workspace'
    })
    createBrowserTab({ geometry, profile: 'default', url: 'https://user:pass@unsafe.test/', workspaceId: 'workspace' })

    syncBrowserPersistence($browserTabs.get(), ordinary.id)
    await vi.waitFor(() => expect(state.upsert).toHaveBeenCalledTimes(1))
    expect(state.upsert).toHaveBeenCalledWith(expect.objectContaining({
      descriptor: expect.objectContaining({ restoreId: ordinary.restoreId, url: 'https://safe.test/path' }),
      epoch: 'epoch-current',
      profile: 'default'
    }))
  })

  it('keeps workspace reset and site-data clear as distinct destructive scopes', async () => {
    const state = installBridge()
    await hydrateBrowserProfile('default')
    createBrowserTab({ geometry, profile: 'default', url: 'https://safe.test/', workspaceId: 'workspace' })

    await expect(resetBrowserWorkspace('default', 'workspace', false)).resolves.toBe(true)
    expect(state.resetWorkspace).toHaveBeenCalledWith({ includeHistory: false, profile: 'default', workspaceId: 'workspace' })
    expect(state.clearSiteData).not.toHaveBeenCalled()

    createBrowserTab({ geometry, profile: 'default', url: 'https://safe.test/', workspaceId: 'workspace-two' })
    await expect(clearBrowserSiteData('default', 'https://safe.test')).resolves.toBe(true)
    expect(state.clearSiteData).toHaveBeenCalledWith({ origin: 'https://safe.test', profile: 'default' })
    expect(state.resetWorkspace).toHaveBeenCalledTimes(1)
    expect($browserTabs.get()).toHaveLength(1)
  })

  it('refreshes the mutation epoch when restore is disabled and re-enabled', async () => {
    const state = installBridge()
    await hydrateBrowserProfile('default')
    createBrowserTab({ geometry, profile: 'default', url: 'https://safe.test/', workspaceId: 'workspace' })
    state.snapshot.mockResolvedValue({ degraded: false, descriptors: [], epoch: 'epoch-rotated', restoreEnabled: false, selectedRestoreId: '' })
    await expect(setBrowserRestoreEnabled('default', false)).resolves.toBe(true)
    state.upsert.mockClear()
    syncBrowserPersistence($browserTabs.get(), $foregroundBrowserTabId.get())
    await Promise.resolve()
    expect(state.upsert).not.toHaveBeenCalled()

    state.snapshot.mockResolvedValue({ degraded: false, descriptors: [], epoch: 'epoch-reenabled', restoreEnabled: true, selectedRestoreId: '' })
    await expect(setBrowserRestoreEnabled('default', true)).resolves.toBe(true)
    await vi.waitFor(() => expect(state.upsert).toHaveBeenCalledWith(expect.objectContaining({ epoch: 'epoch-reenabled' })))
  })

  it('atomically seeds a fresh epoch after degraded repair so writes resume', async () => {
    const state = installBridge()
    state.snapshot.mockResolvedValueOnce({ degraded: true, descriptors: [], epoch: '', restoreEnabled: true, selectedRestoreId: '' })
    await hydrateBrowserProfile('default')
    const tab = createBrowserTab({ geometry, profile: 'default', url: 'https://safe.test/', workspaceId: 'workspace' })
    expect(reseedBrowserPersistence('default', { epoch: 'epoch-after-repair', restoreEnabled: true })).toBe(true)
    await vi.waitFor(() => expect(state.upsert).toHaveBeenCalledWith(expect.objectContaining({
      epoch: 'epoch-after-repair', profile: 'default', descriptor: expect.objectContaining({ restoreId: tab.restoreId })
    })))
  })

  it('preserves a restore-disabled local scope across workspace reset', async () => {
    const state = installBridge()
    state.snapshot.mockResolvedValueOnce({ degraded: false, descriptors: [], epoch: 'disabled', restoreEnabled: false, selectedRestoreId: '' })
    await hydrateBrowserProfile('default')
    await expect(resetBrowserWorkspace('default', 'workspace')).resolves.toBe(true)
    state.upsert.mockClear()
    createBrowserTab({ geometry, profile: 'default', url: 'https://safe.test/', workspaceId: 'workspace-two' })
    syncBrowserPersistence($browserTabs.get(), $foregroundBrowserTabId.get())
    await Promise.resolve()
    expect(state.upsert).not.toHaveBeenCalled()
  })
})
