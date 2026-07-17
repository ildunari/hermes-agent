import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $currentUsage, setCurrentUsage } from '@/store/session'
import type { RpcEvent } from '@/types/hermes'

import { useMessageStream } from './index'

const ACTIVE_SID = 'session-active'
const BACKGROUND_SID = 'session-background'
let handleEvent: ((event: RpcEvent) => void) | null = null

function Harness() {
  const activeSessionIdRef = useRef<string | null>(ACTIVE_SID)
  const sessionStateByRuntimeIdRef = useRef(new Map<string, ClientSessionState>())
  const queryClientRef = useRef(new QueryClient())

  const stream = useMessageStream({
    activeSessionIdRef,
    hydrateFromStoredSession: vi.fn(async () => undefined),
    queryClient: queryClientRef.current,
    refreshHermesConfig: vi.fn(async () => undefined),
    refreshSessions: vi.fn(async () => undefined),
    sessionStateByRuntimeIdRef,
    updateSessionState: (sessionId, updater) => {
      const current = sessionStateByRuntimeIdRef.current.get(sessionId) ?? createClientSessionState()
      const next = updater(current)
      sessionStateByRuntimeIdRef.current.set(sessionId, next)

      return next
    }
  })

  useEffect(() => {
    handleEvent = stream.handleGatewayEvent
  }, [stream.handleGatewayEvent])

  return null
}

describe('useMessageStream usage scoping', () => {
  beforeEach(() => {
    handleEvent = null
    setCurrentUsage({
      calls: 1,
      context_max: 272_000,
      context_percent: 18,
      context_used: 49_300,
      input: 10,
      output: 2,
      total: 12
    })
  })

  afterEach(() => {
    cleanup()
    setCurrentUsage({ calls: 0, input: 0, output: 0, total: 0 })
    vi.restoreAllMocks()
  })

  it('does not replace the visible context bar when a background session completes', async () => {
    render(<Harness />)
    await waitFor(() => expect(handleEvent).not.toBeNull())

    act(() =>
      handleEvent!({
        payload: {
          text: 'done',
          usage: {
            calls: 4,
            context_max: 272_000,
            context_percent: 61,
            context_used: 166_800,
            input: 40,
            output: 8,
            total: 48
          }
        },
        session_id: BACKGROUND_SID,
        type: 'message.complete'
      })
    )

    expect($currentUsage.get()).toMatchObject({ context_percent: 18, context_used: 49_300 })
  })
})
