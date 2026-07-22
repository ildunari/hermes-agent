import { useStore } from '@nanostores/react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { useI18n } from '@/i18n'
import { Activity, Pause, Play, SteeringWheel, StopFilled } from '@/lib/icons'

import { type BrowserTab, closeBrowserTab, reconstructBrowserTabForHandBack } from './browser-store'
import {
  $browserSupervision,
  $browserTimeline,
  $browserTimelineDegraded,
  beginBrowserHandBack,
  requestLocalBrowserControl,
  stopSupervisedBrowser
} from './browser-supervision'

interface BrowserControllerProps {
  guestGeneration: string | null
  tab: BrowserTab
}

const STATE_COPY = {
  agent: 'agent',
  'handing-back': 'handingBack',
  'local-takeover': 'localTakeover',
  paused: 'paused'
} as const

const OPERATION_COPY = {
  action: 'action',
  idle: 'idle',
  navigate: 'navigate',
  snapshot: 'snapshot'
} as const

export function BrowserController({ guestGeneration, tab }: BrowserControllerProps) {
  const supervision = useStore($browserSupervision)
  const timeline = useStore($browserTimeline)
  const timelineDegraded = useStore($browserTimelineDegraded)
  const { locale, t } = useI18n()
  const [busy, setBusy] = useState<'hand-back' | 'pause' | 'stop' | 'takeover' | null>(null)
  const [error, setError] = useState(false)
  const record = Object.values(supervision).find(candidate => candidate.tabId === tab.id)
  const copy = t.browserSupervision

  if (!record) {
    return (
      <div
        className="pointer-events-auto absolute right-2 top-2 z-20 rounded-md bg-(--ui-bg-elevated) p-1 shadow-nous ring-1 ring-(--stroke-nous) [-webkit-app-region:no-drag]"
        data-browser-user-controller
      >
        <Button aria-label={copy.stopAndClose} onClick={() => closeBrowserTab(tab.id)} size="icon-xs" variant="ghost">
          <StopFilled aria-hidden className="size-3" />
        </Button>
      </div>
    )
  }

  const exactRequest = guestGeneration
    ? {
        guestGeneration,
        tabId: record.tabId,
        taskGeneration: record.generation,
        taskId: record.taskId
      }
    : null

  const recentTimeline = timeline.filter(entry => entry.taskId === record.taskId).slice(-5)

  const run = async (action: NonNullable<typeof busy>, operation: () => boolean | Promise<boolean>) => {
    if (busy) {
      return
    }

    setBusy(action)
    setError(false)

    try {
      if (!(await operation())) {
        setError(true)
      }
    } catch {
      setError(true)
    } finally {
      setBusy(null)

      if (action !== 'stop') {
        window.requestAnimationFrame(() => {
          const controller = Array.from(window.document.querySelectorAll<HTMLElement>('[data-browser-controller]'))
            .find(candidate => candidate.dataset.browserController === record.taskId)

          const successor = action === 'pause' || action === 'takeover' ? 'hand-back' : 'stop'
          const target = controller?.querySelector<HTMLElement>(`[data-browser-action="${successor}"]`)
          const focusTarget = target ?? controller

          focusTarget?.focus()
        })
      }
    }
  }

  const requestControl = (state: 'local-takeover' | 'paused') => {
    if (!exactRequest) {
      setError(true)

      return
    }

    void run(state === 'paused' ? 'pause' : 'takeover', () =>
      requestLocalBrowserControl(exactRequest, state, window.hermesDesktop.browserGuest.revokeLocal)
    )
  }

  const handBack = () => {
    void run('hand-back', () =>
      beginBrowserHandBack(record.taskId, record.generation, () => {
        const reconstructed = reconstructBrowserTabForHandBack(record.tabId)

        return reconstructed?.binding
          ? { binding: reconstructed.binding, surfaceEpoch: reconstructed.tab.surfaceEpoch }
          : null
      })
    )
  }

  const stop = () => {
    if (!exactRequest) {
      setError(true)

      return
    }

    void run('stop', async () => {
      const stopped = await stopSupervisedBrowser(exactRequest, window.hermesDesktop.browserGuest.stopAndClose)

      if (stopped) {
        closeBrowserTab(record.tabId)
      }

      return stopped
    })
  }

  return (
    <section
      aria-label={copy.controllerLabel}
      className="pointer-events-auto absolute inset-x-2 top-2 z-20 max-w-[min(54rem,calc(100%-1rem))] bg-(--ui-bg-elevated) text-(--ui-text-primary) shadow-nous ring-1 ring-(--stroke-nous) [-webkit-app-region:no-drag]"
      data-browser-controller={record.taskId}
      tabIndex={-1}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-2 px-2 py-1.5">
        <span className="shrink-0 text-xs font-semibold">{copy.title}</span>
        <span
          className="shrink-0 rounded-[2px] bg-(--ui-bg-quaternary) px-1.5 py-0.5 text-[0.6875rem] font-medium"
          data-browser-control-state={record.state}
          role="status"
        >
          {copy.states[STATE_COPY[record.state]]}
        </span>
        <dl className="flex min-w-60 flex-1 flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[0.625rem] text-(--ui-text-tertiary)">
          <div className="flex min-w-0 gap-1"><dt>{copy.owner}</dt><dd className="break-all">{record.ownerId}</dd></div>
          <div className="flex min-w-0 gap-1"><dt>{copy.session}</dt><dd className="break-all">{record.sessionId}</dd></div>
          <div className="flex min-w-0 gap-1"><dt>{copy.profile}</dt><dd className="break-all">{record.profile}</dd></div>
          <div className="flex min-w-0 gap-1"><dt>{copy.tab}</dt><dd className="break-all">{record.tabId}</dd></div>
          <div className="flex shrink-0 gap-1"><dt>{copy.generation}</dt><dd>{record.generation}</dd></div>
          <div className="flex shrink-0 gap-1"><dt>{copy.operation}</dt><dd>{copy.operations[OPERATION_COPY[record.operation]]}</dd></div>
        </dl>
        <div className="flex shrink-0 items-center gap-1">
          {record.state === 'agent' ? (
            <>
              <Button
                aria-label={copy.pause}
                data-browser-action="pause"
                disabled={Boolean(busy) || !exactRequest}
                onClick={() => requestControl('paused')}
                size="sm"
                title={copy.pause}
                variant="ghost"
              ><Pause aria-hidden className="shrink-0" /><span>{copy.pause}</span></Button>
              <Button
                aria-label={copy.takeControl}
                data-browser-action="takeover"
                disabled={Boolean(busy) || !exactRequest}
                onClick={() => requestControl('local-takeover')}
                size="sm"
                title={copy.takeControl}
                variant="ghost"
              ><SteeringWheel aria-hidden className="shrink-0" /><span>{copy.takeControl}</span></Button>
            </>
          ) : record.state !== 'handing-back' ? (
            <>
              {record.state === 'paused' ? (
                <Button
                  aria-label={copy.takeControl}
                  data-browser-action="takeover"
                  disabled={Boolean(busy)}
                  onClick={() => requestControl('local-takeover')}
                  size="sm"
                  title={copy.takeControl}
                  variant="ghost"
                ><SteeringWheel aria-hidden className="shrink-0" /><span>{copy.takeControl}</span></Button>
              ) : null}
              <Button
                aria-label={copy.handBack}
                data-browser-action="hand-back"
                disabled={Boolean(busy)}
                onClick={handBack}
                size="sm"
                title={copy.handBack}
                variant="ghost"
              ><Play aria-hidden className="shrink-0" /><span>{copy.handBack}</span></Button>
            </>
          ) : null}
          <Button
            aria-label={copy.stopAndClose}
            data-browser-action="stop"
            disabled={Boolean(busy) || !exactRequest}
            onClick={stop}
            size="sm"
            title={copy.stopAndClose}
            variant="ghost"
          ><StopFilled aria-hidden className="shrink-0" /><span>{copy.stopAndClose}</span></Button>
        </div>
      </div>

      {(recentTimeline.length > 0 || error || timelineDegraded) && (
        <div className="flex flex-wrap items-center gap-2 border-t border-(--ui-stroke-tertiary) px-2 py-1 text-[0.625rem] text-(--ui-text-tertiary)">
          <Activity aria-hidden className="shrink-0" />
          <span className="max-w-96 shrink-0">
            <span className="block font-medium">{copy.activityTitle}</span>
            <span className="block">{copy.activityRetention}</span>
          </span>
          {timelineDegraded ? <span role="status">{copy.activityUnavailable}</span> : null}
          {error ? <span className="text-destructive" role="alert">{copy.controlFailed}</span> : null}
          <ol aria-label={copy.activity} className="flex min-w-0 flex-wrap gap-3">
            {recentTimeline.map(entry => (
              <li className="shrink-0" key={entry.id}>
                <time dateTime={new Date(entry.at).toISOString()}>{new Date(entry.at).toLocaleTimeString(locale === 'zh' ? 'zh-Hans' : locale === 'zh-hant' ? 'zh-Hant' : locale, { hour: '2-digit', minute: '2-digit' })}</time>
                {' · '}{copy.reasons[entry.reason === 'hand-back' ? 'handBack' : entry.reason]}
                {' · '}{copy.states[STATE_COPY[entry.state]]}
              </li>
            ))}
          </ol>
        </div>
      )}
    </section>
  )
}
