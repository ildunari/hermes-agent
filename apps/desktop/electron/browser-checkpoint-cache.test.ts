import { afterEach, describe, expect, it, vi } from 'vitest'

import { BrowserCheckpointCache } from './browser-checkpoint-cache'

afterEach(() => vi.useRealTimers())

function bytes(size: number, value = 7) { return new Uint8Array(size).fill(value) }

describe('BrowserCheckpointCache', () => {
  it('expires previews after at most fifteen minutes and zeroes evicted bytes', () => {
    vi.useFakeTimers()
    let now = 1_000
    const cache = new BrowserCheckpointCache({ now: () => now })
    expect(cache.put({ bytes: bytes(4), id: 'one', profile: 'default', tabIncarnationId: 'tab-a' })).toBeTruthy()
    now += 15 * 60 * 1_000
    vi.advanceTimersByTime(15 * 60 * 1_000)
    expect(cache.get('one')).toBeNull()
  })

  it('evicts oldest-first above three per tab and sixteen per profile', () => {
    let now = 0
    const cache = new BrowserCheckpointCache({ now: () => now++ })

    for (let index = 0; index < 4; index += 1) {
      cache.put({ bytes: bytes(1), id: `tab-${index}`, profile: 'default', tabIncarnationId: 'tab-a' })
    }

    expect(cache.get('tab-0')).toBeNull()
    expect(cache.list('default')).toHaveLength(3)

    for (let index = 0; index < 17; index += 1) {
      cache.put({ bytes: bytes(1), id: `profile-${index}`, profile: 'default', tabIncarnationId: `other-${index}` })
    }

    expect(cache.list('default')).toHaveLength(16)
    expect(cache.get('tab-1')).toBeNull()
  })

  it('counts encoded and decoded bytes against 64 MiB and rejects a single oversize capture', () => {
    let now = 0
    const cache = new BrowserCheckpointCache({ now: () => now++ })
    expect(cache.put({ bytes: bytes(1), decodedBytes: 64 * 1024 * 1024, id: 'oversize', profile: 'p', tabIncarnationId: 't' })).toBeNull()

    for (let index = 0; index < 3; index += 1) {
      expect(cache.put({ bytes: bytes(1), decodedBytes: 24 * 1024 * 1024, id: `large-${index}`, profile: 'p', tabIncarnationId: `t-${index}` })).toBeTruthy()
    }

    expect(cache.get('large-0')).toBeNull()
    expect(cache.list('p')).toHaveLength(2)
  })

  it('isolates profiles and supports exact tab/profile/memory-pressure lifecycle eviction', () => {
    const cache = new BrowserCheckpointCache()
    cache.put({ bytes: bytes(2), id: 'a', profile: 'a', tabIncarnationId: 'tab' })
    cache.put({ bytes: bytes(2), id: 'b', profile: 'b', tabIncarnationId: 'tab' })
    cache.evictTab('a', 'tab')
    expect(cache.get('a')).toBeNull()
    expect(cache.get('b')).toBeTruthy()
    cache.evictTabAcrossProfiles('tab')
    expect(cache.get('b')).toBeNull()
    cache.put({ bytes: bytes(2), id: 'b2', profile: 'b', tabIncarnationId: 'tab' })
    cache.handleMemoryPressure('b')
    expect(cache.get('b2')).toBeNull()
  })
})
