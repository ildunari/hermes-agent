import crypto from 'node:crypto'

import type { App, DownloadItem, IpcMain, IpcMainInvokeEvent, Session, WebContents } from 'electron'

import {
  type AnnotationIsolatedWorldRequest,
  collectAnnotationIsolatedWorldReport
} from './browser-annotation-isolated-world'
import { collectAnnotationTrustedMainReport } from './browser-annotation-main-report'
import { annotationProjectionTags, projectTrustedAnnotations } from './browser-annotation-projection'
import type { AnnotationScreenshotMarker, AnnotationScreenshotViewport } from './browser-annotation-screenshot'
import {
  BrowserConsentAuthority,
  type BrowserConsentCategory,
  type BrowserConsentOutcome,
  type BrowserConsentPrompt,
  type BrowserConsentRequest,
  type BrowserUploadConsentDetail
} from './browser-consent'
import {
  type BrowserNavigationDecision,
  browserNavigationPolicy,
  type BrowserNavigationSource,
  isAllowedBrowserNavigation,
  isPrivateBrowserAddress
} from './browser-navigation-policy'
import type { SnpObservation, SnpScope } from './browser-sensitive-navigation-policy'

const ATTACH_PREFIX = 'about:blank#hermes-browser-attach='
const ATTACH_URL_RESOLUTION_TIMEOUT_MS = 1_000
const ATTACH_URL_POLL_INTERVAL_MS = 10
export const BROWSER_PARTITION = 'persist:hermes-browser'
const PRIVATE_PARTITION_PREFIX = 'hermes-browser-private:v1:'
const REPORTER_WORLD_ID = 1004
const MAX_REPORTS_IN_FLIGHT_PER_BINDING = 2
const MAX_REPORTS_PER_BINDING_WINDOW = 12
const REPORT_RATE_WINDOW_MS = 1_000
const PIXEL_GRANT_TTL_MS = 60_000
const SITE_CONSENT_TTL_MS = 30 * 60_000
const MAX_TRANSIENT_PIXEL_BYTES = 8 * 1024 * 1024
const MAX_ANNOTATION_SCREENSHOT_BYTES = 32 * 1024 * 1024
const PIXEL_CONSENT_METHOD = 'Hermes.requestPixelConsent'
const PIXEL_GRANT_PARAM = '__hermesPixelConsent'
const VIEWPORT_SCREENSHOT_PARAMS = Object.freeze({ captureBeyondViewport: false, format: 'png', fromSurface: true })
const INTERNAL_DEBUGGER_METHODS = new Set(['Page.enable', 'Page.setInterceptFileChooserDialog'])

const AUTOMATION_DEBUGGER_METHODS = new Set([
  'Accessibility.disable',
  'Accessibility.enable',
  'Accessibility.getFullAXTree',
  'DOM.describeNode',
  'DOM.disable',
  'DOM.enable',
  'DOM.getBoxModel',
  'DOM.getDocument',
  'DOM.querySelector',
  'Input.dispatchKeyEvent',
  'Input.dispatchMouseEvent',
  'Input.insertText',
  'Network.disable',
  'Network.enable',
  'Network.getCookies',
  'Page.captureScreenshot',
  'Page.disable',
  'Page.enable',
  'Page.getFrameTree',
  'Page.getLayoutMetrics',
  'Page.navigate',
  'Page.reload',
  'Runtime.disable',
  'Runtime.enable',
  'Runtime.evaluate'
])

const RAW_CDP_DENIED_METHODS = new Set(['Network.getCookies', 'Runtime.evaluate'])

// A successor created by explicit local-control hand-back is read-only until
// the agent has observed the replacement document. None of these methods can
// navigate, synthesize input, evaluate page JavaScript, or read cookies.
const HAND_BACK_SNAPSHOT_METHODS = new Set([
  'Accessibility.disable',
  'Accessibility.enable',
  'Accessibility.getFullAXTree',
  'DOM.describeNode',
  'DOM.disable',
  'DOM.enable',
  'DOM.getBoxModel',
  'DOM.getDocument',
  'DOM.querySelector',
  'Page.disable',
  'Page.enable',
  'Page.getFrameTree',
  'Page.getLayoutMetrics',
  'Runtime.disable',
  'Runtime.enable'
])

const AUTHENTICATED_PIXEL_METHODS = new Set([
  'HeadlessExperimental.beginFrame',
  'Page.captureScreenshot',
  'Page.printToPDF',
  'Page.startScreencast'
])

interface BrowserGuestRequest {
  partition: string
  private: boolean
  profile: string
  surfaceEpoch: string
  tabId: string
  workspaceId?: string
}

interface BrowserGuestIdentity {
  generation: string
  hostId: number
  partition: string
  profile: string
  surfaceEpoch: string
  tabId: string
  workspaceId: string
}

interface BrowserGuestClaim extends BrowserGuestIdentity {
  expiresAt: number
  token: string
}

interface BrowserGuestBinding extends BrowserGuestIdentity {
  activationSequence: number
  annotationLabels: Map<string, number>
  documentGeneration: number
  frameSessions: Map<string, string>
  guest: WebContents
  nextAnnotationLabel: number
  reportInFlight: number
  reportWindowCount: number
  reportWindowStartedAt: number
  uploadGeneration: number
}

interface BrowserGuestActivation {
  generation: string
  tabId: string
  url: string
}

interface BrowserGuestRelease {
  generation: string
  tabId: string
}

interface BrowserGuestReport extends BrowserGuestRelease {
  documentGeneration?: number
  kind: 'viewport'
}

interface BrowserAnnotationProjectionRequest extends BrowserGuestRelease {
  records: unknown
  workspaceId: string
}

interface BrowserAutomationBindingRequest {
  guestGeneration: string
  requireFreshSnapshot?: boolean
  tabId: string
  taskGeneration: number
  taskId: string
}

export type BrowserRelayRole = 'automation' | 'raw-cdp'

export interface BrowserTaskLifecycleFrame extends BrowserAutomationBindingRequest {
  profile: string
  type: 'bind' | 'unbind'
}

export interface BrowserUploadLifecycleInvalidation {
  documentGeneration?: number
  frameId?: string
  guestGeneration: string
  profile: string
  tabId: string
  taskGeneration?: number
  taskId?: string
}

interface BrowserAutomationBinding extends BrowserAutomationBindingRequest {
  awaitingFreshSnapshot: boolean
  hostId: number
  profile: string
  role: BrowserRelayRole
}

export interface BrowserAutomationDispatch extends BrowserAutomationBindingRequest {
  bindingGeneration?: number
  capabilityGeneration?: number
  connectionId?: string
  frame: Record<string, unknown>
  operationId?: string
  profile?: string
  remainingDurationMs?: number
  role: BrowserRelayRole
}

interface BrowserAutomationFrame extends BrowserAutomationBindingRequest {
  frame: Record<string, unknown>
  role: BrowserRelayRole
}

interface BrowserGuestControllerDeps {
  app: Pick<App, 'on'>
  authorizeResourceRequest?: (
    partition: string,
    webContentsId: number | undefined,
    url: string,
    admitArtifact: boolean
  ) => boolean
  chooseDownloadDestination?: (hostId: number, prompt: Readonly<BrowserConsentPrompt>) => Promise<string | null>
  durablePermissionDecision?: (profile: string, origin: string, permission: string) => 'allow' | 'deny' | null
  buildUploadConsent?: (
    chooser: Readonly<BrowserPendingUploadChooser>,
    files: readonly BrowserUploadAssignmentFile[]
  ) => BrowserUploadConsentDetail
  handleUploadChooser?: (chooser: Readonly<BrowserPendingUploadChooser>) => Promise<void> | void
  invalidateAssignedUploads?: (scope: Readonly<BrowserUploadLifecycleInvalidation>) => void
  notifyUploadExpired?: (chooser: Readonly<BrowserPendingUploadChooser>) => void
  ipcMain: Pick<IpcMain, 'handle'>
  launchExternal?: (target: string) => Promise<void>
  notifyFreshSnapshot?: (event: BrowserFreshSnapshotEvent & { hostId: number }) => void
  notifyConsentResolved?: (event: BrowserConsentOutcome & { hostId: number }) => void
  notifyRetired?: (event: BrowserGuestRetiredEvent & { hostId: number }) => void
  presentConsent?: (hostId: number, prompt: Readonly<BrowserConsentPrompt>) => void
  recordTransfer?: (profile: string, input: {
    actor: 'human'
    byteSize: null | number
    direction: 'download'
    origin: string
    outcome: 'canceled' | 'completed' | 'failed'
    redactedName: string
    tabIncarnationId: string
  }) => void
  requestPixelConsent?: (prompt: BrowserPixelConsentPrompt) => Promise<boolean>
  saveAnnotationScreenshot?: (
    hostId: number,
    png: Buffer,
    viewport: AnnotationScreenshotViewport,
    markers: readonly AnnotationScreenshotMarker[]
  ) => Promise<'canceled' | 'saved'>
  installResourceSession?: (browserSession: Session, partition: string) => void
  sessionFromPartition: (partition: string) => Session
}

export interface BrowserPendingUploadChooser {
  accept: string
  backendNodeId: number
  chooserId: string
  directory: false
  documentGeneration: number
  formActionOrigin: string
  formActionUrl: string
  formFingerprint: string
  formLabel: string
  formMethod: 'dialog' | 'get' | 'post'
  frameId: string
  guestGeneration: string
  hostId: number
  inputLabel: string
  inputName: string
  mode: 'selectMultiple' | 'selectSingle'
  origin: string
  profile: string
  signal: AbortSignal
  tabId: string
  taskGeneration: number
  taskId: string
}

export interface BrowserUploadAssignmentFile {
  displayName: string
  mimeType: string
  originalDisplayName?: string
  sha256: string
  size: number
}

export interface BrowserUploadAssignmentRequest {
  consume: () => Promise<readonly string[]>
  files: readonly BrowserUploadAssignmentFile[]
  settled?: (outcome: 'completed' | 'failed' | 'expired') => Promise<void> | void
}

export type BrowserUploadAssignmentOutcome = 'completed' | 'not_started' | 'outcome_unknown'

interface BrowserUploadInputDescriptor {
  accept: string
  directory: false
  formActionOrigin: string
  formActionUrl: string
  formFingerprint: string
  formLabel: string
  formMethod: 'dialog' | 'get' | 'post'
  inputLabel: string
  inputName: string
}

interface BrowserPendingUploadChooserRecord {
  abortController: AbortController
  binding: BrowserGuestBinding
  chooser: Readonly<BrowserPendingUploadChooser>
  sessionId?: string
  task: BrowserAutomationBinding
}

interface BrowserAssignedUploadRecord {
  browserSession: Session
  chooser: Readonly<BrowserPendingUploadChooser>
  guestId: number
  partition: string
  paths: readonly string[]
  requestId?: number
  settled: NonNullable<BrowserUploadAssignmentRequest['settled']>
  timer: ReturnType<typeof setTimeout>
}

export interface BrowserResourceBinding {
  generation: string
  guestId: number
  hostId: number
  partition: string
  profile: string
  tabId: string
  workspaceId: string
}

export interface BrowserFreshSnapshotEvent extends BrowserAutomationBindingRequest {
  surfaceEpoch: string
}

export interface BrowserSensitiveNavigationObservation extends BrowserGuestRelease {
  hostId: number
  signal: SnpObservation
}

export interface BrowserGuestRetiredEvent {
  guestGeneration: string
  reason:
    | 'activation-failed'
    | 'crashed'
    | 'debugger-detached'
    | 'destroyed'
    | 'policy-denied'
    | 'profile-deleted'
    | 'setup-failed'
    | 'unresponsive'
    | 'workspace-reset'
  tabId: string
}

interface BrowserAuthenticatedPixelScope {
  binding_generation: number
  capability_generation: number
  connection_id: string
  document_generation: number
  guest_generation: string
  profile: string
  tab_id: string
  task_generation: number
  task_id: string
}

export interface BrowserPixelConsentPrompt {
  captureKind: 'viewport-screenshot'
  maxBytes: number
  origin: string
  purpose: string
  recipient: string
  retention: 'memory-only-transient'
  scope: Readonly<BrowserAuthenticatedPixelScope>
  tabTitle: string
}

interface BrowserPixelGrant {
  captureParams: string
  expiresAt: number
  expiryTimer: ReturnType<typeof setTimeout>
  maxBytes: number
  purpose: string
  recipient: string
  scope: BrowserAuthenticatedPixelScope
}

interface BrowserSiteGrant {
  expiresAt: number
  guestGeneration: string
  profile: string
  site: string
  tabId: string
  taskGeneration: number
  taskId: string
}

interface BrowserNavigationAdmission {
  classification: BrowserNavigationDecision['classification']
  destinationSite: string
  expiresAt: number
  guestGeneration: string
  operationId: string
  profile: string
  reasonCodes: readonly string[]
  sourceSite: string
  tabId: string
  url: string
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function stableJson(value: unknown): string | null {
  if (value === null || typeof value === 'boolean' || typeof value === 'string') {
    return JSON.stringify(value)
  }

  if (typeof value === 'number') {
    return Number.isFinite(value) ? JSON.stringify(value) : null
  }

  if (Array.isArray(value)) {
    const rows = value.map(stableJson)

    return rows.every((row): row is string => row !== null) ? `[${rows.join(',')}]` : null
  }

  if (!isRecord(value)) {return null}
  const rows: string[] = []

  for (const key of Object.keys(value).sort()) {
    const encoded = stableJson(value[key])

    if (encoded === null) {return null}
    rows.push(`${JSON.stringify(key)}:${encoded}`)
  }

  return `{${rows.join(',')}}`
}

function frameSecurityOriginFromTree(value: unknown, frameId: string): string | null {
  if (!isRecord(value)) {return null}
  const frame = value.frame

  if (isRecord(frame) && frame.id === frameId && typeof frame.securityOrigin === 'string') {
    return frame.securityOrigin
  }

  const children = value.childFrames

  if (!Array.isArray(children)) {return null}

  for (const child of children) {
    const found = frameSecurityOriginFromTree(child, frameId)

    if (found !== null) {return found}
  }

  return null
}

const UPLOAD_INPUT_DESCRIPTOR_FUNCTION = `function () {
  if (!(this instanceof HTMLInputElement) || this.type.toLowerCase() !== 'file') return null
  const form = this.form
  const text = value => String(value || '').replace(/\\s+/g, ' ').trim()
  const labelledBy = text(this.getAttribute('aria-labelledby'))
    .split(' ')
    .filter(Boolean)
    .map(id => text(this.ownerDocument.getElementById(id)?.textContent))
    .filter(Boolean)
    .join(' ')
  const inputLabel = text(this.getAttribute('aria-label')) || labelledBy ||
    text(Array.from(this.labels || []).map(label => label.textContent).join(' '))
  const formLabel = form ? text(form.getAttribute('aria-label')) ||
    text(form.getAttribute('aria-labelledby')?.split(/\\s+/).map(id =>
      form.ownerDocument.getElementById(id)?.textContent || '').join(' ')) : ''
  let actionOrigin = ''
  let actionUrl = ''
  try {
    const action = new URL(form?.getAttribute('action') || '', this.ownerDocument.baseURI)
    actionOrigin = action.origin
    actionUrl = action.href
  } catch {}
  return {
    accept: this.getAttribute('accept') || '',
    actionOrigin,
    actionUrl,
    directory: this.hasAttribute('webkitdirectory') || this.hasAttribute('directory'),
    formId: form?.getAttribute('id') || '',
    formLabel,
    formMethod: (form?.getAttribute('method') || 'get').toLowerCase(),
    formName: form?.getAttribute('name') || '',
    inputLabel,
    inputName: this.getAttribute('name') || '',
    multiple: this.hasAttribute('multiple')
  }
}`

function browserUploadInputDescriptor(
  value: unknown,
  formBackendNodeId: number,
  scope: {
    backendNodeId: number
    chooserMode: 'selectMultiple' | 'selectSingle'
    documentGeneration: number
    frameId: string
    origin: string
  }
): BrowserUploadInputDescriptor | null {
  if (!isRecord(value)) {return null}
  const fields = ['accept', 'actionOrigin', 'actionUrl', 'formId', 'formLabel', 'formMethod', 'formName', 'inputLabel', 'inputName'] as const
  if (fields.some(field => typeof value[field] !== 'string' || (value[field] as string).length > 4_096) ||
    typeof value.directory !== 'boolean' || typeof value.multiple !== 'boolean' ||
    !Number.isSafeInteger(formBackendNodeId) || formBackendNodeId < 0) {return null}
  if (value.formMethod !== 'get' && value.formMethod !== 'post' && value.formMethod !== 'dialog') {return null}
  let actionOrigin: string
  let actionUrl: string
  try {
    const parsed = new URL(value.actionUrl as string)
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {return null}
    actionOrigin = parsed.origin
    actionUrl = parsed.href
  } catch {return null}
  if (actionOrigin !== value.actionOrigin || actionUrl !== value.actionUrl || value.directory ||
    (scope.chooserMode === 'selectMultiple') !== value.multiple) {return null}

  const fingerprintInput = stableJson({
    ...Object.fromEntries(fields.map(field => [field, value[field]])),
    backendNodeId: scope.backendNodeId,
    chooserMode: scope.chooserMode,
    directory: value.directory,
    documentGeneration: scope.documentGeneration,
    formBackendNodeId,
    frameId: scope.frameId,
    multiple: value.multiple,
    origin: scope.origin
  })

  if (!fingerprintInput) {return null}
  return {
    accept: (value.accept as string).slice(0, 1_024),
    directory: false,
    formActionOrigin: actionOrigin,
    formActionUrl: actionUrl,
    formFingerprint: crypto.createHash('sha256').update(fingerprintInput).digest('base64url'),
    formLabel: (value.formLabel as string).slice(0, 512),
    formMethod: value.formMethod as 'dialog' | 'get' | 'post',
    inputLabel: (value.inputLabel as string).slice(0, 512),
    inputName: (value.inputName as string).slice(0, 256)
  }
}

function normalizeProfile(profile: unknown): string {
  return typeof profile === 'string' ? profile.trim().toLowerCase() : ''
}

function isAllowedBrowserRequest(rawUrl: string): boolean {
  try {
    const parsed = new URL(rawUrl)

    return (
      (parsed.protocol === 'http:' ||
        parsed.protocol === 'https:' ||
        parsed.protocol === 'ws:' ||
        parsed.protocol === 'wss:') &&
      !isPrivateBrowserAddress(parsed.hostname)
    )
  } catch {
    return false
  }
}

function validIdentifier(value: unknown, max = 256): value is string {
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

function attachmentTokenFromUrl(rawUrl: unknown): string | null {
  if (typeof rawUrl !== 'string' || !rawUrl.startsWith(ATTACH_PREFIX)) {
    return null
  }

  const token = rawUrl.slice(ATTACH_PREFIX.length)

  return /^[A-Za-z0-9_-]{43}$/.test(token) ? token : null
}

function isBrowserPartition(partition: unknown): partition is string {
  return (
    typeof partition === 'string' &&
    (partition === BROWSER_PARTITION || partition.startsWith(PRIVATE_PARTITION_PREFIX))
  )
}

function denyEvent(event: { preventDefault?: () => void }) {
  event.preventDefault?.()
}

function safeDestroy(contents: WebContents) {
  try {
    if (!contents.isDestroyed()) {
      contents.close()
    }
  } catch {
    // The guest may already be terminating.
  }
}

export class BrowserGuestSecurityController {
  readonly #acceptedClaimsByHost = new Map<number, BrowserGuestClaim[]>()
  readonly #bindings = new Map<string, BrowserGuestBinding>()
  readonly #bindingSetups = new Map<string, string>()
  readonly #taskBindings = new Map<string, BrowserAutomationBinding>()
  readonly #latestTaskGenerations = new Map<string, number>()
  readonly #claims = new Map<string, BrowserGuestClaim>()
  readonly #hostIds = new Set<number>()
  readonly #installedPartitions = new Set<string>()
  readonly #privatePartitionOwners = new Map<string, string>()
  readonly #resolutionLocks = new Map<string, Promise<readonly string[]>>()
  readonly #deps: BrowserGuestControllerDeps
  readonly #consents: BrowserConsentAuthority
  readonly #navigationAdmissions = new Map<number, BrowserNavigationAdmission>()
  readonly #pendingUploadChoosers = new Map<string, BrowserPendingUploadChooserRecord>()
  readonly #assignedUploads = new Map<string, BrowserAssignedUploadRecord>()
  readonly #assignedUploadOwners = new Map<string, string>()
  readonly #pixelGrants = new Map<string, BrowserPixelGrant>()
  readonly #siteGrants = new Map<string, BrowserSiteGrant>()
  #pixelLifecycleGeneration = 0
  #frameSink: ((frame: BrowserAutomationFrame) => void) | null = null
  #taskLifecycleSink: ((frame: BrowserTaskLifecycleFrame) => void) | null = null
  #installed = false

  constructor(deps: BrowserGuestControllerDeps) {
    this.#deps = deps
    this.#consents = new BrowserConsentAuthority({
      present: (hostId, prompt) => {
        if (!this.#hostIds.has(hostId) || !this.#deps.presentConsent) {
          throw new Error('trusted-browser-consent-unavailable')
        }

        this.#deps.presentConsent(hostId, prompt)
      },
      settled: (hostId, outcome) => this.#deps.notifyConsentResolved?.({ hostId, ...outcome })
    })
  }

  install() {
    if (this.#installed) {
      return
    }

    this.#installed = true
    this.#deps.app.on('web-contents-created', (_event, contents) => this.#guardCreatedContents(contents))
    this.#deps.app.on('before-quit', () => this.#consents.revokeAll())
    this.#deps.ipcMain.handle('hermes:browser-guest:prepare', (event, request) => this.prepare(event, request))
    this.#deps.ipcMain.handle('hermes:browser-guest:activate', (event, request) => this.activate(event, request))
    this.#deps.ipcMain.handle('hermes:browser-guest:release', (event, request) => this.release(event, request))
    this.#deps.ipcMain.handle('hermes:browser-guest:report', (event, request) => this.report(event, request))
    this.#deps.ipcMain.handle('hermes:browser-guest:resolve-annotations', (event, request) =>
      this.resolveAnnotations(event, request)
    )
    this.#deps.ipcMain.handle('hermes:browser-guest:export-annotation-screenshot', (event, request) =>
      this.exportAnnotationScreenshot(event, request)
    )
    this.#deps.ipcMain.handle('hermes:browser-consent:resolve', (event, response) =>
      this.#consents.resolve(event.sender.id, response)
    )
    this.#deps.ipcMain.handle('hermes:browser-guest:bind-automation', (event, request) =>
      this.bindAutomation(event, request)
    )
    this.#deps.ipcMain.handle('hermes:browser-guest:unbind-automation', (event, request) =>
      this.unbindAutomation(event, request)
    )
    this.#deps.ipcMain.handle('hermes:browser-guest:revoke-local', (event, request) =>
      this.revokeLocal(event, request)
    )
    this.#deps.ipcMain.handle('hermes:browser-guest:stop-and-close', (event, request) =>
      this.stopAndClose(event, request)
    )
  }

  setFrameSink(sink: ((frame: BrowserAutomationFrame) => void) | null) {
    this.#frameSink = sink
  }

  setTaskLifecycleSink(sink: ((frame: BrowserTaskLifecycleFrame) => void) | null) {
    this.#taskLifecycleSink = sink
  }

  retireAutomationProfile(profile: string) {
    const normalized = normalizeProfile(profile)

    this.#invalidatePixelGrants(grant => grant.scope.profile === normalized)
    this.#consents.revoke(prompt => prompt.profile === normalized)
    this.#cancelUploadChoosers(record => record.chooser.profile === normalized)

    for (const [key, grant] of this.#siteGrants) {
      if (grant.profile === normalized) {this.#siteGrants.delete(key)}
    }

    for (const [guestId, admission] of this.#navigationAdmissions) {
      if (admission.profile === normalized) {this.#navigationAdmissions.delete(guestId)}
    }

    for (const [key, task] of this.#taskBindings) {
      if (task.profile !== normalized) {continue}

      this.#taskBindings.delete(key)

      if (task.role === 'automation') {
        this.#taskLifecycleSink?.({
          guestGeneration: task.guestGeneration,
          profile: task.profile,
          tabId: task.tabId,
          taskGeneration: task.taskGeneration,
          taskId: task.taskId,
          type: 'unbind'
        })
      }
    }
  }

  retireProfileGuests(profile: string, reason: 'profile-deleted' | 'workspace-reset' = 'profile-deleted') {
    const normalized = normalizeProfile(profile)
    this.retireAutomationProfile(normalized)

    for (const binding of [...this.#bindings.values()]) {
      if (binding.profile === normalized) {this.#retireBinding(binding, reason)}
    }
  }

  retireWorkspaceGuests(profile: string, workspaceId: string) {
    const normalized = normalizeProfile(profile)

    for (const binding of [...this.#bindings.values()]) {
      if (binding.profile === normalized && binding.workspaceId === workspaceId) {
        this.#retireBinding(binding, 'workspace-reset')
      }
    }
  }

  #taskBindingKey(taskId: string, role: BrowserRelayRole) {
    return `${taskId}:${role}`
  }

  registerHost(contents: WebContents) {
    this.#hostIds.add(contents.id)
    contents.once('destroyed', () => {
      this.#hostIds.delete(contents.id)
      this.#retireHost(contents.id)
    })
  }

  invalidatePixelGrants() {
    this.#pixelLifecycleGeneration += 1

    for (const grant of this.#pixelGrants.values()) {clearTimeout(grant.expiryTimer)}
    this.#pixelGrants.clear()
  }

  invalidateUploadChoosers() {
    this.#cancelUploadChoosers(() => true)
  }

  invalidateAssignedUploads() {
    this.#retireAssignedUploads(() => true)
  }

  #invalidateAssignedUploads(scope: Readonly<BrowserUploadLifecycleInvalidation>) {
    this.#retireAssignedUploads(record => {
      const chooser = record.chooser
      return chooser.profile === scope.profile && chooser.tabId === scope.tabId &&
        chooser.guestGeneration === scope.guestGeneration &&
        (scope.documentGeneration === undefined || chooser.documentGeneration === scope.documentGeneration) &&
        (scope.frameId === undefined || chooser.frameId === scope.frameId) &&
        (scope.taskId === undefined || chooser.taskId === scope.taskId) &&
        (scope.taskGeneration === undefined || chooser.taskGeneration === scope.taskGeneration)
    })
    this.#deps.invalidateAssignedUploads?.(scope)
  }

  #retireAssignedUploads(predicate: (record: BrowserAssignedUploadRecord) => boolean) {
    for (const [chooserId, record] of this.#assignedUploads) {
      if (!predicate(record)) {continue}
      // Delete synchronously so queued expiry and request-completion callbacks
      // cannot settle or warn about an upload retired by lifecycle invalidation.
      this.#assignedUploads.delete(chooserId)
      clearTimeout(record.timer)
      record.requestId = undefined
    }
  }

  prepare(event: IpcMainInvokeEvent, request: BrowserGuestRequest) {
    if (!this.#hostIds.has(event.sender.id)) {
      return { error: 'browser-host-not-authorized', ok: false }
    }

    const profile = normalizeProfile(request?.profile)
    const tabId = request?.tabId
    const surfaceEpoch = request?.surfaceEpoch
    const partition = request?.partition
    const workspaceId = validIdentifier(request?.workspaceId) ? request.workspaceId : `legacy:${tabId}`

    if (
      !validIdentifier(tabId) || !validIdentifier(surfaceEpoch) ||
      !validIdentifier(profile) || !validIdentifier(workspaceId)
    ) {
      return { error: 'invalid-browser-identity', ok: false }
    }

    const validPrivatePartition =
      request.private &&
      typeof partition === 'string' &&
      /^hermes-browser-private:v1:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
        partition
      )

    const validPersistentPartition = !request.private && partition === BROWSER_PARTITION

    if (!validPrivatePartition && !validPersistentPartition) {
      return { error: 'browser-partition-mismatch', ok: false }
    }

    const ownerKey = `${event.sender.id}:${profile}:${surfaceEpoch}:${tabId}`
    const existingOwner = this.#privatePartitionOwners.get(partition)

    if (request.private && existingOwner && existingOwner !== ownerKey) {
      return { error: 'private-browser-partition-owned', ok: false }
    }

    if (request.private) {
      this.#privatePartitionOwners.set(partition, ownerKey)
    }

    const token = crypto.randomBytes(32).toString('base64url')
    const generation = crypto.randomUUID()

    for (const [existingToken, existingClaim] of this.#claims) {
      if (existingClaim.hostId === event.sender.id && existingClaim.tabId === tabId) {
        this.#claims.delete(existingToken)
      }
    }

    const claim: BrowserGuestClaim = {
      expiresAt: Date.now() + 30_000,
      generation,
      hostId: event.sender.id,
      partition,
      profile,
      surfaceEpoch,
      tabId,
      token,
      workspaceId
    }

    this.#claims.set(token, claim)

    return { attachmentUrl: `${ATTACH_PREFIX}${token}`, generation, ok: true }
  }

  async activate(event: IpcMainInvokeEvent, request: BrowserGuestActivation) {
    const binding = await this.#waitForBinding(event, request)

    if (!binding) {
      return { error: 'browser-guest-not-bound', ok: false }
    }

    const activationSequence = ++binding.activationSequence

    if (!(await this.#isResolvedDestinationAllowed(binding.partition, request.url, 'trusted-activation', binding.guest.id))) {
      return activationSequence !== binding.activationSequence
        ? { ok: true, superseded: true }
        : { error: 'browser-navigation-denied', ok: false }
    }

    if (activationSequence !== binding.activationSequence) {
      return { ok: true, superseded: true }
    }

    try {
      await binding.guest.loadURL(request.url)

      return activationSequence !== binding.activationSequence ? { ok: true, superseded: true } : { ok: true }
    } catch (error) {
      const superseded = activationSequence !== binding.activationSequence
      const errorCode = (error as { code?: number | string })?.code

      if (superseded || errorCode === 'ERR_ABORTED' || errorCode === -3) {
        return { ok: true, superseded: true }
      }

      this.#retireBinding(binding, 'activation-failed')

      return { error: 'browser-navigation-failed', ok: false }
    }
  }

  release(event: IpcMainInvokeEvent, request: BrowserGuestRelease) {
    const binding = this.#bindingForSender(event, request)

    if (binding) {
      this.#retireBinding(binding)
    } else if (this.#hostIds.has(event.sender.id)) {
      for (const [token, claim] of this.#claims) {
        if (
          claim.hostId === event.sender.id &&
          claim.tabId === request?.tabId &&
          claim.generation === request?.generation
        ) {
          this.#claims.delete(token)
        }
      }

      const accepted = this.#acceptedClaimsByHost.get(event.sender.id)

      if (accepted) {
        const next = accepted.filter(
          claim => claim.tabId !== request?.tabId || claim.generation !== request?.generation
        )

        if (next.length > 0) {
          this.#acceptedClaimsByHost.set(event.sender.id, next)
        } else {
          this.#acceptedClaimsByHost.delete(event.sender.id)
        }
      }
    }

    return { ok: true }
  }

  bindAutomation(event: IpcMainInvokeEvent, request: BrowserAutomationBindingRequest) {
    if (
      !this.#hostIds.has(event.sender.id) ||
      !validIdentifier(request?.taskId) ||
      !validIdentifier(request?.tabId) ||
      !validIdentifier(request?.guestGeneration) ||
      (request?.requireFreshSnapshot !== undefined && typeof request.requireFreshSnapshot !== 'boolean') ||
      !Number.isSafeInteger(request?.taskGeneration) ||
      request.taskGeneration <= 0
    ) {
      return { error: 'browser-automation-binding-invalid', ok: false }
    }

    const guest = this.#bindings.get(this.#bindingKey(event.sender.id, request.tabId))

    if (!guest || guest.generation !== request.guestGeneration) {
      return { error: 'browser-automation-guest-stale', ok: false }
    }

    const existingTask = this.#taskBindings.get(this.#taskBindingKey(request.taskId, 'automation'))
    const latestTaskGeneration = this.#latestTaskGenerations.get(request.taskId)

    const existingTab = [...this.#taskBindings.values()].find(
      binding =>
        binding.role === 'automation' &&
        binding.hostId === event.sender.id &&
        binding.tabId === request.tabId &&
        binding.taskId !== request.taskId
    )

    if (existingTab) {
      return { error: 'browser-automation-tab-owned', ok: false }
    }

    if (existingTask && request.taskGeneration <= existingTask.taskGeneration) {
      const unchanged =
        existingTask.hostId === event.sender.id &&
        existingTask.tabId === request.tabId &&
        existingTask.guestGeneration === request.guestGeneration &&
        existingTask.taskGeneration === request.taskGeneration

      return unchanged ? { ok: true } : { error: 'browser-automation-generation-stale', ok: false }
    }

    if (!existingTask && latestTaskGeneration !== undefined && request.taskGeneration <= latestTaskGeneration) {
      return { error: 'browser-automation-generation-stale', ok: false }
    }

    if (existingTask) {
      this.#invalidateAssignedUploads({
        guestGeneration: existingTask.guestGeneration,
        profile: existingTask.profile,
        tabId: existingTask.tabId,
        taskGeneration: existingTask.taskGeneration,
        taskId: existingTask.taskId
      })
      this.#cancelUploadChoosers(record => record.task === existingTask)
    }

    for (const role of ['automation', 'raw-cdp'] as const) {
      this.#taskBindings.set(this.#taskBindingKey(request.taskId, role), {
        awaitingFreshSnapshot: request.requireFreshSnapshot === true,
        guestGeneration: request.guestGeneration,
        hostId: event.sender.id,
        profile: guest.profile,
        role,
        tabId: request.tabId,
        taskGeneration: request.taskGeneration,
        taskId: request.taskId
      })
    }

    this.#invalidatePixelGrants(grant => grant.scope.task_id === request.taskId)
    this.#latestTaskGenerations.set(request.taskId, request.taskGeneration)
    this.#taskLifecycleSink?.({
      guestGeneration: request.guestGeneration,
      profile: guest.profile,
      tabId: request.tabId,
      taskGeneration: request.taskGeneration,
      taskId: request.taskId,
      type: 'bind'
    })

    return { ok: true, roles: ['automation', 'raw-cdp'] }
  }

  unbindAutomation(event: IpcMainInvokeEvent, request: BrowserAutomationBindingRequest) {
    this.#revokeExactAutomation(event, request)

    return { ok: true }
  }

  revokeLocal(event: IpcMainInvokeEvent, request: BrowserAutomationBindingRequest) {
    return { ok: true, retired: this.#revokeExactAutomation(event, request) }
  }

  stopAndClose(event: IpcMainInvokeEvent, request: BrowserAutomationBindingRequest) {
    const binding = this.#taskBindings.get(this.#taskBindingKey(request?.taskId, 'automation'))

    if (
      binding &&
      binding.hostId === event.sender.id &&
      binding.tabId === request?.tabId &&
      binding.guestGeneration === request?.guestGeneration &&
      binding.taskGeneration === request?.taskGeneration
    ) {
      const guest = this.#bindings.get(this.#bindingKey(binding.hostId, binding.tabId))

      if (guest?.generation === binding.guestGeneration) {
        this.#retireBinding(guest)

        return { ok: true, retired: true }
      }
    }

    return { ok: true, retired: false }
  }

  #revokeExactAutomation(event: IpcMainInvokeEvent, request: BrowserAutomationBindingRequest): boolean {
    const binding = this.#taskBindings.get(this.#taskBindingKey(request?.taskId, 'automation'))

    if (
      !binding ||
      binding.hostId !== event.sender.id ||
      binding.tabId !== request?.tabId ||
      binding.guestGeneration !== request?.guestGeneration ||
      binding.taskGeneration !== request?.taskGeneration
    ) {
      return false
    }

    this.#invalidatePixelGrants(
      grant =>
        grant.scope.task_id === binding.taskId &&
        grant.scope.guest_generation === binding.guestGeneration &&
        grant.scope.task_generation === binding.taskGeneration
    )
    this.#consents.revoke(
      prompt =>
        prompt.taskId === binding.taskId &&
        prompt.guestGeneration === binding.guestGeneration &&
        prompt.taskGeneration === binding.taskGeneration
    )
    this.#cancelUploadChoosers(
      record =>
        record.task.taskId === binding.taskId &&
        record.task.taskGeneration === binding.taskGeneration &&
        record.task.guestGeneration === binding.guestGeneration
    )
    this.#invalidateAssignedUploads({
      guestGeneration: binding.guestGeneration,
      profile: binding.profile,
      tabId: binding.tabId,
      taskGeneration: binding.taskGeneration,
      taskId: binding.taskId
    })
    this.#taskBindings.delete(this.#taskBindingKey(binding.taskId, 'automation'))
    this.#taskBindings.delete(this.#taskBindingKey(binding.taskId, 'raw-cdp'))
    this.#taskLifecycleSink?.({ ...request, profile: binding.profile, type: 'unbind' })

    return true
  }

  #isExternalHandlerScheme(scheme: string): boolean {
    return /^[a-z][a-z0-9+.-]*$/i.test(scheme) && !new Set([
      'about', 'blob', 'chrome', 'chrome-extension', 'data', 'devtools', 'file',
      'filesystem', 'http', 'https', 'javascript', 'view-source'
    ]).has(scheme.toLowerCase())
  }

  #siteForUrl(rawUrl: unknown): string {
    return browserNavigationPolicy.canonicalIdentity(rawUrl)?.siteKey ?? 'opaque origin'
  }

  #snpScope(task: BrowserAutomationBinding, guest: BrowserGuestBinding): SnpScope {
    return {
      guestGeneration: task.guestGeneration,
      profile: task.profile,
      tabId: task.tabId,
      taskGeneration: task.taskGeneration,
      taskId: task.taskId,
      workspaceId: guest.workspaceId
    }
  }

  #admissionCovers(
    admission: BrowserNavigationAdmission | undefined,
    guest: BrowserGuestBinding,
    target: string,
    classification: BrowserNavigationDecision
  ) {
    if (!admission || admission.expiresAt < Date.now() || admission.profile !== guest.profile ||
      admission.tabId !== guest.tabId || admission.guestGeneration !== guest.generation ||
      classification.siteKey !== admission.destinationSite) {return false}
    if (classification.classification === 'sensitive' && admission.classification !== 'sensitive') {return false}
    if (classification.classification === 'sensitive' &&
      classification.reasonCodes.some(reason => !admission.reasonCodes.includes(reason))) {return false}
    const parsed = browserNavigationPolicy.canonicalize(target)
    return Boolean(parsed && classification.siteKey)
  }

  #taskForGuest(guest: BrowserGuestBinding): BrowserAutomationBinding | null {
    return [...this.#taskBindings.values()].find(
      candidate =>
        candidate.role === 'automation' &&
        candidate.hostId === guest.hostId &&
        candidate.tabId === guest.tabId &&
        candidate.guestGeneration === guest.generation
    ) ?? null
  }

  async #requestConsent(
    task: BrowserAutomationBinding,
    guest: BrowserGuestBinding,
    category: BrowserConsentCategory,
    operationId: string,
    extra: Partial<BrowserConsentRequest> = {},
    ttlMs?: number
  ): Promise<boolean> {
    if (!validIdentifier(operationId, 512)) {return false}

    const request: BrowserConsentRequest = {
      category,
      guestGeneration: task.guestGeneration,
      operationId,
      profile: task.profile,
      site: extra.site ?? this.#siteForUrl(guest.guest.getURL()),
      tabId: task.tabId,
      taskGeneration: task.taskGeneration,
      taskId: task.taskId,
      ...extra
    }

    const outcome = await this.#consents.request(task.hostId, request, ttlMs)

    return (
      outcome.reason === 'allow' &&
      this.#taskBindings.get(this.#taskBindingKey(task.taskId, 'automation')) === task &&
      this.#bindings.get(this.#bindingKey(guest.hostId, guest.tabId)) === guest
    )
  }

  async assignPendingUpload(
    chooserId: string,
    request: BrowserUploadAssignmentRequest
  ): Promise<BrowserUploadAssignmentOutcome> {
    const record = this.#pendingUploadChoosers.get(chooserId)
    if (!record || record.abortController.signal.aborted || !Array.isArray(request?.files) ||
      request.files.length === 0 || (record.chooser.mode === 'selectSingle' && request.files.length !== 1)) {
      return 'not_started'
    }
    const files = request.files.map(file => ({ ...file }))
    if (files.length < 1 || files.length > 20 ||
      files.some(file => !validIdentifier(file.displayName, 1024) || !validIdentifier(file.mimeType, 256) ||
      typeof file.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(file.sha256) ||
      !Number.isSafeInteger(file.size) || file.size < 0)) {return 'not_started'}
    const upload = this.#deps.buildUploadConsent?.(record.chooser, files)
    if (!upload) {return 'not_started'}
    const operationId = `upload-${crypto.randomBytes(16).toString('base64url')}`
    const approved = await this.#requestConsent(
      record.task,
      record.binding,
      'upload-assignment',
      operationId,
      { documentGeneration: record.chooser.documentGeneration, site: record.chooser.origin, upload }
    )
    if (!approved || this.#pendingUploadChoosers.get(chooserId) !== record || record.abortController.signal.aborted) {
      return 'not_started'
    }
    const descriptor = await this.#resolveUploadInputDescriptor(record)
    if (!descriptor || descriptor.formFingerprint !== record.chooser.formFingerprint ||
      this.#pendingUploadChoosers.get(chooserId) !== record || record.abortController.signal.aborted) {
      this.#cancelUploadChoosers(candidate => candidate === record)
      return 'not_started'
    }
    let paths: readonly string[]
    try {
      paths = await request.consume()
    } catch {
      this.#cancelUploadChoosers(candidate => candidate === record)
      return 'not_started'
    }
    if (paths.length !== files.length || this.#pendingUploadChoosers.get(chooserId) !== record ||
      record.abortController.signal.aborted) {
      this.#cancelUploadChoosers(candidate => candidate === record)
      return 'not_started'
    }
    this.#pendingUploadChoosers.delete(chooserId)
    record.abortController.abort()
    if (request.settled) {
      this.#assignedUploadOwners.set(this.#assignedUploadOwnerKey({
        backendNodeId: String(record.chooser.backendNodeId),
        documentGeneration: String(record.chooser.documentGeneration),
        frameId: record.chooser.frameId,
        guestGeneration: record.chooser.guestGeneration,
        profile: record.chooser.profile,
        tabId: record.chooser.tabId
      }), record.chooser.chooserId)
      this.#trackAssignedUpload(
        record.chooser,
        record.binding.guest.id,
        record.binding.partition,
        record.binding.guest.session,
        paths,
        request.settled
      )
    }
    try {
      await record.binding.guest.debugger.sendCommand(
        'DOM.setFileInputFiles',
        { backendNodeId: record.chooser.backendNodeId, files: [...paths] },
        record.sessionId
      )
      return 'completed'
    } catch {
      return 'outcome_unknown'
    }
  }

  #trackAssignedUpload(
    chooser: Readonly<BrowserPendingUploadChooser>,
    guestId: number,
    partition: string,
    browserSession: Session,
    paths: readonly string[],
    settled: NonNullable<BrowserUploadAssignmentRequest['settled']>
  ) {
    const previous = this.#assignedUploads.get(chooser.chooserId)
    if (previous) {clearTimeout(previous.timer)}
    const record = {} as BrowserAssignedUploadRecord
    const timer = setTimeout(() => {
      if (this.#assignedUploads.get(chooser.chooserId) !== record) {return}
      this.#assignedUploads.delete(chooser.chooserId)
      void this.clearAssignedUpload({
        backendNodeId: String(chooser.backendNodeId), chooserId: chooser.chooserId,
        documentGeneration: String(chooser.documentGeneration), frameId: chooser.frameId,
        guestGeneration: chooser.guestGeneration, profile: chooser.profile,
        tabId: chooser.tabId, taskId: chooser.taskId
      }).finally(() => Promise.resolve(settled('expired')).catch(() => undefined))
      this.#deps.notifyUploadExpired?.(chooser)
    }, 30 * 60_000)
    timer.unref()
    Object.assign(record, {
      browserSession,
      chooser,
      guestId,
      partition,
      paths: Object.freeze([...paths]),
      settled,
      timer
    })
    this.#assignedUploads.set(chooser.chooserId, record)
  }

  #observeAssignedUploadRequest(partition: string, browserSession: Session, details: {
    id: number; method: string; uploadData?: readonly { file?: string }[]; url: string; webContentsId?: number
  }) {
    for (const record of this.#assignedUploads.values()) {
      if (record.requestId !== undefined || record.partition !== partition || record.browserSession !== browserSession ||
        details.webContentsId !== record.guestId ||
        details.method.toLowerCase() !== record.chooser.formMethod || details.url !== record.chooser.formActionUrl) {continue}
      const uploaded = new Set((details.uploadData ?? []).map(part => part.file).filter(Boolean))
      if (!record.paths.every(file => uploaded.has(file))) {continue}
      record.requestId = details.id
    }
  }

  #settleAssignedUploadRequest(
    partition: string,
    browserSession: Session,
    details: { id: number; webContentsId?: number },
    outcome: 'completed' | 'failed'
  ) {
    for (const [chooserId, record] of this.#assignedUploads) {
      if (record.partition !== partition || record.browserSession !== browserSession ||
        record.guestId !== details.webContentsId || record.requestId !== details.id) {continue}
      this.#assignedUploads.delete(chooserId)
      clearTimeout(record.timer)
      void Promise.resolve(record.settled(outcome)).catch(() => undefined)
    }
  }

  async clearAssignedUpload(binding: {
    backendNodeId: string
    chooserId: string
    documentGeneration: string
    frameId: string
    guestGeneration: string
    profile: string
    tabId: string
    taskId: string
  }): Promise<void> {
    const ownerKey = this.#assignedUploadOwnerKey(binding)
    if (this.#assignedUploadOwners.get(ownerKey) !== binding.chooserId) {return}
    const guest = [...this.#bindings.values()].find(candidate =>
      candidate.profile === binding.profile && candidate.tabId === binding.tabId &&
      candidate.generation === binding.guestGeneration)
    const backendNodeId = Number(binding.backendNodeId)
    if (!guest || String(guest.documentGeneration) !== binding.documentGeneration ||
      !Number.isSafeInteger(backendNodeId) || backendNodeId <= 0) {
      if (this.#assignedUploadOwners.get(ownerKey) === binding.chooserId) {
        this.#assignedUploadOwners.delete(ownerKey)
      }
      return
    }
    await guest.guest.debugger.sendCommand(
      'DOM.setFileInputFiles',
      { backendNodeId, files: [] },
      guest.frameSessions.get(binding.frameId)
    )
    if (this.#assignedUploadOwners.get(ownerKey) === binding.chooserId) {
      this.#assignedUploadOwners.delete(ownerKey)
    }
  }

  #assignedUploadOwnerKey(binding: {
    backendNodeId: string
    documentGeneration: string
    frameId: string
    guestGeneration: string
    profile: string
    tabId: string
  }) {
    return [
      binding.profile, binding.tabId, binding.guestGeneration, binding.documentGeneration,
      binding.frameId, binding.backendNodeId
    ].join('\0')
  }

  #siteGrantKey(task: BrowserAutomationBinding, site: string) {
    return `${task.profile}\0${task.taskId}\0${task.taskGeneration}\0${task.guestGeneration}\0${task.tabId}\0${site}`
  }

  async #authorizeNavigation(
    request: BrowserAutomationDispatch,
    task: BrowserAutomationBinding,
    guest: BrowserGuestBinding,
    target: string
  ): Promise<boolean> {
    const scope = this.#snpScope(task, guest)
    let classification = browserNavigationPolicy.lexicalDecision(target, 'cdp-navigate', scope)
    const destinationSite = classification.siteKey ?? 'opaque origin'
    const currentSite = this.#siteForUrl(guest.guest.getURL())

    if (destinationSite === 'opaque origin' || classification.disposition === 'block' ||
      !validIdentifier(request.operationId, 512)) {return false}
    if (classification.classification === 'ordinary' && destinationSite === currentSite) {return true}

    const key = this.#siteGrantKey(task, destinationSite)
    const grant = this.#siteGrants.get(key)
    if (classification.classification === 'ordinary' && grant && grant.expiresAt >= Date.now()) {return true}
    if (grant) {this.#siteGrants.delete(key)}

    const outcome = await this.#consents.request(task.hostId, {
      category: 'navigation',
      detail: currentSite,
      guestGeneration: task.guestGeneration,
      navigationPolicy: {
        categoryCodes: classification.categoryCodes,
        classification: classification.classification,
        exceptionEligible: classification.exceptionEligible,
        policyVersion: classification.revision,
        provenance: classification.provenance,
        pslVersion: classification.pslVersion,
        reasonCodes: classification.reasonCodes,
        urlParserVersion: classification.urlParserVersion
      },
      operationId: request.operationId!,
      profile: task.profile,
      site: destinationSite,
      tabId: task.tabId,
      taskGeneration: task.taskGeneration,
      taskId: task.taskId
    }, request.remainingDurationMs)
    const stillLive =
      this.#taskBindings.get(this.#taskBindingKey(task.taskId, 'automation')) === task &&
      this.#bindings.get(this.#bindingKey(guest.hostId, guest.tabId)) === guest
    if (!stillLive || (outcome.reason !== 'allow' && outcome.reason !== 'ordinary-for-task')) {return false}

    if (outcome.reason === 'ordinary-for-task') {
      const requestedTtl = Number.isSafeInteger(request.remainingDurationMs) && request.remainingDurationMs! > 0
        ? request.remainingDurationMs!
        : SITE_CONSENT_TTL_MS
      const exception = browserNavigationPolicy.sensitive.createException({
        ...scope,
        expiresAt: Date.now() + Math.min(SITE_CONSENT_TTL_MS, requestedTtl),
        url: target
      })
      if (!exception) {return false}
      classification = browserNavigationPolicy.lexicalDecision(target, 'cdp-navigate', scope)
      if (classification.classification !== 'ordinary') {return false}
    }

    this.#navigationAdmissions.set(guest.guest.id, {
      classification: classification.classification,
      destinationSite,
      expiresAt: Date.now() + 10_000,
      guestGeneration: task.guestGeneration,
      operationId: request.operationId!,
      profile: task.profile,
      reasonCodes: classification.reasonCodes,
      sourceSite: currentSite,
      tabId: task.tabId,
      url: browserNavigationPolicy.canonicalize(target)?.href ?? target
    })
    if (classification.classification === 'ordinary') {
      this.#siteGrants.set(key, {
        expiresAt: Date.now() + SITE_CONSENT_TTL_MS,
        guestGeneration: task.guestGeneration,
        profile: task.profile,
        site: destinationSite,
        tabId: task.tabId,
        taskGeneration: task.taskGeneration,
        taskId: task.taskId
      })
    }

    return true
  }

  #commandConsentCategory(method: string, params: Record<string, unknown>): BrowserConsentCategory | null {
    if (method === 'Runtime.evaluate') {
      // JavaScript is not safely classifiable from source text. Computed property
      // access, aliased functions, setters, event dispatch, and network primitives
      // can all hide website effects from a regex. Gate every evaluation instead
      // of maintaining a necessarily incomplete mutation denylist.
      return 'destructive-action'
    }

    if (
      method === 'Input.dispatchKeyEvent' &&
      (params.type === 'keyDown' || params.type === 'rawKeyDown') &&
      (params.key === 'Enter' || params.code === 'Enter' || params.windowsVirtualKeyCode === 13)
    ) {
      return 'website-submission'
    }

    // Every Input-domain method in the automation allowlist can change page
    // state or trigger handlers. This includes releases, inserted text, and
    // control events—not only the historically recognized mousePressed case.
    if (method.startsWith('Input.')) {return 'destructive-action'}

    return null
  }

  async dispatchAutomationCommand(request: BrowserAutomationDispatch): Promise<Record<string, unknown>> {
    const frame = request?.frame
    const id = frame?.id
    const method = frame?.method
    const params = frame?.params ?? {}

    const fail = (hermesCode: string, message: string, extra: Record<string, unknown> = {}) => ({
      id,
      error: { code: -32000, data: { disposition: 'not_started', hermesCode, ...extra }, message }
    })

    const uncertain = (hermesCode: string, message: string) => ({
      id,
      error: { code: -32002, data: { disposition: 'outcome_unknown', hermesCode }, message }
    })

    const binding = this.#taskBindings.get(
      this.#taskBindingKey(request?.taskId, request?.role)
    )

    if (
      !binding ||
      (request.role !== 'automation' && request.role !== 'raw-cdp') ||
      binding.role !== request.role ||
      binding.tabId !== request.tabId ||
      binding.guestGeneration !== request.guestGeneration ||
      binding.taskGeneration !== request.taskGeneration
    ) {
      return fail('NAVIGATION_TARGET_STALE', 'Browser automation binding is stale')
    }

    const guestBinding = this.#bindings.get(this.#bindingKey(binding.hostId, binding.tabId))

    if (!guestBinding || guestBinding.generation !== binding.guestGeneration) {
      return fail('NAVIGATION_TARGET_STALE', 'Browser guest is no longer live')
    }

    if (
      binding.awaitingFreshSnapshot &&
      (binding.role !== 'automation' || typeof method !== 'string' || !HAND_BACK_SNAPSHOT_METHODS.has(method))
    ) {
      return fail(
        'HAND_BACK_SNAPSHOT_REQUIRED',
        'A fresh browser snapshot is required before the handed-back generation may act'
      )
    }

    const stillBound = () =>
      this.#taskBindings.get(this.#taskBindingKey(binding.taskId, binding.role)) === binding &&
      this.#bindings.get(this.#bindingKey(binding.hostId, binding.tabId)) === guestBinding

    const debuggerFailed = () =>
      !stillBound() || guestBinding.guest.isDestroyed() || guestBinding.guest.isCrashed()

    const initialRemaining = request.remainingDurationMs

    const operationDeadline = Number.isSafeInteger(initialRemaining) && (initialRemaining as number) >= 0 && (initialRemaining as number) <= 120_000
      ? Date.now() + (initialRemaining as number)
      : null

    const sendCommand = async (commandMethod: string, commandParams: Record<string, unknown>) => {
      const remaining = operationDeadline === null ? undefined : Math.max(0, operationDeadline - Date.now())

      if (remaining === undefined) {
        return {
          result: await guestBinding.guest.debugger.sendCommand(commandMethod, commandParams),
          timedOut: false as const
        }
      }

      if (remaining <= 0) {
        return { timedOut: true as const }
      }

      let timer: ReturnType<typeof setTimeout> | undefined

      const timeout = new Promise<{ result: undefined; timedOut: true }>(resolve => {
        timer = setTimeout(() => resolve({ result: undefined, timedOut: true }), remaining)
        timer.unref()
      })

      try {
        return await Promise.race([
          guestBinding.guest.debugger
            .sendCommand(commandMethod, commandParams)
            .then(result => ({ result, timedOut: false as const })),
          timeout
        ])
      } finally {
        if (timer) {clearTimeout(timer)}
      }
    }

    if (method === PIXEL_CONSENT_METHOD) {
      if (
        request.role !== 'automation' ||
        (typeof id !== 'number' && typeof id !== 'string') ||
        typeof id === 'boolean' ||
        !isRecord(params)
      ) {
        return fail('CAPTURE_CONSENT_REQUIRED', 'Trusted pixel consent request was rejected')
      }

      return { id, result: await this.#requestPixelGrant(request, binding, guestBinding, params) }
    }

    if (typeof method === 'string' && AUTHENTICATED_PIXEL_METHODS.has(method)) {
      if (method === 'Page.captureScreenshot' && isRecord(params)) {
        const consumed = this.#consumePixelGrant(request, params)

        if (consumed) {
          try {
            const dispatched = await sendCommand(method, consumed.params)

            if (dispatched.timedOut) {
              return uncertain('BROWSER_OUTCOME_UNKNOWN', 'Browser command exceeded its authenticated deadline')
            }

            const result = dispatched.result
            const data = result?.data

            if (typeof data !== 'string') {
              return fail('CAPTURE_OUTPUT_INVALID', 'Transient screenshot result was malformed')
            }

            const transient = Buffer.from(data, 'base64')

            try {
              if (transient.byteLength === 0 || transient.byteLength > consumed.grant.maxBytes) {
                return fail('CAPTURE_OUTPUT_TOO_LARGE', 'Transient screenshot exceeded its exact grant')
              }
            } finally {
              transient.fill(0)
            }

            // This payload remains on the authenticated memory-only operation.
            // It is not exposed through renderer IPC or any logging seam.
            return { id, result: { data } }
          } catch {
            return debuggerFailed()
              ? uncertain('BROWSER_OUTCOME_UNKNOWN', 'Browser guest crashed after screenshot dispatch')
              : { id, error: { code: -32001, message: 'Electron debugger command failed' } }
          }
        }
      }

      return fail(
        'CAPTURE_CONSENT_REQUIRED',
        'Authenticated pixels require an exact trusted one-shot consent grant'
      )
    }

    if (
      method === 'DOM.setFileInputFiles' ||
      method === 'Page.handleFileChooser' ||
      method === 'Hermes.assignStagedUpload'
    ) {
      return fail(
        'UPLOAD_UNSUPPORTED',
        'Upload assignment requires an exact verified local staged handle; no such handle exists'
      )
    }

    if (
      (typeof id !== 'number' && typeof id !== 'string') ||
      typeof id === 'boolean' ||
      typeof method !== 'string' ||
      !AUTOMATION_DEBUGGER_METHODS.has(method) ||
      params === null ||
      typeof params !== 'object' ||
      Array.isArray(params)
    ) {
      return fail('CDP_METHOD_BLOCKED', 'CDP method is outside the automation role')
    }

    const commandParams = params as Record<string, unknown>

    if (binding.role === 'raw-cdp' && RAW_CDP_DENIED_METHODS.has(method)) {
      return fail(
        'RAW_CDP_METHOD_BLOCKED',
        `${method} is reserved for trusted browser automation`
      )
    }

    const consentTask = this.#taskBindings.get(this.#taskBindingKey(binding.taskId, 'automation'))

    if (!consentTask || consentTask.hostId !== binding.hostId || consentTask.taskGeneration !== binding.taskGeneration) {
      return fail('NAVIGATION_TARGET_STALE', 'Trusted consent scope is no longer active')
    }

    if (method === 'Page.navigate' && typeof commandParams.url === 'string') {
      const parsed = browserNavigationPolicy.canonicalize(commandParams.url)
      const scheme = parsed?.protocol.replace(/:$/, '') ?? ''

      if (this.#isExternalHandlerScheme(scheme)) {
        const approved = await this.#requestConsent(
          consentTask,
          guestBinding,
          'external-handler',
          request.operationId ?? '',
          { scheme, site: `${scheme}:` },
          request.remainingDurationMs
        )

        if (!approved || !this.#deps.launchExternal) {
          return fail('EXTERNAL_HANDLER_DENIED', 'External handler launch was not authorized')
        }

        try {
          await this.#deps.launchExternal(commandParams.url)

          return { id, result: { launched: true } }
        } catch {
          return fail('EXTERNAL_HANDLER_FAILED', 'External handler launch failed')
        }
      }
    }

    const navigation = await browserNavigationPolicy.evaluateDebuggerCommand(
      method,
      commandParams,
      guestBinding.guest.getURL(),
      hostname => this.#resolveHostAddresses(guestBinding.partition, hostname),
      this.#snpScope(consentTask, guestBinding)
    )

    if (navigation.disposition === 'block' || navigation.reason === 'local-or-private-unknown') {
      return fail(
        navigation.hermesCode ?? 'NAVIGATION_POLICY_BLOCKED',
        'Navigation blocked by Hermes policy',
        { method, policyRevision: navigation.revision, ...(navigation.scheme ? { scheme: navigation.scheme } : {}) }
      )
    }

    if (
      (method === 'Page.navigate' || method === 'Page.reload') &&
      !(await this.#authorizeNavigation(
        request,
        consentTask,
        guestBinding,
        method === 'Page.navigate' ? commandParams.url as string : guestBinding.guest.getURL()
      ))
    ) {
      return fail('CONSENT_REQUIRED', 'Navigation requires exact trusted consent')
    }

    const actionCategory = this.#commandConsentCategory(method, commandParams)

    if (
      actionCategory &&
      !(await this.#requestConsent(
        consentTask,
        guestBinding,
        actionCategory,
        request.operationId ?? '',
        {},
        request.remainingDurationMs
      ))
    ) {
      return fail('CONSENT_REQUIRED', 'Browser action requires exact trusted consent')
    }

    // DNS/policy evaluation is asynchronous. Local teardown or takeover wins
    // before debugger dispatch rather than allowing an obsolete lease to act.
    if (!stillBound()) {
      return fail('NAVIGATION_TARGET_STALE', 'Browser automation binding retired before dispatch')
    }

    try {
      const dispatched = await sendCommand(method, commandParams)

      if (dispatched.timedOut) {
        return uncertain('BROWSER_OUTCOME_UNKNOWN', 'Browser command exceeded its authenticated deadline')
      }

      const result = dispatched.result

      // Once dispatch occurred, losing the lease means the website effect may
      // have happened. Never report a stale late success as confirmed.
      if (!stillBound()) {
        return uncertain('BROWSER_OUTCOME_UNKNOWN', 'Browser binding retired after dispatch')
      }

      if (
        binding.awaitingFreshSnapshot &&
        method === 'Accessibility.getFullAXTree' &&
        isRecord(result) &&
        Array.isArray(result.nodes)
      ) {
        const automation = this.#taskBindings.get(this.#taskBindingKey(binding.taskId, 'automation'))
        const rawCdp = this.#taskBindings.get(this.#taskBindingKey(binding.taskId, 'raw-cdp'))

        if (automation === binding) {
          automation.awaitingFreshSnapshot = false

          if (rawCdp) {
            rawCdp.awaitingFreshSnapshot = false
          }

          this.#deps.notifyFreshSnapshot?.({
            guestGeneration: binding.guestGeneration,
            hostId: binding.hostId,
            surfaceEpoch: guestBinding.surfaceEpoch,
            tabId: binding.tabId,
            taskGeneration: binding.taskGeneration,
            taskId: binding.taskId
          })
        }
      }

      return { id, result: result ?? {} }
    } catch {
      return debuggerFailed()
        ? uncertain('BROWSER_OUTCOME_UNKNOWN', 'Browser guest crashed after debugger dispatch')
        : { id, error: { code: -32001, message: 'Electron debugger command failed' } }
    }
  }

  async #requestPixelGrant(
    request: BrowserAutomationDispatch,
    taskBinding: BrowserAutomationBinding,
    guestBinding: BrowserGuestBinding,
    params: Record<string, unknown>
  ): Promise<Record<string, unknown>> {
    const scope = params.scope
    const captureParams = params.captureParams
    const purpose = params.purpose
    const recipient = params.recipient
    const maxBytes = params.maxBytes
    const retention = params.retention
    const encodedCapture = stableJson(captureParams)
    const exactViewportCapture = stableJson(VIEWPORT_SCREENSHOT_PARAMS)

    if (
      !isRecord(scope) ||
      !isRecord(captureParams) ||
      encodedCapture === null ||
      encodedCapture !== exactViewportCapture ||
      captureParams[PIXEL_GRANT_PARAM] !== undefined ||
      !validIdentifier(purpose, 1024) ||
      !validIdentifier(recipient, 512) ||
      retention !== 'memory-only-transient' ||
      !Number.isSafeInteger(maxBytes) ||
      (maxBytes as number) <= 0 ||
      (maxBytes as number) > MAX_TRANSIENT_PIXEL_BYTES
    ) {
      return { error: 'CAPTURE_CONSENT_REQUEST_INVALID', granted: false }
    }

    const exactScope: BrowserAuthenticatedPixelScope = {
      binding_generation: scope.binding_generation as number,
      capability_generation: scope.capability_generation as number,
      connection_id: scope.connection_id as string,
      document_generation: scope.document_generation as number,
      guest_generation: scope.guest_generation as string,
      profile: scope.profile as string,
      tab_id: scope.tab_id as string,
      task_generation: scope.task_generation as number,
      task_id: scope.task_id as string
    }

    const positive = (value: unknown) => Number.isSafeInteger(value) && (value as number) > 0

    const exact =
      exactScope.profile === request.profile &&
      exactScope.profile === taskBinding.profile &&
      exactScope.connection_id === request.connectionId &&
      exactScope.capability_generation === request.capabilityGeneration &&
      exactScope.binding_generation === request.bindingGeneration &&
      exactScope.tab_id === request.tabId &&
      exactScope.tab_id === taskBinding.tabId &&
      exactScope.task_id === request.taskId &&
      exactScope.task_id === taskBinding.taskId &&
      exactScope.guest_generation === request.guestGeneration &&
      exactScope.guest_generation === taskBinding.guestGeneration &&
      exactScope.task_generation === request.taskGeneration &&
      exactScope.task_generation === taskBinding.taskGeneration &&
      positive(exactScope.capability_generation) &&
      positive(exactScope.binding_generation) &&
      positive(exactScope.document_generation) &&
      validIdentifier(exactScope.connection_id, 512)

    if (!exact) {return { error: 'CAPTURE_SCOPE_MISMATCH', granted: false }}

    let origin = 'opaque origin'

    try {
      origin = new URL(guestBinding.guest.getURL()).origin
    } catch {
      // Trusted chrome shows an opaque origin rather than malformed raw URL data.
    }

    const getTitle = (guestBinding.guest as WebContents & { getTitle?: () => string }).getTitle
    const tabTitle = typeof getTitle === 'function' ? getTitle.call(guestBinding.guest).slice(0, 256) : 'Browser tab'
    const frozenScope = Object.freeze({ ...exactScope })

    const prompt = Object.freeze({
      captureKind: 'viewport-screenshot' as const,
      maxBytes: maxBytes as number,
      origin,
      purpose,
      recipient,
      retention: 'memory-only-transient' as const,
      scope: frozenScope,
      tabTitle
    })

    const lifecycleGeneration = this.#pixelLifecycleGeneration

    const approved = this.#deps.requestPixelConsent
      ? await this.#deps.requestPixelConsent(prompt)
      : await this.#requestConsent(
          taskBinding,
          guestBinding,
          'outbound-pixels',
          request.operationId ?? '',
          {
            captureScope: 'viewport',
            documentGeneration: exactScope.document_generation,
            maxBytes: maxBytes as number,
            purpose,
            recipient,
            retention: 'memory-only-transient',
            site: origin,
            tabTitle
          },
          request.remainingDurationMs
        )

    const stillBound =
      lifecycleGeneration === this.#pixelLifecycleGeneration &&
      this.#taskBindings.get(this.#taskBindingKey(taskBinding.taskId, 'automation')) === taskBinding &&
      this.#bindings.get(this.#bindingKey(guestBinding.hostId, guestBinding.tabId)) === guestBinding

    if (!approved || !stillBound) {
      return { error: approved ? 'CAPTURE_SCOPE_STALE' : 'CAPTURE_DENIED', granted: false }
    }

    const grantId = crypto.randomBytes(32).toString('base64url')

    let grant!: BrowserPixelGrant

    const expiryTimer = setTimeout(() => {
      if (this.#pixelGrants.get(grantId) === grant) {this.#pixelGrants.delete(grantId)}
    }, PIXEL_GRANT_TTL_MS)

    expiryTimer.unref()
    grant = {
      captureParams: encodedCapture,
      expiresAt: Date.now() + PIXEL_GRANT_TTL_MS,
      expiryTimer,
      maxBytes: maxBytes as number,
      purpose,
      recipient,
      scope: exactScope
    }
    this.#pixelGrants.set(grantId, grant)

    return { expiresInMs: PIXEL_GRANT_TTL_MS, grantId, granted: true }
  }

  #consumePixelGrant(
    request: BrowserAutomationDispatch,
    params: Record<string, unknown>
  ): { grant: BrowserPixelGrant; params: Record<string, unknown> } | null {
    const envelope = params[PIXEL_GRANT_PARAM]

    if (!isRecord(envelope)) {return null}
    const grantId = envelope.grantId

    if (!validIdentifier(grantId, 128)) {return null}
    const grant = this.#pixelGrants.get(grantId)

    if (!grant) {return null}
    // Delete before any await/dispatch: concurrent races and replay cannot
    // produce a second screenshot.
    clearTimeout(grant.expiryTimer)
    this.#pixelGrants.delete(grantId)
    const commandParams = { ...params }

    delete commandParams[PIXEL_GRANT_PARAM]
    const scope = grant.scope

    const exact =
      grant.expiresAt >= Date.now() &&
      request.role === 'automation' &&
      envelope.recipient === grant.recipient &&
      envelope.purpose === grant.purpose &&
      stableJson(envelope.scope) === stableJson(scope) &&
      stableJson(commandParams) === grant.captureParams &&
      request.profile === scope.profile &&
      request.connectionId === scope.connection_id &&
      request.capabilityGeneration === scope.capability_generation &&
      request.bindingGeneration === scope.binding_generation &&
      request.tabId === scope.tab_id &&
      request.taskId === scope.task_id &&
      request.guestGeneration === scope.guest_generation &&
      request.taskGeneration === scope.task_generation

    return exact ? { grant, params: commandParams } : null
  }

  #invalidatePixelGrants(predicate: (grant: BrowserPixelGrant) => boolean) {
    this.#pixelLifecycleGeneration += 1

    for (const [grantId, grant] of this.#pixelGrants) {
      if (predicate(grant)) {
        clearTimeout(grant.expiryTimer)
        this.#pixelGrants.delete(grantId)
      }
    }
  }

  async report(event: IpcMainInvokeEvent, request: BrowserGuestReport) {
    const binding = this.#bindingForSender(event, request)

    if (!binding || request.kind !== 'viewport') {
      return { error: 'browser-report-denied', ok: false }
    }

    const documentGeneration = this.#reportDocumentGeneration(binding, request)

    return documentGeneration === null
      ? { error: 'browser-report-stale', ok: false }
      : this.#collectReport(binding, documentGeneration, { kind: 'viewport' })
  }

  /** Main-process-only escalation seam for browser-owned protocol/form/accessibility
   * semantics. Page text and renderer claims never call this authority. */
  observeSensitiveNavigation(input: BrowserSensitiveNavigationObservation) {
    const binding = this.#bindings.get(this.#bindingKey(input.hostId, input.tabId))
    if (!binding || binding.generation !== input.generation) {return false}
    return Boolean(browserNavigationPolicy.sensitive.observeSensitive({
      profile: binding.profile,
      signal: input.signal,
      url: binding.guest.getURL(),
      workspaceId: binding.workspaceId
    }))
  }

  /** Main-process-only semantic collection seam. Raw guest semantics must never
   * be returned by a renderer-facing IPC handler. */
  async collectSemanticCandidates(
    identity: BrowserGuestRelease & { hostId: number },
    documentGeneration: number,
    tags: readonly string[]
  ) {
    const binding = this.#bindings.get(this.#bindingKey(identity.hostId, identity.tabId))

    if (
      !binding ||
      binding.generation !== identity.generation ||
      !Number.isSafeInteger(documentGeneration) ||
      documentGeneration !== binding.documentGeneration
    ) {
      return { error: 'browser-report-stale', ok: false }
    }

    return this.#collectReport(binding, documentGeneration, { kind: 'semantic-candidates', tags })
  }

  /** Resolver-ready annotation input. This seam remains main-only: raw text and
   * attribute values are digested before the result leaves this method. */
  async collectTrustedAnnotationReport(
    identity: BrowserGuestRelease & { hostId: number },
    documentGeneration: number,
    tags: readonly string[]
  ) {
    const binding = this.#bindings.get(this.#bindingKey(identity.hostId, identity.tabId))

    if (
      !binding ||
      binding.generation !== identity.generation ||
      !Number.isSafeInteger(documentGeneration) ||
      documentGeneration !== binding.documentGeneration
    ) {
      return { error: 'browser-report-stale', ok: false }
    }

    if (!this.#enterReport(binding)) {
      return { error: 'browser-report-rate-limited', ok: false }
    }

    try {
      const value = await collectAnnotationTrustedMainReport({
        debuggerClient: binding.guest.debugger,
        documentGeneration,
        frameSessions: binding.frameSessions,
        isCurrent: () => this.#isCurrentReportBinding(binding, documentGeneration),
        tags
      })

      return this.#isCurrentReportBinding(binding, documentGeneration)
        ? { documentGeneration, ok: true, value }
        : { error: 'browser-report-stale', ok: false }
    } catch (error) {
      return (error as Error)?.message === 'browser-annotation-main-stale'
        ? { error: 'browser-report-stale', ok: false }
        : { error: 'browser-report-failed', ok: false }
    } finally {
      binding.reportInFlight = Math.max(0, binding.reportInFlight - 1)
    }
  }

  async resolveAnnotations(event: IpcMainInvokeEvent, request: BrowserAnnotationProjectionRequest) {
    const binding = this.#bindingForSender(event, request)
    if (!binding || request?.workspaceId !== binding.workspaceId) {
      return { error: 'browser-annotation-scope-denied', ok: false }
    }
    const scope = {
      documentGeneration: binding.documentGeneration,
      profile: binding.profile,
      tabId: binding.tabId,
      workspaceId: binding.workspaceId
    }
    const tags = annotationProjectionTags(request.records, scope)
    if (!tags) {return { error: 'browser-annotation-invalid', ok: false }}

    const collected = await this.collectTrustedAnnotationReport(
      { generation: binding.generation, hostId: binding.hostId, tabId: binding.tabId },
      binding.documentGeneration,
      tags
    )
    if (!collected.ok || !('value' in collected) || !collected.value) {return collected}

    const projected = projectTrustedAnnotations(request.records, collected.value, scope, {
      labels: binding.annotationLabels,
      nextLabel: binding.nextAnnotationLabel
    })
    if (!projected || !this.#isCurrentReportBinding(binding, scope.documentGeneration)) {
      return { error: 'browser-annotation-stale', ok: false }
    }
    binding.annotationLabels = projected.labels.labels
    binding.nextAnnotationLabel = projected.labels.nextLabel
    return {
      documentGeneration: scope.documentGeneration,
      ok: true,
      projections: projected.projections
    }
  }

  async exportAnnotationScreenshot(event: IpcMainInvokeEvent, request: BrowserAnnotationProjectionRequest) {
    const binding = this.#bindingForSender(event, request)
    if (!binding || request?.workspaceId !== binding.workspaceId || binding.partition.startsWith(PRIVATE_PARTITION_PREFIX)) {
      return { error: 'browser-annotation-scope-denied', ok: false }
    }
    const scope = {
      documentGeneration: binding.documentGeneration,
      profile: binding.profile,
      tabId: binding.tabId,
      workspaceId: binding.workspaceId
    }
    const tags = annotationProjectionTags(request.records, scope)
    if (!tags) {return { error: 'browser-annotation-invalid', ok: false }}

    const collected = await this.collectTrustedAnnotationReport(
      { generation: binding.generation, hostId: binding.hostId, tabId: binding.tabId },
      binding.documentGeneration,
      tags
    )
    if (!collected.ok || !('value' in collected) || !collected.value) {return collected}
    const projected = projectTrustedAnnotations(request.records, collected.value, scope, {
      labels: binding.annotationLabels,
      nextLabel: binding.nextAnnotationLabel
    })
    if (!projected || !this.#isCurrentReportBinding(binding, scope.documentGeneration)) {
      return { error: 'browser-annotation-stale', ok: false }
    }

    // Commit label authority before the screenshot await. A canceled export still
    // consumes newly issued labels, so concurrent refresh/export can never recycle.
    binding.annotationLabels = projected.labels.labels
    binding.nextAnnotationLabel = projected.labels.nextLabel
    if (!this.#deps.saveAnnotationScreenshot) {return { error: 'browser-annotation-export-unavailable', ok: false }}

    let png: Buffer | null = null
    try {
      const captured = await binding.guest.debugger.sendCommand('Page.captureScreenshot', VIEWPORT_SCREENSHOT_PARAMS)
      const data = captured?.data
      if (typeof data !== 'string' || !this.#isCurrentReportBinding(binding, scope.documentGeneration)) {
        return { error: 'browser-annotation-stale', ok: false }
      }
      png = Buffer.from(data, 'base64')
      if (png.byteLength === 0 || png.byteLength > MAX_ANNOTATION_SCREENSHOT_BYTES) {
        return { error: 'browser-annotation-export-too-large', ok: false }
      }
      const outcome = await this.#deps.saveAnnotationScreenshot(binding.hostId, png, projected.viewport, projected.markers)
      return { canceled: outcome === 'canceled', ok: true }
    } catch {
      return { error: 'browser-annotation-export-failed', ok: false }
    } finally {
      png?.fill(0)
    }
  }

  async #collectReport(
    binding: BrowserGuestBinding,
    documentGeneration: number,
    isolatedRequest: AnnotationIsolatedWorldRequest
  ) {

    if (!this.#enterReport(binding)) {
      return { error: 'browser-report-rate-limited', ok: false }
    }

    try {
      const frameTree = await binding.guest.debugger.sendCommand('Page.getFrameTree')
      const frameId = frameTree?.frameTree?.frame?.id

      if (!validIdentifier(frameId)) {
        return { error: 'browser-report-failed', ok: false }
      }

      if (!this.#isCurrentReportBinding(binding, documentGeneration)) {
        return { error: 'browser-report-stale', ok: false }
      }

      const value = await collectAnnotationIsolatedWorldReport(binding.guest.debugger, frameId, isolatedRequest)

      if (!this.#isCurrentReportBinding(binding, documentGeneration)) {
        return { error: 'browser-report-stale', ok: false }
      }

      return { documentGeneration, ok: true, value }
    } catch {
      return { error: 'browser-report-failed', ok: false }
    } finally {
      binding.reportInFlight = Math.max(0, binding.reportInFlight - 1)
    }
  }

  #reportDocumentGeneration(binding: BrowserGuestBinding, request: BrowserGuestReport): number | null {
    if (request.documentGeneration !== undefined) {
      return Number.isSafeInteger(request.documentGeneration) && request.documentGeneration === binding.documentGeneration
        ? binding.documentGeneration
        : null
    }

    return binding.documentGeneration
  }

  #enterReport(binding: BrowserGuestBinding): boolean {
    const now = Date.now()

    if (now - binding.reportWindowStartedAt >= REPORT_RATE_WINDOW_MS) {
      binding.reportWindowStartedAt = now
      binding.reportWindowCount = 0
    }

    if (
      binding.reportInFlight >= MAX_REPORTS_IN_FLIGHT_PER_BINDING ||
      binding.reportWindowCount >= MAX_REPORTS_PER_BINDING_WINDOW
    ) {
      return false
    }

    binding.reportInFlight += 1
    binding.reportWindowCount += 1

    return true
  }

  #isCurrentReportBinding(binding: BrowserGuestBinding, documentGeneration: number): boolean {
    return (
      binding.documentGeneration === documentGeneration &&
      this.#bindings.get(this.#bindingKey(binding.hostId, binding.tabId)) === binding &&
      !binding.guest.isDestroyed() &&
      !binding.guest.isCrashed()
    )
  }

  async sendInternalDebuggerCommand(
    identity: BrowserGuestRelease & { hostId: number },
    method: string,
    params: Record<string, unknown> = {}
  ) {
    if (!INTERNAL_DEBUGGER_METHODS.has(method)) {
      throw new Error('browser-debugger-method-denied')
    }

    const binding = this.#bindings.get(this.#bindingKey(identity.hostId, identity.tabId))

    if (!binding || binding.generation !== identity.generation) {
      throw new Error('browser-debugger-binding-stale')
    }

    return binding.guest.debugger.sendCommand(method, params)
  }

  #guardCreatedContents(contents: WebContents) {
    contents.on('will-attach-webview', (event, webPreferences, params) => {
      if (!isBrowserPartition(params?.partition)) {
        return
      }

      const token = attachmentTokenFromUrl(params?.src)
      const claim = token ? this.#claims.get(token) : undefined

      if (
        !claim ||
        claim.expiresAt < Date.now() ||
        claim.hostId !== contents.id ||
        claim.partition !== params.partition
      ) {
        if (token) {
          this.#claims.delete(token)
        }

        denyEvent(event)

        return
      }

      this.#claims.delete(claim.token)
      const accepted = this.#acceptedClaimsByHost.get(contents.id) ?? []
      accepted.push(claim)
      this.#acceptedClaimsByHost.set(contents.id, accepted)
      webPreferences.preload = undefined
      webPreferences.nodeIntegration = false
      webPreferences.nodeIntegrationInWorker = false
      webPreferences.nodeIntegrationInSubFrames = false
      webPreferences.contextIsolation = true
      webPreferences.sandbox = true
      webPreferences.webSecurity = true
      webPreferences.allowRunningInsecureContent = false
      webPreferences.webviewTag = false
      webPreferences.devTools = false
      webPreferences.navigateOnDragDrop = false
      webPreferences.safeDialogs = true
      delete params.preload
      params.name = ''

      this.#installPartitionGuards(claim.partition)
    })

    contents.on('did-attach-webview', (_event, guest) => {
      void this.#consumeAcceptedClaimForGuest(contents.id, guest).then(consumed => {
        if (!consumed || consumed.hostId !== contents.id) {
          if (this.#isInstalledBrowserSession(guest.session)) {
            safeDestroy(guest)
          }

          return
        }

        void this.#bindGuest(consumed, guest)
      }).catch(() => {
        if (this.#isInstalledBrowserSession(guest.session)) {
          safeDestroy(guest)
        }
      })
    })
  }

  async #consumeAcceptedClaimForGuest(hostId: number, guest: WebContents): Promise<BrowserGuestClaim | undefined> {
    const deadline = Date.now() + ATTACH_URL_RESOLUTION_TIMEOUT_MS

    while (!guest.isDestroyed()) {
      const guestUrl = guest.getURL()
      const token = attachmentTokenFromUrl(guestUrl)

      if (token) {
        const accepted = this.#acceptedClaimsByHost.get(hostId) ?? []
        const acceptedIndex = accepted.findIndex(
          claim => claim.token === token && guest.session === this.#deps.sessionFromPartition(claim.partition)
        )
        const consumed = acceptedIndex >= 0 ? accepted.splice(acceptedIndex, 1)[0] : undefined

        if (accepted.length === 0) {
          this.#acceptedClaimsByHost.delete(hostId)
        }

        return consumed
      }

      // Electron 40 emits did-attach-webview before the initial about:blank URL
      // (and therefore its attachment token) is observable on WebContents.
      // Wait only for that transient blank state; any other URL is unclaimed.
      if (guestUrl !== '' && guestUrl !== 'about:blank') {
        return undefined
      }
      if (Date.now() >= deadline) {
        return undefined
      }

      await new Promise(resolve => setTimeout(resolve, ATTACH_URL_POLL_INTERVAL_MS))
    }

    return undefined
  }

  #isInstalledBrowserSession(candidate: Session): boolean {
    for (const partition of this.#installedPartitions) {
      if (candidate === this.#deps.sessionFromPartition(partition)) {
        return true
      }
    }

    return false
  }

  async #bindGuest(claim: BrowserGuestClaim, guest: WebContents) {
    const key = this.#bindingKey(claim.hostId, claim.tabId)
    const previous = this.#bindings.get(key)

    if (previous) {
      this.#retireBinding(previous)
    }

    // Fence debugger setup before its awaits. A later guest replacement becomes
    // authoritative immediately, so an older setup completion cannot overwrite
    // its successor.
    this.#bindingSetups.set(key, claim.generation)
    let setupRetirementNotified = false
    const frameSessions = new Map<string, string>()
    const detachedSessions = new Set<string>()

    const notifySetupRetired = (reason: BrowserGuestRetiredEvent['reason']) => {
      if (setupRetirementNotified) {return}
      setupRetirementNotified = true
      this.#notifyRetired(claim, reason)
    }

    try {
      guest.setWindowOpenHandler(details => {
        const target = typeof details?.url === 'string' ? details.url : ''
        const parsed = browserNavigationPolicy.canonicalize(target)
        const scheme = parsed?.protocol.replace(/:$/, '') ?? ''

        if (this.#isExternalHandlerScheme(scheme)) {
          const live = this.#bindings.get(key)
          const task = live ? this.#taskForGuest(live) : null

          if (live && task && this.#deps.launchExternal) {
            const operationId = `native-${crypto.randomBytes(16).toString('base64url')}`
            void this.#requestConsent(task, live, 'external-handler', operationId, {
              scheme,
              site: `${scheme}:`
            }).then(approved => approved ? this.#deps.launchExternal?.(target) : undefined).catch(() => undefined)
          }
        }

        // Popup creation is always denied. External handlers, when approved, are
        // launched by trusted main and never inherit opener authority.
        return { action: 'deny' }
      })
      guest.on('will-navigate', (event, url) => {
        const live = this.#bindings.get(key)
        const task = live ? this.#taskForGuest(live) : null
        const lexical = browserNavigationPolicy.lexicalDecision(
          url,
          'guest-main-frame',
          live && task ? this.#snpScope(task, live) : undefined
        )
        const parsed = browserNavigationPolicy.canonicalize(url)
        const scheme = parsed?.protocol.replace(/:$/, '') ?? ''

        if (live && task && this.#isExternalHandlerScheme(scheme)) {
          denyEvent(event)
          const operationId = `native-${crypto.randomBytes(16).toString('base64url')}`
          void this.#requestConsent(task, live, 'external-handler', operationId, {
            scheme,
            site: `${scheme}:`
          }).then(approved => approved ? this.#deps.launchExternal?.(url) : undefined).catch(() => undefined)

          return
        }

        if (!live || lexical.disposition === 'block') {
          denyEvent(event)

          return
        }

        const admission = this.#navigationAdmissions.get(guest.id)
        if (this.#admissionCovers(admission, live, url, lexical)) {return}
        if (!task) {
          denyEvent(event)

          return
        }

        const currentSite = this.#siteForUrl(guest.getURL())
        const targetSite = lexical.siteKey ?? 'opaque origin'
        const grant = this.#siteGrants.get(this.#siteGrantKey(task, targetSite))
        if (lexical.classification === 'ordinary' &&
          (targetSite === currentSite || (grant && grant.expiresAt >= Date.now()))) {return}
        denyEvent(event)
        const operationId = `native-${crypto.randomBytes(16).toString('base64url')}`
        void this.#authorizeNavigation(
          { ...task, frame: {}, operationId, role: 'automation' },
          task,
          live,
          url
        )
      })
      guest.on('will-frame-navigate', event => {
        const parsed = browserNavigationPolicy.canonicalize(event.url)
        if (browserNavigationPolicy.lexicalDecision(event.url, 'guest-frame').disposition === 'block' ||
          !parsed || isPrivateBrowserAddress(parsed.hostname)) {
          denyEvent(event)
        }
      })
      guest.on('will-redirect', (event, url) => {
        const live = this.#bindings.get(key)
        const task = live ? this.#taskForGuest(live) : null
        const lexical = browserNavigationPolicy.lexicalDecision(
          url,
          'guest-redirect',
          live && task ? this.#snpScope(task, live) : undefined
        )
        const targetSite = lexical.siteKey ?? 'opaque origin'
        const currentSite = this.#siteForUrl(guest.getURL())
        const grant = task ? this.#siteGrants.get(this.#siteGrantKey(task, targetSite)) : null

        if (!live || lexical.disposition === 'block') {
          denyEvent(event)

          return
        }

        const admission = this.#navigationAdmissions.get(guest.id)
        if (this.#admissionCovers(admission, live, url, lexical)) {return}
        if (!task) {
          denyEvent(event)

          return
        }
        if (lexical.classification === 'ordinary' &&
          (targetSite === currentSite || (grant && grant.expiresAt >= Date.now()))) {return}
        // Electron cannot safely suspend and replay an arbitrary redirect body.
        // Cancel this hop, collect exact consent, and require a fresh operation.
        denyEvent(event)
        const operationId = `redirect-${crypto.randomBytes(16).toString('base64url')}`
        void this.#authorizeNavigation(
          { ...task, frame: {}, operationId, role: 'automation' },
          task,
          live,
          url
        )
      })
      guest.on('did-start-navigation', (_event, url) => {
        const uploadBinding = this.#bindings.get(key)

        if (uploadBinding?.generation === claim.generation) {
          this.#invalidateAssignedUploads({
            documentGeneration: uploadBinding.documentGeneration,
            guestGeneration: claim.generation,
            profile: claim.profile,
            tabId: claim.tabId
          })
          uploadBinding.uploadGeneration += 1
        }
        this.#invalidatePixelGrants(
          grant => grant.scope.tab_id === claim.tabId && grant.scope.guest_generation === claim.generation
        )
        this.#consents.revoke(
          prompt => prompt.tabId === claim.tabId && prompt.guestGeneration === claim.generation,
          'stale'
        )
        this.#cancelUploadChoosers(
          record =>
            record.binding.hostId === claim.hostId &&
            record.binding.tabId === claim.tabId &&
            record.binding.generation === claim.generation
        )
        void this.#enforceNavigationPostcondition(claim, guest, url)
      })
      guest.on('did-frame-navigate', (_event, url, _httpResponseCode, _httpStatusText, isMainFrame) => {
        if (isMainFrame === true) {
          this.#advanceDocumentGeneration(claim, guest)
        }

        void this.#enforceNavigationPostcondition(claim, guest, url).finally(() => {
          if (isMainFrame === true) {this.#navigationAdmissions.delete(guest.id)}
        })
      })
      guest.on('ipc-message', () => safeDestroy(guest))

      const retireCrashedGuest = (reason: 'crashed' | 'unresponsive') => {
        const current = this.#bindings.get(key)

        if (current?.guest === guest && current.generation === claim.generation) {
          setupRetirementNotified = true
          this.#retireBinding(current, reason)
        } else {
          if (this.#bindingSetups.get(key) === claim.generation) {this.#bindingSetups.delete(key)}
          notifySetupRetired(reason)
          safeDestroy(guest)
        }
      }

      guest.once('render-process-gone', () => retireCrashedGuest('crashed'))
      guest.once('unresponsive', () => retireCrashedGuest('unresponsive'))
      guest.once('destroyed', () => {
        const current = this.#bindings.get(key)

        if (current?.guest === guest && current.generation === claim.generation) {
          setupRetirementNotified = true
          this.#retireBinding(current, 'destroyed', false)
        } else if (this.#bindingSetups.get(key) === claim.generation) {
          notifySetupRetired('destroyed')
        }

        if (this.#bindingSetups.get(key) === claim.generation) {this.#bindingSetups.delete(key)}
      })
      guest.debugger.once('detach', () => {
        const current = this.#bindings.get(key)

        if (current?.guest === guest && current.generation === claim.generation) {
          setupRetirementNotified = true
          this.#retireBinding(current, 'debugger-detached')
        } else {
          safeDestroy(guest)
        }
      })
      guest.debugger.on('message', (_event, method, params, sessionId) => {
        if (typeof method === 'string' && params && typeof params === 'object' && !Array.isArray(params)) {
          if (method === 'Page.fileChooserOpened') {
            void this.#handleFileChooserOpened(claim, guest, params as Record<string, unknown>, sessionId)

            return
          }

          if (method === 'Target.attachedToTarget') {
            const attached = params as { sessionId?: unknown; targetInfo?: { targetId?: unknown; type?: unknown } }

            if (
              attached.targetInfo?.type === 'iframe' &&
              validIdentifier(attached.targetInfo.targetId) &&
              validIdentifier(attached.sessionId)
            ) {
              const frameId = attached.targetInfo.targetId
              const childSessionId = attached.sessionId
              // Reserve routing before asynchronous setup so an impossible root
              // event cannot be mistaken for this known OOPIF during the gap.
              frameSessions.set(frameId, childSessionId)
              void (async () => {
                try {
                  // Flattened auto-attach does not inherit Page-domain state.
                  // Enable and intercept on every OOPIF session, and recurse so
                  // nested process-isolated frames receive the same protection.
                  await guest.debugger.sendCommand('Page.enable', undefined, childSessionId)
                  await guest.debugger.sendCommand('Target.setAutoAttach', {
                    autoAttach: true,
                    flatten: true,
                    waitForDebuggerOnStart: false
                  }, childSessionId)
                  await guest.debugger.sendCommand(
                    'Page.setInterceptFileChooserDialog',
                    { cancel: true, enabled: true },
                    childSessionId
                  )
                  const current = this.#bindings.get(key)
                  const setupLive = this.#bindingSetups.get(key) === claim.generation
                  const bindingLive = current?.guest === guest && current.generation === claim.generation
                  if (detachedSessions.has(childSessionId) || (!setupLive && !bindingLive)) {return}
                  if (bindingLive) {current.uploadGeneration += 1}
                  frameSessions.set(frameId, childSessionId)
                } catch {
                  // A child without interception is never entered into routing,
                  // so any chooser from it remains Chromium-auto-canceled.
                  if (frameSessions.get(frameId) === childSessionId) {frameSessions.delete(frameId)}
                }
              })()
            }

            return
          }

          if (method === 'Target.detachedFromTarget') {
            const detached = (params as { sessionId?: unknown }).sessionId

            if (validIdentifier(detached)) {
              detachedSessions.add(detached)
              const current = this.#bindings.get(key)
              if (current?.guest === guest && current.generation === claim.generation) {
                current.uploadGeneration += 1
              }
              this.#cancelUploadChoosers(record => record.sessionId === detached)
              for (const [frameId, attachedSession] of frameSessions) {
                if (attachedSession === detached) {
                  this.#invalidateAssignedUploads({
                    frameId,
                    guestGeneration: claim.generation,
                    profile: claim.profile,
                    tabId: claim.tabId
                  })
                  frameSessions.delete(frameId)
                }
              }
            }

            return
          }

          if (method === 'Page.frameDetached') {
            const detachedFrame = (params as { frameId?: unknown }).frameId

            if (validIdentifier(detachedFrame)) {
              const current = this.#bindings.get(key)
              if (current?.guest === guest && current.generation === claim.generation) {
                current.uploadGeneration += 1
              }
              this.#cancelUploadChoosers(record => record.chooser.frameId === detachedFrame)
              this.#invalidateAssignedUploads({
                frameId: detachedFrame,
                guestGeneration: claim.generation,
                profile: claim.profile,
                tabId: claim.tabId
              })
            }
          }

          let navigationUrl: unknown

          if (method === 'Page.frameNavigated') {
            navigationUrl = (params as { frame?: { url?: unknown } }).frame?.url
          } else if (method === 'Page.navigatedWithinDocument') {
            navigationUrl = (params as { url?: unknown }).url
          }

          if (navigationUrl !== undefined || method === 'Page.frameNavigated' || method === 'Page.navigatedWithinDocument') {
            const navigatedFrame = method === 'Page.frameNavigated'
              ? (params as { frame?: { id?: unknown } }).frame?.id
              : (params as { frameId?: unknown }).frameId
            if (method === 'Page.navigatedWithinDocument' && validIdentifier(navigatedFrame)) {
              const current = this.#bindings.get(key)
              if (current?.guest === guest && current.generation === claim.generation) {
                current.uploadGeneration += 1
              }
              this.#cancelUploadChoosers(record =>
                record.binding.guest === guest && record.chooser.frameId === navigatedFrame)
            }
            this.#invalidateAssignedUploads({
              ...(validIdentifier(navigatedFrame) ? { frameId: navigatedFrame } : {}),
              guestGeneration: claim.generation,
              profile: claim.profile,
              tabId: claim.tabId
            })
            // Do not publish a committed navigation event to the gateway until
            // its destination passes the same resolved postcondition policy.
            void this.#enforceNavigationPostcondition(
              claim,
              guest,
              typeof navigationUrl === 'string' ? navigationUrl : ''
            ).then(allowed => {
              if (allowed) {this.#forwardDebuggerEvent(claim, method, params as Record<string, unknown>)}
            })

            return
          }

          this.#forwardDebuggerEvent(claim, method, params as Record<string, unknown>)
        }
      })

      if (!guest.debugger.isAttached()) {
        guest.debugger.attach('1.3')
      }

      await guest.debugger.sendCommand('Page.enable')
      await guest.debugger.sendCommand('Target.setAutoAttach', {
        autoAttach: true,
        flatten: true,
        waitForDebuggerOnStart: false
      })
      await guest.debugger.sendCommand('Page.setInterceptFileChooserDialog', { cancel: true, enabled: true })

      if (this.#bindingSetups.get(key) !== claim.generation) {
        safeDestroy(guest)

        return
      }

      this.#bindingSetups.delete(key)
      this.#bindings.set(key, {
        ...claim,
        activationSequence: 0,
        annotationLabels: new Map(),
        documentGeneration: 1,
        frameSessions,
        guest,
        nextAnnotationLabel: 1,
        reportInFlight: 0,
        reportWindowCount: 0,
        reportWindowStartedAt: 0,
        uploadGeneration: 1
      })
    } catch (error) {
      console.error('[browser-guest] setup failed', {
        error,
        generation: claim.generation,
        guestId: guest.id,
        tabId: claim.tabId
      })
      if (this.#bindingSetups.get(key) === claim.generation) {this.#bindingSetups.delete(key)}
      notifySetupRetired('setup-failed')
      safeDestroy(guest)
    }
  }

  #bindingForGuestContents(contents: WebContents): BrowserGuestBinding | null {
    return [...this.#bindings.values()].find(binding => binding.guest === contents) ?? null
  }

  async #handlePermissionRequest(
    contents: WebContents,
    permission: string,
    callback: (granted: boolean) => void,
    requestingUrl?: string
  ) {
    const guest = this.#bindingForGuestContents(contents)
    const task = guest ? this.#taskForGuest(guest) : null

    if (!guest || !validIdentifier(permission, 256)) {
      callback(false)

      return
    }

    const origin = this.#siteForUrl(requestingUrl ?? guest.guest.getURL())
    const durable = guest.partition === BROWSER_PARTITION
      ? this.#deps.durablePermissionDecision?.(guest.profile, origin, permission) ?? null
      : null
    if (durable === 'deny' || (!task && durable !== 'allow')) {callback(false); return}
    if (!task && durable === 'allow') {callback(true); return}

    const operationId = `permission-${crypto.randomBytes(16).toString('base64url')}`
    const approved = await this.#requestConsent(task!, guest, 'permission', operationId, { permission })
    callback(approved)
  }

  async #handleDownload(
    item: DownloadItem,
    contents: WebContents
  ) {
    const guest = this.#bindingForGuestContents(contents)
    const task = guest ? this.#taskForGuest(guest) : null

    if (!guest || !task || !this.#deps.chooseDownloadDestination) {
      item.cancel()

      return
    }

    const filename = item.getFilename().slice(0, 512)
    const operationId = `download-${crypto.randomBytes(16).toString('base64url')}`

    const outcome = await this.#consents.request(task.hostId, {
      category: 'download',
      filename,
      guestGeneration: task.guestGeneration,
      operationId,
      profile: task.profile,
      site: this.#siteForUrl(guest.guest.getURL()),
      tabId: task.tabId,
      taskGeneration: task.taskGeneration,
      taskId: task.taskId
    })

    if (
      outcome.reason !== 'allow' ||
      this.#taskBindings.get(this.#taskBindingKey(task.taskId, 'automation')) !== task ||
      this.#bindings.get(this.#bindingKey(guest.hostId, guest.tabId)) !== guest
    ) {
      item.cancel()

      return
    }

    const prompt: BrowserConsentPrompt = {
      category: 'download',
      consentId: outcome.consentId,
      expiresAt: Date.now(),
      filename,
      guestGeneration: task.guestGeneration,
      operationId,
      profile: task.profile,
      site: this.#siteForUrl(guest.guest.getURL()),
      tabId: task.tabId,
      taskGeneration: task.taskGeneration,
      taskId: task.taskId
    }

    const destination = await this.#deps.chooseDownloadDestination(task.hostId, prompt)

    if (!destination || this.#taskBindings.get(this.#taskBindingKey(task.taskId, 'automation')) !== task) {
      item.cancel()

      return
    }

    item.setSavePath(destination)
    const downloadOrigin = this.#siteForUrl(guest.guest.getURL())
    if (
      guest.partition === BROWSER_PARTITION &&
      typeof item.once === 'function' &&
      typeof item.getTotalBytes === 'function'
    ) {
      item.once('done', (_event, state) => this.#deps.recordTransfer?.(guest.profile, {
        actor: 'human',
        byteSize: item.getTotalBytes() >= 0 ? item.getTotalBytes() : null,
        direction: 'download',
        origin: downloadOrigin,
        outcome: state === 'completed' ? 'completed' : state === 'cancelled' ? 'canceled' : 'failed',
        redactedName: filename,
        tabIncarnationId: guest.tabId
      }))
    }
    item.resume()
  }

  #installPartitionGuards(partition: string) {
    if (this.#installedPartitions.has(partition)) {
      return
    }

    this.#installedPartitions.add(partition)
    const browserSession = this.#deps.sessionFromPartition(partition)
    this.#deps.installResourceSession?.(browserSession, partition)
    browserSession.setPermissionRequestHandler((contents, permission, callback, details) => {
      void this.#handlePermissionRequest(contents, permission, callback, details?.requestingUrl).catch(() => callback(false))
    })
    browserSession.setPermissionCheckHandler((contents, permission, requestingOrigin) => {
      const guest = contents ? this.#bindingForGuestContents(contents) : null
      if (!guest || guest.partition !== BROWSER_PARTITION || !validIdentifier(permission, 256)) {return false}
      const task = this.#taskForGuest(guest)
      // Agent-bound permissions always retain exact one-shot consent. Durable
      // policy can pre-deny them, but never turns a stored grant into task authority.
      if (task) {return false}
      return this.#deps.durablePermissionDecision?.(guest.profile, this.#siteForUrl(requestingOrigin), permission) === 'allow'
    })
    browserSession.setDevicePermissionHandler?.(() => false)
    browserSession.on('will-download', (event, item, contents) => {
      if (!item || typeof item.pause !== 'function' || !contents) {
        denyEvent(event)

        return
      }

      item.pause()
      void this.#handleDownload(item, contents).catch(() => item.cancel())
    })
    browserSession.webRequest.onBeforeRequest((details, callback) => {
      this.#observeAssignedUploadRequest(partition, browserSession, details)
      void this.#isResolvedDestinationAllowed(
        partition,
        details.url,
        'network-request',
        details.webContentsId,
        details.resourceType === 'mainFrame'
      ).then(
        allowed => callback({ cancel: !allowed }),
        () => callback({ cancel: true })
      )
    })
    browserSession.webRequest.onCompleted?.(details =>
      this.#settleAssignedUploadRequest(partition, browserSession, details, 'completed'))
    browserSession.webRequest.onErrorOccurred?.(details =>
      this.#settleAssignedUploadRequest(partition, browserSession, details, 'failed'))
  }

  async #isResolvedDestinationAllowed(
    partition: string,
    rawUrl: string,
    source: BrowserNavigationSource,
    webContentsId?: number,
    topLevel = true
  ): Promise<boolean> {
    if (this.#deps.authorizeResourceRequest?.(partition, webContentsId, rawUrl, source === 'network-request')) {
      return true
    }
    if (!isAllowedBrowserRequest(rawUrl)) {return false}
    const parsed = new URL(rawUrl)

    if (parsed.protocol === 'ws:' || parsed.protocol === 'wss:' || !topLevel) {
      const addresses = await this.#resolveHostAddresses(partition, parsed.hostname)
      return addresses.length > 0 && addresses.every(address => !isPrivateBrowserAddress(address))
    }

    const guest = webContentsId === undefined ? null : [...this.#bindings.values()].find(
      candidate => candidate.guest.id === webContentsId && candidate.partition === partition
    ) ?? null
    const task = guest ? this.#taskForGuest(guest) : null
    const scope = guest && task ? this.#snpScope(task, guest) : undefined
    const classification = browserNavigationPolicy.lexicalDecision(rawUrl, source, scope)
    const admission = webContentsId === undefined ? undefined : this.#navigationAdmissions.get(webContentsId)

    if (guest && this.#admissionCovers(admission, guest, rawUrl, classification)) {
      const addresses = await this.#resolveHostAddresses(partition, parsed.hostname)
      return addresses.length > 0 && addresses.every(address => !isPrivateBrowserAddress(address))
    }

    const result = await browserNavigationPolicy.evaluate(rawUrl, {
      resolveHost: hostname => this.#resolveHostAddresses(partition, hostname),
      scope,
      source
    })

    // A renderer activation is itself an exact, foreground human navigation
    // decision. Keep a short one-chain admission so Chromium's following
    // onBeforeRequest sees the same authority without creating a site allowlist.
    if (source === 'trusted-activation' && guest && result.disposition === 'require-grant' && result.siteKey) {
      this.#navigationAdmissions.set(guest.guest.id, {
        classification: result.classification,
        destinationSite: result.siteKey,
        expiresAt: Date.now() + 10_000,
        guestGeneration: guest.generation,
        operationId: 'trusted-activation',
        profile: guest.profile,
        reasonCodes: result.reasonCodes,
        sourceSite: this.#siteForUrl(guest.guest.getURL()),
        tabId: guest.tabId,
        url: parsed.href
      })
      return true
    }

    return result.disposition === 'allow'
  }

  #resolveHostAddresses(partition: string, rawHostname: string): Promise<readonly string[]> {
    const hostname = rawHostname.toLowerCase().replace(/^\[|\]$/g, '')

    if (isPrivateBrowserAddress(hostname)) {return Promise.resolve([])}

    if (/^[0-9a-f:.]+$/i.test(hostname)) {return Promise.resolve([hostname])}

    const key = `${partition}:${hostname}`
    const existing = this.#resolutionLocks.get(key)

    if (existing) {
      return existing
    }

    const browserSession = this.#deps.sessionFromPartition(partition)

    const resolution = Promise.allSettled(
      (['A', 'AAAA'] as const).map(queryType =>
        browserSession.resolveHost(hostname, {
          cacheUsage: 'disallowed',
          queryType,
          source: 'any'
        })
      )
    )
      .then(results => {
        const addresses = results.flatMap(result =>
          result.status === 'fulfilled' ? result.value.endpoints.map(endpoint => endpoint.address) : []
        )

        return addresses
      })
      .catch(() => [] as string[])
      .finally(() => this.#resolutionLocks.delete(key))

    this.#resolutionLocks.set(key, resolution)

    return resolution
  }

  #advanceDocumentGeneration(identity: BrowserGuestIdentity, guest: WebContents) {
    const binding = this.#bindings.get(this.#bindingKey(identity.hostId, identity.tabId))

    if (binding?.generation === identity.generation && binding.guest === guest) {
      binding.documentGeneration += 1
    }
  }

  async #enforceNavigationPostcondition(
    identity: BrowserGuestIdentity,
    guest: WebContents,
    rawUrl: string
  ): Promise<boolean> {
    const binding = this.#bindings.get(this.#bindingKey(identity.hostId, identity.tabId))

    if (!binding || binding.generation !== identity.generation || binding.guest !== guest) {return false}

    const allowed = await this.#isResolvedDestinationAllowed(binding.partition, rawUrl, 'postcondition', guest.id)

    if (!allowed) {
      this.#retireBinding(binding, 'policy-denied')

      return false
    }

    return this.#bindings.get(this.#bindingKey(identity.hostId, identity.tabId)) === binding
  }

  async resourceBinding(event: IpcMainInvokeEvent, request: BrowserGuestRelease): Promise<BrowserResourceBinding | null> {
    const binding = await this.#waitForBinding(event, request)

    return binding
      ? {
          generation: binding.generation,
          guestId: binding.guest.id,
          hostId: binding.hostId,
          partition: binding.partition,
          profile: binding.profile,
          tabId: binding.tabId,
          workspaceId: binding.workspaceId
        }
      : null
  }

  async #waitForBinding(
    event: IpcMainInvokeEvent,
    request: BrowserGuestRelease
  ): Promise<BrowserGuestBinding | null> {
    for (let attempt = 0; attempt < 80; attempt += 1) {
      const binding = this.#bindingForSender(event, request)

      if (binding) {
        return binding
      }

      if (!this.#hostIds.has(event.sender.id)) {
        return null
      }

      await new Promise(resolve => setTimeout(resolve, 25))
    }

    return null
  }

  #bindingForSender(event: IpcMainInvokeEvent, request: BrowserGuestRelease): BrowserGuestBinding | null {
    if (!this.#hostIds.has(event.sender.id) || !validIdentifier(request?.tabId) || !validIdentifier(request?.generation)) {
      return null
    }

    const binding = this.#bindings.get(this.#bindingKey(event.sender.id, request.tabId))

    return binding?.generation === request.generation ? binding : null
  }

  #bindingKey(hostId: number, tabId: string): string {
    return `${hostId}:${tabId}`
  }

  #forwardDebuggerEvent(binding: BrowserGuestIdentity, method: string, params: Record<string, unknown>) {
    const task = [...this.#taskBindings.values()].find(
      candidate =>
        candidate.hostId === binding.hostId &&
        candidate.tabId === binding.tabId &&
        candidate.guestGeneration === binding.generation
    )

    if (task) {
      this.#frameSink?.({
        guestGeneration: task.guestGeneration,
        tabId: task.tabId,
        taskGeneration: task.taskGeneration,
        taskId: task.taskId,
        role: task.role,
        frame: { method, params }
      })
    }
  }

  #notifyRetired(identity: BrowserGuestIdentity, reason: BrowserGuestRetiredEvent['reason']) {
    this.#deps.notifyRetired?.({
      guestGeneration: identity.generation,
      hostId: identity.hostId,
      reason,
      tabId: identity.tabId
    })
  }

  #retireBinding(
    binding: BrowserGuestBinding,
    reason?: BrowserGuestRetiredEvent['reason'],
    destroy = true
  ) {
    this.#invalidatePixelGrants(
      grant => grant.scope.tab_id === binding.tabId && grant.scope.guest_generation === binding.generation
    )
    this.#consents.revoke(
      prompt => prompt.tabId === binding.tabId && prompt.guestGeneration === binding.generation,
      'stale'
    )
    this.#cancelUploadChoosers(
      record =>
        record.binding.hostId === binding.hostId &&
        record.binding.tabId === binding.tabId &&
        record.binding.generation === binding.generation
    )
    this.#invalidateAssignedUploads({
      guestGeneration: binding.generation,
      profile: binding.profile,
      tabId: binding.tabId
    })
    this.#navigationAdmissions.delete(binding.guest.id)

    for (const [grantKey, grant] of this.#siteGrants) {
      if (grant.tabId === binding.tabId && grant.guestGeneration === binding.generation) {
        this.#siteGrants.delete(grantKey)
      }
    }

    this.#bindings.delete(this.#bindingKey(binding.hostId, binding.tabId))
    this.#retireTaskBindings(binding.hostId, binding.tabId)

    if (reason) {this.#notifyRetired(binding, reason)}

    if (destroy) {safeDestroy(binding.guest)}
  }

  #retireTaskBindings(hostId: number, tabId: string) {
    for (const [key, task] of this.#taskBindings) {
      if (task.hostId === hostId && task.tabId === tabId) {
        this.#taskBindings.delete(key)

        if (task.role === 'automation') {
          this.#taskLifecycleSink?.({
            guestGeneration: task.guestGeneration,
            profile: task.profile,
            tabId: task.tabId,
            taskGeneration: task.taskGeneration,
            taskId: task.taskId,
            type: 'unbind'
          })
        }
      }
    }
  }

  #retireHost(hostId: number) {
    this.#consents.revoke((_prompt, pendingHostId) => pendingHostId === hostId)
    this.#cancelUploadChoosers(record => record.binding.hostId === hostId)

    for (const [token, claim] of this.#claims) {
      if (claim.hostId === hostId) {
        this.#claims.delete(token)
      }
    }

    this.#acceptedClaimsByHost.delete(hostId)

    for (const binding of this.#bindings.values()) {
      if (binding.hostId === hostId) {
        this.#retireBinding(binding)
      }
    }
  }

  async #resolveUploadInputDescriptor(
    record: BrowserPendingUploadChooserRecord
  ): Promise<BrowserUploadInputDescriptor | null> {
    const { binding, chooser, sessionId, task } = record
    if (record.abortController.signal.aborted ||
      this.#pendingUploadChoosers.get(chooser.chooserId) !== record ||
      this.#bindings.get(this.#bindingKey(binding.hostId, binding.tabId)) !== binding ||
      this.#taskBindings.get(this.#taskBindingKey(task.taskId, 'automation')) !== task ||
      binding.documentGeneration !== chooser.documentGeneration ||
      binding.frameSessions.get(chooser.frameId) !== sessionId) {return null}
    let inputObjectId: string | null = null
    let formObjectId: string | null = null
    try {
      const isolatedWorld = await binding.guest.debugger.sendCommand(
        'Page.createIsolatedWorld',
        { frameId: chooser.frameId, grantUniveralAccess: false, worldName: 'hermes-upload-descriptor-v1' },
        sessionId
      )
      const executionContextId = isolatedWorld?.executionContextId
      if (!Number.isSafeInteger(executionContextId) || executionContextId <= 0) {return null}
      const resolved = await binding.guest.debugger.sendCommand(
        'DOM.resolveNode',
        { backendNodeId: chooser.backendNodeId, executionContextId },
        sessionId
      )
      inputObjectId = isRecord(resolved?.object) && validIdentifier(resolved.object.objectId, 512)
        ? resolved.object.objectId : null
      if (!inputObjectId) {return null}
      const described = await binding.guest.debugger.sendCommand(
        'Runtime.callFunctionOn',
        { awaitPromise: false, functionDeclaration: UPLOAD_INPUT_DESCRIPTOR_FUNCTION,
          objectId: inputObjectId, returnByValue: true, silent: true },
        sessionId
      )
      const formResult = await binding.guest.debugger.sendCommand(
        'Runtime.callFunctionOn',
        { awaitPromise: false, functionDeclaration: 'function () { return this.form }',
          objectId: inputObjectId, returnByValue: false, silent: true },
        sessionId
      )
      formObjectId = isRecord(formResult?.result) && validIdentifier(formResult.result.objectId, 512)
        ? formResult.result.objectId : null
      let formBackendNodeId = 0
      if (formObjectId) {
        const requested = await binding.guest.debugger.sendCommand('DOM.requestNode', { objectId: formObjectId }, sessionId)
        const formNode = await binding.guest.debugger.sendCommand('DOM.describeNode', { nodeId: requested?.nodeId }, sessionId)
        formBackendNodeId = Number.isSafeInteger(formNode?.node?.backendNodeId) && formNode.node.backendNodeId > 0
          ? formNode.node.backendNodeId : -1
      }
      const descriptor = browserUploadInputDescriptor(described?.result?.value, formBackendNodeId, {
        backendNodeId: chooser.backendNodeId,
        chooserMode: chooser.mode,
        documentGeneration: chooser.documentGeneration,
        frameId: chooser.frameId,
        origin: chooser.origin
      })
      return this.#pendingUploadChoosers.get(chooser.chooserId) === record &&
        this.#bindings.get(this.#bindingKey(binding.hostId, binding.tabId)) === binding &&
        this.#taskBindings.get(this.#taskBindingKey(task.taskId, 'automation')) === task &&
        binding.documentGeneration === chooser.documentGeneration &&
        binding.frameSessions.get(chooser.frameId) === sessionId ? descriptor : null
    } catch {
      return null
    } finally {
      for (const objectId of [formObjectId, inputObjectId]) {
        if (objectId) {
          void binding.guest.debugger.sendCommand('Runtime.releaseObject', { objectId }, sessionId).catch(() => undefined)
        }
      }
    }
  }

  async #handleFileChooserOpened(
    identity: BrowserGuestIdentity,
    guest: WebContents,
    params: Record<string, unknown>,
    sessionId: unknown
  ) {
    const binding = this.#bindings.get(this.#bindingKey(identity.hostId, identity.tabId))
    const task = binding ? this.#taskForGuest(binding) : null
    const frameId = params.frameId
    const mode = params.mode
    const backendNodeId = params.backendNodeId
    const eventSessionId = validIdentifier(sessionId) ? sessionId : undefined

    const clear = async (commandSessionId?: string) => {
      if (!Number.isSafeInteger(backendNodeId) || (backendNodeId as number) <= 0) {return}

      try {
        await guest.debugger.sendCommand(
          'DOM.setFileInputFiles',
          { backendNodeId, files: [] },
          commandSessionId
        )
      } catch {
        // A detached or retired debugger already made the intercepted chooser unusable.
      }
    }

    if (
      !binding || binding.guest !== guest || binding.generation !== identity.generation ||
      !task || !validIdentifier(frameId) ||
      (mode !== 'selectSingle' && mode !== 'selectMultiple') ||
      !Number.isSafeInteger(backendNodeId) || (backendNodeId as number) <= 0
    ) {
      return
    }

    // Abort the old pipeline before any asynchronous discovery for its
    // successor. Auto-cancel interception has already dismissed both native
    // dialogs; this cancellation stops staged downstream work immediately.
    this.#cancelUploadChoosers(
      record =>
        record.binding.hostId === binding.hostId &&
        record.binding.tabId === binding.tabId &&
        record.binding.generation === binding.generation
    )

    const frameSessionId = binding.frameSessions.get(frameId)
    if (eventSessionId !== frameSessionId) {return}
    const uploadGeneration = ++binding.uploadGeneration
    let frameTree: unknown

    try {
      frameTree = await guest.debugger.sendCommand('Page.getFrameTree', undefined, frameSessionId)
    } catch {
      return
    }

    const securityOrigin = frameSecurityOriginFromTree(
      (frameTree as { frameTree?: unknown })?.frameTree,
      frameId
    )
    let origin: string | null = null

    try {
      const parsed = new URL(securityOrigin ?? '')
      if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {origin = parsed.origin}
    } catch { /* reject malformed or opaque frame URLs below */ }

    if (
      !origin || binding.uploadGeneration !== uploadGeneration ||
      binding.frameSessions.get(frameId) !== frameSessionId ||
      this.#bindings.get(this.#bindingKey(binding.hostId, binding.tabId)) !== binding ||
      this.#taskBindings.get(this.#taskBindingKey(task.taskId, 'automation')) !== task
    ) {
      return
    }
    let inputDescriptor: BrowserUploadInputDescriptor | null = null
    let inputObjectId: string | null = null
    let formObjectId: string | null = null

    try {
      const isolatedWorld = await guest.debugger.sendCommand(
        'Page.createIsolatedWorld',
        { frameId, grantUniveralAccess: false, worldName: 'hermes-upload-descriptor-v1' },
        frameSessionId
      )
      const executionContextId = isolatedWorld?.executionContextId
      if (!Number.isSafeInteger(executionContextId) || executionContextId <= 0) {return}
      const resolved = await guest.debugger.sendCommand(
        'DOM.resolveNode',
        { backendNodeId, executionContextId },
        frameSessionId
      )
      inputObjectId = isRecord(resolved?.object) && validIdentifier(resolved.object.objectId, 512)
        ? resolved.object.objectId
        : null
      if (!inputObjectId) {return}
      const described = await guest.debugger.sendCommand(
        'Runtime.callFunctionOn',
        {
          awaitPromise: false,
          functionDeclaration: UPLOAD_INPUT_DESCRIPTOR_FUNCTION,
          objectId: inputObjectId,
          returnByValue: true,
          silent: true
        },
        frameSessionId
      )
      const formResult = await guest.debugger.sendCommand(
        'Runtime.callFunctionOn',
        {
          awaitPromise: false,
          functionDeclaration: 'function () { return this.form }',
          objectId: inputObjectId,
          returnByValue: false,
          silent: true
        },
        frameSessionId
      )
      formObjectId = isRecord(formResult?.result) && validIdentifier(formResult.result.objectId, 512)
        ? formResult.result.objectId
        : null
      let formBackendNodeId = 0

      if (formObjectId) {
        const requested = await guest.debugger.sendCommand('DOM.requestNode', { objectId: formObjectId }, frameSessionId)
        const formNode = await guest.debugger.sendCommand('DOM.describeNode', { nodeId: requested?.nodeId }, frameSessionId)
        formBackendNodeId = Number.isSafeInteger(formNode?.node?.backendNodeId) && formNode.node.backendNodeId > 0
          ? formNode.node.backendNodeId
          : -1
      }
      inputDescriptor = browserUploadInputDescriptor(
        described?.result?.value,
        formBackendNodeId,
        {
          backendNodeId: backendNodeId as number,
          chooserMode: mode,
          documentGeneration: binding.documentGeneration,
          frameId,
          origin
        }
      )
    } catch { /* a detached or unreadable input is not assignable */ }
    finally {
      for (const objectId of [formObjectId, inputObjectId]) {
        if (objectId) {
          void guest.debugger.sendCommand('Runtime.releaseObject', { objectId }, frameSessionId).catch(() => undefined)
        }
      }
    }

    if (
      !inputDescriptor || binding.uploadGeneration !== uploadGeneration ||
      binding.frameSessions.get(frameId) !== frameSessionId ||
      this.#bindings.get(this.#bindingKey(binding.hostId, binding.tabId)) !== binding ||
      this.#taskBindings.get(this.#taskBindingKey(task.taskId, 'automation')) !== task
    ) {
      return
    }
    const chooserId = crypto.randomBytes(32).toString('base64url')
    const abortController = new AbortController()
    const chooser = Object.freeze({
      accept: inputDescriptor.accept,
      backendNodeId: backendNodeId as number,
      chooserId,
      directory: inputDescriptor.directory,
      documentGeneration: binding.documentGeneration,
      formActionOrigin: inputDescriptor.formActionOrigin,
      formActionUrl: inputDescriptor.formActionUrl,
      formFingerprint: inputDescriptor.formFingerprint,
      formLabel: inputDescriptor.formLabel,
      formMethod: inputDescriptor.formMethod,
      frameId,
      guestGeneration: binding.generation,
      hostId: binding.hostId,
      inputLabel: inputDescriptor.inputLabel,
      inputName: inputDescriptor.inputName,
      mode,
      origin,
      profile: binding.profile,
      signal: abortController.signal,
      tabId: binding.tabId,
      taskGeneration: task.taskGeneration,
      taskId: task.taskId
    })
    const record = { abortController, binding, chooser, sessionId: frameSessionId, task }

    this.#pendingUploadChoosers.set(chooserId, record)

    if (!this.#deps.handleUploadChooser) {
      this.#pendingUploadChoosers.delete(chooserId)
      abortController.abort()
      await clear(frameSessionId)

      return
    }

    try {
      await this.#deps.handleUploadChooser(chooser)
    } catch {
      if (this.#pendingUploadChoosers.get(chooserId) === record) {
        this.#pendingUploadChoosers.delete(chooserId)
        abortController.abort()
        await clear(frameSessionId)
      }
    }
  }

  #cancelUploadChoosers(predicate: (record: BrowserPendingUploadChooserRecord) => boolean): number {
    let count = 0

    for (const [chooserId, record] of this.#pendingUploadChoosers) {
      if (!predicate(record)) {continue}
      this.#pendingUploadChoosers.delete(chooserId)
      record.abortController.abort()
      count += 1
      void record.binding.guest.debugger
        .sendCommand(
          'DOM.setFileInputFiles',
          { backendNodeId: record.chooser.backendNodeId, files: [] },
          record.sessionId
        )
        .catch(() => undefined)
    }

    return count
  }
}

export function createBrowserGuestSecurityController(deps: BrowserGuestControllerDeps) {
  return new BrowserGuestSecurityController(deps)
}

export { ATTACH_PREFIX, INTERNAL_DEBUGGER_METHODS, isAllowedBrowserNavigation, REPORTER_WORLD_ID }
