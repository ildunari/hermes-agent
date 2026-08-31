import { describe, expect, it, vi } from 'vitest'

import { createPluginContext, disposePluginResources, type HermesPlugin, registerPluginResources } from './plugin'
import { registry } from './registry'

describe('plugin activation lifecycle', () => {
  it('rejects ids that could escape plugin storage and contribution namespaces', () => {
    expect(() => createPluginContext('alpha.state')).toThrow(/Invalid plugin id/)
    expect(() => createPluginContext('alpha:row')).toThrow(/Invalid plugin id/)
    expect(() => createPluginContext('alpha-plugin')).not.toThrow()
  })

  it('rolls back partial registration when plugin activation throws', () => {
    const plugin: HermesPlugin = {
      id: 'partial-failure',
      register(ctx) {
        ctx.register({ area: 'test.partial', id: 'row', render: () => null })
        ctx.styles.add('layout', ':scope { --partial-width: 10rem; }')
        throw new Error('activation failed')
      }
    }

    expect(() => registerPluginResources(plugin)).toThrow('activation failed')
    expect(registry.getArea('test.partial')).toEqual([])
    expect(document.head.querySelector('style[data-hermes-plugin-style="partial-failure:layout"]')).toBeNull()
  })

  it('runs all cleanup callbacks even when one cleanup throws', () => {
    const first = vi.fn()
    const last = vi.fn()

    const broken = vi.fn(() => {
      throw new Error('cleanup failed')
    })

    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const disposers = [first, broken, last]

    disposePluginResources(disposers)

    expect(first).toHaveBeenCalledOnce()
    expect(broken).toHaveBeenCalledOnce()
    expect(last).toHaveBeenCalledOnce()
    expect(disposers).toEqual([])
    expect(error).toHaveBeenCalledWith('[plugins] cleanup failed', expect.any(Error))
    error.mockRestore()
  })
})