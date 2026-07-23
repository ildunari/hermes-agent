import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useSessionStatusPresence } from '@/app/chat/composer/hooks/use-status-presence'
import { type SessionView, SessionViewProvider } from '@/app/chat/session-view'
import { $previewStatusBySession } from '@/store/preview-status'
import { $activeSessionId, $currentCwd } from '@/store/session'

import { Thread } from '../thread'

const createdAt = new Date('2026-07-23T00:00:00.000Z')

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal('ResizeObserver', TestResizeObserver)
vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
  window.setTimeout(() => callback(performance.now()), 0)
)
vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))

Element.prototype.scrollTo = function scrollTo() {}

Element.prototype.animate = function animate() {
  return {
    cancel: () => {},
    finished: Promise.resolve()
  } as unknown as Animation
}

function previewMessage(): ThreadMessage {
  return {
    id: 'assistant-preview-a',
    role: 'assistant',
    content: [
      {
        type: 'tool-call',
        toolCallId: 'read-preview-a',
        toolName: 'read_file',
        args: { path: '/a/index.html' },
        argsText: JSON.stringify({ path: '/a/index.html' }),
        result: { content: '1|<!doctype html>' }
      }
    ],
    status: { type: 'complete', reason: 'stop' },
    createdAt,
    metadata: {
      unstable_state: null,
      unstable_annotations: [],
      unstable_data: [],
      steps: [],
      custom: {}
    }
  } as ThreadMessage
}

function sessionView(runtimeId: string, cwd: string): SessionView {
  return {
    kind: 'tile',
    $awaitingResponse: atom(false),
    $busy: atom(false),
    $cwd: atom(cwd),
    $fast: atom(false),
    $lastVisibleIsUser: atom(false),
    $messages: atom([]),
    $messagesEmpty: atom(false),
    $model: atom(''),
    $provider: atom(''),
    $reasoningEffort: atom(''),
    $runtimeId: atom(runtimeId),
    $storedId: atom('stored-a')
  }
}

function StatusPresenceProbe({ sessionId }: { sessionId: string }) {
  const present = useSessionStatusPresence(sessionId)

  return <output data-testid={`status-${sessionId}`}>{String(present)}</output>
}

function PreviewHarness({ revision }: { revision: number }) {
  const message = previewMessage()

  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages: [message],
    isRunning: false,
    onNew: async () => {}
  })

  const view = sessionView('session-a', '/a')

  return (
    <SessionViewProvider value={view}>
      <AssistantRuntimeProvider runtime={runtime}>
        <div data-revision={revision}>
          <Thread />
          <StatusPresenceProbe sessionId="session-b" />
        </div>
      </AssistantRuntimeProvider>
    </SessionViewProvider>
  )
}

beforeEach(() => {
  $previewStatusBySession.set({})
  $activeSessionId.set('session-b')
  $currentCwd.set('/b')
})

afterEach(() => {
  cleanup()
  $previewStatusBySession.set({})
  $activeSessionId.set(null)
  $currentCwd.set('')
})

describe('preview artifact session ownership', () => {
  it('keeps a background session tool preview out of the globally active composer across rerenders', async () => {
    const view = render(<PreviewHarness revision={1} />)

    await waitFor(() => {
      expect($previewStatusBySession.get()['session-a']?.map(item => ({ cwd: item.cwd, target: item.target }))).toEqual(
        [{ cwd: '/a', target: '/a/index.html' }]
      )
    })

    view.rerender(<PreviewHarness revision={2} />)

    await waitFor(() => {
      expect($previewStatusBySession.get()['session-a']?.map(item => item.target)).toEqual(['/a/index.html'])
      expect($previewStatusBySession.get()['session-b']).toBeUndefined()
      expect(screen.getByTestId('status-session-b').textContent).toBe('false')
    })
  })
})
