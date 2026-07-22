import crypto from 'node:crypto'

export const BROWSER_CONSENT_TTL_MS = 60_000

export type BrowserConsentCategory =
  | 'destructive-action'
  | 'download'
  | 'external-handler'
  | 'navigation'
  | 'outbound-pixels'
  | 'permission'
  | 'upload-assignment'
  | 'website-submission'

export type BrowserConsentDecision = 'allow' | 'deny' | 'ordinary-for-task'
export type BrowserConsentTerminalReason = BrowserConsentDecision | 'expired' | 'revoked' | 'stale'

export interface BrowserConsentScope {
  guestGeneration: string
  operationId: string
  profile: string
  site: string
  tabId: string
  taskGeneration: number
  taskId: string
}

export interface BrowserUploadConsentFile {
  displayName: string
  mimeType: string
  originalDisplayName?: string
  size: number
}

export interface BrowserUploadConsentDetail {
  accept: string
  aggregateSize: number
  destinationOrigin: string
  files: readonly BrowserUploadConsentFile[]
  formActionOrigin: string
  formLabel: string
  formMethod: 'dialog' | 'get' | 'post'
  immediateSubmissionPossible: true
  inputLabel: string
  mode: 'selectMultiple' | 'selectSingle'
  source: 'studio-session-artifact'
}

export interface BrowserConsentRequest extends BrowserConsentScope {
  captureScope?: 'viewport'
  category: BrowserConsentCategory
  detail?: string
  documentGeneration?: number
  filename?: string
  maxBytes?: number
  navigationPolicy?: {
    categoryCodes: readonly string[]
    classification: 'ordinary' | 'sensitive' | 'unknown'
    exceptionEligible: boolean
    policyVersion: string
    provenance: readonly object[]
    pslVersion: string
    reasonCodes: readonly string[]
    urlParserVersion: string
  }
  permission?: string
  purpose?: string
  recipient?: string
  retention?: 'memory-only-transient'
  scheme?: string
  tabTitle?: string
  upload?: BrowserUploadConsentDetail
}

export interface BrowserConsentPrompt extends BrowserConsentRequest {
  consentId: string
  expiresAt: number
}

export interface BrowserConsentOutcome {
  consentId: string
  reason: BrowserConsentTerminalReason
}

interface PendingConsent {
  hostId: number
  prompt: Readonly<BrowserConsentPrompt>
  resolve: (outcome: BrowserConsentOutcome) => void
  timer: ReturnType<typeof setTimeout>
}

interface BrowserConsentAuthorityDeps {
  now?: () => number
  present: (hostId: number, prompt: Readonly<BrowserConsentPrompt>) => void
  randomBytes?: (size: number) => { toString(encoding: 'base64url'): string }
  settled?: (hostId: number, outcome: Readonly<BrowserConsentOutcome>) => void
  setTimer?: typeof setTimeout
  clearTimer?: typeof clearTimeout
}

function validConsentId(value: unknown): value is string {
  return typeof value === 'string' && /^[A-Za-z0-9_-]{43}$/.test(value)
}

/**
 * Main-process authorization ledger for browser consent. Presentation is delegated
 * to trusted renderer chrome, but only this ledger can correlate and consume a
 * decision. Entries are cryptographic, exact-host scoped, expiring, and one-shot.
 */
export class BrowserConsentAuthority {
  readonly #pending = new Map<string, PendingConsent>()
  readonly #deps: Required<BrowserConsentAuthorityDeps>

  constructor(deps: BrowserConsentAuthorityDeps) {
    this.#deps = {
      now: deps.now ?? Date.now,
      present: deps.present,
      randomBytes: deps.randomBytes ?? crypto.randomBytes,
      settled: deps.settled ?? (() => undefined),
      setTimer: deps.setTimer ?? setTimeout,
      clearTimer: deps.clearTimer ?? clearTimeout
    }
  }

  request(hostId: number, request: BrowserConsentRequest, ttlMs = BROWSER_CONSENT_TTL_MS) {
    const boundedTtl = Number.isSafeInteger(ttlMs) && ttlMs >= 0
      ? Math.max(1, Math.min(ttlMs, BROWSER_CONSENT_TTL_MS))
      : BROWSER_CONSENT_TTL_MS

    const consentId = this.#deps.randomBytes(32).toString('base64url')

    if (!validConsentId(consentId) || this.#pending.has(consentId)) {
      throw new Error('browser-consent-id-invalid')
    }

    const prompt = Object.freeze({
      ...request,
      consentId,
      expiresAt: this.#deps.now() + boundedTtl
    })

    return new Promise<BrowserConsentOutcome>(resolve => {
      const timer = this.#deps.setTimer(() => this.#settle(consentId, 'expired'), boundedTtl)

      ;(timer as ReturnType<typeof setTimeout> & { unref?: () => void }).unref?.()
      this.#pending.set(consentId, { hostId, prompt, resolve, timer })

      try {
        this.#deps.present(hostId, prompt)
      } catch {
        this.#settle(consentId, 'revoked')
      }
    })
  }

  resolve(
    hostId: number,
    response: { consentId?: unknown; decision?: unknown }
  ): { error?: string; ok: boolean } {
    if (!validConsentId(response?.consentId) ||
      (response.decision !== 'allow' && response.decision !== 'deny' && response.decision !== 'ordinary-for-task')) {
      return { error: 'browser-consent-response-invalid', ok: false }
    }

    const pending = this.#pending.get(response.consentId)

    if (!pending || pending.hostId !== hostId) {
      return { error: 'browser-consent-stale', ok: false }
    }

    if (response.decision === 'ordinary-for-task' &&
      (pending.prompt.category !== 'navigation' || pending.prompt.navigationPolicy?.exceptionEligible !== true)) {
      return { error: 'browser-consent-decision-not-eligible', ok: false }
    }

    if (pending.prompt.expiresAt < this.#deps.now()) {
      this.#settle(response.consentId, 'expired')

      return { error: 'browser-consent-expired', ok: false }
    }

    // Delete before resolving. A renderer retry, duplicate event, or concurrent
    // response can never authorize a second effect.
    this.#settle(response.consentId, response.decision)

    return { ok: true }
  }

  revoke(
    predicate: (prompt: Readonly<BrowserConsentPrompt>, hostId: number) => boolean,
    reason: BrowserConsentTerminalReason = 'revoked'
  ) {
    for (const [consentId, pending] of this.#pending) {
      if (predicate(pending.prompt, pending.hostId)) {
        this.#settle(consentId, reason)
      }
    }
  }

  revokeAll() {
    this.revoke(() => true)
  }

  #settle(consentId: string, reason: BrowserConsentTerminalReason) {
    const pending = this.#pending.get(consentId)

    if (!pending) {return false}
    this.#pending.delete(consentId)
    this.#deps.clearTimer(pending.timer)
    const outcome = Object.freeze({ consentId, reason })

    try {
      this.#deps.settled(pending.hostId, outcome)
    } catch {
      // Renderer dismissal is presentation-only. It must never restore or
      // interrupt authority that has already been consumed in main.
    }

    pending.resolve(outcome)

    return true
  }
}
