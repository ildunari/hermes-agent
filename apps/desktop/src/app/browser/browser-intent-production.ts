import { translateNow } from '@/i18n'
import { notify } from '@/store/notifications'
import { $activeProfile } from '@/store/profile'
import { $activeSessionId, $connection, $currentCwd, $selectedStoredSessionId } from '@/store/session'

import {
  type BrowserIntentResolution,
  type BrowserIntentSource,
  type BrowserIntentTarget,
  executeBrowserIntent,
  resolveBrowserIntent
} from './browser-intent-resolver'
import {
  type BrowserGeometry,
  type BrowserTab,
  closeBrowserTab,
  createBrowserTab,
  currentBrowserFocusIntentRevision
} from './browser-store'

export interface BrowserIntentScopeSnapshot {
  profile: string
  workspaceId: string
}

export interface OpenExplicitBrowserIntentInput {
  geometry?: BrowserGeometry
  scopeSnapshot?: BrowserIntentScopeSnapshot
  source: BrowserIntentSource
  target?: Extract<BrowserIntentTarget, 'preview' | 'web'>
  targetRef: string
}

export interface OpenExplicitBrowserIntentResult {
  applied: boolean
  resolution: BrowserIntentResolution
}

function currentWorkspaceId(): string {
  return (
    $selectedStoredSessionId.get()?.trim() ||
    $activeSessionId.get()?.trim() ||
    $currentCwd.get()?.trim() ||
    'desktop-window'
  )
}

export function captureBrowserIntentScope(): BrowserIntentScopeSnapshot {
  return { profile: $activeProfile.get(), workspaceId: currentWorkspaceId() }
}

function currentGeometry(): BrowserGeometry {
  return {
    height: Math.max(0, window.innerHeight),
    width: Math.max(0, window.innerWidth),
    x: 0,
    y: 0
  }
}

export function browserTargetLocation(targetRef: string): 'internet' | 'macbook-loopback' | 'studio-loopback' {
  try {
    const hostname = new URL(targetRef).hostname.toLowerCase().replace(/\.$/, '').replace(/^\[|\]$/g, '')
    const mappedIpv4 = hostname.match(/^::ffff:(\d{1,3}(?:\.\d{1,3}){3})$/)?.[1]
    const mappedHex = hostname.match(/^::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/)
    const mappedHexLoopback = mappedHex ? (Number.parseInt(mappedHex[1], 16) >> 8) === 127 : false
    const ipv4 = mappedIpv4 ?? hostname
    const octets = ipv4.split('.').map(value => Number(value))

    const ipv4Loopback =
      octets.length === 4 && octets.every(value => Number.isInteger(value) && value >= 0 && value <= 255) && octets[0] === 127

    const loopback =
      hostname === '::' ||
      hostname === '::1' ||
      hostname === '0:0:0:0:0:0:0:1' ||
      hostname === '::ffff:0:0' ||
      hostname === '0.0.0.0' ||
      hostname === 'localhost' ||
      hostname.endsWith('.localhost') ||
      mappedHexLoopback ||
      ipv4Loopback

    if (!loopback) {
      return 'internet'
    }

    return $connection.get()?.mode === 'remote' ? 'studio-loopback' : 'macbook-loopback'
  } catch {
    return 'internet'
  }
}

/**
 * Production renderer entry point for a direct, synchronous user click.
 * Scope, topology, geometry, and the one-turn intent token are derived here;
 * content callers provide only provenance and the candidate HTTP(S) target.
 */
export function openExplicitBrowserIntent(input: OpenExplicitBrowserIntentInput): OpenExplicitBrowserIntentResult {
  const currentScope = captureBrowserIntentScope()
  const scope = input.scopeSnapshot ?? currentScope
  const token = globalThis.crypto.randomUUID()
  const location = browserTargetLocation(input.targetRef)

  const resolution = resolveBrowserIntent({
    artifactGrant: 'not-applicable',
    credentialAuthorization: 'not-applicable',
    currentScope,
    foregroundIntent: {
      currentToken: token,
      explicit: true,
      mayFocus: true,
      principal: 'user',
      token
    },
    previewGrant: location === 'studio-loopback' ? 'required' : 'not-applicable',
    requestedDisposition: 'new-tab',
    source: input.source,
    target: input.target ?? 'web',
    targetLocation: location,
    targetRef: input.targetRef,
    targetScope: scope,
    validation: 'resolved'
  })

  const execution = executeBrowserIntent({ geometry: input.geometry ?? currentGeometry(), resolution })

  if (!execution.applied) {
    const isOffer =
      resolution.kind === 'external-offer' ||
      resolution.kind === 'download-offer' ||
      (('activation' in resolution) && resolution.activation === 'offer')

    notify({
      detail:
        resolution.kind === 'blocked' ? resolution.reason : 'reason' in execution ? execution.reason : resolution.kind,
      kind: isOffer ? 'info' : 'warning',
      message: translateNow(
        isOffer ? 'notifications.browserIntentOfferMessage' : 'notifications.browserIntentBlockedMessage'
      ),
      title: translateNow(isOffer ? 'notifications.browserIntentOfferTitle' : 'notifications.browserIntentBlockedTitle')
    })
  }

  return { applied: execution.applied, resolution }
}

export interface OpenExplicitBrowserResourceIntentInput {
  geometry?: BrowserGeometry
  kind: 'artifact' | 'preview'
  scopeSnapshot: BrowserIntentScopeSnapshot
  sourceSessionId: string
  target: string
}

/**
 * Reserves a non-visible tab only after the async caller's captured scope is
 * still current. Navigation/focus remains pending until main returns an exact
 * generation-bound grant and completeExplicitBrowserResourceIntent re-runs the
 * closed P5.1 resolver.
 */
export function openExplicitBrowserResourceIntent(input: OpenExplicitBrowserResourceIntentInput): BrowserTab | null {
  const currentScope = captureBrowserIntentScope()

  if (
    currentScope.profile.trim().toLowerCase() !== input.scopeSnapshot.profile.trim().toLowerCase() ||
    currentScope.workspaceId !== input.scopeSnapshot.workspaceId ||
    !input.sourceSessionId.trim() ||
    !input.target.trim() ||
    (input.kind === 'preview' && browserTargetLocation(input.target) !== 'studio-loopback')
  ) {
    notify({
      detail: 'scope-mismatch',
      kind: 'warning',
      message: translateNow('notifications.browserIntentBlockedMessage'),
      title: translateNow('notifications.browserIntentBlockedTitle')
    })

    return null
  }

  return createBrowserTab({
    geometry: input.geometry ?? currentGeometry(),
    profile: currentScope.profile,
    resource: {
      focusIntentRevisionAtClick: currentBrowserFocusIntentRevision(),
      kind: input.kind,
      sourceSessionId: input.sourceSessionId,
      target: input.target
    },
    url: '',
    workspaceId: currentScope.workspaceId
  })
}

export function completeExplicitBrowserResourceIntent(tab: BrowserTab, guestUrl: string): OpenExplicitBrowserIntentResult {
  const currentScope = captureBrowserIntentScope()
  const token = globalThis.crypto.randomUUID()
  const resource = tab.resource
  const mayFocus = resource?.focusIntentRevisionAtClick === currentBrowserFocusIntentRevision()

  const resolution = resolveBrowserIntent({
    artifactGrant: resource?.kind === 'artifact' ? 'available' : 'not-applicable',
    credentialAuthorization: 'not-applicable',
    currentScope,
    foregroundIntent: {
      currentToken: token,
      explicit: true,
      mayFocus,
      principal: 'user',
      token
    },
    previewGrant: resource?.kind === 'preview' ? 'available' : 'not-applicable',
    requestedDisposition: 'current-tab',
    source: 'transcript-link',
    target: resource?.kind ?? 'artifact',
    targetLocation: resource?.kind === 'preview' ? 'studio-loopback' : 'internet',
    targetRef: guestUrl,
    targetScope: { profile: tab.profile, workspaceId: tab.workspaceId },
    validation: resource ? 'resolved' : 'unresolved'
  })
  const execution = executeBrowserIntent({
    currentTabId: tab.id,
    geometry: tab.geometry,
    resolution
  })

  if (!execution.applied) {
    failExplicitBrowserResourceIntent(tab, resolution.kind === 'blocked' ? resolution.reason : resolution.kind)
  }

  return { applied: execution.applied, resolution }
}

export function failExplicitBrowserResourceIntent(tab: BrowserTab, detail: string): void {
  closeBrowserTab(tab.id)
  notify({
    detail,
    kind: 'warning',
    message: translateNow('notifications.browserResourceFailedMessage'),
    title: translateNow('notifications.browserResourceFailedTitle')
  })
}

/**
 * Revalidates an async explicit click before the vetted native preview opener
 * performs the user-selected system-browser disposition. The file URL remains
 * outside browser-tab state; only the closed resolver result authorizes IPC.
 */
export function authorizeExplicitSystemPreviewIntent(
  scopeSnapshot: BrowserIntentScopeSnapshot,
  targetRef: string
): BrowserIntentResolution {
  const currentScope = captureBrowserIntentScope()
  const token = globalThis.crypto.randomUUID()
  const remote = $connection.get()?.mode === 'remote'

  const resolution = resolveBrowserIntent({
    artifactGrant: 'not-applicable',
    credentialAuthorization: 'not-applicable',
    currentScope,
    foregroundIntent: {
      currentToken: token,
      explicit: true,
      mayFocus: false,
      principal: 'user',
      token
    },
    previewGrant: remote ? 'required' : 'not-applicable',
    requestedDisposition: remote ? 'new-tab' : 'external',
    source: 'transcript-link',
    target: remote ? 'preview' : 'external',
    targetLocation: remote ? 'studio-loopback' : browserTargetLocation(targetRef),
    targetRef,
    targetScope: scopeSnapshot,
    validation: 'resolved'
  })

  if (resolution.kind !== 'external-offer') {
    notify({
      detail: resolution.kind === 'blocked' ? resolution.reason : resolution.kind,
      kind: 'warning',
      message: translateNow('notifications.browserIntentBlockedMessage'),
      title: translateNow('notifications.browserIntentBlockedTitle')
    })
  }

  return resolution
}
