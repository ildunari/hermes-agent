import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import { setActiveProfile } from '@/store/profile'

import { reseedBrowserPersistence, resetBrowserWorkspaceDetailed } from '../browser/browser-persistence'

import { BrowserSettings } from './browser-settings'

vi.mock('../browser/browser-persistence', () => ({
  reseedBrowserPersistence: vi.fn(() => true),
  resetBrowserWorkspaceDetailed: vi.fn(async () => ({ activity: true, ok: true, state: true })),
  setBrowserRestoreEnabled: vi.fn(async () => true)
}))

function bridge(degraded = false) {
  const browserState = {
    clearBrowsingData: vi.fn(async () => ({ metadata: true, ok: true, permissions: true, siteData: true })),
    clearMetadata: vi.fn(async () => ({ ok: true })),
    clearSiteData: vi.fn(async () => ({ ok: true, permissions: true, siteData: true })),
    exportQuarantinedMetadata: vi.fn(async () => ({ ok: true })),
    history: vi.fn(async () => ({
      degraded,
      rows: degraded ? [] : [{
        origin: 'https://example.test', redactionClass: 'none', title: 'Example', url: 'https://example.test/page',
        visitId: 'visit-one', visitedAt: 1, workspaceId: 'workspace-one'
      }]
    })),
    origins: vi.fn(async () => ({
      degraded,
      rows: degraded ? [] : [
        { lastUsedAt: 2, origin: 'https://older-origin.test' },
        { lastUsedAt: 1, origin: 'https://example.test' }
      ]
    })),
    permissions: vi.fn(async () => ({
      degraded,
      rows: degraded ? [] : [{ decidedAt: 1, decision: 'deny' as const, origin: 'https://blocked.test', permission: 'notifications' }]
    })),
    removePermission: vi.fn(async () => ({ ok: true })),
    repair: vi.fn(async () => ({ activity: true, epoch: 'epoch-repaired', metadata: true, ok: true, restoreEnabled: true })),
    setPermission: vi.fn(async () => ({ ok: true })),
    snapshot: vi.fn(async () => ({
      degraded, descriptors: [], epoch: 'epoch-one', restoreEnabled: true, selectedRestoreId: null
    }))
  }
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { browserState } })
  return browserState
}

function mount() {
  return render(<I18nProvider configClient={null} initialLocale="en"><BrowserSettings /></I18nProvider>)
}

describe('BrowserSettings', () => {
  beforeEach(() => {
    setActiveProfile('coding')
    vi.stubGlobal('confirm', vi.fn(() => true))
    vi.stubGlobal('alert', vi.fn())
  })

  it('uses the full origin summary and clears an origin outside the 500-row history view', async () => {
    const api = bridge()
    mount()
    expect((await screen.findAllByText('https://older-origin.test')).length).toBeGreaterThan(0)
    fireEvent.click(screen.getAllByRole('button', { name: 'Clear site data' })[0])
    await waitFor(() => expect(api.clearSiteData).toHaveBeenCalledWith({ origin: 'https://older-origin.test', profile: 'coding' }))
    expect(api.clearBrowsingData).not.toHaveBeenCalled()
  })

  it('provides production-reachable exact-origin grant, deny, list, and remove controls', async () => {
    const api = bridge()
    mount()
    expect(await screen.findByText('Site permissions')).not.toBeNull()
    fireEvent.change(screen.getByRole('textbox', { name: 'Exact site origin' }), { target: { value: 'https://camera.test' } })
    fireEvent.change(screen.getByRole('combobox', { name: 'Permission name' }), { target: { value: 'media' } })
    fireEvent.click(screen.getByRole('button', { name: 'Allow' }))
    await waitFor(() => expect(api.setPermission).toHaveBeenCalledWith({
      decision: 'allow', origin: 'https://camera.test', permission: 'media', persistence: 'durable', profile: 'coding'
    }))
    fireEvent.click(screen.getByRole('button', { name: 'Deny' }))
    await waitFor(() => expect(api.setPermission).toHaveBeenCalledWith(expect.objectContaining({ decision: 'deny' })))
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(api.removePermission).toHaveBeenCalledWith({
      origin: 'https://blocked.test', permission: 'notifications', profile: 'coding'
    }))
  })

  it('surfaces the exact failed scope after a partially destructive clear', async () => {
    const api = bridge()
    api.clearBrowsingData.mockResolvedValueOnce({ metadata: false, ok: false, permissions: true, siteData: true })
    mount()
    await screen.findAllByText('Clear all browsing data')
    fireEvent.click(screen.getAllByRole('button', { name: 'Clear all browsing data' }).at(-1)!)
    await waitFor(() => expect(window.alert).toHaveBeenCalledWith(expect.stringContaining('local metadata')))
  })

  it('surfaces the exact failed scope after a partially destructive workspace reset', async () => {
    bridge()
    vi.mocked(resetBrowserWorkspaceDetailed).mockResolvedValueOnce({ activity: false, ok: false, state: true })
    mount()
    await screen.findByText('Browser workspaces')
    fireEvent.click(screen.getByRole('button', { name: 'Reset saved tabs' }))
    await waitFor(() => expect(window.alert).toHaveBeenCalledWith(expect.stringContaining('activity')))
  })

  it('shows bounded corruption recovery without restoring history', async () => {
    const api = bridge(true)
    mount()
    expect(await screen.findByText('Browser metadata is unavailable')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(api.repair).toHaveBeenCalledWith({ mode: 'retry', profile: 'coding' }))
    expect(reseedBrowserPersistence).toHaveBeenCalledWith('coding', expect.objectContaining({ epoch: 'epoch-repaired' }))
    fireEvent.click(screen.getByRole('button', { name: 'Export damaged metadata' }))
    await waitFor(() => expect(api.exportQuarantinedMetadata).toHaveBeenCalledWith({ profile: 'coding' }))
  })
})
