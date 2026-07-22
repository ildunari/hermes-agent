import { useStore } from '@nanostores/react'
import { type FormEvent, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { Activity, ChevronLeft, ChevronRight, PanelLeftIcon, Plus, RefreshCw, X } from '@/lib/icons'
import { $activeProfile, normalizeProfileKey } from '@/store/profile'
import { $activeSessionId, $currentCwd, $selectedStoredSessionId } from '@/store/session'

import { $browserAnnotationsOpen, toggleBrowserAnnotations } from './browser-annotations-layout'
import {
  $browserPaneGeometry,
  $browserPaneOpen,
  $browserTabs,
  $foregroundBrowserTabId,
  type BrowserTab,
  closeBrowserTab,
  createBrowserTab,
  openBrowserPane,
  selectBrowserTab,
  setBrowserPaneGeometry,
  setBrowserTabGeometry,
  setBrowserTabUrl
} from './browser-store'
import { $browserSupervision } from './browser-supervision'
import { runBrowserNavigation } from './browser-webviews'

export const BROWSER_PANE_ID = 'browser'

const STATE_COPY = {
  agent: 'agent',
  'handing-back': 'handingBack',
  'local-takeover': 'localTakeover',
  paused: 'paused'
} as const

function workspaceId(): string {
  return (
    $selectedStoredSessionId.get()?.trim() ||
    $activeSessionId.get()?.trim() ||
    $currentCwd.get()?.trim() ||
    'desktop-window'
  )
}

function normalizedAddress(value: string): string | null {
  const trimmed = value.trim()

  if (!trimmed) {
    return null
  }

  try {
    const url = new URL(/^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`)

    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.toString() : null
  } catch {
    return null
  }
}

function tabLabel(tab: BrowserTab, fallback: string): string {
  if (tab.title?.trim()) {
    return tab.title.trim()
  }

  try {
    return new URL(tab.url).hostname || fallback
  } catch {
    return fallback
  }
}

export function BrowserPane() {
  const { t } = useI18n()
  const tabs = useStore($browserTabs)
  const foregroundTabId = useStore($foregroundBrowserTabId)
  const paneOpen = useStore($browserPaneOpen)
  const annotationsOpen = useStore($browserAnnotationsOpen)
  const supervision = useStore($browserSupervision)
  const foregroundTab = tabs.find(tab => tab.id === foregroundTabId) ?? null
  const control = foregroundTab
    ? Object.values(supervision).find(candidate => candidate.tabId === foregroundTab.id)
    : undefined
  const [address, setAddress] = useState(foregroundTab?.url ?? '')
  const [invalidAddress, setInvalidAddress] = useState(false)
  const viewportRef = useRef<HTMLDivElement | null>(null)

  const addTab = useCallback(() => {
    const geometry = $browserPaneGeometry.get()

    createBrowserTab({
      foreground: true,
      geometry,
      profile: normalizeProfileKey($activeProfile.get()),
      url: '',
      workspaceId: workspaceId()
    })
    openBrowserPane()
  }, [])

  useEffect(() => {
    if (paneOpen && tabs.length === 0) {
      addTab()
    } else if (paneOpen && tabs.length > 0 && !foregroundTabId) {
      selectBrowserTab(tabs[tabs.length - 1].id)
    }
  }, [addTab, foregroundTabId, paneOpen, tabs])

  useEffect(() => {
    setAddress(foregroundTab?.url ?? '')
    setInvalidAddress(false)
  }, [foregroundTab?.id, foregroundTab?.url])

  useLayoutEffect(() => {
    const viewport = viewportRef.current

    if (!viewport) {
      return
    }

    let frame = 0

    const measure = () => {
      frame = 0
      const rect = viewport.getBoundingClientRect()
      const geometry = {
        height: Math.max(0, Math.round(rect.height)),
        width: Math.max(0, Math.round(rect.width)),
        x: Math.round(rect.x),
        y: Math.round(rect.y)
      }

      setBrowserPaneGeometry(geometry)

      for (const tab of $browserTabs.get()) {
        setBrowserTabGeometry(tab.id, geometry)
      }
    }

    const scheduleMeasure = () => {
      if (!frame) {
        frame = window.requestAnimationFrame(measure)
      }
    }

    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(scheduleMeasure)

    observer?.observe(viewport)
    window.addEventListener('resize', scheduleMeasure)
    scheduleMeasure()

    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', scheduleMeasure)
      window.cancelAnimationFrame(frame)
    }
  }, [])

  const closeTab = (tab: BrowserTab) => {
    const index = tabs.findIndex(candidate => candidate.id === tab.id)
    const successor = tabs[index + 1] ?? tabs[index - 1]

    closeBrowserTab(tab.id)

    if (foregroundTabId === tab.id && successor) {
      selectBrowserTab(successor.id)
    }
  }

  const navigate = (event: FormEvent) => {
    event.preventDefault()
    const target = normalizedAddress(address)

    if (!target) {
      setInvalidAddress(true)

      return
    }

    setInvalidAddress(false)

    if (foregroundTab) {
      setBrowserTabUrl(foregroundTab.id, target)
    } else {
      const geometry = $browserPaneGeometry.get()

      createBrowserTab({
        foreground: true,
        geometry,
        profile: normalizeProfileKey($activeProfile.get()),
        url: target,
        workspaceId: workspaceId()
      })
    }
  }

  return (
    <section
      aria-label={t.browserPane.label}
      className="flex h-full min-h-0 w-full flex-col overflow-hidden bg-(--ui-chat-surface-background)"
      data-browser-pane
    >
      <div className="flex h-8 shrink-0 items-stretch border-b border-(--ui-stroke-tertiary) bg-(--ui-bg-chrome) px-1">
        <div aria-label={t.browserPane.tabs} className="flex min-w-0 flex-1 overflow-x-auto" role="tablist">
          {tabs.map(tab => (
            <div className="flex min-w-32 max-w-52 items-center border-r border-(--ui-stroke-tertiary)" key={tab.id}>
              <Button
                aria-selected={tab.id === foregroundTabId}
                className="min-w-0 flex-1 self-stretch justify-start truncate font-normal"
                onClick={() => selectBrowserTab(tab.id)}
                role="tab"
                size="sm"
                variant={tab.id === foregroundTabId ? 'secondary' : 'ghost'}
              >
                <span className="truncate">{tabLabel(tab, t.browserPane.newTab)}</span>
              </Button>
              <Tip label={t.browserPane.closeTab}>
                <Button
                  aria-label={t.browserPane.closeTab}
                  onClick={() => closeTab(tab)}
                  size="icon-xs"
                  variant="ghost"
                >
                  <X aria-hidden />
                </Button>
              </Tip>
            </div>
          ))}
        </div>
        <Tip label={t.browserPane.newTab}>
          <Button aria-label={t.browserPane.newTab} onClick={addTab} size="icon-xs" variant="ghost">
            <Plus aria-hidden />
          </Button>
        </Tip>
      </div>

      <div className="flex h-9 shrink-0 items-center gap-1 border-b border-(--ui-stroke-tertiary) px-1.5">
        <Tip label={t.browserPane.back}>
          <Button
            aria-label={t.browserPane.back}
            disabled={!foregroundTab}
            onClick={() => foregroundTab && runBrowserNavigation(foregroundTab.id, 'back')}
            size="icon-xs"
            variant="ghost"
          >
            <ChevronLeft aria-hidden />
          </Button>
        </Tip>
        <Tip label={t.browserPane.forward}>
          <Button
            aria-label={t.browserPane.forward}
            disabled={!foregroundTab}
            onClick={() => foregroundTab && runBrowserNavigation(foregroundTab.id, 'forward')}
            size="icon-xs"
            variant="ghost"
          >
            <ChevronRight aria-hidden />
          </Button>
        </Tip>
        <Tip label={t.browserPane.reload}>
          <Button
            aria-label={t.browserPane.reload}
            disabled={!foregroundTab}
            onClick={() => foregroundTab && runBrowserNavigation(foregroundTab.id, 'reload')}
            size="icon-xs"
            variant="ghost"
          >
            <RefreshCw aria-hidden />
          </Button>
        </Tip>
        <form className="min-w-0 flex-1" onSubmit={navigate}>
          <Input
            aria-invalid={invalidAddress || undefined}
            aria-label={t.browserPane.address}
            className="w-full"
            onChange={event => setAddress(event.target.value)}
            placeholder={invalidAddress ? t.browserPane.invalidAddress : t.browserPane.addressPlaceholder}
            size="xs"
            value={address}
          />
        </form>
        <Tip label={annotationsOpen ? t.browserAnnotations.hideLabel : t.browserAnnotations.showLabel}>
          <Button
            aria-label={annotationsOpen ? t.browserAnnotations.hideLabel : t.browserAnnotations.showLabel}
            aria-pressed={annotationsOpen}
            data-browser-annotations-toggle
            disabled={!foregroundTab || foregroundTab.private}
            onClick={toggleBrowserAnnotations}
            size="icon-xs"
            variant={annotationsOpen ? 'secondary' : 'ghost'}
          >
            <PanelLeftIcon aria-hidden />
          </Button>
        </Tip>
        <div className="flex shrink-0 items-center gap-1 px-1 text-[0.625rem] text-(--ui-text-tertiary)" role="status">
          <Activity aria-hidden className={control?.state === 'agent' ? 'text-(--ui-accent)' : undefined} />
          <span>{control ? t.browserSupervision.states[STATE_COPY[control.state]] : t.browserPane.ready}</span>
        </div>
      </div>

      <div
        className="relative min-h-0 flex-1 overflow-hidden bg-(--ui-chat-surface-background)"
        data-browser-pane-viewport
        ref={viewportRef}
      >
        {!foregroundTab ? (
          <div className="grid h-full place-items-center px-6 text-center text-xs text-(--ui-text-tertiary)">
            {t.browserPane.empty}
          </div>
        ) : null}
      </div>
    </section>
  )
}
