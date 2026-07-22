import { describe, expect, it, vi } from 'vitest'

import { BROWSER_PARTITION } from './browser-guest-security'
import { BrowserResourceDeliveryController, type BrowserResourceGuestBinding } from './browser-resource-delivery'
import { BrowserResourceGrantRegistry } from './browser-resource-grants'

const binding: BrowserResourceGuestBinding = {
  generation: 'guest-generation-1',
  guestId: 91,
  hostId: 41,
  partition: BROWSER_PARTITION,
  profile: 'coding',
  tabId: 'browser:tab-1',
  workspaceId: 'session-1'
}

function request(url: string, guestId = binding.guestId, init: RequestInit = {}): Request {
  const value = new Request(url, init) as Request & { webContentsId?: number }
  Object.defineProperty(value, 'webContentsId', { value: guestId })

  return value
}

function fixture(kind: 'artifact' | 'preview' = 'artifact', mimeType = 'application/pdf') {
  let protocolHandler: ((request: Request) => Promise<Response>) | undefined
  let beforeHeaders:
    | ((
        details: { requestHeaders: Record<string, string>; url: string; webContentsId: number },
        callback: (result: { cancel?: boolean; requestHeaders?: Record<string, string> }) => void
      ) => void)
    | undefined
  const revoke = vi.fn(async () => undefined)
  const fetch = vi.fn(async () =>
    new Response('2345', {
      headers: { 'Accept-Ranges': 'bytes', 'Content-Range': 'bytes 2-5/10', 'Content-Type': mimeType },
      status: 206
    })
  )
  const grants = new BrowserResourceGrantRegistry({
    now: () => 1_000,
    ref: () => 'l'.repeat(32)
  })
  const controller = new BrowserResourceDeliveryController({
    fetch,
    now: () => 1_000,
    registry: grants,
    mintGateway: async requestedKind =>
      requestedKind === 'artifact'
        ? {
            connectionId: 'connection-1',
            deliveryCredential: 'c'.repeat(32),
            displayName: mimeType.startsWith('text/markdown') ? 'readme.md' : 'report.pdf',
            expiresInSeconds: 120,
            gatewayOrigin: 'https://studio.example',
            kind: 'artifact',
            mimeType,
            opaqueRef: 'r'.repeat(32),
            recipient: 'dashboard:user-1',
            revoke,
            size: 10
          }
        : {
            connectionId: 'connection-1',
            deliveryCredential: 'c'.repeat(32),
            expiresInSeconds: 120,
            gatewayOrigin: 'https://studio.example',
            kind: 'preview',
            opaqueRef: 'r'.repeat(32),
            proxyPath: `/api/browser/preview/${'r'.repeat(32)}/app/`,
            recipient: 'dashboard:user-1',
            revoke
          }
  })
  const session = {
    protocol: {
      handle: vi.fn((_scheme, handler) => {
        protocolHandler = handler
      })
    },
    webRequest: {
      onBeforeSendHeaders: vi.fn(handler => {
        beforeHeaders = handler
      })
    }
  }

  controller.installSession(session as never, binding.partition)

  return { beforeHeaders: () => beforeHeaders!, controller, fetch, kind, protocolHandler: () => protocolHandler!, revoke }
}

function mint(controller: BrowserResourceDeliveryController, kind: 'artifact' | 'preview') {
  return controller.mint(binding, {
    generation: binding.generation,
    kind,
    profile: binding.profile,
    sourceSessionId: 'session-source',
    tabId: binding.tabId,
    target: kind === 'artifact' ? '/workspace/report.pdf' : 'http://127.0.0.1:4173/app/',
    workspaceId: binding.workspaceId
  })
}

describe('BrowserResourceDeliveryController', () => {
  it('delivers an exact guest-bound artifact range without exposing gateway authority', async () => {
    const { controller, fetch, protocolHandler } = fixture()
    const publicRef = await mint(controller, 'artifact')

    expect(JSON.stringify(publicRef)).not.toContain('studio.example')
    expect(JSON.stringify(publicRef)).not.toContain('c'.repeat(32))
    expect(controller.isAuthorizedRequest(binding.partition, binding.guestId, publicRef.guestUrl, true)).toBe(true)
    expect(controller.isAuthorizedRequest(binding.partition, 92, publicRef.guestUrl)).toBe(false)

    const response = await protocolHandler()(request(publicRef.guestUrl, binding.guestId, {
      headers: { Range: 'bytes=2-5' }
    }))

    expect(response.status).toBe(206)
    expect(await response.text()).toBe('2345')
    expect(response.headers.get('content-range')).toBe('bytes 2-5/10')
    expect(response.headers.get('content-security-policy')).toContain("default-src 'none'")
    expect(fetch).toHaveBeenCalledWith(
      `https://studio.example/api/browser/artifacts/${'r'.repeat(32)}`,
      expect.objectContaining({
        credentials: 'omit',
        headers: expect.objectContaining({ Range: 'bytes=2-5', 'X-Hermes-Browser-Grant': 'c'.repeat(32) }),
        redirect: 'error'
      })
    )

    expect((await protocolHandler()(request(publicRef.guestUrl, 92))).status).toBe(404)
  })

  it('injects preview authority only for the exact guest/path, including websocket URLs', async () => {
    const { beforeHeaders, controller } = fixture('preview')
    const publicRef = await mint(controller, 'preview')
    const wsUrl = publicRef.guestUrl.replace('https:', 'wss:') + 'socket'
    let exactResult: { cancel?: boolean; requestHeaders?: Record<string, string> } = {}

    beforeHeaders()(
      {
        requestHeaders: {
          Authorization: 'Bearer page-controlled',
          Cookie: 'gateway=page-controlled',
          'X-Hermes-Browser-Grant': 'forged'
        },
        url: wsUrl,
        webContentsId: binding.guestId
      },
      result => {
        exactResult = result
      }
    )

    expect(controller.isAuthorizedRequest(binding.partition, binding.guestId, wsUrl)).toBe(true)
    expect(exactResult.cancel).not.toBe(true)
    expect(exactResult.requestHeaders).toMatchObject({
      'X-Hermes-Browser-Connection': 'connection-1',
      'X-Hermes-Browser-Generation': binding.generation,
      'X-Hermes-Browser-Grant': 'c'.repeat(32),
      'X-Hermes-Browser-Recipient': 'dashboard:user-1',
      'X-Hermes-Browser-Tab': binding.tabId
    })
    expect(exactResult.requestHeaders).not.toHaveProperty('Authorization')
    expect(exactResult.requestHeaders).not.toHaveProperty('Cookie')

    let otherResult: { requestHeaders?: Record<string, string> } = {}
    beforeHeaders()(
      { requestHeaders: { Cookie: 'normal=1' }, url: wsUrl, webContentsId: 92 },
      result => {
        otherResult = result
      }
    )
    expect(otherResult.requestHeaders).toEqual({ Cookie: 'normal=1' })
  })

  it('revokes the authenticated gateway copy on lifecycle scope retirement', async () => {
    const { controller, revoke } = fixture()
    const publicRef = await mint(controller, 'artifact')

    await expect(controller.revokeWhere({ guestGeneration: binding.generation, tabId: binding.tabId })).resolves.toBe(1)
    expect(revoke).toHaveBeenCalledTimes(1)
    expect(controller.isAuthorizedRequest(binding.partition, binding.guestId, publicRef.guestUrl)).toBe(false)
  })

  it('fails closed instead of presenting unsanitized Markdown as a viewer', async () => {
    const { controller, revoke } = fixture('artifact', 'text/markdown; charset=utf-8')

    await expect(mint(controller, 'artifact')).rejects.toThrow('artifact-unsupported')
    expect(revoke).toHaveBeenCalledTimes(1)
  })
})
