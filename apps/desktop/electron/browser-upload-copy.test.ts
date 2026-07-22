import { describe, expect, it } from 'vitest'

import { BROWSER_UPLOAD_MAIN_COPY } from './browser-upload-copy'

describe('trusted upload source-selection copy', () => {
  it('states the staging, assignment, and later website-effect gates in every locale', () => {
    const english = BROWSER_UPLOAD_MAIN_COPY.en

    expect(english.sourceStagingWarning).toContain('temporary staging')
    expect(english.sourceStagingWarning).toContain('separate approval')
    expect(english.sourceStagingWarning).toContain('own later approval')

    for (const locale of ['ja', 'zh', 'zh-hant'] as const) {
      const copy = BROWSER_UPLOAD_MAIN_COPY[locale]

      expect(copy.sourceStagingWarning.length).toBeGreaterThan(20)
      expect(copy.sourceStagingWarning).not.toBe(english.sourceStagingWarning)
    }
  })
})
