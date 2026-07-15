import type { Codec } from '@/lib/persisted'
import { persistentAtom } from '@/lib/persisted'

const KEY = 'hermes.desktop.chatWidth.v1'

export const CHAT_WIDTHS = ['normal', 'wide', 'full'] as const
export type ChatWidth = (typeof CHAT_WIDTHS)[number]

const WIDTH_VALUES: Record<ChatWidth, string> = {
  full: '100%',
  normal: '48.75rem',
  wide: '68rem'
}

const codec: Codec<ChatWidth> = {
  decode: raw => (CHAT_WIDTHS.includes(raw as ChatWidth) ? (raw as ChatWidth) : 'normal'),
  encode: value => value
}

export const $chatWidth = persistentAtom<ChatWidth>(KEY, 'normal', codec)

export function setChatWidth(width: ChatWidth): void {
  $chatWidth.set(width)
}

if (typeof document !== 'undefined') {
  $chatWidth.subscribe(width => {
    document.documentElement.style.setProperty('--composer-width', WIDTH_VALUES[width])
  })
}
