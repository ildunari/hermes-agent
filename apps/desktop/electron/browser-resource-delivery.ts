import type { Session } from 'electron'

import {
  type BrowserGrantPublicRef,
  BrowserResourceGrantError,
  BrowserResourceGrantRegistry,
  type BrowserResourceGrantScope
} from './browser-resource-grants'

const ARTIFACT_SCHEME = 'hermes-artifact'
const GRANT_HEADER_PREFIX = 'x-hermes-browser-'
const MAX_SOURCE_TARGET = 16 * 1024
const SAFE_METHODS = new Set(['GET', 'HEAD'])

export interface BrowserResourceGuestBinding {
  generation: string
  guestId: number
  hostId: number
  partition: string
  profile: string
  tabId: string
  workspaceId: string
}

export interface BrowserResourceMintRequest {
  generation: string
  kind: 'artifact' | 'preview'
  profile: string
  sourceSessionId: string
  tabId: string
  target: string
  workspaceId: string
}

interface GatewayGrantBase {
  connectionId: string
  deliveryCredential: string
  expiresInSeconds: number
  gatewayOrigin: string
  opaqueRef: string
  recipient: string
  revoke: () => Promise<void>
}

export interface GatewayArtifactGrant extends GatewayGrantBase {
  displayName: string
  kind: 'artifact'
  mimeType: string
  size: number
}

export interface GatewayPreviewGrant extends GatewayGrantBase {
  kind: 'preview'
  proxyPath: string
}

export type GatewayResourceGrant = GatewayArtifactGrant | GatewayPreviewGrant

interface RetainedBinding {
  expiresAt: number
  gatewayOrigin: string
  guestId: number
  guestUrl: string
  kind: BrowserGrantPublicRef['kind']
  localRef: string
  mimeType?: string
  partition: string
  revoke: () => Promise<void>
  scope: BrowserResourceGrantScope
}

interface ResourceDeliveryDeps {
  fetch: (url: string, init: RequestInit & { bypassCustomProtocolHandlers?: boolean }) => Promise<Response>
  mintGateway: (
    kind: BrowserResourceMintRequest['kind'],
    target: string,
    scope: BrowserResourceGrantScope
  ) => Promise<GatewayResourceGrant>
  now?: () => number
  registry?: BrowserResourceGrantRegistry
}

function validText(value: unknown, max = 256): value is string {
  return (
    typeof value === 'string' &&
    value.length > 0 &&
    value.length <= max &&
    !Array.from(value).some(character => {
      const code = character.charCodeAt(0)

      return code <= 31 || code === 127
    })
  )
}

function artifactRef(rawUrl: string): string | null {
  try {
    const parsed = new URL(rawUrl)

    return parsed.protocol === `${ARTIFACT_SCHEME}:` && /^g-[A-Za-z0-9_-]{32}$/.test(parsed.hostname)
      ? parsed.hostname.slice(2)
      : null
  } catch {
    return null
  }
}

function responseHeaders(mimeType: string, upstream: Headers): Headers {
  const headers = new Headers()

  for (const name of ['accept-ranges', 'content-length', 'content-range']) {
    const value = upstream.get(name)

    if (value) {headers.set(name, value)}
  }

  headers.set('Cache-Control', 'no-store')
  headers.set('Content-Type', mimeType)
  headers.set('Cross-Origin-Resource-Policy', 'same-origin')
  headers.set('Referrer-Policy', 'no-referrer')
  headers.set('X-Content-Type-Options', 'nosniff')
  headers.set(
    'Content-Security-Policy',
    mimeType.startsWith('text/html')
      ? "sandbox allow-scripts allow-forms; default-src 'none'; img-src 'self' data: blob:; media-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; font-src 'self' data:; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
      : "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
  )

  return headers
}

function exactContentType(actual: string | null, expected: string): boolean {
  return Boolean(actual) && actual!.split(';', 1)[0].trim().toLowerCase() === expected.split(';', 1)[0].trim().toLowerCase()
}

/** Main-process owner for grant minting, protocol delivery, request injection, and revocation. */
export class BrowserResourceDeliveryController {
  readonly #artifactAdmissions = new Map<string, { count: number; expiresAt: number }>()
  readonly #bindings = new Map<string, RetainedBinding>()
  readonly #deps: Required<Pick<ResourceDeliveryDeps, 'fetch' | 'mintGateway'>>
  readonly #installedPartitions = new Set<string>()
  readonly #now: () => number
  readonly #registry: BrowserResourceGrantRegistry

  constructor(deps: ResourceDeliveryDeps) {
    this.#deps = deps
    this.#now = deps.now ?? Date.now
    this.#registry = deps.registry ?? new BrowserResourceGrantRegistry({ now: this.#now })
  }

  async mint(binding: BrowserResourceGuestBinding, request: BrowserResourceMintRequest): Promise<BrowserGrantPublicRef> {
    if (
      request?.generation !== binding.generation ||
      request?.tabId !== binding.tabId ||
      request?.profile.trim().toLowerCase() !== binding.profile ||
      request?.workspaceId !== binding.workspaceId ||
      !validText(request?.sourceSessionId) ||
      !validText(request?.target, MAX_SOURCE_TARGET) ||
      (request?.kind !== 'artifact' && request?.kind !== 'preview')
    ) {
      throw new BrowserResourceGrantError('grant-scope-mismatch')
    }

    const provisionalScope: BrowserResourceGrantScope = {
      connectionId: '',
      guestGeneration: binding.generation,
      hostId: binding.hostId,
      profile: binding.profile,
      recipient: '',
      sourceSessionId: request.sourceSessionId,
      tabId: binding.tabId
    }
    const gateway = await this.#deps.mintGateway(request.kind, request.target, provisionalScope)

    if (gateway.kind === 'artifact' && gateway.mimeType.startsWith('text/markdown')) {
      await gateway.revoke().catch(() => undefined)
      throw new BrowserResourceGrantError('artifact-unsupported')
    }

    const scope = {
      ...provisionalScope,
      connectionId: gateway.connectionId,
      recipient: gateway.recipient
    }

    if (!validText(scope.connectionId) || !validText(scope.recipient)) {
      await gateway.revoke().catch(() => undefined)
      throw new BrowserResourceGrantError('invalid-remote-grant')
    }

    let publicRef: BrowserGrantPublicRef
    const ttlMs = gateway.expiresInSeconds * 1000

    try {
      publicRef =
        gateway.kind === 'artifact'
          ? this.#registry.retainRemoteArtifact(
              scope,
              {
                deliveryCredential: gateway.deliveryCredential,
                displayName: gateway.displayName,
                gatewayOrigin: gateway.gatewayOrigin,
                mimeType: gateway.mimeType,
                remoteRef: gateway.opaqueRef,
                size: gateway.size
              },
              ttlMs
            )
          : this.#registry.retainRemotePreview(
              scope,
              {
                deliveryCredential: gateway.deliveryCredential,
                gatewayOrigin: gateway.gatewayOrigin,
                proxyPath: gateway.proxyPath,
                remoteRef: gateway.opaqueRef
              },
              ttlMs
            )
    } catch (error) {
      await gateway.revoke().catch(() => undefined)
      throw error
    }

    this.#bindings.set(publicRef.localRef, {
      expiresAt: this.#now() + ttlMs,
      gatewayOrigin: gateway.gatewayOrigin,
      guestId: binding.guestId,
      guestUrl: publicRef.guestUrl,
      kind: publicRef.kind,
      localRef: publicRef.localRef,
      mimeType: gateway.kind === 'artifact' ? gateway.mimeType : undefined,
      partition: binding.partition,
      revoke: gateway.revoke,
      scope
    })

    return publicRef
  }

  installSession(browserSession: Session, partition: string): void {
    if (this.#installedPartitions.has(partition)) {return}
    this.#installedPartitions.add(partition)

    void browserSession.protocol.handle(ARTIFACT_SCHEME, request => this.#handleArtifact(request, partition))
    browserSession.webRequest.onBeforeSendHeaders((details, callback) => {
      const binding = this.#previewBinding(details.webContentsId, partition, details.url)

      if (!binding) {
        callback({ requestHeaders: details.requestHeaders })

        return
      }

      const requestHeaders = Object.fromEntries(
        Object.entries(details.requestHeaders).filter(([name]) => {
          const lower = name.toLowerCase()

          return lower !== 'authorization' && lower !== 'cookie' && lower !== 'x-hermes-session-token' && !lower.startsWith(GRANT_HEADER_PREFIX)
        })
      )

      try {
        Object.assign(
          requestHeaders,
          this.#registry.previewHeadersFor(binding.localRef, binding.scope, details.url)
        )
        callback({ requestHeaders })
      } catch {
        void this.#revokeBinding(binding)
        callback({ cancel: true, requestHeaders })
      }
    })
  }

  isAuthorizedRequest(
    partition: string,
    webContentsId: number | undefined,
    rawUrl: string,
    admitArtifact = false
  ): boolean {
    this.#purgeExpired()
    if (!Number.isSafeInteger(webContentsId)) {return false}

    const ref = artifactRef(rawUrl)

    if (ref) {
      const binding = this.#bindings.get(ref)
      const allowed = Boolean(
        binding &&
        binding.kind !== 'remote-preview' &&
        binding.partition === partition &&
        binding.guestId === webContentsId
      )

      if (allowed && admitArtifact) {
        const key = this.#artifactAdmissionKey(partition, rawUrl)
        const admission = this.#artifactAdmissions.get(key)

        this.#artifactAdmissions.set(key, {
          count: Math.min(32, (admission?.count ?? 0) + 1),
          expiresAt: this.#now() + 5_000
        })
      }

      return allowed
    }

    return Boolean(this.#previewBinding(webContentsId, partition, rawUrl))
  }

  async revokeWhere(selector: Partial<BrowserResourceGrantScope>): Promise<number> {
    const rows = [...this.#bindings.values()].filter(binding =>
      Object.entries(selector).every(([key, value]) => binding.scope[key as keyof BrowserResourceGrantScope] === value)
    )

    await Promise.allSettled(rows.map(binding => this.#revokeBinding(binding)))

    return rows.length
  }

  async revokeAll(): Promise<number> {
    const rows = [...this.#bindings.values()]

    await Promise.allSettled(rows.map(binding => this.#revokeBinding(binding)))
    this.#registry.revokeAll()

    return rows.length
  }

  async #handleArtifact(request: Request, partition: string): Promise<Response> {
    const method = request.method.toUpperCase()
    const ref = artifactRef(request.url)
    const binding = ref ? this.#bindings.get(ref) : undefined

    if (
      !SAFE_METHODS.has(method) ||
      !binding ||
      binding.kind !== 'remote-artifact' ||
      binding.partition !== partition ||
      binding.guestUrl !== request.url ||
      !this.#consumeArtifactAdmission(partition, request.url) ||
      !binding.mimeType ||
      binding.mimeType.startsWith('text/markdown')
    ) {
      return new Response('Artifact unavailable', { status: binding?.mimeType?.startsWith('text/markdown') ? 415 : 404 })
    }

    try {
      const delivery = this.#registry.remoteArtifactDelivery(binding.localRef, binding.scope)
      const range = request.headers.get('range')
      const upstream = await this.#deps.fetch(delivery.url, {
        bypassCustomProtocolHandlers: true,
        credentials: 'omit',
        headers: { ...delivery.headers, ...(range ? { Range: range } : {}) },
        method,
        redirect: 'error'
      })

      if (!upstream.ok || !exactContentType(upstream.headers.get('content-type'), binding.mimeType)) {
        await this.#revokeBinding(binding)

        return new Response('Artifact unavailable', { status: upstream.status >= 400 ? upstream.status : 502 })
      }

      return new Response(method === 'HEAD' ? null : upstream.body, {
        headers: responseHeaders(binding.mimeType, upstream.headers),
        status: upstream.status,
        statusText: upstream.statusText
      })
    } catch {
      await this.#revokeBinding(binding)

      return new Response('Artifact unavailable', { status: 404 })
    }
  }

  #previewBinding(webContentsId: number | undefined, partition: string, rawUrl: string): RetainedBinding | null {
    this.#purgeExpired()
    if (!Number.isSafeInteger(webContentsId)) {return null}

    let target: URL
    try {
      target = new URL(rawUrl)
    } catch {
      return null
    }

    for (const binding of this.#bindings.values()) {
      if (
        binding.kind === 'remote-preview' &&
        binding.partition === partition &&
        binding.guestId === webContentsId
      ) {
        const granted = new URL(binding.guestUrl)
        const prefix = granted.pathname.slice(0, granted.pathname.indexOf('/', '/api/browser/preview/'.length) + 1)

        const targetOrigin =
          target.protocol === 'ws:' || target.protocol === 'wss:'
            ? `${target.protocol === 'wss:' ? 'https:' : 'http:'}//${target.host}`
            : target.origin

        if (targetOrigin === granted.origin && target.pathname.startsWith(prefix)) {return binding}
      }
    }

    return null
  }

  async #revokeBinding(binding: RetainedBinding): Promise<void> {
    if (this.#bindings.get(binding.localRef) !== binding) {return}
    this.#bindings.delete(binding.localRef)
    this.#artifactAdmissions.delete(this.#artifactAdmissionKey(binding.partition, binding.guestUrl))
    this.#registry.revokeRef(binding.localRef)
    await binding.revoke().catch(() => undefined)
  }

  #artifactAdmissionKey(partition: string, rawUrl: string): string {
    return `${partition}\0${rawUrl}`
  }

  #consumeArtifactAdmission(partition: string, rawUrl: string): boolean {
    const key = this.#artifactAdmissionKey(partition, rawUrl)
    const admission = this.#artifactAdmissions.get(key)

    if (!admission || admission.expiresAt <= this.#now()) {
      this.#artifactAdmissions.delete(key)

      return false
    }

    if (admission.count <= 1) {
      this.#artifactAdmissions.delete(key)
    } else {
      this.#artifactAdmissions.set(key, { ...admission, count: admission.count - 1 })
    }

    return true
  }

  #purgeExpired(): void {
    const now = this.#now()

    for (const [key, admission] of this.#artifactAdmissions) {
      if (admission.expiresAt <= now) {this.#artifactAdmissions.delete(key)}
    }

    for (const binding of this.#bindings.values()) {
      if (binding.expiresAt <= now) {void this.#revokeBinding(binding)}
    }
  }
}

export { ARTIFACT_SCHEME }
