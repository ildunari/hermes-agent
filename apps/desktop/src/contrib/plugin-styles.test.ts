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

  it.each([
    '@import url(https://example.test/x.css);',
    '@font-face { font-family: stolen; src: url(https://example.test/font); }',
    '@keyframes host-animation { from { opacity: 0 } to { opacity: 1 } }',
    '@property --host-token { syntax: "<color>"; inherits: true; initial-value: red; }'
  ])('rejects stylesheet-global rule %s', css => {
    const styles = createPluginStyles('demo', dispose => dispose)
    expect(() => styles.add('bad', css)).toThrow(/stylesheet-global/)
    expect(document.head.querySelector('style[data-hermes-plugin-style="demo:bad"]')).toBeNull()
  })

  it('rejects CSS that tries to close the host scope', () => {
    const styles = createPluginStyles('demo', dispose => dispose)
    expect(() => styles.add('escape', '} body { display: none }')).toThrow(/host scope/)
    expect(document.head.querySelector('style[data-hermes-plugin-style="demo:escape"]')).toBeNull()
  })
})
