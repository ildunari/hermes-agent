import { normalizeProfileKey } from '@/store/profile'

import {
  $browserTabs,
  type BrowserGeometry,
  type BrowserTab,
  type BrowserTabId,
  createBrowserTab,
  selectBrowserTab,
  setBrowserTabUrl
} from './browser-store'

export type BrowserIntentSource =
  | 'agent-navigation'
  | 'background-result'
  | 'browser-page-link'
  | 'download'
  | 'passive-link'
  | 'scheduled-result'
  | 'transcript-link'
  | 'unsolicited-agent'

export type BrowserIntentTarget = 'artifact' | 'download' | 'external' | 'preview' | 'web'
export type BrowserRequestedDisposition = 'current-tab' | 'download' | 'external' | 'new-tab'
export type BrowserTargetValidation = 'hostile' | 'resolved' | 'unresolved'
export type BrowserArtifactGrant = 'available' | 'not-applicable' | 'required'

export interface BrowserForegroundIntent {
  currentToken: string
  explicit: boolean
  mayFocus: boolean
  principal: 'agent' | 'user'
  token: string
}

export interface BrowserIntentScope {
  profile: string
  workspaceId: string
}

export interface BrowserIntentRequest {
  artifactGrant: BrowserArtifactGrant
  credentialAuthorization: 'confirmed' | 'not-applicable' | 'required'
  currentScope: BrowserIntentScope
  foregroundIntent?: BrowserForegroundIntent
  requestedDisposition: BrowserRequestedDisposition
  source: BrowserIntentSource
  target: BrowserIntentTarget
  targetLocation: 'internet' | 'macbook-loopback' | 'studio-loopback'
  previewGrant: BrowserArtifactGrant
  targetRef: string
  targetScope: BrowserIntentScope
  validation: BrowserTargetValidation
}

export type BrowserIntentResolution =
  | {
      activation: 'execute' | 'offer'
      disposition: 'current-tab' | 'new-tab'
      focus: boolean
      kind: 'browser-tab'
      scope: BrowserIntentScope
      targetRef: string
    }
  | {
      activation: 'execute' | 'offer'
      disposition: 'current-tab' | 'new-tab'
      focus: boolean
      kind: 'artifact-tab'
      scope: BrowserIntentScope
      targetRef: string
    }
  | { kind: 'download-offer'; reason: 'human-confirmation-required' }
  | { kind: 'external-offer'; reason: 'explicit-system-disposition-required' }
  | {
      kind: 'blocked'
      reason:
        | 'grant-required'
        | 'hostile-target'
        | 'intent-expired'
        | 'intent-required'
        | 'invalid-target'
        | 'principal-mismatch'
        | 'scope-mismatch'
        | 'unresolved-target'
    }

const PASSIVE_SOURCES = new Set<BrowserIntentSource>([
  'background-result',
  'passive-link',
  'scheduled-result',
  'unsolicited-agent'
])

function blocked(reason: Extract<BrowserIntentResolution, { kind: 'blocked' }>['reason']): BrowserIntentResolution {
  return { kind: 'blocked', reason }
}

function matchingScope(request: BrowserIntentRequest): BrowserIntentScope | null {
  if (!request.currentScope.profile.trim() || !request.targetScope.profile.trim()) {
    return null
  }

  if (!request.currentScope.workspaceId.trim() || !request.targetScope.workspaceId.trim()) {
    return null
  }

  const currentProfile = normalizeProfileKey(request.currentScope.profile)
  const targetProfile = normalizeProfileKey(request.targetScope.profile)

  return currentProfile === targetProfile && request.currentScope.workspaceId === request.targetScope.workspaceId
    ? { profile: targetProfile, workspaceId: request.targetScope.workspaceId }
    : null
}

function foregroundIntentFailure(
  request: BrowserIntentRequest
): 'intent-expired' | 'intent-required' | 'principal-mismatch' | null {
  const intent = request.foregroundIntent

  if (!intent?.explicit) {
    return 'intent-required'
  }

  if (!intent.token || intent.token !== intent.currentToken) {
    return 'intent-expired'
  }

  return request.source === 'agent-navigation' || intent.principal === 'user' ? null : 'principal-mismatch'
}

function canonicalInAppTarget(request: BrowserIntentRequest): null | string {
  if (request.target === 'artifact') {
    try {
      const url = new URL(request.targetRef)

      return url.protocol === 'hermes-artifact:' && Boolean(url.host) && !url.username && !url.password ? url.href : null
    } catch {
      return null
    }
  }

  if (request.target !== 'web' && request.target !== 'preview') {
    return request.targetRef
  }

  try {
    const url = new URL(request.targetRef)

    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null
  } catch {
    return null
  }
}

/** Pure renderer authority for every browser-routing decision. */
export function resolveBrowserIntent(request: BrowserIntentRequest): BrowserIntentResolution {
  const scope = matchingScope(request)

  if (!scope) {
    return blocked('scope-mismatch')
  }

  if (request.validation === 'hostile') {
    return blocked('hostile-target')
  }

  if (request.validation === 'unresolved') {
    return blocked('unresolved-target')
  }

  if (request.foregroundIntent) {
    const failure = foregroundIntentFailure(request)

    if (failure) {
      return blocked(failure)
    }
  }

  if (request.target === 'external' || request.requestedDisposition === 'external') {
    return { kind: 'external-offer', reason: 'explicit-system-disposition-required' }
  }

  if (
    request.target === 'download' ||
    request.requestedDisposition === 'download' ||
    request.source === 'download'
  ) {
    return { kind: 'download-offer', reason: 'human-confirmation-required' }
  }

  if (request.target === 'artifact' && request.artifactGrant !== 'available') {
    return blocked('grant-required')
  }

  if (request.targetLocation === 'studio-loopback' && request.previewGrant !== 'available') {
    return blocked('grant-required')
  }

  const targetRef = canonicalInAppTarget(request)

  if (!targetRef) {
    return blocked('invalid-target')
  }

  if (request.target !== 'artifact' && targetRef) {
    const url = new URL(targetRef)

    if ((url.username || url.password) && request.credentialAuthorization !== 'confirmed') {
      return blocked('grant-required')
    }
  }

  const disposition = request.requestedDisposition === 'current-tab' ? 'current-tab' : 'new-tab'
  const popupOffer = request.source === 'browser-page-link' && disposition === 'new-tab'
  const activation = PASSIVE_SOURCES.has(request.source) || popupOffer || !request.foregroundIntent ? 'offer' : 'execute'
  const focus = activation === 'execute' && request.foregroundIntent?.mayFocus === true

  return request.target === 'artifact'
    ? { activation, disposition, focus, kind: 'artifact-tab', scope, targetRef }
    : { activation, disposition, focus, kind: 'browser-tab', scope, targetRef }
}

export interface ExecuteBrowserIntentInput {
  currentTabId?: BrowserTabId
  geometry: BrowserGeometry
  private?: boolean
  resolution: BrowserIntentResolution
}

export type ExecuteBrowserIntentResult =
  | { applied: false; reason: 'current-tab-required' | 'current-tab-scope-mismatch' }
  | { applied: false; resolution: BrowserIntentResolution }
  | { applied: true; tab: BrowserTab }

/** Applies accepted in-app commands only. Offers and refusals remain inert. */
export function executeBrowserIntent(input: ExecuteBrowserIntentInput): ExecuteBrowserIntentResult {
  const resolution = input.resolution

  if (resolution.kind !== 'browser-tab' && resolution.kind !== 'artifact-tab') {
    return { applied: false, resolution }
  }

  if (resolution.activation === 'offer') {
    return { applied: false, resolution }
  }

  if (resolution.disposition === 'current-tab') {
    if (!input.currentTabId) {
      return { applied: false, reason: 'current-tab-required' }
    }

    const currentTab = $browserTabs.get().find(candidate => candidate.id === input.currentTabId)

    if (
      !currentTab ||
      currentTab.profile !== resolution.scope.profile ||
      currentTab.workspaceId !== resolution.scope.workspaceId
    ) {
      return { applied: false, reason: 'current-tab-scope-mismatch' }
    }

    setBrowserTabUrl(input.currentTabId, resolution.targetRef)

    if (resolution.focus) {
      selectBrowserTab(input.currentTabId)
    }

    const tab = $browserTabs.get().find(candidate => candidate.id === input.currentTabId)

    if (!tab) {
      throw new Error(`Unknown browser tab: ${input.currentTabId}`)
    }

    return { applied: true, tab }
  }

  return {
    applied: true,
    tab: createBrowserTab({
      foreground: resolution.focus,
      geometry: input.geometry,
      private: input.private,
      profile: resolution.scope.profile,
      url: resolution.targetRef,
      workspaceId: resolution.scope.workspaceId
    })
  }
}
