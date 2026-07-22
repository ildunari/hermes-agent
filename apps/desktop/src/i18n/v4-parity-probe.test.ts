import { describe, expect, it } from 'vitest'

import { en } from './en'
import { ja } from './ja'
import { zh } from './zh'
import { zhHant } from './zh-hant'

const LOCALES = { ja, zh, 'zh-hant': zhHant } as const

// Browser-facing top-level catalog sections that must have full, translated parity.
const BROWSER_SECTIONS = [
  'browserSupervision',
  'browserAnnotations',
  'browserConsent',
  'browserUpload'
] as const

type AnyRecord = Record<string, unknown>

function collectStringLeaves(node: unknown, path: string, out: Map<string, string>): void {
  if (typeof node === 'string') {
    out.set(path, node)
    return
  }
  if (typeof node === 'function' || node === null || typeof node !== 'object') {
    return
  }
  for (const [key, value] of Object.entries(node as AnyRecord)) {
    collectStringLeaves(value, path ? `${path}.${key}` : key, out)
  }
}

describe('V4 browser/consent/supervision locale parity', () => {
  for (const section of BROWSER_SECTIONS) {
    const enLeaves = new Map<string, string>()
    collectStringLeaves((en as unknown as AnyRecord)[section], section, enLeaves)

    for (const [localeName, catalog] of Object.entries(LOCALES)) {
      it(`${localeName}: ${section} has every en key and no English leak`, () => {
        const localeLeaves = new Map<string, string>()
        collectStringLeaves((catalog as unknown as AnyRecord)[section], section, localeLeaves)

        const missing: string[] = []
        const identical: string[] = []
        for (const [path, enValue] of enLeaves) {
          if (!localeLeaves.has(path)) {
            missing.push(path)
            continue
          }
          const localeValue = localeLeaves.get(path)!
          // Allow identical only for non-alphabetic tokens (units, symbols, mono ids).
          if (localeValue === enValue && /[A-Za-z]{4,}/.test(enValue)) {
            identical.push(`${path} = "${enValue}"`)
          }
        }

        expect({ missing, identical }).toEqual({ missing: [], identical: [] })
      })
    }
  }

  // The browser settings subtree lives under settings.browser.
  for (const [localeName, catalog] of Object.entries(LOCALES)) {
    it(`${localeName}: settings.browser has every en key translated`, () => {
      const enLeaves = new Map<string, string>()
      collectStringLeaves(((en as unknown as AnyRecord).settings as AnyRecord).browser, 'settings.browser', enLeaves)
      const localeLeaves = new Map<string, string>()
      collectStringLeaves(((catalog as unknown as AnyRecord).settings as AnyRecord).browser, 'settings.browser', localeLeaves)

      const missing: string[] = []
      const identical: string[] = []
      for (const [path, enValue] of enLeaves) {
        if (!localeLeaves.has(path)) {
          missing.push(path)
          continue
        }
        if (localeLeaves.get(path) === enValue && /[A-Za-z]{4,}/.test(enValue)) {
          identical.push(`${path} = "${enValue}"`)
        }
      }
      expect({ missing, identical }).toEqual({ missing: [], identical: [] })
    })
  }
})
