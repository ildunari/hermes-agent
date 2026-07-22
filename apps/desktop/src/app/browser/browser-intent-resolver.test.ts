import { beforeEach, describe, expect, it } from 'vitest'

import {
  type BrowserIntentRequest,
  type BrowserIntentSource,
  executeBrowserIntent,
  resolveBrowserIntent
} from './browser-intent-resolver'
import {
  $browserTabs,
  $foregroundBrowserTabId,
  type BrowserGeometry,
  clearBrowserTabs,
  createBrowserTab
} from './browser-store'

const scope = { profile: 'coding', workspaceId: 'workspace-1' }
const geometry: BrowserGeometry = { height: 600, width: 900, x: 0, y: 0 }
const targetRef = 'https://example.test/'

function request(overrides: Partial<BrowserIntentRequest> = {}): BrowserIntentRequest {
  return {
    artifactGrant: 'not-applicable',
    credentialAuthorization: 'not-applicable',
    currentScope: scope,
    foregroundIntent: {
      currentToken: 'intent-current',
      explicit: true,
      mayFocus: true,
      principal: 'user',
      token: 'intent-current'
    },
    previewGrant: 'not-applicable',
    requestedDisposition: 'new-tab',
    source: 'transcript-link',
    target: 'web',
    targetLocation: 'internet',
    targetRef,
    targetScope: scope,
    validation: 'resolved',
    ...overrides
  }
}

function tabResult(overrides: Record<string, unknown> = {}) {
  return {
    activation: 'execute',
    disposition: 'new-tab',
    focus: true,
    kind: 'browser-tab',
    scope,
    targetRef,
    ...overrides
  }
}

beforeEach(clearBrowserTabs)

describe('resolveBrowserIntent', () => {
  it('executes a direct current foreground user activation in-app', () => {
    expect(resolveBrowserIntent(request())).toEqual(tabResult())
  })

  it.each<BrowserIntentSource>([
    'background-result',
    'passive-link',
    'scheduled-result',
    'unsolicited-agent'
  ])('%s remains an inert visible in-app offer', source => {
    expect(resolveBrowserIntent(request({ foregroundIntent: undefined, source }))).toEqual(
      tabResult({ activation: 'offer', focus: false })
    )
  })

  it('offers agent navigation without foreground authority and executes a live authorized command without focus theft', () => {
    expect(resolveBrowserIntent(request({ foregroundIntent: undefined, source: 'agent-navigation' }))).toEqual(
      tabResult({ activation: 'offer', focus: false })
    )
    expect(
      resolveBrowserIntent(
        request({
          foregroundIntent: {
            currentToken: 'agent-current',
            explicit: true,
            mayFocus: false,
            principal: 'agent',
            token: 'agent-current'
          },
          requestedDisposition: 'current-tab',
          source: 'agent-navigation'
        })
      )
    ).toEqual(tabResult({ disposition: 'current-tab', focus: false }))
  })

  it.each([
    ['scope mismatch', { targetScope: { profile: 'other', workspaceId: 'workspace-1' } }, 'scope-mismatch'],
    ['hostile target', { validation: 'hostile' }, 'hostile-target'],
    ['unresolved target', { validation: 'unresolved' }, 'unresolved-target'],
    [
      'expired interaction',
      {
        foregroundIntent: {
          currentToken: 'newer-intent',
          explicit: true,
          mayFocus: true,
          principal: 'user',
          token: 'stale-intent'
        }
      },
      'intent-expired'
    ]
  ] as const)('blocks %s without fallback', (_name, overrides, reason) => {
    expect(resolveBrowserIntent(request(overrides as Partial<BrowserIntentRequest>))).toEqual({
      kind: 'blocked',
      reason
    })
  })

  it('requires an opaque artifact grant before returning an artifact tab command', () => {
    expect(resolveBrowserIntent(request({ artifactGrant: 'required', target: 'artifact' }))).toEqual({
      kind: 'blocked',
      reason: 'grant-required'
    })
    expect(
      resolveBrowserIntent(
        request({
          artifactGrant: 'available',
          target: 'artifact',
          targetRef: 'hermes-artifact://opaque-grant/file.pdf'
        })
      )
    ).toEqual(tabResult({ kind: 'artifact-tab', targetRef: 'hermes-artifact://opaque-grant/file.pdf' }))

    for (const invalidRef of ['/Users/kosta/secret.pdf', 'hermes-artifact:', 'hermes-artifact:/etc/passwd']) {
      expect(
        resolveBrowserIntent(request({ artifactGrant: 'available', target: 'artifact', targetRef: invalidRef }))
      ).toEqual({ kind: 'blocked', reason: 'invalid-target' })
    }
  })

  it('keeps external and download dispositions offer-only', () => {
    expect(resolveBrowserIntent(request({ requestedDisposition: 'external' }))).toEqual({
      kind: 'external-offer',
      reason: 'explicit-system-disposition-required'
    })
    expect(resolveBrowserIntent(request({ requestedDisposition: 'download' }))).toEqual({
      kind: 'download-offer',
      reason: 'human-confirmation-required'
    })
  })

  it('applies validation and scope checks before any external offer', () => {
    expect(resolveBrowserIntent(request({ requestedDisposition: 'external', validation: 'hostile' }))).toEqual({
      kind: 'blocked',
      reason: 'hostile-target'
    })
  })

  it.each(['javascript:alert(1)', 'data:text/html,hostile', 'mailto:test@example.com', 'not a url'])(
    'rejects %s as an in-app web target even when a caller claims it was resolved',
    invalidTarget => {
      expect(resolveBrowserIntent(request({ targetRef: invalidTarget }))).toEqual({
        kind: 'blocked',
        reason: 'invalid-target'
      })
    }
  )

  it('requires explicit credential authorization for credential-bearing web targets', () => {
    const credentialTarget = 'https://user:secret@example.test/private'

    expect(
      resolveBrowserIntent(
        request({ credentialAuthorization: 'required', targetRef: credentialTarget })
      )
    ).toEqual({ kind: 'blocked', reason: 'grant-required' })
    expect(
      resolveBrowserIntent(
        request({ credentialAuthorization: 'confirmed', targetRef: credentialTarget })
      )
    ).toEqual(tabResult({ targetRef: credentialTarget }))
  })

  it('rejects blank scope and compares normalized profile identities', () => {
    expect(resolveBrowserIntent(request({ targetScope: { profile: ' ', workspaceId: scope.workspaceId } }))).toEqual({
      kind: 'blocked',
      reason: 'scope-mismatch'
    })
    expect(
      resolveBrowserIntent(
        request({ currentScope: { ...scope, profile: ' coding ' }, targetScope: { ...scope, profile: 'coding' } })
      )
    ).toEqual(tabResult())
  })

  it('turns a browser popup into an offer while allowing same-tab user navigation', () => {
    expect(resolveBrowserIntent(request({ source: 'browser-page-link' }))).toEqual(
      tabResult({ activation: 'offer', focus: false })
    )
    expect(
      resolveBrowserIntent(request({ requestedDisposition: 'current-tab', source: 'browser-page-link' }))
    ).toEqual(tabResult({ disposition: 'current-tab' }))
  })

  it('requires an exact Studio preview grant but not a MacBook loopback grant', () => {
    const preview = { target: 'preview' as const, targetRef: 'http://127.0.0.1:5174/' }

    expect(
      resolveBrowserIntent(
        request({ ...preview, previewGrant: 'required', targetLocation: 'studio-loopback' })
      )
    ).toEqual({ kind: 'blocked', reason: 'grant-required' })
    expect(
      resolveBrowserIntent(
        request({ ...preview, previewGrant: 'not-applicable', targetLocation: 'macbook-loopback' })
      )
    ).toEqual(tabResult({ targetRef: preview.targetRef }))

    expect(
      resolveBrowserIntent(
        request({
          previewGrant: 'required',
          target: 'web',
          targetLocation: 'studio-loopback',
          targetRef: 'http://10.0.0.5:5173/'
        })
      )
    ).toEqual({ kind: 'blocked', reason: 'grant-required' })
  })

  it('reports a source/principal mismatch separately from expiry', () => {
    expect(
      resolveBrowserIntent(
        request({
          foregroundIntent: {
            currentToken: 'agent-current',
            explicit: true,
            mayFocus: true,
            principal: 'agent',
            token: 'agent-current'
          },
          source: 'transcript-link'
        })
      )
    ).toEqual({ kind: 'blocked', reason: 'principal-mismatch' })

    expect(
      resolveBrowserIntent(
        request({
          foregroundIntent: {
            currentToken: 'intent-current',
            explicit: false,
            mayFocus: true,
            principal: 'user',
            token: 'intent-current'
          }
        })
      )
    ).toEqual({ kind: 'blocked', reason: 'intent-required' })
  })
})

describe('executeBrowserIntent', () => {
  it('creates and focuses only an executable new-tab result', () => {
    const result = executeBrowserIntent({ geometry, resolution: resolveBrowserIntent(request()) })

    expect(result.applied).toBe(true)
    expect($browserTabs.get()).toHaveLength(1)
    expect($foregroundBrowserTabId.get()).toBe($browserTabs.get()[0].id)
  })

  it('updates an authorized current tab without changing foreground selection', () => {
    const foreground = createBrowserTab({
      foreground: true,
      geometry,
      profile: 'coding',
      url: 'https://one.test',
      workspaceId: scope.workspaceId
    })

    const background = createBrowserTab({
      geometry,
      profile: 'coding',
      url: 'https://two.test',
      workspaceId: scope.workspaceId
    })

    const resolution = resolveBrowserIntent(
      request({
        foregroundIntent: {
            currentToken: 'agent-current',
            explicit: true,
            mayFocus: false,
            principal: 'agent',
            token: 'agent-current'
          },
        requestedDisposition: 'current-tab',
        source: 'agent-navigation',
        targetRef: 'https://updated.test/'
      })
    )

    expect(executeBrowserIntent({ currentTabId: background.id, geometry, resolution })).toMatchObject({
      applied: true,
      tab: { id: background.id, url: 'https://updated.test/' }
    })
    expect($foregroundBrowserTabId.get()).toBe(foreground.id)
  })

  it('creates an authorized background tab without selecting it', () => {
    const resolution = resolveBrowserIntent(
      request({
        foregroundIntent: {
          currentToken: 'agent-current',
          explicit: true,
          mayFocus: false,
          principal: 'agent',
          token: 'agent-current'
        },
        source: 'agent-navigation'
      })
    )

    expect(executeBrowserIntent({ geometry, resolution }).applied).toBe(true)
    expect($browserTabs.get()).toHaveLength(1)
    expect($foregroundBrowserTabId.get()).toBeNull()
  })

  it('does not create a substitute tab when current-tab identity is absent', () => {
    const resolution = resolveBrowserIntent(
      request({ requestedDisposition: 'current-tab' })
    )

    expect(executeBrowserIntent({ geometry, resolution })).toEqual({
      applied: false,
      reason: 'current-tab-required'
    })
    expect($browserTabs.get()).toHaveLength(0)
  })

  it('refuses to retarget a current-tab command across its resolved scope', () => {
    const other = createBrowserTab({
      geometry,
      profile: 'other',
      url: 'https://other.test',
      workspaceId: scope.workspaceId
    })

    const resolution = resolveBrowserIntent(
      request({
        foregroundIntent: {
            currentToken: 'agent-current',
            explicit: true,
            mayFocus: false,
            principal: 'agent',
            token: 'agent-current'
          },
        requestedDisposition: 'current-tab',
        source: 'agent-navigation'
      })
    )

    expect(executeBrowserIntent({ currentTabId: other.id, geometry, resolution })).toEqual({
      applied: false,
      reason: 'current-tab-scope-mismatch'
    })
    expect($browserTabs.get()[0].url).toBe('https://other.test')
  })

  it.each([
    resolveBrowserIntent(request({ foregroundIntent: undefined, source: 'background-result' })),
    resolveBrowserIntent(request({ requestedDisposition: 'external' })),
    resolveBrowserIntent(request({ validation: 'hostile' }))
  ])('leaves store state untouched for inert result %#', resolution => {
    const before = $browserTabs.get()
    const result = executeBrowserIntent({ geometry, resolution })

    expect(result.applied).toBe(false)
    expect($browserTabs.get()).toBe(before)
    expect($foregroundBrowserTabId.get()).toBeNull()
  })
})
