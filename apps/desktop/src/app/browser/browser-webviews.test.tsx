import { act, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $notifications, clearNotifications } from '@/store/notifications'

import { browserPartitionForProfile, browserProfileScope } from './browser-partition'
import { __resetBrowserPersistenceForTests } from './browser-persistence'
import {
  $browserTabs,
  $foregroundBrowserTabId,
  bindAutomationTask,
  clearBrowserTabs,
  closeBrowserPane,
  closeBrowserTab,
  createBrowserTab,
  openBrowserPane,
  selectBrowserTab,
  setBrowserPaneGeometry,
  setBrowserTabGeometry
} from './browser-store'
import { BrowserWebviewLayer } from './browser-webviews'

const geometry = { height: 400, width: 640, x: 12, y: 24 }

const prepare = vi.fn(async request => ({
  attachmentUrl: `about:blank#hermes-browser-attach=${'A'.repeat(43)}`,
  generation: `generation:${request.tabId}`,
  ok: true
}))

const activate = vi.fn(async () => ({ ok: true }))
const release = vi.fn(async () => ({ ok: true }))
const bindAutomation = vi.fn(async () => ({ ok: true, role: 'automation' }))
const mintResource = vi.fn(async request => ({
  guestUrl:
    request.kind === 'artifact'
      ? `hermes-artifact://g-${'l'.repeat(32)}/report.pdf`
      : `https://studio.example/api/browser/preview/${'r'.repeat(32)}/app/`,
  kind: request.kind === 'artifact' ? 'remote-artifact' : 'remote-preview',
  localRef: 'l'.repeat(32),
  ok: true
}))
const unbindAutomation = vi.fn(async () => ({ ok: true }))
let retiredListener: ((event: { guestGeneration: string; reason: string; tabId: string }) => void) | undefined

let freshSnapshotListener:
  | ((event: {
      guestGeneration: string
      surfaceEpoch: string
      tabId: string
      taskGeneration: number
      taskId: string
    }) => void)
  | undefined

describe('browser webview layer', () => {
  beforeEach(() => {
    clearBrowserTabs()
    closeBrowserPane()
    clearNotifications()
    __resetBrowserPersistenceForTests()
    prepare.mockClear()
    activate.mockClear()
    release.mockClear()
    bindAutomation.mockClear()
    mintResource.mockClear()
    unbindAutomation.mockClear()
    retiredListener = undefined
    freshSnapshotListener = undefined
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        browserGuest: {
          activate,
          bindAutomation,
          mintResource,
          onRetired: vi.fn(callback => {
            retiredListener = callback

            return () => {
              retiredListener = undefined
            }
          }),
          onFreshSnapshot: vi.fn(callback => {
            freshSnapshotListener = callback

            return () => {
              freshSnapshotListener = undefined
            }
          }),
          prepare,
          release,
          report: vi.fn(),
          unbindAutomation
        },
        browserState: {
          clearSiteData: vi.fn(async () => ({ ok: true })),
          history: vi.fn(async () => ({ degraded: false, rows: [] })),
          remove: vi.fn(async () => ({ ok: true })),
          resetWorkspace: vi.fn(async () => ({ epoch: 'epoch-reset', ok: true })),
          select: vi.fn(async () => ({ ok: true })),
          snapshot: vi.fn(async () => ({
            degraded: false,
            descriptors: [],
            epoch: 'epoch-one',
            restoreEnabled: true,
            selectedRestoreId: null
          })),
          upsert: vi.fn(async request => ({ ok: true, persisted: request.descriptor }))
        }
      }
    })
  })
  afterEach(() => {
    clearBrowserTabs()
    closeBrowserPane()
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it('maps normalized profiles to the canonical persistent Chromium partition', async () => {
    await expect(browserProfileScope('')).resolves.toBe('O7U3sPz8CoQw576B7YjZjO')
    await expect(browserProfileScope(' coding ')).resolves.toBe('TikzYIaYz8WsHCfGsGIDLa')
    await expect(browserPartitionForProfile('Team Alpha')).resolves.toBe(
      'persist:hermes-browser:v1:XKzgqNyJtaQF9dBSPJEnDi'
    )
  })

  it('maps case aliases to one profile partition without merging distinct profiles', async () => {
    await expect(browserPartitionForProfile('Default')).resolves.toBe(await browserPartitionForProfile('default'))
    await expect(browserPartitionForProfile(' CODING ')).resolves.toBe(await browserPartitionForProfile('coding'))
    await expect(browserPartitionForProfile('Team-Alpha')).resolves.toBe(await browserPartitionForProfile('team-alpha'))
    await expect(browserPartitionForProfile('team-alpha')).resolves.not.toBe(
      await browserPartitionForProfile('team-beta')
    )
  })

  it('closes a failed resource tab with a visible localized notification', async () => {
    mintResource.mockResolvedValueOnce({ ok: false } as never)
    createBrowserTab({
      geometry,
      profile: 'default',
      resource: { kind: 'artifact', sourceSessionId: 'session', target: '/workspace/report.svg' },
      url: '',
      workspaceId: 'w'
    })

    const rendered = render(<BrowserWebviewLayer />)

    await waitFor(() => expect($browserTabs.get()).toEqual([]))
    expect($notifications.get()[0]).toMatchObject({
      kind: 'warning',
      message: expect.stringContaining('empty browser tab was closed'),
      title: 'Resource not opened'
    })
    rendered.unmount()
  })

  it('keeps one live webview per open tab while visual selection and geometry change', async () => {
    const rendered = render(<BrowserWebviewLayer />)
    let first!: ReturnType<typeof createBrowserTab>
    let second!: ReturnType<typeof createBrowserTab>

    act(() => {
      openBrowserPane()
      setBrowserPaneGeometry(geometry)
      first = createBrowserTab({
        foreground: true,
        geometry,
        profile: 'default',
        url: 'https://one.test',
        workspaceId: 'w'
      })
      second = createBrowserTab({ geometry, profile: 'default', url: 'https://two.test', workspaceId: 'w' })
    })

    await waitFor(() => expect(rendered.container.querySelectorAll('webview')).toHaveLength(2))

    const firstWebview = rendered.container.querySelector(`[data-browser-tab-id="${first.id}"]`)
    const secondWebview = rendered.container.querySelector(`[data-browser-tab-id="${second.id}"]`)
    const firstHost = rendered.container.querySelector(`[data-browser-tab-host="${first.id}"]`) as HTMLElement
    const secondHost = rendered.container.querySelector(`[data-browser-tab-host="${second.id}"]`) as HTMLElement

    expect(firstWebview?.getAttribute('partition')).toBe('persist:hermes-browser:v1:O7U3sPz8CoQw576B7YjZjO')
    expect(firstWebview?.getAttribute('src')).toMatch(/^about:blank#hermes-browser-attach=/)
    expect(firstWebview?.hasAttribute('preload')).toBe(false)
    await waitFor(() => {
      expect(activate).toHaveBeenCalledWith({
        generation: `generation:${first.id}`,
        tabId: first.id,
        url: 'https://one.test'
      })
      expect(activate).toHaveBeenCalledWith({
        generation: `generation:${second.id}`,
        tabId: second.id,
        url: 'https://two.test'
      })
    })
    expect(firstHost.style.visibility).toBe('visible')
    expect(secondHost.style.visibility).toBe('hidden')
    expect(secondHost.style.display).toBe('')

    act(() => closeBrowserPane())

    expect(firstHost.style.visibility).toBe('hidden')
    expect(firstWebview?.isConnected).toBe(true)
    expect(secondWebview?.isConnected).toBe(true)

    act(() => {
      openBrowserPane()
      selectBrowserTab(second.id)
      setBrowserTabGeometry(second.id, { ...geometry, height: 512, width: 768 })
    })

    expect(rendered.container.querySelector(`[data-browser-tab-id="${first.id}"]`)).toBe(firstWebview)
    expect(rendered.container.querySelector(`[data-browser-tab-id="${second.id}"]`)).toBe(secondWebview)
    expect(firstHost.style.visibility).toBe('hidden')
    expect(secondHost.style.visibility).toBe('visible')
    expect(secondHost.style.width).toBe('768px')
    expect(secondHost.style.height).toBe('512px')

    act(() => closeBrowserTab(first.id))

    expect(firstWebview?.isConnected).toBe(false)
    expect(rendered.container.querySelectorAll('webview')).toHaveLength(1)
    await waitFor(() => expect(release).toHaveBeenCalledWith({ generation: `generation:${first.id}`, tabId: first.id }))
    rendered.unmount()
  })

  it('uses an isolated nonpersistent partition for private tabs', async () => {
    const rendered = render(<BrowserWebviewLayer />)
    let tab!: ReturnType<typeof createBrowserTab>

    act(() => {
      tab = createBrowserTab({
        foreground: true,
        geometry,
        private: true,
        profile: 'default',
        url: 'https://private.test',
        workspaceId: 'private'
      })
    })

    await waitFor(() => expect(rendered.container.querySelector('webview')).not.toBeNull())

    expect(rendered.container.querySelector('webview')?.getAttribute('partition')).toBe(tab.privatePartition)
    expect(tab.privatePartition).toMatch(/^hermes-browser-private:v1:[0-9a-f-]{36}$/)
    rendered.unmount()
  })

  it('reconstructs a crashed guest without reusing tab, surface, or task identities', async () => {
    const rendered = render(<BrowserWebviewLayer />)
    let original!: ReturnType<typeof createBrowserTab>

    act(() => {
      original = createBrowserTab({
        foreground: true,
        geometry,
        profile: 'default',
        url: 'https://recover.test',
        workspaceId: 'w'
      })
      bindAutomationTask('task-crash-test', original.id)
    })

    await waitFor(() =>
      expect(bindAutomation).toHaveBeenCalledWith(
        expect.objectContaining({ tabId: original.id, taskGeneration: 1, taskId: 'task-crash-test' })
      )
    )

    const oldWebview = rendered.container.querySelector(`[data-browser-tab-id="${original.id}"]`)!

    act(() => oldWebview.dispatchEvent(new Event('render-process-gone')))
    act(() => oldWebview.dispatchEvent(new Event('unresponsive')))

    await waitFor(() => {
      const [replacement] = $browserTabs.get()
      expect(replacement.id).not.toBe(original.id)
      expect(replacement.surfaceEpoch).not.toBe(original.surfaceEpoch)
      expect($foregroundBrowserTabId.get()).toBe(replacement.id)
      expect(rendered.container.querySelector(`[data-browser-tab-id="${replacement.id}"]`)).not.toBeNull()
      expect(bindAutomation).toHaveBeenCalledWith(
        expect.objectContaining({ tabId: replacement.id, taskGeneration: 2, taskId: 'task-crash-test' })
      )
    })

    expect($browserTabs.get()).toHaveLength(1)
    await waitFor(() =>
      expect(unbindAutomation).toHaveBeenCalledWith(expect.objectContaining({ tabId: original.id, taskGeneration: 1 }))
    )
    rendered.unmount()
  })

  it('accepts only an exact main-process retirement generation', async () => {
    const rendered = render(<BrowserWebviewLayer />)
    let original!: ReturnType<typeof createBrowserTab>

    act(() => {
      original = createBrowserTab({ geometry, profile: 'default', url: 'https://retire.test', workspaceId: 'w' })
    })
    await waitFor(() => expect(retiredListener).toBeTypeOf('function'))

    act(() => retiredListener?.({ guestGeneration: 'stale-generation', reason: 'destroyed', tabId: original.id }))
    expect($browserTabs.get()[0].id).toBe(original.id)

    act(() =>
      retiredListener?.({
        guestGeneration: `generation:${original.id}`,
        reason: 'debugger-detached',
        tabId: original.id
      })
    )
    await waitFor(() => expect($browserTabs.get()[0].id).not.toBe(original.id))
    rendered.unmount()
  })
})
