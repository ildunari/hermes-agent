/** Local carry-only desktop strings.
 *
 * Kept out of the large locale object literals so upstream i18n churn does not
 * conflict on every update. Merge via defineLocale / catalog, not by inserting
 * into en/ja/zh mid-literal.
 */
import type { Locale } from './types'
export const localCarryComposer = {
  draftPendingNotice: {
    en: 'Draft only — press Send to queue it after the current run.',
    ja: 'まだ下書きです — 送信すると現在の実行後にキューへ入ります。',
    zh: '仅为草稿——按发送可在当前运行后排队。',
    'zh-hant': '仍是草稿——按傳送可在目前執行後排隊。',
  },
} as const

export interface LocalCarryCopy {
  title: string
  description: string
}

/**
 * Typed wide (not `as const`) so indexing with the full `Locale` type stays
 * legal as upstream adds locales (2026-07-24: ar); callers fall back to en.
 */
export const localCarrySettings: {
  localEnhancements: { en: LocalCarryCopy } & Partial<Record<Locale, LocalCarryCopy>>
} = {
  localEnhancements: {
    en: {
      title: 'Local Enhancements',
      description: 'Features maintained in your Hermes Desktop build.'
    },
    ja: {
      title: 'ローカル拡張',
      description: 'この Hermes Desktop ビルドで管理されている機能です。'
    },
    zh: {
      title: '本地增强功能',
      description: '由你的 Hermes Desktop 版本维护的功能。'
    },
    'zh-hant': {
      title: '本機增強功能',
      description: '由你的 Hermes Desktop 版本維護的功能。'
    }
  }
}
