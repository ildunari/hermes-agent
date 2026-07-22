import { describe, expect, it } from 'vitest'

import type { BrowserPendingUploadChooser } from './browser-guest-security'
import { buildBrowserUploadConsentDetail } from './browser-upload-consent'

function chooser(): BrowserPendingUploadChooser {
  return {
    accept: 'application/pdf',
    backendNodeId: 8,
    chooserId: 'chooser-1',
    directory: false,
    documentGeneration: 3,
    formActionOrigin: 'https://uploads.example.test',
    formActionUrl: 'https://uploads.example.test/submit',
    formFingerprint: 'fingerprint-secret',
    formLabel: 'Expense report',
    formMethod: 'post',
    frameId: 'frame-1',
    guestGeneration: 'guest-1',
    hostId: 10,
    inputLabel: '',
    inputName: 'receipt',
    mode: 'selectSingle',
    origin: 'https://uploads.example.test',
    profile: 'default',
    signal: new AbortController().signal,
    tabId: 'tab-1',
    taskGeneration: 4,
    taskId: 'task-1'
  }
}

describe('production upload consent projection', () => {
  it('projects exact decision metadata while omitting main-only secrets', () => {
    const detail = buildBrowserUploadConsentDetail(chooser(), [{
      displayName: 'receipt.pdf',
      mimeType: 'application/pdf',
      originalDisplayName: 'Receipt 2026.pdf',
      sha256: 'a'.repeat(64),
      size: 321
    }])

    expect(detail).toEqual({
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
      inputLabel: 'receipt',
      mode: 'selectSingle',
      source: 'studio-session-artifact'
    })
    const serialized = JSON.stringify(detail)
    expect(serialized).not.toContain('a'.repeat(64))
    expect(serialized).not.toContain('fingerprint-secret')
    expect(serialized).not.toContain('/submit')
  })
})
