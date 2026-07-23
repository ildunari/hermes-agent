import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $browserTabs,
  $foregroundBrowserTabId,
  $taskTabBindings,
  bindAutomationTask,
  clearBrowserTabs,
  closeBrowserTab,
  createBrowserTab,
  markBrowserTabRecoveryStable,
  reconstructBrowserTab,
  reconstructBrowserTabForHandBack,
  resolveAutomationTask,
  restoreBrowserTabs,
  retryBrowserTabRecovery,
  selectBrowserTab,
  setBrowserTabGeometry,
  shouldInitiallyOpenBrowserPane,
  unbindAutomationTask
} from './browser-store'

const geometry = { height: 600, width: 900, x: 10, y: 20 }

describe('Browser Dev bootstrap', () => {
  it('opens the pane only for the explicit development target', () => {
    expect(shouldInitiallyOpenBrowserPane(true, '1')).toBe(true)
    expect(shouldInitiallyOpenBrowserPane(true, undefined)).toBe(false)
    expect(shouldInitiallyOpenBrowserPane(false, '1')).toBe(false)
  })
})

describe('browser tab registry', () => {
  beforeEach(() => clearBrowserTabs())
  afterEach(() => clearBrowserTabs())

  it('owns foreground selection and geometry without replacing tabs on no-op updates', () => {
    const first = createBrowserTab({ foreground: true, geometry, profile: '', url: 'https://one.test', workspaceId: 'w' })
    const second = createBrowserTab({ geometry, profile: ' coding ', url: 'https://two.test', workspaceId: 'w' })
    const registry = $browserTabs.get()

    expect(first.profile).toBe('default')
    expect(second.profile).toBe('coding')

    setBrowserTabGeometry(first.id, geometry)
    expect($browserTabs.get()).toBe(registry)

    setBrowserTabGeometry(first.id, { ...geometry, width: 700 })
    expect($browserTabs.get().find(tab => tab.id === first.id)?.geometry.width).toBe(700)

    selectBrowserTab(second.id)
    expect($browserTabs.get()).toHaveLength(2)
  })

  it('binds automation explicitly with exclusive, monotonic generations independent of selection', () => {
    const first = createBrowserTab({ foreground: true, geometry, profile: 'default', url: 'https://one.test', workspaceId: 'w' })
    const second = createBrowserTab({ geometry, profile: 'default', url: 'https://two.test', workspaceId: 'w' })
    const initial = bindAutomationTask('task-binding-test', first.id)

    selectBrowserTab(second.id)

    expect($taskTabBindings.get()['task-binding-test']).toEqual(initial)
    expect(resolveAutomationTask('task-binding-test', initial.generation)).toMatchObject({
      status: 'bound',
      tab: { id: first.id }
    })
    expect(() => bindAutomationTask('other-task-binding-test', first.id)).toThrow('already bound')

    const next = bindAutomationTask('task-binding-test', second.id)

    expect(next.generation).toBe(initial.generation + 1)
    expect(resolveAutomationTask('task-binding-test', initial.generation)).toEqual({
      currentGeneration: next.generation,
      status: 'stale'
    })
    expect(unbindAutomationTask('task-binding-test', initial.generation)).toBe(false)
    expect(unbindAutomationTask('task-binding-test', next.generation)).toBe(true)
    expect(resolveAutomationTask('task-binding-test', next.generation)).toEqual({
      currentGeneration: next.generation,
      status: 'stale'
    })
  })

  it('retires a closed tab binding instead of retargeting it to the foreground', () => {
    const bound = createBrowserTab({ geometry, profile: 'default', url: 'https://bound.test', workspaceId: 'w' })

    const foreground = createBrowserTab({
      foreground: true,
      geometry,
      profile: 'default',
      url: 'https://foreground.test',
      workspaceId: 'w'
    })

    const binding = bindAutomationTask('task-close-test', bound.id)

    closeBrowserTab(bound.id)

    expect($browserTabs.get().map(tab => tab.id)).toEqual([foreground.id])
    expect(resolveAutomationTask('task-close-test', binding.generation)).toMatchObject({ status: 'stale' })
  })

  it('reconstructs a retired guest with fresh surface, tab, and task identities', () => {
    const original = createBrowserTab({
      foreground: true,
      geometry,
      private: true,
      profile: 'default',
      url: 'https://recover.test',
      workspaceId: 'w'
    })

    const originalBinding = bindAutomationTask('task-reconstruct-test', original.id)
    const reconstructed = reconstructBrowserTab(original.id)

    expect(reconstructed).not.toBeNull()
    expect(reconstructed!.tab.id).not.toBe(original.id)
    expect(reconstructed!.tab.surfaceEpoch).not.toBe(original.surfaceEpoch)
    expect(reconstructed!.tab.privatePartition).not.toBe(original.privatePartition)
    expect(reconstructed!.binding).toEqual({
      generation: originalBinding.generation + 1,
      tabId: reconstructed!.tab.id,
      taskId: originalBinding.taskId
    })
    expect($foregroundBrowserTabId.get()).toBe(reconstructed!.tab.id)
    expect(resolveAutomationTask(originalBinding.taskId, originalBinding.generation)).toEqual({
      currentGeneration: originalBinding.generation + 1,
      status: 'stale'
    })
  })

  it('does not spend crash recovery attempts on intentional hand-back identities', () => {
    let tab = createBrowserTab({ geometry, profile: 'default', url: 'https://handoff.test', workspaceId: 'w' })

    bindAutomationTask('task-hand-back-test', tab.id)

    for (let handBack = 0; handBack < 5; handBack += 1) {
      const reconstructed = reconstructBrowserTabForHandBack(tab.id)

      expect(reconstructed).not.toBeNull()
      expect(reconstructed!.tab.recovery).toMatchObject({ attempts: 0, state: 'stable' })
      tab = reconstructed!.tab
    }
  })

  it('restores descriptors as fresh unbound live incarnations and never restores private state', () => {
    const oldTabId = 'browser:old-incarnation'
    const [restored] = restoreBrowserTabs(' coding ', [{
      createdAt: 1,
      ordinal: 0,
      pinned: false,
      restoreId: 'restore-stable',
      restoredFromTabId: oldTabId,
      title: 'Safe title',
      updatedAt: 2,
      url: 'https://restore.test/path',
      workspaceId: 'workspace-restore'
    }], 'restore-stable', geometry)

    expect(restored.id).not.toBe(oldTabId)
    expect(restored.restoreId).toBe('restore-stable')
    expect(restored.restoredFromTabId).toBe(oldTabId)
    expect(restored.profile).toBe('coding')
    expect(restored.private).toBe(false)
    expect(restored.privatePartition).toBeUndefined()
    expect($taskTabBindings.get()).toEqual({})
    expect($foregroundBrowserTabId.get()).toBe(restored.id)

    const privateTab = createBrowserTab({ geometry, private: true, profile: 'coding', url: 'https://private.test', workspaceId: 'private' })
    expect(privateTab.restoreId).toBeUndefined()
  })

  it('bounds automatic reconstruction and requires an explicit retry after repeated failure', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-07-18T00:00:00Z'))

    let tab = createBrowserTab({ geometry, profile: 'default', url: 'https://loop.test', workspaceId: 'w' })

    for (let attempt = 0; attempt < 3; attempt += 1) {
      const reconstructed = reconstructBrowserTab(tab.id)
      expect(reconstructed).not.toBeNull()
      tab = reconstructed!.tab
      vi.advanceTimersByTime(31_000)
    }

    expect(reconstructBrowserTab(tab.id)).toBeNull()
    expect($browserTabs.get()[0].recovery.state).toBe('failed')
    markBrowserTabRecoveryStable(tab.id)
    expect($browserTabs.get()[0].recovery.state).toBe('failed')

    const retried = retryBrowserTabRecovery(tab.id)
    expect(retried).not.toBeNull()
    expect(retried!.tab.recovery).toMatchObject({ attempts: 1, state: 'active' })
    vi.useRealTimers()
  })
})
