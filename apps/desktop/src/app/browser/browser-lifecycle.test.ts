import { beforeEach, describe, expect, it } from 'vitest'

import { applyBrowserToolLifecycle } from './browser-lifecycle'
import {
  $browserTabs,
  $foregroundBrowserTabId,
  $taskTabBindings,
  clearBrowserTabs
} from './browser-store'
import {
  $browserSupervision,
  clearBrowserSupervision
} from './browser-supervision'

describe('production browser tool lifecycle', () => {
  beforeEach(() => {
    clearBrowserTabs()
    clearBrowserSupervision()
  })

  it('creates an unfocused exact task binding and trusted redacted supervision record', () => {
    expect(
      applyBrowserToolLifecycle('start', 'session-browser', 'coding', {
        args: { url: 'https://must-not-enter-trusted-state.test/private' },
        name: 'browser_navigate',
        tool_id: 'tool-opaque'
      })
    ).toBe(true)

    const binding = $taskTabBindings.get()['session-browser']
    const record = $browserSupervision.get()['session-browser']

    expect(binding).toBeDefined()
    expect($browserTabs.get()).toHaveLength(1)
    expect($browserTabs.get()[0]).toMatchObject({ profile: 'coding', url: '', workspaceId: 'session-browser' })
    expect($foregroundBrowserTabId.get()).toBeNull()
    expect(record).toMatchObject({
      generation: binding.generation,
      operation: 'navigate',
      ownerId: 'tool-opaque',
      profile: 'coding',
      sessionId: 'session-browser',
      state: 'agent',
      tabId: binding.tabId,
      taskId: 'session-browser'
    })
    expect(JSON.stringify(record)).not.toContain('must-not-enter-trusted-state')
  })

  it('updates live operation state on later browser tools and returns to idle on completion', () => {
    applyBrowserToolLifecycle('start', 'session-browser', 'default', {
      name: 'browser_snapshot',
      tool_id: 'snapshot-1'
    })
    const binding = $taskTabBindings.get()['session-browser']

    expect($browserSupervision.get()['session-browser'].operation).toBe('snapshot')
    expect(
      applyBrowserToolLifecycle('start', 'session-browser', 'default', {
        name: 'browser_click',
        tool_id: 'click-1'
      })
    ).toBe(true)
    expect($taskTabBindings.get()['session-browser']).toEqual(binding)
    expect($browserSupervision.get()['session-browser'].operation).toBe('action')

    expect(
      applyBrowserToolLifecycle('complete', 'session-browser', 'default', {
        name: 'browser_click',
        tool_id: 'click-1'
      })
    ).toBe(true)
    expect($browserSupervision.get()['session-browser'].operation).toBe('idle')
  })

  it('fails closed when a same-valued session id arrives from another profile', () => {
    applyBrowserToolLifecycle('start', 'shared-session', 'coding', {
      name: 'browser_snapshot',
      tool_id: 'coding-tool'
    })
    const before = $browserSupervision.get()['shared-session']

    expect(
      applyBrowserToolLifecycle('start', 'shared-session', 'default', {
        name: 'browser_navigate',
        tool_id: 'default-tool'
      })
    ).toBe(false)
    expect($browserSupervision.get()['shared-session']).toEqual(before)
    expect($browserTabs.get()).toHaveLength(1)
  })

  it('ignores non-browser tool events without creating authority', () => {
    expect(
      applyBrowserToolLifecycle('start', 'session-terminal', 'default', {
        name: 'terminal',
        tool_id: 'terminal-1'
      })
    ).toBe(false)
    expect($browserTabs.get()).toEqual([])
    expect($taskTabBindings.get()).toEqual({})
    expect($browserSupervision.get()).toEqual({})
  })
})
