import { Codicon } from '@/components/ui/codicon'
import { useI18n } from '@/i18n'

const FALLBACK =
  'Draft only — press Send to queue it after the current run.'

export function DraftPendingNotice() {
  const { t } = useI18n()
  const notice =
    (t.composer as { draftPendingNotice?: string }).draftPendingNotice ?? FALLBACK

  return (
    <div
      className="flex items-center gap-1.5 rounded-lg border border-[color-mix(in_srgb,var(--dt-composer-ring)_26%,transparent)] bg-[color-mix(in_srgb,var(--dt-card)_70%,transparent)] px-2 py-1 text-[0.68rem] text-muted-foreground/88"
      data-testid="composer-draft-pending-notice"
    >
      <Codicon className="text-[color-mix(in_srgb,var(--dt-composer-ring)_75%,var(--muted-foreground))]" name="edit" size="0.72rem" />
      <span>{notice}</span>
    </div>
  )
}
