import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  __resetBrowserAnnotationsLayoutForTests,
  setBrowserAnnotationsOpen
} from './browser-annotations-layout'
import { BrowserAnnotationsPanel, isAnnotationActionCurrent } from './browser-annotations-panel'
import type { BrowserTab } from './browser-store'

const tab = {
  createdAt: 1,
  geometry: { height: 400, width: 800, x: 0, y: 0 },
  id: 'browser:tab-1',
  private: false,
  profile: 'default',
  recovery: { attempts: 0, state: 'stable', windowStartedAt: 1 },
  surfaceEpoch: 'surface-1',
  url: 'https://example.test',
  workspaceId: 'workspace-1'
} as BrowserTab

const record = {
  annotationId: 'annotation-1',
  kind: 'element',
  revision: 2,
  scope: {
    browserWorkspaceId: tab.workspaceId,
    documentGenerationId: '7',
    profileId: tab.profile,
    tabId: tab.id
  },
  status: 'open'
}

describe('browser annotations side panel', () => {
  beforeEach(() => {
    __resetBrowserAnnotationsLayoutForTests()
    setBrowserAnnotationsOpen(true)
  })

  afterEach(() => {
    __resetBrowserAnnotationsLayoutForTests()
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it('fetches backend authority and paints only the main-safe projection', async () => {
    const api = vi.fn(async () => [record])
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        api,
        browserGuest: {
          report: vi.fn(async () => ({ documentGeneration: 7, ok: true, value: { devicePixelRatio: 2, height: 400, width: 800 } })),
          resolveAnnotations: vi.fn(async () => ({
            documentGeneration: 7,
            ok: true,
            projections: [{ annotationId: record.annotationId, externalLabel: 4, health: 'resolved' }]
          }))
        }
      }
    })

    render(<BrowserAnnotationsPanel guestGeneration="guest-1" tab={tab} />)

    expect(await screen.findByText('#4')).not.toBeNull()
    expect(screen.getByText('Element · Open')).not.toBeNull()
    expect(api).toHaveBeenCalledWith(expect.objectContaining({
      path: '/api/browser/annotations?profile=default&workspace_id=workspace-1',
      profile: 'default'
    }))
    expect(screen.queryByText('https://example.test')).toBeNull()
  })

  it('refuses a status action when the trusted document generation has advanced', async () => {
    const api = vi.fn(async request => request.method === 'PATCH' ? record : [record])

    const report = vi.fn()
      .mockResolvedValueOnce({ documentGeneration: 7, ok: true, value: { devicePixelRatio: 2, height: 400, width: 800 } })
      .mockResolvedValueOnce({ documentGeneration: 8, ok: true, value: { devicePixelRatio: 2, height: 400, width: 800 } })

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        api,
        browserGuest: {
          report,
          resolveAnnotations: vi.fn(async () => ({
            documentGeneration: 7,
            ok: true,
            projections: [{ annotationId: record.annotationId, externalLabel: 1, health: 'resolved' }]
          }))
        }
      }
    })

    render(<BrowserAnnotationsPanel guestGeneration="guest-1" tab={tab} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Resolve' }))

    expect(await screen.findByText('The page changed. Refresh before acting.')).not.toBeNull()
    expect(api).not.toHaveBeenCalledWith(expect.objectContaining({ method: 'PATCH' }))
  })

  it('requests a main-owned labeled screenshot without receiving pixel bytes', async () => {
    const exportAnnotationScreenshot = vi.fn(async () => ({ canceled: false, ok: true }))

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        api: vi.fn(async () => [record]),
        browserGuest: {
          exportAnnotationScreenshot,
          report: vi.fn(async () => ({ documentGeneration: 7, ok: true, value: {} })),
          resolveAnnotations: vi.fn(async () => ({
            documentGeneration: 7,
            ok: true,
            projections: [{ annotationId: record.annotationId, externalLabel: 8, health: 'resolved' }]
          }))
        }
      }
    })

    render(<BrowserAnnotationsPanel guestGeneration="guest-1" tab={tab} />)
    const exportButton = await screen.findByRole('button', { name: 'Export labeled screenshot' })
    await act(async () => {
      fireEvent.click(exportButton)
      await Promise.resolve()
    })

    await vi.waitFor(() => expect(exportAnnotationScreenshot).toHaveBeenCalledWith({
      generation: 'guest-1',
      records: [record],
      tabId: tab.id,
      workspaceId: tab.workspaceId
    }))
    await vi.waitFor(() => expect((exportButton as HTMLButtonElement).disabled).toBe(false))
    expect(await exportAnnotationScreenshot.mock.results[0].value).toEqual({ canceled: false, ok: true })
  })

  it('resizes with the accessible separator and closes from the panel header', async () => {
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        api: vi.fn(async () => []),
        browserGuest: {
          report: vi.fn(async () => ({ documentGeneration: 7, ok: true, value: {} })),
          resolveAnnotations: vi.fn(async () => ({ documentGeneration: 7, ok: true, projections: [] }))
        }
      }
    })

    const toolbarToggle = document.createElement('button')
    toolbarToggle.dataset.browserAnnotationsToggle = ''
    document.body.append(toolbarToggle)
    render(<BrowserAnnotationsPanel guestGeneration="guest-1" tab={tab} />)
    const panel = await screen.findByRole('complementary', { name: 'Page annotations' })
    const separator = screen.getByRole('separator', { name: 'Resize annotations panel' })

    expect(panel.getAttribute('style')).toContain('width: 288px')
    fireEvent.keyDown(separator, { key: 'ArrowRight' })
    expect(panel.getAttribute('style')).toContain('width: 304px')
    fireEvent.pointerDown(separator, { clientX: 304 })
    fireEvent.pointerMove(window, { clientX: 420 })
    fireEvent.blur(window)
    fireEvent.pointerMove(window, { clientX: 500 })
    expect(panel.getAttribute('style')).toContain('width: 420px')
    fireEvent.click(screen.getByRole('button', { name: 'Hide annotations' }))
    expect(screen.queryByRole('complementary', { name: 'Page annotations' })).toBeNull()
    await vi.waitFor(() => expect(document.activeElement).toBe(toolbarToggle))
  })

  it('requires both current generation and actionable health', () => {
    expect(isAnnotationActionCurrent({ health: 'resolved', projectedDocumentGeneration: 3 }, 3)).toBe(true)
    expect(isAnnotationActionCurrent({ health: 'shifted', projectedDocumentGeneration: 3 }, 4)).toBe(false)
    expect(isAnnotationActionCurrent({ health: 'ambiguous', projectedDocumentGeneration: 3 }, 3)).toBe(false)
  })
})
