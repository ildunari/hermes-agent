import { describe, expect, it } from 'vitest'

import { contextSegmentPercent, resolveContextUsage } from './context-usage'

describe('contextSegmentPercent', () => {
  it('sizes a category against the context window rather than the category sum', () => {
    expect(contextSegmentPercent(25_000, 100_000)).toBe(25)
  })

  it('bounds malformed or overflowing values', () => {
    expect(contextSegmentPercent(-1, 100_000)).toBe(0)
    expect(contextSegmentPercent(150_000, 100_000)).toBe(100)
    expect(contextSegmentPercent(1, 0)).toBe(0)
  })
})

describe('resolveContextUsage', () => {
  it('keeps the status-bar measurement authoritative over a fetched breakdown', () => {
    expect(
      resolveContextUsage(
        {
          calls: 1,
          context_max: 272_000,
          context_percent: 18,
          context_used: 49_300,
          input: 0,
          output: 0,
          total: 0
        },
        {
          categories: [],
          context_max: 272_000,
          context_percent: 61,
          context_used: 166_800,
          estimated_total: 166_800
        }
      )
    ).toEqual({ contextMax: 272_000, contextPercent: 18, contextUsed: 49_300 })
  })

  it('falls back to the breakdown before live usage is available', () => {
    expect(
      resolveContextUsage(
        { calls: 0, input: 0, output: 0, total: 0 },
        {
          categories: [],
          context_max: 100_000,
          context_percent: 10,
          context_used: 10_000,
          estimated_total: 10_000
        }
      )
    ).toEqual({ contextMax: 100_000, contextPercent: 10, contextUsed: 10_000 })
  })
})
