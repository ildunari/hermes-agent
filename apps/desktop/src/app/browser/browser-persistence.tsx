import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import { $activeProfile, normalizeProfileKey } from '@/store/profile'

import { hydrateBrowserAnnotationsLayout } from './browser-annotations-layout'
import {
  $browserTabs,
  $foregroundBrowserTabId,
  type BrowserTab,
  clearBrowserTabsForProfile,
  clearBrowserWorkspace,
  restoreBrowserTabs
} from './browser-store'

interface PersistenceScope {
  enabled: boolean
  epoch: string
  hydrated: boolean
  persisted: Set<string>
  queue: Promise<void>
}

type PersistableBrowserTab = Omit<BrowserTab, 'restoreId'> & { restoreId: string }

const scopes = new Map<string, PersistenceScope>()
const hydration = new Map<string, Promise<void>>()

function persistable(tab: BrowserTab): tab is PersistableBrowserTab {
  if (tab.private || tab.resource || !tab.restoreId) {return false}

  try {
    const parsed = new URL(tab.url)
    return ['http:', 'https:'].includes(parsed.protocol) && !parsed.username && !parsed.password
  } catch {
    return false
  }
}

export async function hydrateBrowserProfile(profile: string): Promise<void> {
  const normalized = normalizeProfileKey(profile)
  if (scopes.get(normalized)?.hydrated) {return}
  const pending = hydration.get(normalized)
  if (pending) {return pending}

  const request = (async () => {
    const snapshot = await window.hermesDesktop.browserState.snapshot({ profile: normalized })
    if (snapshot.degraded || !snapshot.epoch) {return}

    const scope: PersistenceScope = {
      enabled: snapshot.restoreEnabled,
      epoch: snapshot.epoch,
      hydrated: true,
      persisted: new Set(snapshot.descriptors.map(row => row.restoreId)),
      queue: Promise.resolve()
    }
    scopes.set(normalized, scope)
    if (snapshot.restoreEnabled) {restoreBrowserTabs(normalized, snapshot.descriptors, snapshot.selectedRestoreId)}
    syncBrowserPersistence($browserTabs.get(), $foregroundBrowserTabId.get())
  })().finally(() => hydration.delete(normalized))

  hydration.set(normalized, request)
  return request
}

export function syncBrowserPersistence(tabs: readonly BrowserTab[], foregroundId: string | null): void {
  for (const [profile, scope] of scopes) {
    if (!scope.hydrated || !scope.enabled) {continue}
    const current = tabs.filter((tab): tab is PersistableBrowserTab =>
      tab.profile === profile && persistable(tab)
    )
    const currentIds = new Set(current.map(tab => tab.restoreId))
    const selected = current.find(tab => tab.id === foregroundId)

    scope.queue = scope.queue.then(async () => {
      for (const [ordinal, tab] of current.entries()) {
        const result = await window.hermesDesktop.browserState.upsert({
          descriptor: {
            createdAt: tab.createdAt,
            ordinal,
            pinned: false,
            restoreId: tab.restoreId,
            restoredFromTabId: tab.restoredFromTabId ?? null,
            title: tab.title ?? '',
            updatedAt: Date.now(),
            url: tab.url,
            workspaceId: tab.workspaceId
          },
          epoch: scope.epoch,
          profile
        })
        if (result.ok) {scope.persisted.add(tab.restoreId)}
      }

      for (const restoreId of [...scope.persisted]) {
        if (currentIds.has(restoreId)) {continue}
        const removed = await window.hermesDesktop.browserState.remove({ epoch: scope.epoch, profile, restoreId })
        if (removed.ok) {scope.persisted.delete(restoreId)}
      }

      if (selected) {
        await window.hermesDesktop.browserState.select({
          epoch: scope.epoch,
          profile,
          restoreId: selected.restoreId,
          workspaceId: selected.workspaceId
        })
      }
    }).catch(() => undefined)
  }
}

export async function resetBrowserWorkspaceDetailed(
  profile: string,
  workspaceId: string,
  includeHistory = false
): Promise<{ activity: boolean; ok: boolean; state: boolean }> {
  const normalized = normalizeProfileKey(profile)
  const result = await window.hermesDesktop.browserState.resetWorkspace({
    includeHistory,
    profile: normalized,
    workspaceId
  })

  const state = result.state ?? result.ok
  const activity = result.activity ?? result.ok
  if (state && result.epoch) {
    const enabled = result.restoreEnabled ?? scopes.get(normalized)?.enabled ?? false
    clearBrowserWorkspace(normalized, workspaceId)
    scopes.set(normalized, { enabled, epoch: result.epoch, hydrated: true, persisted: new Set(), queue: Promise.resolve() })
  }
  return { activity, ok: result.ok, state }
}

export function reseedBrowserPersistence(
  profile: string,
  result: { epoch: string; restoreEnabled: boolean }
): boolean {
  const normalized = normalizeProfileKey(profile)
  if (!result.epoch) {return false}
  scopes.set(normalized, {
    enabled: result.restoreEnabled,
    epoch: result.epoch,
    hydrated: true,
    persisted: new Set(),
    queue: Promise.resolve()
  })
  if (result.restoreEnabled) {syncBrowserPersistence($browserTabs.get(), $foregroundBrowserTabId.get())}
  return true
}

export async function resetBrowserWorkspace(
  profile: string,
  workspaceId: string,
  includeHistory = false
): Promise<boolean> {
  return (await resetBrowserWorkspaceDetailed(profile, workspaceId, includeHistory)).ok
}

export async function clearBrowserSiteData(profile: string, origin?: string): Promise<boolean> {
  const normalized = normalizeProfileKey(profile)
  const result = await window.hermesDesktop.browserState.clearSiteData({
    ...(origin ? { origin } : {}),
    profile: normalized
  })
  return result.ok
}

export async function setBrowserRestoreEnabled(profile: string, enabled: boolean): Promise<boolean> {
  const normalized = normalizeProfileKey(profile)
  const result = await window.hermesDesktop.browserState.setRestoreEnabled({ enabled, profile: normalized })
  if (!result.ok) {return false}
  const snapshot = await window.hermesDesktop.browserState.snapshot({ profile: normalized })
  if (snapshot.degraded || !snapshot.epoch) {return false}
  const scope = scopes.get(normalized)
  if (scope) {
    scope.enabled = enabled
    scope.epoch = snapshot.epoch
    if (!enabled) {scope.persisted.clear()}
  }
  if (enabled) {syncBrowserPersistence($browserTabs.get(), $foregroundBrowserTabId.get())}
  return true
}

export function forgetBrowserProfile(profile: string): void {
  const normalized = normalizeProfileKey(profile)
  scopes.delete(normalized)
  hydration.delete(normalized)
  clearBrowserTabsForProfile(normalized)
}

export function __resetBrowserPersistenceForTests(): void {
  scopes.clear()
  hydration.clear()
}

export function BrowserPersistenceCoordinator() {
  const activeProfile = useStore($activeProfile)
  const tabs = useStore($browserTabs)
  const foregroundId = useStore($foregroundBrowserTabId)

  useEffect(() => {
    hydrateBrowserAnnotationsLayout()
  }, [])

  useEffect(() => {
    void hydrateBrowserProfile(activeProfile)
  }, [activeProfile])

  useEffect(() => {
    syncBrowserPersistence(tabs, foregroundId)
  }, [foregroundId, tabs])

  return null
}
