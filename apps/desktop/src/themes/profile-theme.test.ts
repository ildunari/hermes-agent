import { beforeEach, describe, expect, it } from 'vitest'

import { modePref, skinPref } from './context'
import { DEFAULT_SKIN_NAME } from './presets'

// Skin and mode share one per-profile contract, so assert it once over both.
interface Pref {
  resolve: (profile: string) => string
  assign: (profile: string, value: string) => void
}

const cases = [
  {
    name: 'skin',
    pref: skinPref as unknown as Pref,
    fallback: DEFAULT_SKIN_NAME,
    a: 'ember',
    b: 'midnight',
    junk: 'nope',
    preserveUnknown: true
  },
  {
    name: 'mode',
    pref: modePref as unknown as Pref,
    fallback: 'light',
    a: 'dark',
    b: 'system',
    junk: 'dusk',
    preserveUnknown: false
  }
]

describe.each(cases)('per-profile $name', ({ pref, fallback, a, b, junk, preserveUnknown }) => {
  beforeEach(() => window.localStorage.clear())

  it('falls back to the default when unassigned', () => {
    expect(pref.resolve('default')).toBe(fallback)
    expect(pref.resolve('work')).toBe(fallback)
  })

  it('keeps each profile on its own value', () => {
    pref.assign('work', a)
    pref.assign('default', b)
    expect(pref.resolve('work')).toBe(a)
    expect(pref.resolve('default')).toBe(b)
  })

  it('lets unassigned profiles inherit the default profile as the global fallback', () => {
    pref.assign('default', a)
    expect(pref.resolve('never-themed')).toBe(a)
  })

  it('preserves or rejects unknown stored values according to the preference contract', () => {
    pref.assign('work', junk)
    expect(pref.resolve('work')).toBe(preserveUnknown ? junk : fallback)
  })
})
