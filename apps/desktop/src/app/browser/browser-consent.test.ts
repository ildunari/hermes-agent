import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $browserConsentPrompts,
  enqueueBrowserConsent,
  resetBrowserConsentForTests,
  resolveBrowserConsent,
  startBrowserConsentListener
} from './browser-consent'

const prompt = (consentId: string, operationId: string): BrowserConsentPrompt => ({
  category: 'permission',
  consentId,
  expiresAt: Date.now() + 60_000,
  guestGeneration: 'guest-1',
  operationId,
  permission: 'notifications',
  profile: 'default',
  site: 'https://example.com',
  tabId: 'tab-1',
  taskGeneration: 4,
  taskId: 'task-1'
})

const uploadPrompt = (consentId: string): BrowserConsentPrompt => ({
  ...prompt(consentId, 'operation-upload'),
  category: 'upload-assignment',
  documentGeneration: 7,
  permission: undefined,
  site: 'https://uploads.example.test',
  upload: {
    accept: 'application/pdf',
    aggregateSize: 123,
    destinationOrigin: 'https://uploads.example.test',
    files: [{ displayName: 'report.pdf', mimeType: 'application/pdf', size: 123 }],
    formActionOrigin: 'https://uploads.example.test',
    formLabel: 'Receipt',
    formMethod: 'post',
    immediateSubmissionPossible: true,
    inputLabel: 'Upload receipt',
    mode: 'selectSingle',
    source: 'studio-session-artifact'
  }
})

describe('browser consent renderer correlation', () => {
  const resolveConsent = vi.fn(async () => ({ ok: true }))
  let requested: (prompt: BrowserConsentPrompt) => void
  let resolved: (outcome: { consentId: string; reason: 'revoked' }) => void

  beforeEach(() => {
    resetBrowserConsentForTests()
    resolveConsent.mockClear()
    Object.assign(window, {
      hermesDesktop: {
        browserGuest: {
          onConsentRequested: (callback: typeof requested) => {
            requested = callback

            return vi.fn()
          },
          onConsentResolved: (callback: typeof resolved) => {
            resolved = callback

            return vi.fn()
          },
          resolveConsent
        }
      }
    })
  })

  it('resolves the selected cryptographic id rather than consuming FIFO', async () => {
    const first = 'A'.repeat(43)
    const second = 'B'.repeat(43)
    enqueueBrowserConsent(prompt(first, 'operation-first'))
    enqueueBrowserConsent(prompt(second, 'operation-second'))

    await resolveBrowserConsent(second, 'allow')

    expect(resolveConsent).toHaveBeenCalledWith({ consentId: second, decision: 'allow' })
    expect($browserConsentPrompts.get().map(value => value.consentId)).toEqual([first])
  })

  it('rejects malformed and expired presentations before they reach UI state', () => {
    expect(enqueueBrowserConsent({ ...prompt('short', 'operation'), consentId: 'short' })).toBe(false)
    expect(enqueueBrowserConsent({ ...prompt('C'.repeat(43), 'operation'), expiresAt: Date.now() - 1 })).toBe(false)
    expect($browserConsentPrompts.get()).toEqual([])
  })

  it('admits only the closed body-free upload presentation schema', () => {
    const valid = uploadPrompt('U'.repeat(43))

    expect(enqueueBrowserConsent(valid)).toBe(true)
    resetBrowserConsentForTests()
    expect(enqueueBrowserConsent({ ...valid, detail: 'SHA-256 ' + 'a'.repeat(64) })).toBe(false)
    expect(enqueueBrowserConsent({ ...valid, upload: { ...valid.upload!, sha256: 'a'.repeat(64) } })).toBe(false)
    expect(enqueueBrowserConsent({
      ...valid,
      upload: { ...valid.upload!, aggregateSize: 124 }
    })).toBe(false)
    expect(enqueueBrowserConsent({ ...prompt('V'.repeat(43), 'operation'), upload: valid.upload })).toBe(false)
    expect(enqueueBrowserConsent({ ...valid, site: 'https://other.example.test' })).toBe(false)
    expect($browserConsentPrompts.get()).toEqual([])
  })

  it('rejects sparse or decorated upload arrays and upload-scoped top-level filenames', () => {
    const valid = uploadPrompt('W'.repeat(43))
    const sparse = new Array(1) as BrowserUploadConsentFile[]
    expect(enqueueBrowserConsent({ ...valid, upload: { ...valid.upload!, files: sparse } })).toBe(false)

    const decorated = [...valid.upload!.files]
    Object.defineProperty(decorated, 'metadata', { enumerable: false, value: 'hidden' })
    expect(enqueueBrowserConsent({ ...valid, upload: { ...valid.upload!, files: decorated } })).toBe(false)
    expect(enqueueBrowserConsent({ ...valid, filename: 'report.pdf' })).toBe(false)
    expect($browserConsentPrompts.get()).toEqual([])
  })

  it('removes only the exact main-process dismissal and blocks a late renderer allow', async () => {
    const stop = startBrowserConsentListener()
    const first = 'D'.repeat(43)
    const revoked = 'E'.repeat(43)

    requested(prompt(first, 'operation-retained'))
    requested(prompt(revoked, 'operation-revoked'))
    resolved({ consentId: revoked, reason: 'revoked' })

    expect($browserConsentPrompts.get().map(value => value.consentId)).toEqual([first])
    await expect(resolveBrowserConsent(revoked, 'allow')).resolves.toBe(false)
    expect(resolveConsent).not.toHaveBeenCalled()
    stop()
  })
})
