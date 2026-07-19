import { useCallback, useRef } from 'react'

import { getCronJobs, listAllProfileSessions, listSidebarSessions, type SessionInfo } from '@/hermes'
import { sameCronSignature } from '@/lib/session-signatures'
import {
  isMessagingSource,
  LOCAL_SESSION_SOURCE_IDS,
  MESSAGING_SESSION_SOURCE_IDS,
  normalizeSessionSource
} from '@/lib/session-source'
import { setCronJobs } from '@/store/cron'
import { $pinnedSessionIds, $sessionsLimit, bumpSessionsLimit, SIDEBAR_SESSIONS_PAGE_SIZE } from '@/store/layout'
import { ALL_PROFILES, normalizeProfileKey } from '@/store/profile'
import {
  $messagingSessions,
  $selectedStoredSessionId,
  $sessions,
  CRON_SECTION_LIMIT,
  mergeSessionPage,
  MESSAGING_SECTION_LIMIT,
  setCronSessions,
  setMessagingPlatformTotals,
  setMessagingSessions,
  setMessagingTruncated,
  setSessionProfileTotals,
  setSessions,
  setSessionsLoading,
  setSessionsTotal
} from '@/store/session'
import { $workingSessionIds, getRecentlySettledSessionIds } from '@/store/session-states'

// The recents list is local-only: cron rows and messaging platform rows have
// independent sidebar sections. The coalesced snapshot preserves those slices
// without letting gateway threads bury interactive local chats during paging.
const SIDEBAR_EXCLUDED_SOURCES = ['cron', 'subagent', 'tool', 'smoke-test', ...MESSAGING_SESSION_SOURCE_IDS]
// The messaging slice is the inverse: drop cron + every local source so only
// external-platform conversations remain, then split per platform in the UI.
const MESSAGING_EXCLUDED_SOURCES = ['cron', ...LOCAL_SESSION_SOURCE_IDS]

// Rows a session refresh must preserve even if the aggregator omits them:
// in-flight first turns (message_count 0), pinned rows aged off the page, the
// actively-viewed chat (its "working" flag clears a beat before the aggregator
// sees the persisted row), and sessions whose turn just settled (same race, but
// for a chat the user has already navigated away from). Pass `scope` to only
// keep the active row when it belongs to the profile being paged.
function sessionsToKeep(scope?: string): Set<string> {
  const keep = new Set<string>([
    ...$workingSessionIds.get(),
    ...$pinnedSessionIds.get(),
    ...getRecentlySettledSessionIds()
  ])

  const active = $selectedStoredSessionId.get()

  if (active) {
    const session = scope ? $sessions.get().find(s => s.id === active) : null

    if (!scope || !session || normalizeProfileKey(session.profile) === scope) {
      keep.add(active)
    }
  }

  return keep
}

interface UseSessionListActionsArgs {
  profileScope: string
}

/** Owns the sidebar's session-list fetching + paging: recents, cron runs/jobs,
 *  and the per-platform messaging slices. Returns the callbacks the controller
 *  wires into the sidebar and refresh effects. */
export function useSessionListActions({ profileScope }: UseSessionListActionsArgs) {
  const refreshSessionsRequestRef = useRef(0)

  // Messaging-platform sessions are also refreshed on their own interval, so
  // platform sections can update between the coalesced sidebar snapshots.
  const refreshMessagingSessions = useCallback(async () => {
    try {
      const result = await listAllProfileSessions(MESSAGING_SECTION_LIMIT, 1, 'exclude', 'recent', 'all', {
        excludeSources: MESSAGING_EXCLUDED_SOURCES
      })

      // Drop any non-messaging source the broad exclude didn't catch (custom
      // sources) — those stay in local recents, not a platform section.
      const rows = result.sessions.filter(s => isMessagingSource(s.source))

      setMessagingSessions(prev => (sameCronSignature(prev, rows) ? prev : rows))
      // Hit the cap → at least one platform may have more on disk than loaded,
      // so platform sections offer their own per-platform "load more".
      setMessagingTruncated(result.sessions.length >= MESSAGING_SECTION_LIMIT)
    } catch {
      // Non-fatal: the messaging sections just stay empty/stale.
    }
  }, [])

  // Page a single platform's section independently (mirrors the per-profile
  // pager): fetch that source's next window and merge it back in place, leaving
  // every other platform's rows untouched. Resolves the platform's exact total.
  const loadMoreMessagingForPlatform = useCallback(async (platform: string) => {
    const inPlatform = (s: SessionInfo) => normalizeSessionSource(s.source) === platform
    const loaded = $messagingSessions.get().filter(inPlatform).length

    const result = await listAllProfileSessions(loaded + SIDEBAR_SESSIONS_PAGE_SIZE, 1, 'exclude', 'recent', 'all', {
      source: platform
    })

    const incoming = result.sessions.filter(s => normalizeSessionSource(s.source) === platform)

    setMessagingSessions(prev => [
      ...prev.filter(s => !inPlatform(s)),
      ...mergeSessionPage(prev.filter(inPlatform), incoming, sessionsToKeep())
    ])

    const total = result.total ?? incoming.length
    setMessagingPlatformTotals(prev => ({ ...prev, [platform]: Math.max(total, incoming.length) }))
  }, [])

  // Cron *jobs* drive the sidebar "Cron jobs" section. Jobs are created
  // synchronously (agent tool call or the cron UI), so refreshing here right
  // after an agent turn surfaces a new job immediately; the interval poll keeps
  // next-run/state fresh as the scheduler advances them.
  const refreshCronJobs = useCallback(async () => {
    try {
      const jobs = await getCronJobs()

      setCronJobs(jobs)
    } catch {
      // Non-fatal: the cron section just keeps its last-known jobs.
    }
  }, [])

  const refreshSessions = useCallback(async () => {
    const requestId = refreshSessionsRequestRef.current + 1
    refreshSessionsRequestRef.current = requestId
    // The loading flag exists to drive the initial skeletons (they only render
    // while the list is empty). Turn-complete / reconnect refreshes over a
    // populated list used to flip it true→false anyway, churning every
    // $sessionsLoading subscriber twice per turn for no visible change.
    const showLoading = $sessions.get().length === 0

    if (showLoading) {
      setSessionsLoading(true)
    }

    try {
      const limit = $sessionsLimit.get()

      // One bounded backend request opens every profile DB once, then returns
      // the independent recents, cron, and messaging slices. Keep the scoped
      // recents view so a sparse active profile cannot be windowed out by the
      // global recency page; cron and messaging remain all-profile sections.
      const sessionProfile = profileScope === ALL_PROFILES ? 'all' : profileScope

      const snapshot = await listSidebarSessions({
        recentsProfile: sessionProfile,
        recentsLimit: limit,
        recentsExclude: SIDEBAR_EXCLUDED_SOURCES,
        cronLimit: CRON_SECTION_LIMIT,
        messagingLimit: MESSAGING_SECTION_LIMIT,
        messagingExclude: MESSAGING_EXCLUDED_SOURCES
      })

      if (refreshSessionsRequestRef.current === requestId) {
        const { recents, cron, messaging } = snapshot
        // Signature-gate the swap (same pattern as cron/messaging): a refresh
        // that returns content-identical rows must keep the previous array
        // identity, or every sidebar memo keyed on $sessions recomputes and the
        // whole list re-renders once per turn/broadcast for nothing.
        setSessions(prev => {
          const next = mergeSessionPage(prev, recents.sessions, sessionsToKeep())

          return sameCronSignature(prev, next) ? prev : next
        })
        setSessionsTotal(typeof recents.total === 'number' ? recents.total : recents.sessions.length)
        setSessionProfileTotals(prev => {
          const next = recents.profile_totals ?? {}
          const prevKeys = Object.keys(prev)

          return prevKeys.length === Object.keys(next).length && prevKeys.every(key => prev[key] === next[key])
            ? prev
            : next
        })
        setCronSessions(prev => (sameCronSignature(prev, cron.sessions) ? prev : cron.sessions))

        // Drop any non-messaging custom source that the broad SQL exclusion did
        // not catch; those rows belong in local recents, not platform sections.
        const messagingRows = messaging.sessions.filter(session => isMessagingSource(session.source))
        setMessagingSessions(prev => (sameCronSignature(prev, messagingRows) ? prev : messagingRows))
        setMessagingTruncated(messaging.sessions.length >= MESSAGING_SECTION_LIMIT)
      }
    } finally {
      if (showLoading && refreshSessionsRequestRef.current === requestId) {
        setSessionsLoading(false)
      }
    }

    // Cron *jobs* are a distinct API (getCronJobs), not a session slice.
    void refreshCronJobs()
  }, [profileScope, refreshCronJobs])

  const loadMoreSessions = useCallback(async () => {
    bumpSessionsLimit()
    await refreshSessions()
  }, [refreshSessions])

  // ALL-profiles view pages one profile at a time: fetch that profile's next
  // page and merge it in place, leaving every other profile's rows untouched.
  const loadMoreSessionsForProfile = useCallback(async (profile: string) => {
    const key = normalizeProfileKey(profile)
    const inKey = (s: SessionInfo) => normalizeProfileKey(s.profile) === key
    const loaded = $sessions.get().filter(inKey).length

    const result = await listAllProfileSessions(loaded + SIDEBAR_SESSIONS_PAGE_SIZE, 1, 'exclude', 'recent', key, {
      excludeSources: SIDEBAR_EXCLUDED_SOURCES
    })

    const keep = sessionsToKeep(key)

    setSessions(prev => [
      ...prev.filter(s => !inKey(s)),
      ...mergeSessionPage(prev.filter(inKey), result.sessions, keep)
    ])

    const total = result.profile_totals?.[key] ?? result.total ?? result.sessions.length
    setSessionProfileTotals(prev => ({ ...prev, [key]: Math.max(total, result.sessions.length) }))
  }, [])

  return {
    loadMoreMessagingForPlatform,
    loadMoreSessions,
    loadMoreSessionsForProfile,
    refreshCronJobs,
    refreshMessagingSessions,
    refreshSessions
  }
}
