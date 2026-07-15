/** Local carry-only desktop strings.
 *
 * Kept out of the large locale object literals so upstream i18n churn does not
 * conflict on every update. Merge via defineLocale / catalog, not by inserting
 * into en/ja/zh mid-literal.
 */
export const localCarryComposer = {
  draftPendingNotice: {
    en: 'Draft only — press Send to queue it after the current run.',
    ja: 'まだ下書きです — 送信すると現在の実行後にキューへ入ります。',
    zh: '仅为草稿——按发送可在当前运行后排队。',
    'zh-hant': '仍是草稿——按傳送可在目前執行後排隊。',
  },
} as const
