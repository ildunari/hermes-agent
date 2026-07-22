import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $foregroundBrowserTabId,
  bindAutomationTask,
  clearBrowserTabs,
  createBrowserTab,
  reconstructBrowserTab,
  unbindAutomationTask
} from './browser-store'
import {
  $browserSupervision,
  $browserTimeline,
  beginBrowserHandBack,
  clearBrowserSupervision,
  completeBrowserHandBack,
  requestLocalBrowserControl,
  stopSupervisedBrowser,
  superviseBrowserTask
} from './browser-supervision'

const geometry = { height: 600, width: 900, x: 0, y: 0 }

function setupTask() {
  const controlled = createBrowserTab({ geometry, profile: 'default', url: 'https://controlled.test', workspaceId: 'w' })
  const foreground = createBrowserTab({ foreground: true, geometry, profile: 'default', url: 'https://foreground.test', workspaceId: 'w' })
  const binding = bindAutomationTask('task-supervision', controlled.id)

  const record = superviseBrowserTask(binding, {
    operation: 'snapshot',
    ownerId: 'owner-opaque',
    profile: 'default',
    sessionId: 'session-opaque'
  })

  const native = {
    guestGeneration: 'guest-opaque',
    tabId: controlled.id,
    taskGeneration: binding.generation,
    taskId: binding.taskId
  }

  return { binding, controlled, foreground, native, record }
}

describe('browser supervision authority', () => {
  beforeEach(() => {
    clearBrowserTabs()
    clearBrowserSupervision()
  })
  afterEach(() => {
    clearBrowserTabs()
    clearBrowserSupervision()
    vi.useRealTimers()
  })

  it('acknowledges local control only after exact native retirement without stealing focus', async () => {
    const { foreground, native } = setupTask()
    let resolve!: (value: { ok: boolean; retired: boolean }) => void
    const revoke = vi.fn(() => new Promise<{ ok: boolean; retired: boolean }>(done => (resolve = done)))
    const pending = requestLocalBrowserControl(native, 'local-takeover', revoke)

    expect($browserSupervision.get()[native.taskId].state).toBe('agent')
    expect($foregroundBrowserTabId.get()).toBe(foreground.id)

    resolve({ ok: true, retired: true })
    await expect(pending).resolves.toBe(true)
    expect($browserSupervision.get()[native.taskId].state).toBe('local-takeover')
    expect($foregroundBrowserTabId.get()).toBe(foreground.id)
  })

  it('cannot apply a stale acknowledgement to a successor generation', async () => {
    const { controlled, native } = setupTask()
    let resolve!: (value: { ok: boolean; retired: boolean }) => void

    const pending = requestLocalBrowserControl(
      native,
      'paused',
      () => new Promise<{ ok: boolean; retired: boolean }>(done => (resolve = done))
    )

    unbindAutomationTask(native.taskId, native.taskGeneration)
    const successor = bindAutomationTask(native.taskId, controlled.id)
    superviseBrowserTask(successor, {
      operation: 'navigate',
      ownerId: 'owner-2',
      profile: 'default',
      sessionId: 'session-2'
    })
    resolve({ ok: true, retired: true })

    await expect(pending).resolves.toBe(false)
    expect($browserSupervision.get()[native.taskId]).toMatchObject({ generation: successor.generation, state: 'agent' })
  })

  it('rejects out-of-order supervision and promotes an acknowledged pause to takeover idempotently', async () => {
    const { binding, native } = setupTask()
    await requestLocalBrowserControl(native, 'paused', async () => ({ ok: true, retired: true }))
    const revoke = vi.fn(async () => ({ ok: true, retired: false }))

    await expect(requestLocalBrowserControl(native, 'local-takeover', revoke)).resolves.toBe(true)
    expect(revoke).not.toHaveBeenCalled()
    expect($browserSupervision.get()[native.taskId].state).toBe('local-takeover')
    expect(
      superviseBrowserTask({ ...binding, generation: binding.generation - 1 }, {
        operation: 'navigate', ownerId: 'stale', profile: 'stale', sessionId: 'stale'
      })
    ).not.toMatchObject({ ownerId: 'stale' })
  })

  it('requires a fresh reconstructed surface and successful exact bind before hand-back', async () => {
    const { controlled, native } = setupTask()
    await requestLocalBrowserControl(native, 'paused', async () => ({ ok: true, retired: true }))
    const reconstructed = reconstructBrowserTab(controlled.id)!

    expect(
      beginBrowserHandBack(native.taskId, native.taskGeneration, () => ({
        binding: reconstructed.binding!,
        surfaceEpoch: reconstructed.tab.surfaceEpoch
      }))
    ).toBe(true)
    expect($browserSupervision.get()[native.taskId].state).toBe('handing-back')
    expect(completeBrowserHandBack(native.taskId, reconstructed.binding!.generation, reconstructed.tab.id, 'stale')).toBe(false)
    expect(
      completeBrowserHandBack(
        native.taskId,
        reconstructed.binding!.generation,
        reconstructed.tab.id,
        reconstructed.tab.surfaceEpoch
      )
    ).toBe(true)
    expect($browserSupervision.get()[native.taskId].state).toBe('agent')
  })

  it('bounds the redacted timeline to enum, time, generation, and opaque identifiers', async () => {
    vi.useFakeTimers()
    const { native } = setupTask()

    for (let index = 0; index < 205; index += 1) {
      const generation = native.taskGeneration + index
      superviseBrowserTask({ generation, tabId: native.tabId as `browser:${string}`, taskId: native.taskId }, {
        operation: 'idle', ownerId: 'owner', profile: 'default', sessionId: 'session'
      })
      await requestLocalBrowserControl(
        { ...native, taskGeneration: generation },
        'paused',
        async () => ({ ok: true, retired: true })
      )
    }

    expect($browserTimeline.get()).toHaveLength(200)
    expect(Object.keys($browserTimeline.get()[0]).sort()).toEqual([
      'at', 'generation', 'id', 'reason', 'state', 'tabId', 'taskId'
    ])
    expect(JSON.stringify($browserTimeline.get())).not.toContain('https://')
  })

  it('removes only the exact record after stop acknowledgement', async () => {
    const { native } = setupTask()
    const stop = vi.fn(async () => ({ ok: true, retired: true }))

    await expect(stopSupervisedBrowser(native, stop)).resolves.toBe(true)
    expect($browserSupervision.get()[native.taskId]).toBeUndefined()
    expect(stop).toHaveBeenCalledOnce()
  })
})
