import crypto from 'node:crypto'
import { promises as fs } from 'node:fs'
import path from 'node:path'

import { resolveReadableFileForIpc } from './hardening'

const REMOTE_REF = /^[A-Za-z0-9_-]{32}$/
const DEFAULT_TTL_MS = 2 * 60_000
const MAX_TTL_MS = 10 * 60_000
const MAX_ACTIVE_GRANTS = 512
const MAX_ARTIFACT_BYTES = 512 * 1024 * 1024

const INLINE_MIME = new Map([
  ['.gif', 'image/gif'],
  ['.htm', 'text/html; charset=utf-8'],
  ['.html', 'text/html; charset=utf-8'],
  ['.jpeg', 'image/jpeg'],
  ['.jpg', 'image/jpeg'],
  ['.md', 'text/markdown; charset=utf-8'],
  ['.pdf', 'application/pdf'],
  ['.png', 'image/png'],
  ['.svg', 'image/svg+xml'],
  ['.webp', 'image/webp']
])

export interface BrowserResourceGrantScope {
  connectionId: string
  guestGeneration: string
  hostId: number
  profile: string
  recipient: string
  sourceSessionId: string
  tabId: string
}

interface BaseGrant {
  expiresAt: number
  kind: 'local-artifact' | 'remote-artifact' | 'remote-preview'
  localRef: string
  scope: BrowserResourceGrantScope
}

interface LocalArtifactGrant extends BaseGrant {
  device: number
  inode: number
  kind: 'local-artifact'
  mimeType: string
  modifiedAt: number
  path: string
  size: number
  workspaceRoot: string
}

interface RemoteGrant extends BaseGrant {
  deliveryCredential: string
  gatewayOrigin: string
  remoteRef: string
}

interface RemoteArtifactGrant extends RemoteGrant {
  displayName: string
  kind: 'remote-artifact'
  mimeType: string
  size: number
}

interface RemotePreviewGrant extends RemoteGrant {
  kind: 'remote-preview'
  proxyPath: string
}

type BrowserResourceGrant = LocalArtifactGrant | RemoteArtifactGrant | RemotePreviewGrant

export interface BrowserGrantPublicRef {
  guestUrl: string
  kind: BrowserResourceGrant['kind']
  localRef: string
}

export interface BrowserGrantDelivery {
  headers: Readonly<Record<string, string>>
  url: string
}

export interface BrowserLocalArtifactRead {
  mimeType: string
  path: string
  size: number
}

export class BrowserResourceGrantError extends Error {
  constructor(readonly code: string) {
    super(code)
  }
}

function validScopeValue(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    value.length > 0 &&
    value.length <= 256 &&
    !Array.from(value).some(character => {
      const code = character.charCodeAt(0)

      return code <= 31 || code === 127
    })
  )
}

function validScope(scope: BrowserResourceGrantScope): BrowserResourceGrantScope {
  if (!Number.isSafeInteger(scope?.hostId) || scope.hostId <= 0) {
    throw new BrowserResourceGrantError('invalid-scope')
  }
  const values = [
    scope.recipient,
    scope.profile,
    scope.connectionId,
    scope.tabId,
    scope.guestGeneration,
    scope.sourceSessionId
  ]
  if (!values.every(validScopeValue)) {
    throw new BrowserResourceGrantError('invalid-scope')
  }
  return Object.freeze({ ...scope })
}

function exactScope(left: BrowserResourceGrantScope, right: BrowserResourceGrantScope): boolean {
  return (
    left.hostId === right.hostId &&
    left.recipient === right.recipient &&
    left.profile === right.profile &&
    left.connectionId === right.connectionId &&
    left.tabId === right.tabId &&
    left.guestGeneration === right.guestGeneration &&
    left.sourceSessionId === right.sourceSessionId
  )
}

function grantExpiry(now: number, ttlMs: number): number {
  if (!Number.isSafeInteger(ttlMs) || ttlMs < 1 || ttlMs > MAX_TTL_MS) {
    throw new BrowserResourceGrantError('invalid-expiry')
  }
  return now + ttlMs
}

function gatewayOrigin(raw: string): string {
  try {
    const parsed = new URL(raw)
    if (
      !['http:', 'https:'].includes(parsed.protocol) ||
      parsed.username ||
      parsed.password ||
      parsed.search ||
      parsed.hash ||
      parsed.pathname !== '/'
    ) {
      throw new Error('invalid')
    }
    return parsed.origin
  } catch {
    throw new BrowserResourceGrantError('invalid-gateway-origin')
  }
}

function opaqueRef(): string {
  return crypto.randomBytes(24).toString('base64url')
}

function containsPath(root: string, candidate: string): boolean {
  const relative = path.relative(root, candidate)
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative))
}

export class BrowserResourceGrantRegistry {
  readonly #grants = new Map<string, BrowserResourceGrant>()
  readonly #now: () => number
  readonly #ref: () => string

  constructor(deps: { now?: () => number; ref?: () => string } = {}) {
    this.#now = deps.now ?? Date.now
    this.#ref = deps.ref ?? opaqueRef
  }

  #newRef(): string {
    const ref = this.#ref()
    if (!REMOTE_REF.test(ref)) {throw new BrowserResourceGrantError('entropy-unavailable')}
    return ref
  }

  #insert(grant: BrowserResourceGrant): BrowserGrantPublicRef {
    this.#purgeExpired()
    if (this.#grants.size >= MAX_ACTIVE_GRANTS) {throw new BrowserResourceGrantError('grant-capacity')}
    this.#grants.set(grant.localRef, grant)
    return {
      guestUrl:
        grant.kind === 'remote-preview'
          ? `${grant.gatewayOrigin}${grant.proxyPath}`
          : `hermes-artifact://g-${grant.localRef}/${encodeURIComponent(
              grant.kind === 'remote-artifact' ? grant.displayName : path.basename(grant.path)
            )}`,
      kind: grant.kind,
      localRef: grant.localRef
    }
  }

  async mintLocalArtifact(
    scopeInput: BrowserResourceGrantScope,
    candidate: string,
    workspaceRoot: string,
    ttlMs = DEFAULT_TTL_MS
  ): Promise<BrowserGrantPublicRef> {
    const scope = validScope(scopeInput)
    if (typeof candidate !== 'string' || candidate.split(/[\\/]/).includes('..')) {
      throw new BrowserResourceGrantError('artifact-out-of-scope')
    }
    const root = await fs.realpath(workspaceRoot)
    const rootInfo = await fs.stat(root)
    if (!rootInfo.isDirectory()) {throw new BrowserResourceGrantError('artifact-out-of-scope')}
    const resolved = await resolveReadableFileForIpc(candidate, { baseDir: root, purpose: 'Browser artifact' })
    if (!containsPath(root, resolved.realPath)) {
      throw new BrowserResourceGrantError('artifact-out-of-scope')
    }
    const mimeType = INLINE_MIME.get(path.extname(resolved.realPath).toLowerCase())
    if (!mimeType) {throw new BrowserResourceGrantError('artifact-unsupported')}
    if (resolved.stat.size > MAX_ARTIFACT_BYTES) {throw new BrowserResourceGrantError('artifact-too-large')}
    const grant: LocalArtifactGrant = {
      device: Number(resolved.stat.dev),
      expiresAt: grantExpiry(this.#now(), ttlMs),
      inode: Number(resolved.stat.ino),
      kind: 'local-artifact',
      localRef: this.#newRef(),
      mimeType,
      modifiedAt: resolved.stat.mtimeMs,
      path: resolved.realPath,
      scope,
      size: resolved.stat.size,
      workspaceRoot: root
    }
    return this.#insert(grant)
  }

  retainRemoteArtifact(
    scopeInput: BrowserResourceGrantScope,
    input: {
      deliveryCredential: string
      displayName: string
      gatewayOrigin: string
      mimeType: string
      remoteRef: string
      size: number
    },
    ttlMs = DEFAULT_TTL_MS
  ): BrowserGrantPublicRef {
    const scope = validScope(scopeInput)
    if (
      !REMOTE_REF.test(input.remoteRef) ||
      !REMOTE_REF.test(input.deliveryCredential) ||
      !validScopeValue(input.displayName) ||
      !Number.isSafeInteger(input.size) ||
      input.size < 0 ||
      input.size > MAX_ARTIFACT_BYTES ||
      ![...INLINE_MIME.values()].includes(input.mimeType)
    ) {
      throw new BrowserResourceGrantError('invalid-remote-grant')
    }
    return this.#insert({
      deliveryCredential: input.deliveryCredential,
      displayName: input.displayName,
      expiresAt: grantExpiry(this.#now(), ttlMs),
      gatewayOrigin: gatewayOrigin(input.gatewayOrigin),
      kind: 'remote-artifact',
      localRef: this.#newRef(),
      mimeType: input.mimeType,
      remoteRef: input.remoteRef,
      scope,
      size: input.size
    })
  }

  retainRemotePreview(
    scopeInput: BrowserResourceGrantScope,
    input: { deliveryCredential: string; gatewayOrigin: string; proxyPath: string; remoteRef: string },
    ttlMs = DEFAULT_TTL_MS
  ): BrowserGrantPublicRef {
    const scope = validScope(scopeInput)
    if (
      !REMOTE_REF.test(input.remoteRef) ||
      !REMOTE_REF.test(input.deliveryCredential) ||
      !input.proxyPath.startsWith(`/api/browser/preview/${input.remoteRef}/`) ||
      input.proxyPath.includes('?') ||
      input.proxyPath.includes('#') ||
      input.proxyPath.includes('\\')
    ) {
      throw new BrowserResourceGrantError('invalid-remote-grant')
    }
    return this.#insert({
      deliveryCredential: input.deliveryCredential,
      expiresAt: grantExpiry(this.#now(), ttlMs),
      gatewayOrigin: gatewayOrigin(input.gatewayOrigin),
      kind: 'remote-preview',
      localRef: this.#newRef(),
      proxyPath: input.proxyPath,
      remoteRef: input.remoteRef,
      scope
    })
  }

  async resolveLocalArtifact(
    localRef: string,
    scopeInput: BrowserResourceGrantScope
  ): Promise<BrowserLocalArtifactRead> {
    const grant = this.#authorize(localRef, scopeInput, 'local-artifact') as LocalArtifactGrant
    const current = await resolveReadableFileForIpc(grant.path, { purpose: 'Browser artifact' })
    if (
      current.realPath !== grant.path ||
      !containsPath(grant.workspaceRoot, current.realPath) ||
      Number(current.stat.dev) !== grant.device ||
      Number(current.stat.ino) !== grant.inode ||
      current.stat.size !== grant.size ||
      current.stat.mtimeMs !== grant.modifiedAt
    ) {
      this.#grants.delete(localRef)
      throw new BrowserResourceGrantError('artifact-changed')
    }
    return { mimeType: grant.mimeType, path: grant.path, size: grant.size }
  }

  remoteArtifactDelivery(localRef: string, scopeInput: BrowserResourceGrantScope): BrowserGrantDelivery {
    const grant = this.#authorize(localRef, scopeInput, 'remote-artifact') as RemoteArtifactGrant
    return {
      headers: this.#headers(grant),
      url: `${grant.gatewayOrigin}/api/browser/artifacts/${grant.remoteRef}`
    }
  }

  previewHeadersFor(
    localRef: string,
    scopeInput: BrowserResourceGrantScope,
    requestUrl: string
  ): Readonly<Record<string, string>> {
    const grant = this.#authorize(localRef, scopeInput, 'remote-preview') as RemotePreviewGrant
    let target: URL
    try {
      target = new URL(requestUrl)
    } catch {
      throw new BrowserResourceGrantError('preview-target-mismatch')
    }
    const prefix = `/api/browser/preview/${grant.remoteRef}/`
    const targetOrigin =
      target.protocol === 'ws:' || target.protocol === 'wss:'
        ? `${target.protocol === 'wss:' ? 'https:' : 'http:'}//${target.host}`
        : target.origin
    if (targetOrigin !== grant.gatewayOrigin || !target.pathname.startsWith(prefix) || target.username || target.password) {
      this.#grants.delete(localRef)
      throw new BrowserResourceGrantError('preview-target-mismatch')
    }
    return this.#headers(grant)
  }

  #headers(grant: RemoteGrant): Readonly<Record<string, string>> {
    return Object.freeze({
      'X-Hermes-Browser-Connection': grant.scope.connectionId,
      'X-Hermes-Browser-Generation': grant.scope.guestGeneration,
      'X-Hermes-Browser-Grant': grant.deliveryCredential,
      'X-Hermes-Browser-Profile': grant.scope.profile,
      'X-Hermes-Browser-Recipient': grant.scope.recipient,
      'X-Hermes-Browser-Source-Session': grant.scope.sourceSessionId,
      'X-Hermes-Browser-Tab': grant.scope.tabId
    })
  }

  #authorize(localRef: string, scopeInput: BrowserResourceGrantScope, kind: BrowserResourceGrant['kind']) {
    const scope = validScope(scopeInput)
    this.#purgeExpired()
    const grant = this.#grants.get(localRef)
    if (!grant || grant.kind !== kind) {throw new BrowserResourceGrantError('grant-unavailable')}
    if (!exactScope(grant.scope, scope)) {
      this.#grants.delete(localRef)
      throw new BrowserResourceGrantError('grant-scope-mismatch')
    }
    return grant
  }

  revokeWhere(selector: Partial<BrowserResourceGrantScope>): number {
    const entries = Object.entries(selector)
    if (entries.length === 0) {throw new BrowserResourceGrantError('invalid-scope')}
    let count = 0
    for (const [ref, grant] of this.#grants) {
      if (entries.every(([key, value]) => grant.scope[key as keyof BrowserResourceGrantScope] === value)) {
        this.#grants.delete(ref)
        count += 1
      }
    }
    return count
  }

  revokeRef(localRef: string): boolean {
    if (!REMOTE_REF.test(localRef)) {return false}

    return this.#grants.delete(localRef)
  }

  revokeAll(): number {
    const count = this.#grants.size
    this.#grants.clear()
    return count
  }

  #purgeExpired() {
    const now = this.#now()
    for (const [ref, grant] of this.#grants) {
      if (grant.expiresAt <= now) {this.#grants.delete(ref)}
    }
  }
}
