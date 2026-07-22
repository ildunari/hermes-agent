import { describe, expect, it } from 'vitest'

import { browserDataClearPlan } from './browser-data-semantics'

describe('browser data clear scopes', () => {
  it('keeps exact-site clearing separate from global cache and HTTP auth clearing', () => {
    expect(browserDataClearPlan('https://example.test')).toEqual(expect.objectContaining({
      clearAuthCache: false,
      clearHttpCache: false,
      origin: 'https://example.test'
    }))
    expect(browserDataClearPlan()).toEqual(expect.objectContaining({
      clearAuthCache: true,
      clearHttpCache: true
    }))
  })

  it('rejects paths, credentials, internal schemes, and noncanonical origins', () => {
    for (const origin of [
      'https://example.test/',
      'https://example.test/path',
      'https://user:pass@example.test',
      'file:///tmp/private',
      'hermes-artifact://g-private/report',
      'not a URL'
    ]) {
      expect(browserDataClearPlan(origin)).toBeNull()
    }
  })
})
