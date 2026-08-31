import { beforeEach, describe, expect, it } from 'vitest'

import { createPluginContext } from './plugin'

beforeEach(() => window.localStorage.clear())

const valid = (value: unknown): value is { mode: string } =>
  Boolean(value && typeof value === 'object' && typeof (value as { mode?: unknown }).mode === 'string')

describe('plugin versioned state', () => {
  it('is namespaced by plugin and survives a simulated relaunch', () => {
    const first = createPluginContext('alpha').state.create({
      defaultValue: { mode: 'fit' },
      key: 'layout',
      validate: valid,
      version: 1
    })

    first.set({ mode: 'scroll' })

    const relaunched = createPluginContext('alpha').state.create({
      defaultValue: { mode: 'fit' },
      key: 'layout',
      validate: valid,
      version: 1
    })

    const otherPlugin = createPluginContext('beta').state.create({
      defaultValue: { mode: 'fit' },
      key: 'layout',
      validate: valid,
      version: 1
    })

    expect(relaunched.get()).toEqual({ mode: 'scroll' })
    expect(otherPlugin.get()).toEqual({ mode: 'fit' })
    expect(window.localStorage.getItem('hermes.plugin.alpha.state.layout')).toContain('"version":1')
    expect(window.localStorage.getItem('hermes.plugin.beta.state.layout')).toBeNull()
  })

  it('migrates an older envelope and persists the new version', () => {
    window.localStorage.setItem(
      'hermes.plugin.alpha.state.layout',
      JSON.stringify({ value: { oldMode: 'wide' }, version: 1 })
    )

    const state = createPluginContext('alpha').state.create({
      defaultValue: { mode: 'fit' },
      key: 'layout',
      migrate: value => ({ mode: (value as { oldMode?: string }).oldMode || 'fit' }),
      validate: valid,
      version: 2
    })

    expect(state.get()).toEqual({ mode: 'wide' })
    expect(window.localStorage.getItem('hermes.plugin.alpha.state.layout')).toBe(
      JSON.stringify({ value: { mode: 'wide' }, version: 2 })
    )
  })

  it('notifies subscribers and stops after unsubscribe', () => {
    const state = createPluginContext('alpha').state.create({
      defaultValue: { mode: 'fit' },
      key: 'layout',
      validate: valid,
      version: 1
    })

    const seen: string[] = []
    const unsubscribe = state.subscribe(value => seen.push(value.mode))

    state.set({ mode: 'wide' })
    unsubscribe()
    state.set({ mode: 'full' })

    expect(seen).toEqual(['wide'])
  })
})
