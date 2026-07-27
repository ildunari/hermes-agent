import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $sessionsLimit, resetSessionsLimit, SIDEBAR_SESSIONS_PAGE_SIZE } from '@/store/layout'
import {
  $cronSessions,
  $currentUsage,
  $freshDraftReady,
  $messagingSessions,
  $sessionProfilesTruncated,
  $sessions,
  $sessionsLoading,
  setCronSessions,
  setCurrentUsage,
  setFreshDraftReady,
  setMessagingSessions,
  setSessionProfilesTruncated,
  setSessions,
  setSessionsLoading
} from '@/store/session'
import { $stalledSessionIds } from '@/store/session-states'

import { $gatewaySwitching, wipeSessionListsForGatewaySwitch } from './gateway-switch'

vi.mock('@/lib/query-client', () => ({
  invalidateProfileScopedQueries: vi.fn()
}))

describe('wipeSessionListsForGatewaySwitch', () => {
  beforeEach(() => {
    $gatewaySwitching.set(false)
    setSessions([{ id: 's1', title: 'old', profile: 'default' } as never])
    setSessionProfilesTruncated({ default: true })
    setCronSessions([{ id: 'c1', title: 'cron', profile: 'default' } as never])
    setMessagingSessions([{ id: 'm1', title: 'tg', profile: 'default' } as never])
    $stalledSessionIds.set(['s1'])
    setSessionsLoading(false)
    setFreshDraftReady(false)
    setCurrentUsage({
      calls: 1,
      context_max: 272_000,
      context_percent: 61,
      context_used: 166_800,
      input: 1,
      output: 1,
      total: 2
    })
    $sessionsLimit.set(SIDEBAR_SESSIONS_PAGE_SIZE * 3)
  })

  afterEach(() => {
    resetSessionsLimit()
    setSessions([])
    setCronSessions([])
    setMessagingSessions([])
    $stalledSessionIds.set([])
    setSessionsLoading(true)
    setCurrentUsage({ calls: 0, input: 0, output: 0, total: 0 })
    $gatewaySwitching.set(false)
  })

  it('clears lists and arms loading so sidebar skeletons retrigger', () => {
    wipeSessionListsForGatewaySwitch()

    expect($sessions.get()).toEqual([])
    expect($sessionProfilesTruncated.get()).toEqual({})
    expect($cronSessions.get()).toEqual([])
    expect($messagingSessions.get()).toEqual([])
    expect($stalledSessionIds.get()).toEqual([])
    expect($sessionsLoading.get()).toBe(true)
    expect($sessionsLimit.get()).toBe(SIDEBAR_SESSIONS_PAGE_SIZE)
    expect($freshDraftReady.get()).toBe(true)
    expect($currentUsage.get()).toEqual({ calls: 0, input: 0, output: 0, total: 0 })
  })
})
