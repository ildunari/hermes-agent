import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $browserTabs, clearBrowserTabs } from '@/app/browser/browser-store'
import { $activeProfile } from '@/store/profile'
import { $activeSessionId, $connection, $selectedStoredSessionId } from '@/store/session'

import { openFailedGeneratedImage } from './generated-image-result'

describe('failed generated image routing', () => {
  const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
  const originalCreateObjectUrl = URL.createObjectURL
  const originalRevokeObjectUrl = URL.revokeObjectURL
  let previousBridge: Window['hermesDesktop'] | undefined

  beforeEach(() => {
    previousBridge = desktopWindow.hermesDesktop
    clearBrowserTabs()
    $activeProfile.set('coding')
    $activeSessionId.set('generated-image-session')
    $selectedStoredSessionId.set(null)
    $connection.set({ mode: 'local' } as never)
  })

  afterEach(() => {
    desktopWindow.hermesDesktop = previousBridge
    URL.createObjectURL = originalCreateObjectUrl
    URL.revokeObjectURL = originalRevokeObjectUrl
    clearBrowserTabs()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('routes an HTTP image through the in-app browser without native fallback', async () => {
    const openExternal = vi.fn().mockResolvedValue(undefined)

    desktopWindow.hermesDesktop = { ...previousBridge, openExternal } as Window['hermesDesktop']

    await openFailedGeneratedImage('https://images.example.test/generated.png')

    expect(openExternal).not.toHaveBeenCalled()
    expect($browserTabs.get()).toHaveLength(1)
    expect($browserTabs.get()[0].url).toBe('https://images.example.test/generated.png')
  })

  it('keeps a remote gateway image behind a pending main-owned artifact grant', async () => {
    const openExternal = vi.fn().mockResolvedValue(undefined)

    $connection.set({ baseUrl: 'https://gateway.test', mode: 'remote', profile: 'coding', token: 'bearer' } as never)
    desktopWindow.hermesDesktop = { ...previousBridge, openExternal } as Window['hermesDesktop']

    await openFailedGeneratedImage('/studio/private/generated.png')

    expect(openExternal).not.toHaveBeenCalled()
    expect($browserTabs.get()).toHaveLength(1)
    expect($browserTabs.get()[0]).toMatchObject({
      resource: {
        kind: 'artifact',
        sourceSessionId: 'generated-image-session',
        target: '/studio/private/generated.png'
      },
      url: ''
    })
    expect(JSON.stringify($browserTabs.get())).not.toContain('bearer')
  })

  it('uses the authorized preview opener for a local generated-image path', async () => {
    const openExternal = vi.fn().mockResolvedValue(undefined)
    const openPreviewInBrowser = vi.fn().mockResolvedValue(undefined)

    desktopWindow.hermesDesktop = {
      ...previousBridge,
      openExternal,
      openPreviewInBrowser
    } as Window['hermesDesktop']

    await openFailedGeneratedImage('/workspace/generated.png')

    expect(openPreviewInBrowser).not.toHaveBeenCalled()
    expect(openExternal).not.toHaveBeenCalled()
    expect($browserTabs.get()).toHaveLength(1)
    expect($browserTabs.get()[0]).toMatchObject({
      resource: {
        kind: 'artifact',
        sourceSessionId: 'generated-image-session',
        target: '/workspace/generated.png'
      },
      url: ''
    })
  })
})
