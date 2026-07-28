import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $previewTarget, closeRightRail, type PreviewTarget } from '@/store/preview'
import { $activeSessionId, $currentCwd } from '@/store/session'
import type { RpcEvent } from '@/types/hermes'

import { usePreviewRouting } from './use-preview-routing'

const SESSION_ID = 'session-1'
let handleEvent: (event: RpcEvent) => void = () => undefined

function PreviewRoutingHarness() {
  const routing = usePreviewRouting({
    baseHandleGatewayEvent: vi.fn(),
    currentCwd: '/work',
    requestGateway: vi.fn()
  })

  useEffect(() => {
    handleEvent = routing.handleDesktopGatewayEvent
  }, [routing.handleDesktopGatewayEvent])

  return null
}

function staleDocxTarget(): PreviewTarget {
  return {
    binary: true,
    kind: 'file',
    label: 'report.docx',
    path: '/tmp/report.docx',
    previewKind: 'binary',
    source: '/tmp/report.docx',
    url: 'file:///tmp/report.docx'
  }
}

describe('usePreviewRouting document previews', () => {
  beforeEach(() => {
    $activeSessionId.set(SESSION_ID)
    $currentCwd.set('/work')
    closeRightRail()
    handleEvent = () => undefined
    window.localStorage.clear()

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        normalizePreviewTarget: vi.fn(async () => staleDocxTarget())
      }
    })
  })

  afterEach(() => {
    cleanup()
    closeRightRail()
    $activeSessionId.set(null)
    window.localStorage.clear()
    vi.restoreAllMocks()
  })

  it('repairs a stale binary DOCX returned by preview IPC', async () => {
    render(<PreviewRoutingHarness />)

    act(() =>
      handleEvent({
        payload: { label: 'DOCX smoke test', url: '/tmp/report.docx' },
        session_id: SESSION_ID,
        type: 'preview.open'
      })
    )

    await waitFor(() => {
      expect($previewTarget.get()).toMatchObject({
        binary: true,
        label: 'DOCX smoke test',
        path: '/tmp/report.docx',
        previewKind: 'docx'
      })
    })
  })

  it('does not open a document preview for a background session', async () => {
    render(<PreviewRoutingHarness />)

    act(() =>
      handleEvent({
        payload: { url: '/tmp/report.docx' },
        session_id: 'other-session',
        type: 'preview.open'
      })
    )

    await Promise.resolve()
    expect($previewTarget.get()).toBeNull()
  })
})
