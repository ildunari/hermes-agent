import { describe, expect, it } from 'vitest'

import { en } from '@/i18n/en'
import { ja } from '@/i18n/ja'
import { zh } from '@/i18n/zh'
import { zhHant } from '@/i18n/zh-hant'

describe('browser activity trust-boundary copy', () => {
  it('ships local retention, non-audit, and no-undo meaning in every locale', () => {
    for (const locale of [en, ja, zh, zhHant]) {
      const copy = locale.browserSupervision

      expect(copy.activityTitle).toBeTruthy()
      expect(copy.activityRetention).toMatch(/7/)
      expect(copy.activityRetention).toMatch(/5[,.]?000/)
      expect(copy.activityUnavailable).toBeTruthy()
    }

    expect(en.browserSupervision.activityTitle).toBe('Recent activity on this Mac')
    expect(en.browserSupervision.activityRetention).toContain('Not an audit log')
    expect(en.browserSupervision.activityRetention).toContain('cannot undo actions on websites')
  })
})
