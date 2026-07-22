import { atom } from 'nanostores'

export const $browserConsentPrompts = atom<readonly BrowserConsentPrompt[]>([])

const CONSENT_CATEGORIES = new Set<BrowserConsentPrompt['category']>([
  'destructive-action',
  'download',
  'external-handler',
  'navigation',
  'outbound-pixels',
  'permission',
  'upload-assignment',
  'website-submission'
])
const PROMPT_KEYS = new Set([
  'captureScope', 'category', 'consentId', 'detail', 'documentGeneration', 'expiresAt', 'filename',
  'guestGeneration', 'maxBytes', 'navigationPolicy', 'operationId', 'permission', 'profile', 'purpose', 'recipient',
  'retention', 'scheme', 'site', 'tabId', 'tabTitle', 'taskGeneration', 'taskId', 'upload'
])
const UPLOAD_KEYS = new Set([
  'accept', 'aggregateSize', 'destinationOrigin', 'files', 'formActionOrigin', 'formLabel',
  'formMethod', 'immediateSubmissionPossible', 'inputLabel', 'mode', 'source'
])
const UPLOAD_FILE_KEYS = new Set(['displayName', 'mimeType', 'originalDisplayName', 'size'])
const NAVIGATION_POLICY_KEYS = new Set([
  'categoryCodes', 'classification', 'exceptionEligible', 'policyVersion', 'provenance',
  'pslVersion', 'reasonCodes', 'urlParserVersion'
])
const PROVENANCE_KEYS = new Set([
  'exceptionId', 'expiresAt', 'policyVersion', 'ruleId', 'scope', 'severity', 'signal', 'source'
])

function boundedString(value: unknown, max: number, allowEmpty = false): value is string {
  return typeof value === 'string' && value.length <= max && (allowEmpty || value.length > 0)
}

function onlyKeys(value: object, allowed: ReadonlySet<string>) {
  return Object.keys(value).every(key => allowed.has(key))
}

function isDenseExactArray(value: unknown, maxLength: number): value is readonly unknown[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > maxLength) {return false}
  const expected = new Set(['length', ...Array.from({ length: value.length }, (_, index) => String(index))])
  const ownNames = Object.getOwnPropertyNames(value)

  return ownNames.length === expected.size && ownNames.every(name => expected.has(name))
}

function isNavigationPolicy(value: unknown): value is NonNullable<BrowserConsentPrompt['navigationPolicy']> {
  const policy = value as NonNullable<BrowserConsentPrompt['navigationPolicy']> | null
  if (!policy || typeof policy !== 'object' || !onlyKeys(policy, NAVIGATION_POLICY_KEYS) ||
    !['ordinary', 'sensitive', 'unknown'].includes(policy.classification) ||
    typeof policy.exceptionEligible !== 'boolean' || !boundedString(policy.policyVersion, 128) ||
    !boundedString(policy.pslVersion, 128) || !boundedString(policy.urlParserVersion, 128) ||
    !Array.isArray(policy.categoryCodes) || policy.categoryCodes.length > 32 ||
    !isDenseExactArray(policy.reasonCodes, 32) || !isDenseExactArray(policy.provenance, 32)) {return false}
  const stableCode = (candidate: unknown): candidate is string =>
    boundedString(candidate, 128) && /^[a-z0-9._-]+$/u.test(candidate)

  if (!policy.categoryCodes.every(stableCode) || !policy.reasonCodes.every(stableCode)) {return false}
  return policy.provenance.every(value => {
    if (!value || typeof value !== 'object' || Array.isArray(value) || !onlyKeys(value, PROVENANCE_KEYS)) {return false}
    return Object.values(value).every(candidate =>
      typeof candidate === 'number' ? Number.isSafeInteger(candidate) :
      typeof candidate === 'boolean' || stableCode(candidate)
    )
  })
}

function exactWebOrigin(value: unknown): value is string {
  if (!boundedString(value, 2048)) {return false}
  try {
    const parsed = new URL(value)
    return (parsed.protocol === 'https:' || parsed.protocol === 'http:') && parsed.origin === value
  } catch {
    return false
  }
}

function isUploadDetail(value: unknown): value is BrowserUploadConsentDetail {
  const upload = value as Partial<BrowserUploadConsentDetail> | null
  if (!upload || typeof upload !== 'object' || !onlyKeys(upload, UPLOAD_KEYS) ||
    !boundedString(upload.accept, 2048, true) || !Number.isSafeInteger(upload.aggregateSize) || upload.aggregateSize! < 0 ||
    !exactWebOrigin(upload.destinationOrigin) || !exactWebOrigin(upload.formActionOrigin) ||
    !boundedString(upload.formLabel, 512, true) || !['dialog', 'get', 'post'].includes(upload.formMethod ?? '') ||
    upload.immediateSubmissionPossible !== true || !boundedString(upload.inputLabel, 512, true) ||
    !['selectMultiple', 'selectSingle'].includes(upload.mode ?? '') || upload.source !== 'studio-session-artifact' ||
    !isDenseExactArray(upload.files, 20)) {return false}

  let total = 0
  for (const value of upload.files) {
    const file = value as Partial<BrowserUploadConsentFile> | null
    if (!file || typeof file !== 'object' || !onlyKeys(file, UPLOAD_FILE_KEYS) ||
      !boundedString(file.displayName, 1024) || !boundedString(file.mimeType, 256) ||
      (file.originalDisplayName !== undefined && !boundedString(file.originalDisplayName, 1024)) ||
      !Number.isSafeInteger(file.size) || file.size! < 0) {return false}
    total += file.size!
    if (!Number.isSafeInteger(total)) {return false}
  }
  return total === upload.aggregateSize
}

function isPrompt(value: unknown): value is BrowserConsentPrompt {
  const prompt = value as Partial<BrowserConsentPrompt> | null
  if (!prompt || typeof prompt !== 'object' || !onlyKeys(prompt, PROMPT_KEYS) ||
    !CONSENT_CATEGORIES.has(prompt.category as BrowserConsentPrompt['category']) ||
    !boundedString(prompt.consentId, 43) || !/^[A-Za-z0-9_-]{43}$/.test(prompt.consentId) ||
    !Number.isFinite(prompt.expiresAt) || !boundedString(prompt.guestGeneration, 512) ||
    !boundedString(prompt.operationId, 512) || !boundedString(prompt.profile, 512) ||
    !boundedString(prompt.site, 2048) || !boundedString(prompt.tabId, 512) ||
    !Number.isSafeInteger(prompt.taskGeneration) || prompt.taskGeneration! < 0 || !boundedString(prompt.taskId, 512)) {
    return false
  }

  const optionalStrings: readonly [unknown, number][] = [
    [prompt.detail, 4096], [prompt.filename, 1024], [prompt.permission, 256], [prompt.purpose, 1024],
    [prompt.recipient, 512], [prompt.scheme, 64], [prompt.tabTitle, 1024]
  ]
  if (optionalStrings.some(([candidate, max]) => candidate !== undefined && !boundedString(candidate, max, true)) ||
    (prompt.filename !== undefined && prompt.category !== 'download') ||
    (prompt.documentGeneration !== undefined && (!Number.isSafeInteger(prompt.documentGeneration) || prompt.documentGeneration < 0)) ||
    (prompt.maxBytes !== undefined && (!Number.isSafeInteger(prompt.maxBytes) || prompt.maxBytes < 0)) ||
    (prompt.captureScope !== undefined && prompt.captureScope !== 'viewport') ||
    (prompt.retention !== undefined && prompt.retention !== 'memory-only-transient')) {return false}

  if (prompt.navigationPolicy !== undefined &&
    (prompt.category !== 'navigation' || !isNavigationPolicy(prompt.navigationPolicy))) {return false}
  if (prompt.category === 'navigation' && !isNavigationPolicy(prompt.navigationPolicy)) {return false}

  return prompt.category === 'upload-assignment'
    ? isUploadDetail(prompt.upload) && prompt.detail === undefined && prompt.site === prompt.upload.destinationOrigin
    : prompt.upload === undefined
}

export function enqueueBrowserConsent(value: unknown) {
  if (!isPrompt(value) || value.expiresAt <= Date.now()) {return false}
  const current = $browserConsentPrompts.get()

  if (current.some(prompt => prompt.consentId === value.consentId)) {return false}
  $browserConsentPrompts.set([...current, value])

  return true
}

export function removeBrowserConsent(consentId: string) {
  $browserConsentPrompts.set(
    $browserConsentPrompts.get().filter(prompt => prompt.consentId !== consentId)
  )
}

export async function resolveBrowserConsent(
  consentId: string,
  decision: 'allow' | 'deny' | 'ordinary-for-task'
) {
  const prompt = $browserConsentPrompts.get().find(candidate => candidate.consentId === consentId)

  if (!prompt) {return false}
  const result = await window.hermesDesktop.browserGuest.resolveConsent({ consentId, decision })
  removeBrowserConsent(consentId)

  return result.ok
}

let listening = false

export function startBrowserConsentListener() {
  if (
    listening ||
    !window.hermesDesktop?.browserGuest?.onConsentRequested ||
    !window.hermesDesktop?.browserGuest?.onConsentResolved
  ) {return () => undefined}

  listening = true
  const unsubscribeRequested = window.hermesDesktop.browserGuest.onConsentRequested(enqueueBrowserConsent)

  const unsubscribeResolved = window.hermesDesktop.browserGuest.onConsentResolved(outcome => {
    if (typeof outcome?.consentId === 'string') {removeBrowserConsent(outcome.consentId)}
  })

  return () => {
    listening = false
    unsubscribeRequested()
    unsubscribeResolved()
  }
}

export function resetBrowserConsentForTests() {
  $browserConsentPrompts.set([])
  listening = false
}
