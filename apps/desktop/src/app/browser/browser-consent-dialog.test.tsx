import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider, TRANSLATIONS } from '@/i18n'
import type { Locale } from '@/i18n'

import { enqueueBrowserConsent, resetBrowserConsentForTests } from './browser-consent'
import { BrowserConsentDialog } from './browser-consent-dialog'

const localeCases: readonly [Locale][] = [['en'], ['ja'], ['zh'], ['zh-hant']]
const resolveConsent = vi.fn(async () => ({ ok: true }))

function pixelPrompt(): BrowserConsentPrompt {
  return {
    captureScope: 'viewport',
    category: 'outbound-pixels',
    consentId: 'P'.repeat(43),
    documentGeneration: 17,
    expiresAt: Date.now() + 60_000,
    guestGeneration: 'guest-pixels',
    maxBytes: 8 * 1024 * 1024,
    operationId: 'operation-pixels',
    profile: 'default',
    purpose: 'Inspect the checkout confirmation',
    recipient: 'Primary vision provider',
    retention: 'memory-only-transient',
    site: 'https://account.example.test',
    tabId: 'tab-pixels',
    tabTitle: 'Authenticated account',
    taskGeneration: 4,
    taskId: 'task-pixels'
  }
}

function uploadPrompt(): BrowserConsentPrompt {
  return {
    category: 'upload-assignment',
    consentId: 'U'.repeat(43),
    documentGeneration: 19,
    expiresAt: Date.now() + 60_000,
    guestGeneration: 'guest-upload',
    operationId: 'operation-upload',
    profile: 'default',
    site: 'https://uploads.example.test',
    tabId: 'tab-upload',
    taskGeneration: 8,
    taskId: 'task-upload',
    upload: {
      accept: 'application/pdf',
      aggregateSize: 321,
      destinationOrigin: 'https://uploads.example.test',
      files: [{
        displayName: 'receipt.pdf',
        mimeType: 'application/pdf',
        originalDisplayName: 'Receipt 2026.pdf',
        size: 321
      }],
      formActionOrigin: 'https://uploads.example.test',
      formLabel: 'Expense report',
      formMethod: 'post',
      immediateSubmissionPossible: true,
      inputLabel: 'Receipt',
      mode: 'selectSingle',
      source: 'studio-session-artifact'
    }
  }
}

function navigationPrompt(): BrowserConsentPrompt {
  return {
    category: 'navigation',
    consentId: 'N'.repeat(43),
    detail: 'https://source.example',
    expiresAt: Date.now() + 60_000,
    guestGeneration: 'guest-navigation',
    navigationPolicy: {
      categoryCodes: [],
      classification: 'unknown',
      exceptionEligible: true,
      policyVersion: 'd-022-snp-v1',
      provenance: [{ source: 'fail-closed' }],
      pslVersion: 'publicsuffix-list-test',
      reasonCodes: ['no-rule-proves-ordinary'],
      urlParserVersion: 'whatwg-url-uts46-v1'
    },
    operationId: 'operation-navigation',
    profile: 'default',
    site: 'https://example.com',
    tabId: 'tab-navigation',
    taskGeneration: 9,
    taskId: 'task-navigation'
  }
}

describe('BrowserConsentDialog outbound pixel disclosure', () => {
  beforeEach(() => {
    resetBrowserConsentForTests()
    resolveConsent.mockClear()
    Object.assign(window, {
      hermesDesktop: {
        browserGuest: {
          onConsentRequested: vi.fn(() => vi.fn()),
          onConsentResolved: vi.fn(() => vi.fn()),
          resolveConsent
        }
      }
    })
  })

  afterEach(() => {
    cleanup()
    resetBrowserConsentForTests()
  })

  it.each(localeCases)('shows the required trusted pixel disclosures in %s', locale => {
    const copy = TRANSLATIONS[locale].browserConsent

    expect(enqueueBrowserConsent(pixelPrompt())).toBe(true)
    render(
      <I18nProvider configClient={null} initialLocale={locale}>
        <BrowserConsentDialog />
      </I18nProvider>
    )

    expect(screen.getByRole('alert').textContent).toBe(copy.pixelWarning)
    expect(screen.getByText('Primary vision provider')).toBeTruthy()
    expect(screen.getByText('Inspect the checkout confirmation')).toBeTruthy()
    expect(screen.getByText(copy.viewportDocumentScope(17))).toBeTruthy()
    expect(screen.getByText(copy.memoryOnlyRetention)).toBeTruthy()
    expect(screen.getByText('default')).toBeTruthy()
    expect(screen.getByText('task-pixels')).toBeTruthy()
    expect(screen.getByText('tab-pixels')).toBeTruthy()
    expect(screen.getByText('guest-pixels')).toBeTruthy()
    expect(screen.getByText('4')).toBeTruthy()
  })

  it.each(localeCases)('shows the exact upload assignment disclosure without secret canaries in %s', locale => {
    const copy = TRANSLATIONS[locale].browserUpload

    expect(enqueueBrowserConsent(uploadPrompt())).toBe(true)
    render(
      <I18nProvider configClient={null} initialLocale={locale}>
        <BrowserConsentDialog />
      </I18nProvider>
    )

    expect(screen.getByRole('alert').textContent).toBe(copy.consent.warning)
    expect(screen.getByText(copy.consent.source)).toBeTruthy()
    expect(screen.getByText('receipt.pdf — application/pdf, 321 ' + copy.bytes)).toBeTruthy()
    expect(screen.getByText('Receipt 2026.pdf → receipt.pdf')).toBeTruthy()
    expect(screen.getByText('POST https://uploads.example.test', { exact: false })).toBeTruthy()
    const presentation = screen.getByRole('dialog').textContent ?? ''
    expect(presentation).not.toContain('/Users/')
    expect(presentation).not.toContain('sha256')
    expect(presentation).not.toContain('deliveryCredential')
  })

  it('starts each queued approval on Deny and prevents a repeated decision while resolving', async () => {
    let finish!: (value: { ok: boolean }) => void

    resolveConsent.mockImplementationOnce(() => new Promise(resolve => (finish = resolve)))
    expect(enqueueBrowserConsent(pixelPrompt())).toBe(true)
    expect(enqueueBrowserConsent({ ...pixelPrompt(), consentId: 'Q'.repeat(43), operationId: 'operation-second', taskId: 'task-second' })).toBe(true)
    render(
      <I18nProvider configClient={null} initialLocale="en">
        <BrowserConsentDialog />
      </I18nProvider>
    )

    const allow = await screen.findByRole('button', { name: 'Allow once' })
    const deny = screen.getByRole('button', { name: 'Deny' })

    await waitFor(() => expect(window.document.activeElement).toBe(deny))
    fireEvent.click(allow)
    fireEvent.click(allow)
    expect(resolveConsent).toHaveBeenCalledTimes(1)
    expect((allow as HTMLButtonElement).disabled).toBe(true)

    finish({ ok: true })
    await screen.findByText('task-second')
    await waitFor(() => expect(window.document.activeElement).toBe(screen.getByRole('button', { name: 'Deny' })))
  })

  it('explains fail-closed navigation and offers only an eligible scoped exception', async () => {
    expect(enqueueBrowserConsent(navigationPrompt())).toBe(true)
    render(
      <I18nProvider configClient={null} initialLocale="en">
        <BrowserConsentDialog />
      </I18nProvider>
    )

    expect(screen.getByText('Unclassified — treated as sensitive')).toBeTruthy()
    expect(screen.getByText('no-rule-proves-ordinary')).toBeTruthy()
    expect(screen.getByText(TRANSLATIONS.en.browserConsent.navigationGateWarning)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Treat as ordinary for this task' }))
    await waitFor(() => expect(resolveConsent).toHaveBeenCalledWith({
      consentId: 'N'.repeat(43),
      decision: 'ordinary-for-task'
    }))
  })

  it('has independently translated trust-boundary labels in every non-English locale', () => {
    const english = TRANSLATIONS.en.browserConsent
    const englishUpload = TRANSLATIONS.en.browserUpload

    expect(englishUpload.sourceStagingWarning).toContain('temporary staging')
    expect(englishUpload.sourceStagingWarning).toContain('own later approval')
    for (const locale of ['ja', 'zh', 'zh-hant'] as const) {
      const copy = TRANSLATIONS[locale].browserConsent
      const uploadCopy = TRANSLATIONS[locale].browserUpload

      expect(copy.browserGeneration).not.toBe(english.browserGeneration)
      expect(copy.taskGeneration).not.toBe(english.taskGeneration)
      expect(copy.scopeWarning).not.toBe(english.scopeWarning)
      expect(copy.categories.websiteSubmission).not.toBe(english.categories.websiteSubmission)
      expect(uploadCopy.sourceStagingWarning).not.toBe(englishUpload.sourceStagingWarning)
      expect(uploadCopy.consent.warning).not.toBe(englishUpload.consent.warning)
    }
  })
})
