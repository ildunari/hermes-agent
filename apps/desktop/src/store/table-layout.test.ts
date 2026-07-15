import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const KEY = 'hermes.desktop.tableLayout.v1'

const storageData = new Map<string, string>()
const localStorageStub: Storage = {
  clear: () => storageData.clear(),
  getItem: key => storageData.get(key) ?? null,
  key: index => [...storageData.keys()][index] ?? null,
  get length() {
    return storageData.size
  },
  removeItem: key => storageData.delete(key),
  setItem: (key, value) => storageData.set(key, value)
}

async function loadStore() {
  vi.resetModules()

  return import('./table-layout')
}

describe('Markdown table layout store', () => {
  beforeEach(() => {
    vi.stubGlobal('localStorage', localStorageStub)
    localStorageStub.clear()
    delete document.documentElement.dataset.hermesTableLayout
  })


  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('defaults to the current fit behavior', async () => {
    const { $tableLayout } = await loadStore()

    expect($tableLayout.get()).toBe('fit')
    expect(document.documentElement.dataset.hermesTableLayout).toBe('fit')
  })

  it('hydrates and applies the persisted scroll behavior', async () => {
    window.localStorage.setItem(KEY, 'scroll')

    const { $tableLayout } = await loadStore()

    expect($tableLayout.get()).toBe('scroll')
    expect(document.documentElement.dataset.hermesTableLayout).toBe('scroll')
  })

  it('persists layout changes', async () => {
    const { setTableLayout } = await loadStore()

    setTableLayout('scroll')

    expect(window.localStorage.getItem(KEY)).toBe('scroll')
    expect(document.documentElement.dataset.hermesTableLayout).toBe('scroll')
  })

  it('falls back safely when the persisted value is unknown', async () => {
    window.localStorage.setItem(KEY, 'overflow')

    const { $tableLayout } = await loadStore()

    expect($tableLayout.get()).toBe('fit')
    expect(document.documentElement.dataset.hermesTableLayout).toBe('fit')
  })
})
