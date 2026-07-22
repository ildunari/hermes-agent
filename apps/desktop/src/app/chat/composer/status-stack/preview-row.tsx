import { useStore } from '@nanostores/react'
import { memo, useState } from 'react'

import {
  browserTargetLocation,
  captureBrowserIntentScope,
  openExplicitBrowserIntent,
  openExplicitBrowserResourceIntent
} from '@/app/browser/browser-intent-production'
import { StatusRow } from '@/components/chat/status-row'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { normalizeOrLocalPreviewTarget } from '@/lib/local-preview'
import { cn } from '@/lib/utils'
import { PREVIEW_PANE_ID } from '@/store/layout'
import { notifyError } from '@/store/notifications'
import { $paneOpen } from '@/store/panes'
import { $previewTarget, dismissPreviewTarget, setCurrentSessionPreviewTarget } from '@/store/preview'
import { type PreviewArtifact } from '@/store/preview-status'

interface PreviewStatusRowProps {
  item: PreviewArtifact
  onDismiss: (id: string) => void
}

/** One detected artifact, single line, always visible: filename + open + close. */
export const PreviewStatusRow = memo(function PreviewStatusRow({ item, onDismiss }: PreviewStatusRowProps) {
  const { t } = useI18n()
  const activePreview = useStore($previewTarget)
  const previewPaneOpen = useStore($paneOpen(PREVIEW_PANE_ID))
  const [opening, setOpening] = useState(false)
  const isOpen = activePreview?.source === item.target && previewPaneOpen

  const resolveTarget = async () => {
    const target = await normalizeOrLocalPreviewTarget(item.target, item.cwd || undefined)

    if (!target) {
      throw new Error(`Could not open preview target: ${item.target}`)
    }

    return target
  }

  const togglePreview = async () => {
    if (opening) {
      return
    }

    if (isOpen) {
      dismissPreviewTarget()

      return
    }

    setOpening(true)

    try {
      setCurrentSessionPreviewTarget(await resolveTarget(), 'tool-result', item.target)
    } catch (error) {
      notifyError(error, t.preview.unavailable)
    } finally {
      setOpening(false)
    }
  }

  const openInBrowser = async () => {
    const scopeSnapshot = captureBrowserIntentScope()

    try {
      const target = await resolveTarget()

      if (target.kind === 'file') {
        openExplicitBrowserResourceIntent({
          kind: 'artifact',
          scopeSnapshot,
          sourceSessionId: scopeSnapshot.workspaceId,
          target: target.path || item.target
        })

        return
      }

      if (browserTargetLocation(target.url) === 'studio-loopback') {
        openExplicitBrowserResourceIntent({
          kind: 'preview',
          scopeSnapshot,
          sourceSessionId: scopeSnapshot.workspaceId,
          target: target.url
        })

        return
      }

      openExplicitBrowserIntent({
        scopeSnapshot,
        source: 'transcript-link',
        target: 'preview',
        targetRef: target.url
      })
    } catch (error) {
      notifyError(error, t.preview.unavailable)
    }
  }

  return (
    <StatusRow
      leading={
        <Codicon
          aria-hidden
          className={cn('text-muted-foreground/70', opening && 'animate-pulse')}
          name="globe"
          size="0.8rem"
        />
      }
      // Plain click opens the link in the browser; ⌘/Ctrl-click opens it in the
      // in-app preview pane instead. (isOpen still toggles the pane closed.)
      onActivate={event => {
        if (event.metaKey || event.ctrlKey) {
          void togglePreview()
        } else {
          void openInBrowser()
        }
      }}
      trailing={
        <Tip label={t.statusStack.dismiss}>
          <Button
            aria-label={t.statusStack.dismiss}
            className="-my-1 size-4 rounded-md text-muted-foreground/60 hover:text-foreground/90"
            onClick={event => {
              event.stopPropagation()
              onDismiss(item.id)
            }}
            size="icon-xs"
            type="button"
            variant="ghost"
          >
            <Codicon name="close" size="0.75rem" />
          </Button>
        </Tip>
      }
      trailingVisible
    >
      <Tip
        label={
          // inline-flex (not flex): a block child collapses Tip's decoration
          // wrapper geometry and mis-positions the tooltip (#62022).
          <span className="inline-flex flex-col gap-0.5">
            <span>{item.target}</span>
            <span className="opacity-70">{t.preview.linkHint}</span>
          </span>
        }
      >
        <span className="min-w-0 max-w-[18rem] truncate text-[0.73rem] leading-4 text-foreground/92">{item.label}</span>
      </Tip>
    </StatusRow>
  )
})
