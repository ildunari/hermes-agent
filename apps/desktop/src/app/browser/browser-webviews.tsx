import { useStore } from '@nanostores/react'
import { useEffect, useRef, useState } from 'react'

import { BrowserAnnotationsPanel } from './browser-annotations-panel'
import { BrowserConsentDialog } from './browser-consent-dialog'
import { BrowserController } from './browser-controller'
import { completeExplicitBrowserResourceIntent, failExplicitBrowserResourceIntent } from './browser-intent-production'
import { BROWSER_PARTITION } from './browser-partition'
import { BrowserPersistenceCoordinator } from './browser-persistence'
import {
  $browserPaneGeometry,
  $browserPaneOpen,
  $browserTabs,
  $foregroundBrowserTabId,
  $taskTabBindings,
  BROWSER_RECOVERY_STABLE_MS,
  type BrowserTab,
  closeBrowserTab,
  markBrowserTabRecoveryStable,
  reconstructBrowserTab,
  setBrowserTabTitle,
  setBrowserTabUrl
} from './browser-store'
import { hydrateBrowserTimeline } from './browser-supervision'
import { completeBrowserHandBack, isBrowserHandBackPending } from './browser-supervision'

type BrowserWebviewElement = HTMLElement & {
  canGoBack?: () => boolean
  canGoForward?: () => boolean
  getURL?: () => string
  goBack?: () => void
  goForward?: () => void
  reload?: () => void
}

export type BrowserNavigationAction = 'back' | 'forward' | 'reload'

export function runBrowserNavigation(tabId: BrowserTab['id'], action: BrowserNavigationAction): boolean {
  const webview = Array.from(document.querySelectorAll<BrowserWebviewElement>('[data-browser-tab-id]')).find(
    candidate => candidate.dataset.browserTabId === tabId
  )

  if (!webview) {
    return false
  }

  if (action === 'back') {
    if (webview.canGoBack?.() === false || !webview.goBack) {
      return false
    }

    webview.goBack()

    return true
  }

  if (action === 'forward') {
    if (webview.canGoForward?.() === false || !webview.goForward) {
      return false
    }

    webview.goForward()

    return true
  }

  if (!webview.reload) {
    return false
  }

  webview.reload()

  return true
}

interface BrowserWebviewProps {
  foreground: boolean
  tab: BrowserTab
}

function BrowserWebview({ foreground, tab }: BrowserWebviewProps) {
  const taskBindings = useStore($taskTabBindings)
  const taskBinding = Object.values(taskBindings).find(binding => binding.tabId === tab.id)
  const [guestGeneration, setGuestGeneration] = useState<string | null>(null)

  const activationRequestRef = useRef(0)

  const automationBindingRef = useRef<{
    guestGeneration: string
    taskGeneration: number
    taskId: string
  } | null>(null)

  const generationRef = useRef<string | null>(null)
  const initialTabRef = useRef(tab)
  const resourceRef = useRef(tab.resource)
  const hostRef = useRef<HTMLDivElement | null>(null)
  const lastActivatedUrlRef = useRef<string | null>(null)
  const latestUrlRef = useRef(tab.url)
  const recoveryPendingRef = useRef(tab.recovery.state === 'active')
  const webviewRef = useRef<BrowserWebviewElement | null>(null)

  latestUrlRef.current = tab.url

  useEffect(() => {
    const host = hostRef.current

    if (!host) {
      return
    }

    let disposed = false
    let reconstructionRequested = false
    let recoveryStableTimer: ReturnType<typeof setTimeout> | undefined
    let webview: BrowserWebviewElement | null = null

    const mount = async () => {
      await hydrateBrowserTimeline(tab.profile, tab.workspaceId)
      const partition = tab.privatePartition ?? BROWSER_PARTITION

      if (disposed) {
        return
      }

      const prepared = await window.hermesDesktop.browserGuest.prepare({
        partition,
        private: tab.private,
        profile: tab.profile,
        surfaceEpoch: tab.surfaceEpoch,
        tabId: tab.id,
        workspaceId: tab.workspaceId
      })

      if (!prepared.ok || !prepared.attachmentUrl || !prepared.generation) {
        return
      }

      if (disposed) {
        void window.hermesDesktop.browserGuest.release({ generation: prepared.generation, tabId: tab.id })

        return
      }

      generationRef.current = prepared.generation
      setGuestGeneration(prepared.generation)
      webview = document.createElement('webview') as BrowserWebviewElement
      webview.className = 'flex h-full w-full flex-1 bg-transparent'
      webview.setAttribute('data-browser-tab-id', tab.id)
      webview.setAttribute('partition', partition)
      webview.setAttribute('src', prepared.attachmentUrl)
      webview.setAttribute('webpreferences', 'contextIsolation=yes,nodeIntegration=no,sandbox=yes')

      const onNavigate = (event: Event) => {
        const detail = event as Event & { url?: string }
        const url = detail.url || webview?.getURL?.()

        if (url && !url.startsWith('about:blank#hermes-browser-attach=')) {
          lastActivatedUrlRef.current = url
          setBrowserTabUrl(tab.id, url)
        }
      }

      const onGuestRetired = (reason?: string) => {
        if (disposed || reconstructionRequested) {
          return
        }

        reconstructionRequested = true
        if (reason === 'profile-deleted' || reason === 'workspace-reset') {
          closeBrowserTab(tab.id)
        } else {
          reconstructBrowserTab(tab.id)
        }
      }

      const offRetired = window.hermesDesktop.browserGuest.onRetired(event => {
        if (event.tabId === tab.id && event.guestGeneration === generationRef.current) {
          onGuestRetired(event.reason)
        }
      })

      const offFreshSnapshot = window.hermesDesktop.browserGuest.onFreshSnapshot(event => {
        if (
          event.guestGeneration === generationRef.current &&
          event.tabId === tab.id &&
          event.surfaceEpoch === tab.surfaceEpoch
        ) {
          completeBrowserHandBack(event.taskId, event.taskGeneration, tab.id, tab.surfaceEpoch)
        }
      })

      const onTitle = (event: Event) => {
        const title = (event as Event & { title?: string }).title
        if (typeof title === 'string') {
          setBrowserTabTitle(tab.id, title)
        }
      }

      const onGuestFailure: EventListener = () => onGuestRetired()

      webview.addEventListener('did-navigate', onNavigate)
      webview.addEventListener('did-navigate-in-page', onNavigate)
      webview.addEventListener('page-title-updated', onTitle)
      webview.addEventListener('render-process-gone', onGuestFailure)
      webview.addEventListener('unresponsive', onGuestFailure)
      host.appendChild(webview)
      webviewRef.current = webview

      const cleanupMountListeners = () => {
        webview?.removeEventListener('did-navigate', onNavigate)
        webview?.removeEventListener('did-navigate-in-page', onNavigate)
        webview?.removeEventListener('page-title-updated', onTitle)
        webview?.removeEventListener('render-process-gone', onGuestFailure)
        webview?.removeEventListener('unresponsive', onGuestFailure)
        offRetired()
        offFreshSnapshot()
      }

      let activationUrl = latestUrlRef.current
      const resource = resourceRef.current

      if (resource) {
        const minted = await window.hermesDesktop.browserGuest.mintResource({
          generation: prepared.generation,
          kind: resource.kind,
          profile: tab.profile,
          sourceSessionId: resource.sourceSessionId,
          tabId: tab.id,
          target: resource.target,
          workspaceId: tab.workspaceId
        })

        if (!minted.ok || !minted.guestUrl || disposed) {
          cleanupMountListeners()
          if (!disposed) {
            failExplicitBrowserResourceIntent(tab, 'resource-grant-failed')
          }

          return
        }

        activationUrl = minted.guestUrl
        lastActivatedUrlRef.current = activationUrl
        latestUrlRef.current = activationUrl

        if (!completeExplicitBrowserResourceIntent(initialTabRef.current, activationUrl).applied) {
          cleanupMountListeners()

          return
        }
      }

      lastActivatedUrlRef.current = activationUrl
      const activationRequest = ++activationRequestRef.current

      const activated = activationUrl
        ? await window.hermesDesktop.browserGuest.activate({
            generation: prepared.generation,
            tabId: tab.id,
            url: activationUrl
          })
        : { ok: true }

      if (!activated.ok && !disposed && activationRequest === activationRequestRef.current) {
        webview.remove()
        webviewRef.current = null
      } else if (activated.ok && !disposed && recoveryPendingRef.current) {
        recoveryStableTimer = setTimeout(() => markBrowserTabRecoveryStable(tab.id), BROWSER_RECOVERY_STABLE_MS)
      }

      const currentTask = Object.values($taskTabBindings.get()).find(binding => binding.tabId === tab.id)

      if (activated.ok && currentTask && !disposed) {
        const request = {
          guestGeneration: prepared.generation,
          requireFreshSnapshot: isBrowserHandBackPending(
            currentTask.taskId,
            currentTask.generation,
            tab.id,
            tab.surfaceEpoch
          ),
          tabId: tab.id,
          taskGeneration: currentTask.generation,
          taskId: currentTask.taskId
        }

        const bound = await window.hermesDesktop.browserGuest.bindAutomation(request)
        const current = $taskTabBindings.get()[request.taskId]

        if (
          bound.ok &&
          !disposed &&
          current?.tabId === request.tabId &&
          current.generation === request.taskGeneration
        ) {
          automationBindingRef.current = request
        } else if (bound.ok) {
          void window.hermesDesktop.browserGuest.unbindAutomation(request)
        }
      }

      return cleanupMountListeners
    }

    let removeListeners: (() => void) | undefined
    void mount().then(remove => {
      if (disposed) {
        remove?.()
        webview?.remove()
      } else {
        removeListeners = remove
      }
    })

    return () => {
      disposed = true
      activationRequestRef.current += 1
      clearTimeout(recoveryStableTimer)
      removeListeners?.()
      webview?.remove()
      webviewRef.current = null
      const generation = generationRef.current
      generationRef.current = null
      const automation = automationBindingRef.current
      automationBindingRef.current = null
      const browserGuest = window.hermesDesktop?.browserGuest

      if (automation && browserGuest) {
        void browserGuest.unbindAutomation({ ...automation, tabId: tab.id })
      }

      if (generation && browserGuest) {
        void browserGuest.release({ generation, tabId: tab.id })
      }
    }
  }, [tab.id, tab.private, tab.privatePartition, tab.profile, tab.surfaceEpoch, tab.workspaceId])

  useEffect(() => {
    const guestGeneration = generationRef.current
    const previous = automationBindingRef.current

    if (
      previous &&
      (!taskBinding || previous.taskId !== taskBinding.taskId || previous.taskGeneration !== taskBinding.generation)
    ) {
      automationBindingRef.current = null
      void window.hermesDesktop.browserGuest.unbindAutomation({ ...previous, tabId: tab.id })
    }

    if (!guestGeneration || !taskBinding || automationBindingRef.current) {
      return
    }

    const request = {
      guestGeneration,
      requireFreshSnapshot: isBrowserHandBackPending(
        taskBinding.taskId,
        taskBinding.generation,
        tab.id,
        tab.surfaceEpoch
      ),
      tabId: tab.id,
      taskGeneration: taskBinding.generation,
      taskId: taskBinding.taskId
    }

    void window.hermesDesktop.browserGuest.bindAutomation(request).then(result => {
      const current = $taskTabBindings.get()[request.taskId]

      if (
        result.ok &&
        generationRef.current === guestGeneration &&
        current?.tabId === request.tabId &&
        current.generation === request.taskGeneration
      ) {
        automationBindingRef.current = request
      } else if (result.ok) {
        void window.hermesDesktop.browserGuest.unbindAutomation(request)
      }
    })
  }, [tab.id, tab.surfaceEpoch, taskBinding])

  useEffect(() => {
    const generation = generationRef.current

    if (generation && tab.url && lastActivatedUrlRef.current !== tab.url) {
      lastActivatedUrlRef.current = tab.url
      activationRequestRef.current += 1
      void window.hermesDesktop.browserGuest.activate({ generation, tabId: tab.id, url: tab.url })
    }
  }, [tab.id, tab.url])

  return (
    <div
      aria-hidden={!foreground}
      className="flex overflow-hidden"
      data-browser-foreground={foreground ? 'true' : 'false'}
      data-browser-tab-host={tab.id}
      ref={hostRef}
      style={{
        height: tab.geometry.height,
        left: tab.geometry.x,
        pointerEvents: foreground ? 'auto' : 'none',
        position: 'absolute',
        top: tab.geometry.y,
        visibility: foreground ? 'visible' : 'hidden',
        width: tab.geometry.width
      }}
    >
      {foreground ? <BrowserAnnotationsPanel guestGeneration={guestGeneration} tab={tab} /> : null}
      {foreground ? <BrowserController guestGeneration={guestGeneration} tab={tab} /> : null}
    </div>
  )
}

/**
 * Window-lifetime browser mount layer. Selection only changes CSS visibility;
 * each open tab keeps the same webview (and therefore its DOM/process state)
 * until the tab itself closes.
 */
export function BrowserWebviewLayer() {
  const tabs = useStore($browserTabs)
  const foregroundTabId = useStore($foregroundBrowserTabId)
  const paneGeometry = useStore($browserPaneGeometry)
  const paneOpen = useStore($browserPaneOpen)
  const paneVisible = paneOpen && paneGeometry.width > 0 && paneGeometry.height > 0

  return (
    <>
      <BrowserPersistenceCoordinator />
      {tabs.length > 0 && (
        <div className="pointer-events-none absolute inset-0 z-10" data-browser-webview-layer>
          {tabs.map(tab => (
            <BrowserWebview foreground={paneVisible && tab.id === foregroundTabId} key={tab.id} tab={tab} />
          ))}
        </div>
      )}
      <BrowserConsentDialog />
    </>
  )
}
