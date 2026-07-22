import { promises as fs } from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  BrowserResourceGrantError,
  BrowserResourceGrantRegistry,
  type BrowserResourceGrantScope
} from './browser-resource-grants'

const scope: BrowserResourceGrantScope = {
  connectionId: 'desktop-connection-1',
  guestGeneration: 'guest-generation-1',
  hostId: 41,
  profile: 'coding',
  recipient: 'dashboard:user-1',
  sourceSessionId: 'session-1',
  tabId: 'browser:tab-1'
}

function registry(now = () => 1000) {
  let sequence = 0
  return new BrowserResourceGrantRegistry({
    now,
    ref: () => `${++sequence}`.padStart(32, '0')
  })
}

describe('BrowserResourceGrantRegistry', () => {
  let root: string

  beforeEach(async () => {
    root = await fs.mkdtemp(path.join(os.tmpdir(), 'hermes-browser-grants-'))
  })

  afterEach(async () => {
    await fs.rm(root, { force: true, recursive: true })
  })

  it('mints a path-free local opaque ref and revalidates exact bytes and scope', async () => {
    const file = path.join(root, 'page.html')
    await fs.writeFile(file, '<h1>hostile</h1>')
    const grants = registry()

    const publicRef = await grants.mintLocalArtifact(scope, file, root)

    expect(publicRef.localRef).not.toContain(root)
    expect(publicRef.guestUrl).not.toContain(root)
    expect(publicRef.guestUrl).toMatch(/^hermes-artifact:\/\/g-[A-Za-z0-9_-]{32}\/page.html$/)
    const canonicalFile = await fs.realpath(file)
    await expect(grants.resolveLocalArtifact(publicRef.localRef, scope)).resolves.toMatchObject({
      mimeType: 'text/html; charset=utf-8',
      path: canonicalFile
    })

    await fs.writeFile(file, '<h1>changed and longer</h1>')
    await expect(grants.resolveLocalArtifact(publicRef.localRef, scope)).rejects.toMatchObject({
      code: 'artifact-changed'
    })
    await expect(grants.resolveLocalArtifact(publicRef.localRef, scope)).rejects.toMatchObject({
      code: 'grant-unavailable'
    })
  })

  it('rejects local traversal, symlink escape, sensitive files, and unsupported bytes', async () => {
    const outside = path.join(path.dirname(root), 'outside.pdf')
    await fs.writeFile(outside, 'outside')
    await fs.symlink(outside, path.join(root, 'escape.pdf'))
    await fs.writeFile(path.join(root, '.env'), 'TOKEN=secret')
    await fs.writeFile(path.join(root, 'archive.zip'), 'zip')
    const grants = registry()

    await expect(grants.mintLocalArtifact(scope, '../outside.pdf', root)).rejects.toBeInstanceOf(
      BrowserResourceGrantError
    )
    await expect(grants.mintLocalArtifact(scope, path.join(root, 'escape.pdf'), root)).rejects.toMatchObject({
      code: 'artifact-out-of-scope'
    })
    await expect(grants.mintLocalArtifact(scope, path.join(root, '.env'), root)).rejects.toThrow(/sensitive/i)
    await expect(grants.mintLocalArtifact(scope, path.join(root, 'archive.zip'), root)).rejects.toMatchObject({
      code: 'artifact-unsupported'
    })

    await fs.rm(outside, { force: true })
  })

  it('keeps remote artifact credentials private and emits exact delivery headers', () => {
    const grants = registry()
    const publicRef = grants.retainRemoteArtifact(scope, {
      deliveryCredential: 'c'.repeat(32),
      displayName: 'report.pdf',
      gatewayOrigin: 'https://studio.example/',
      mimeType: 'application/pdf',
      remoteRef: 'r'.repeat(32),
      size: 42
    })

    expect(JSON.stringify(publicRef)).not.toContain('c'.repeat(32))
    expect(JSON.stringify(publicRef)).not.toContain('studio.example')
    const delivery = grants.remoteArtifactDelivery(publicRef.localRef, scope)
    expect(delivery.url).toBe(`https://studio.example/api/browser/artifacts/${'r'.repeat(32)}`)
    expect(delivery.headers).toEqual({
      'X-Hermes-Browser-Connection': scope.connectionId,
      'X-Hermes-Browser-Generation': scope.guestGeneration,
      'X-Hermes-Browser-Grant': 'c'.repeat(32),
      'X-Hermes-Browser-Profile': scope.profile,
      'X-Hermes-Browser-Recipient': scope.recipient,
      'X-Hermes-Browser-Source-Session': scope.sourceSessionId,
      'X-Hermes-Browser-Tab': scope.tabId
    })
  })

  it('injects preview credentials only on the exact gateway origin and grant path', () => {
    const grants = registry()
    const remoteRef = 'r'.repeat(32)
    const publicRef = grants.retainRemotePreview(scope, {
      deliveryCredential: 'c'.repeat(32),
      gatewayOrigin: 'https://studio.example/',
      proxyPath: `/api/browser/preview/${remoteRef}/app/index.html`,
      remoteRef
    })

    expect(publicRef.guestUrl).toBe(
      `https://studio.example/api/browser/preview/${remoteRef}/app/index.html`
    )
    expect(
      grants.previewHeadersFor(
        publicRef.localRef,
        scope,
        `https://studio.example/api/browser/preview/${remoteRef}/assets/app.js`
      )['X-Hermes-Browser-Grant']
    ).toBe('c'.repeat(32))

    expect(() =>
      grants.previewHeadersFor(
        publicRef.localRef,
        scope,
        `https://evil.example/api/browser/preview/${remoteRef}/assets/app.js`
      )
    ).toThrow('preview-target-mismatch')
    expect(() =>
      grants.previewHeadersFor(
        publicRef.localRef,
        scope,
        `https://studio.example/api/browser/preview/${remoteRef}/assets/app.js`
      )
    ).toThrow('grant-unavailable')
  })

  it('revokes valid credentials on cross-tab/generation use and on lifecycle selectors', () => {
    const grants = registry()
    const input = {
      deliveryCredential: 'c'.repeat(32),
      displayName: 'report.pdf',
      gatewayOrigin: 'https://studio.example/',
      mimeType: 'application/pdf',
      remoteRef: 'r'.repeat(32),
      size: 42
    }
    const first = grants.retainRemoteArtifact(scope, input)
    const wrong = { ...scope, guestGeneration: 'guest-generation-2' }

    expect(() => grants.remoteArtifactDelivery(first.localRef, wrong)).toThrow('grant-scope-mismatch')
    expect(() => grants.remoteArtifactDelivery(first.localRef, scope)).toThrow('grant-unavailable')

    grants.retainRemoteArtifact(scope, input)
    grants.retainRemoteArtifact(scope, { ...input, remoteRef: 's'.repeat(32) })
    expect(grants.revokeWhere({ hostId: scope.hostId, tabId: scope.tabId })).toBe(2)
    expect(grants.revokeAll()).toBe(0)
  })

  it('expires grants without persistence or retargeting', () => {
    let now = 1000
    const grants = registry(() => now)
    const publicRef = grants.retainRemoteArtifact(
      scope,
      {
        deliveryCredential: 'c'.repeat(32),
        displayName: 'report.pdf',
        gatewayOrigin: 'https://studio.example/',
        mimeType: 'application/pdf',
        remoteRef: 'r'.repeat(32),
        size: 42
      },
      10
    )

    now += 10
    expect(() => grants.remoteArtifactDelivery(publicRef.localRef, scope)).toThrow('grant-unavailable')
  })
})
