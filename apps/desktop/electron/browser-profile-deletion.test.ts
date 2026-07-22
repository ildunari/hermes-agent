import { describe, expect, it, vi } from 'vitest'

import { deleteLocalBrowserProfileData } from './browser-profile-deletion'

describe('deleteLocalBrowserProfileData', () => {
  it('deletes metadata unconditionally when site and permission clearing fail', async () => {
    const deleteMetadata = vi.fn(() => true)
    const result = await deleteLocalBrowserProfileData(
      async () => ({ permissions: false, siteData: true }),
      deleteMetadata
    )
    expect(deleteMetadata).toHaveBeenCalledOnce()
    expect(result).toEqual({ metadata: true, ok: false, permissions: false, siteData: true })
  })

  it('deletes metadata even when the partition clear throws', async () => {
    const deleteMetadata = vi.fn(() => true)
    const result = await deleteLocalBrowserProfileData(
      async () => {throw new Error('partition unavailable')},
      deleteMetadata
    )
    expect(deleteMetadata).toHaveBeenCalledOnce()
    expect(result).toEqual({ metadata: true, ok: false, permissions: false, siteData: false })
  })
})
