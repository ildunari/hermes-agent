import { en } from './en'
import { ja } from './ja'
import type { Locale, Translations } from './types'
import { zh } from './zh'
import { zhHant } from './zh-hant'
import { localCarryComposer } from './local-carry'

type ComposerWithLocal = Translations['composer'] & {
  draftPendingNotice: string
}

type TranslationsWithLocal = Omit<Translations, 'composer'> & {
  composer: ComposerWithLocal
}

function withLocalCarry(locale: Locale, base: Translations): TranslationsWithLocal {
  return {
    ...base,
    composer: {
      ...base.composer,
      draftPendingNotice: localCarryComposer.draftPendingNotice[locale],
    },
  }
}

export const TRANSLATIONS: Record<Locale, TranslationsWithLocal> = {
  en: withLocalCarry('en', en),
  zh: withLocalCarry('zh', zh),
  'zh-hant': withLocalCarry('zh-hant', zhHant),
  ja: withLocalCarry('ja', ja),
}
