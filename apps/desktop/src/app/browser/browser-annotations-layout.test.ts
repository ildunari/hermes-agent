import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  $browserAnnotationsOpen,
  $browserAnnotationsWidth,
  __resetBrowserAnnotationsLayoutForTests,
  hydrateBrowserAnnotationsLayout,
  setBrowserAnnotationsOpen,
  setBrowserAnnotationsWidth
} from './browser-annotations-layout'

const STORAGE_KEY = 'hermes.browser.annotations.layout.v1'
let originalStorage: PropertyDescriptor | undefined
let values: Map<string, string>

describe('browser annotations layout persistence', () => {
  beforeEach(() => {
    originalStorage = Object.getOwnPropertyDescriptor(window, 'localStorage')
    values = new Map()
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: {
        getItem: (key: string) => values.get(key) ?? null,
        removeItem: (key: string) => values.delete(key),
        setItem: (key: string, value: string) => values.set(key, value)
      }
    })
    __resetBrowserAnnotationsLayoutForTests()
  })

  afterEach(() => {
    __resetBrowserAnnotationsLayoutForTests()
    if (originalStorage) {
      Object.defineProperty(window, 'localStorage', originalStorage)
    } else {
      Reflect.deleteProperty(window, 'localStorage')
    }
  })

  it('persists profile-independent visibility and width', () => {
    setBrowserAnnotationsOpen(true)
    setBrowserAnnotationsWidth(412)

    expect(JSON.parse(values.get(STORAGE_KEY)!)).toEqual({ open: true, width: 412 })
  })

  it('hydrates and clamps persisted layout values', () => {
    values.set(STORAGE_KEY, JSON.stringify({ open: true, width: 9999 }))
    hydrateBrowserAnnotationsLayout()

    expect($browserAnnotationsOpen.get()).toBe(true)
    expect($browserAnnotationsWidth.get()).toBe(640)
  })
})
