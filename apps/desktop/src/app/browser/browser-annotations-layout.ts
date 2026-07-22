import { atom } from 'nanostores'

const STORAGE_KEY = 'hermes.browser.annotations.layout.v1'
export const BROWSER_ANNOTATIONS_DEFAULT_WIDTH = 288
export const BROWSER_ANNOTATIONS_MIN_WIDTH = 180
export const BROWSER_ANNOTATIONS_MAX_WIDTH = 640

const browserAnnotationsOpen = atom(false)
const browserAnnotationsWidth = atom(BROWSER_ANNOTATIONS_DEFAULT_WIDTH)
let hydrated = false

export const $browserAnnotationsOpen = browserAnnotationsOpen
export const $browserAnnotationsWidth = browserAnnotationsWidth

function clampWidth(width: number): number {
  if (!Number.isFinite(width)) {return BROWSER_ANNOTATIONS_DEFAULT_WIDTH}
  return Math.min(BROWSER_ANNOTATIONS_MAX_WIDTH, Math.max(BROWSER_ANNOTATIONS_MIN_WIDTH, Math.round(width)))
}

function persist(): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({
      open: $browserAnnotationsOpen.get(),
      width: $browserAnnotationsWidth.get()
    }))
  } catch {
    // Layout persistence is best effort; browser operation must not depend on it.
  }
}

export function hydrateBrowserAnnotationsLayout(): void {
  if (hydrated) {return}
  hydrated = true

  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) {return}
    const value = JSON.parse(raw) as { open?: unknown; width?: unknown }
    if (typeof value.open === 'boolean') {$browserAnnotationsOpen.set(value.open)}
    if (typeof value.width === 'number') {$browserAnnotationsWidth.set(clampWidth(value.width))}
  } catch {
    // Ignore unavailable storage and malformed legacy values.
  }
}

export function setBrowserAnnotationsOpen(open: boolean): void {
  if ($browserAnnotationsOpen.get() !== open) {$browserAnnotationsOpen.set(open)}
  persist()
}

export function toggleBrowserAnnotations(): void {
  setBrowserAnnotationsOpen(!$browserAnnotationsOpen.get())
}

export function setBrowserAnnotationsWidth(width: number): void {
  const next = clampWidth(width)
  if ($browserAnnotationsWidth.get() !== next) {$browserAnnotationsWidth.set(next)}
  persist()
}

export function __resetBrowserAnnotationsLayoutForTests(): void {
  hydrated = false
  $browserAnnotationsOpen.set(false)
  $browserAnnotationsWidth.set(BROWSER_ANNOTATIONS_DEFAULT_WIDTH)
}
