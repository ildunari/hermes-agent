import { describe, expect, it } from 'vitest'

import { createPluginStyles } from './plugin-styles'

describe('plugin scoped styles', () => {
  it('scopes, replaces, and removes a plugin style and root marker', () => {
    const disposers: Array<() => void> = []

    const styles = createPluginStyles('demo', dispose => {
      disposers.push(dispose)

      return dispose
    })

    const handle = styles.add('layout', ':scope { --demo-width: 1rem; }')
    const element = document.head.querySelector('style[data-hermes-plugin-style="demo:layout"]')

    expect(element?.textContent).toContain('@scope (:root[data-hermes-plugin-style-demo-layout-')
    expect(element?.textContent).toContain('--demo-width: 1rem')
    expect(
      [...document.documentElement.attributes].some(attr =>
        attr.name.startsWith('data-hermes-plugin-style-demo-layout-')
      )
    ).toBe(true)

    handle.replace(':scope { --demo-width: 2rem; }')
    expect(element?.textContent).toContain('--demo-width: 2rem')

    disposers.forEach(dispose => dispose())
    expect(document.head.contains(element)).toBe(false)
    expect(
      [...document.documentElement.attributes].some(attr =>
        attr.name.startsWith('data-hermes-plugin-style-demo-layout-')
      )
    ).toBe(false)
  })

  it('rejects stylesheet-global imports', () => {
    const styles = createPluginStyles('demo', dispose => dispose)
    expect(() => styles.add('bad', '@import url(https://example.test/x.css);')).toThrow(/cannot contain/)
    expect(document.head.querySelector('style[data-hermes-plugin-style="demo:bad"]')).toBeNull()
  })

  it('rejects CSS that tries to close the host scope', () => {
    const styles = createPluginStyles('demo', dispose => dispose)
    expect(() => styles.add('escape', '} body { display: none }')).toThrow(/host scope/)
    expect(document.head.querySelector('style[data-hermes-plugin-style="demo:escape"]')).toBeNull()
  })
})
