import { type ReactNode, useState } from 'react'

import { useI18n } from '@/i18n'
import { localCarrySettings } from '@/i18n/local-carry'
import { ChevronDown } from '@/lib/icons'
import { cn } from '@/lib/utils'

export function LocalEnhancementsSection({ children }: { children: ReactNode }) {
  const { locale } = useI18n()
  const copy = localCarrySettings.localEnhancements[locale] ?? localCarrySettings.localEnhancements.en
  const [open, setOpen] = useState(true)

  return (
    <section className="mt-6 border-t border-(--ui-stroke-tertiary) pt-3" data-feature-id="desktop.local-enhancements-settings">
      <button
        aria-expanded={open}
        className="flex w-full items-start justify-between gap-3 py-2 text-left"
        onClick={() => setOpen(value => !value)}
        type="button"
      >
        <span>
          <span className="block text-[length:var(--conversation-text-font-size)] font-medium text-foreground">
            {copy.title}
          </span>
          <span className="mt-1 block text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
            {copy.description}
          </span>
        </span>
        <ChevronDown className={cn('mt-0.5 size-4 shrink-0 text-(--ui-text-tertiary) transition-transform', open && 'rotate-180')} />
      </button>
      {open && <div className="mt-1">{children}</div>}
    </section>
  )
}
