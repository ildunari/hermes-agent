import { beforeEach, describe, expect, it, vi } from 'vitest'

const KEY = 'hermes.desktop.chatWidth.v1'

async function loadStore() {
  vi.resetModules()

  return import('./chat-width')
}

describe('chat width store', () => {
  beforeEach(() => {
    window.localStorage.clear()
    document.documentElement.style.removeProperty('--composer-width')
  })

  it('defaults to the existing normal conversation width', async () => {
    const { $chatWidth } = await loadStore()

    expect($chatWidth.get()).toBe('normal')
    expect(document.documentElement.style.getPropertyValue('--composer-width')).toBe('48.75rem')
  })

  it('hydrates a persisted width and applies it before the app renders', async () => {
    window.localStorage.setItem(KEY, 'wide')

    const { $chatWidth } = await loadStore()

    expect($chatWidth.get()).toBe('wide')
    expect(document.documentElement.style.getPropertyValue('--composer-width')).toBe('68rem')
  })

  it('persists full width as the available chat container width', async () => {
    const { setChatWidth } = await loadStore()

    setChatWidth('full')

    expect(window.localStorage.getItem(KEY)).toBe('full')
    expect(document.documentElement.style.getPropertyValue('--composer-width')).toBe('100%')
  })

  it('falls back safely when the persisted value is unknown', async () => {
    window.localStorage.setItem(KEY, 'enormous')

    const { $chatWidth } = await loadStore()

    expect($chatWidth.get()).toBe('normal')
    expect(document.documentElement.style.getPropertyValue('--composer-width')).toBe('48.75rem')
  })
})
