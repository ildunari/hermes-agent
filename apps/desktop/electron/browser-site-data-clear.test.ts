import { describe, expect, it, vi } from 'vitest'

import { runBrowserSiteDataClear } from './browser-site-data-clear'

describe('runBrowserSiteDataClear', () => {
  it('reports site storage cleared when later cache maintenance fails', async () => {
    const clearStorage = vi.fn(async () => undefined)
    const result = await runBrowserSiteDataClear(clearStorage, async () => {throw new Error('cache unavailable')})
    expect(clearStorage).toHaveBeenCalledOnce()
    expect(result).toBe(true)
  })

  it('reports site storage uncleared when its phase fails', async () => {
    const result = await runBrowserSiteDataClear(
      async () => {throw new Error('storage unavailable')},
      vi.fn()
    )
    expect(result).toBe(false)
  })
})
