import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useSlashCompletions } from './use-slash-completions'

describe('useSlashCompletions', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('deduplicates smart update slash rows from built-in, alias, and skill completions', async () => {
    const gateway = {
      request: vi.fn().mockResolvedValue({
        items: [
          {
            text: 'update-smart',
            display: '/update-smart',
            meta: 'Run the Mac Studio branch-first Hermes smart update workflow'
          },
          {
            text: 'update_smart',
            display: '/update_smart',
            meta: 'Run the Mac Studio branch-first Hermes smart update workflow (alias for /update-smart)'
          },
          { text: 'update', display: '/update', meta: 'Update Hermes Agent to the latest version' },
          { text: 'update-smart', display: '/update-smart', meta: '⚡ skill command' }
        ],
        replace_from: 1
      })
    }

    const { result } = renderHook(() => useSlashCompletions({ gateway: gateway as never }))

    expect(result.current.adapter.search?.('update')).toEqual([])

    await waitFor(() => {
      expect(gateway.request).toHaveBeenCalledWith('complete.slash', { text: '/update' })
      expect(result.current.adapter.search?.('update').map(item => item.label)).toEqual(['update-smart'])
    })

    const labels = result.current.adapter.search?.('update').map(item => item.label) ?? []

    expect(labels).toEqual(['update-smart'])
    expect(labels).not.toContain('update_smart')
    expect(labels).not.toContain('update')
  })
})
