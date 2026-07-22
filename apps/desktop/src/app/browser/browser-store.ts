import { atom, computed } from 'nanostores'

import { normalizeProfileKey } from '@/store/profile'

export type BrowserTabId = `browser:${string}`

export interface BrowserGeometry {
  height: number
  width: number
  x: number
  y: number
}

export interface BrowserTab {
  createdAt: number
  geometry: BrowserGeometry
  id: BrowserTabId
  private: boolean
  privatePartition?: string
  profile: string
  recovery: BrowserTabRecovery
  restoreId?: string
  restoredFromTabId?: BrowserTabId
  resource?: BrowserTabResource
  surfaceEpoch: string
  title?: string
  url: string
  workspaceId: string
}

export interface BrowserTabResource {
  focusIntentRevisionAtClick?: number
  kind: 'artifact' | 'preview'
  sourceSessionId: string
  target: string
}

export interface BrowserTabRecovery {
  attempts: number
  state: 'active' | 'failed' | 'stable'
  windowStartedAt: number
}

export interface CreateBrowserTabInput {
  foreground?: boolean
  geometry: BrowserGeometry
  private?: boolean
  profile: string
  resource?: BrowserTabResource
  url: string
  workspaceId: string
}

export interface TaskTabBinding {
  generation: number
  tabId: BrowserTabId
  taskId: string
}

export interface ReconstructedBrowserTab {
  binding?: TaskTabBinding
  tab: BrowserTab
}

export type TaskTabResolution =
  | { binding: TaskTabBinding; status: 'bound'; tab: BrowserTab }
  | { currentGeneration?: number; status: 'stale' }
  | { status: 'unbound' }

const browserTabs = atom<readonly BrowserTab[]>([])
const foregroundBrowserTabId = atom<BrowserTabId | null>(null)
const taskTabBindings = atom<Readonly<Record<string, TaskTabBinding>>>({})
const browserPaneOpen = atom(false)
const browserPaneGeometry = atom<BrowserGeometry>({ height: 0, width: 0, x: 0, y: 0 })
const latestTaskGenerations = new Map<string, number>()
let browserSurfaceEpoch = randomUuid()
let browserFocusIntentRevision = 0
const BROWSER_RECOVERY_LIMIT = 3
export const BROWSER_RECOVERY_STABLE_MS = 30_000

export const $browserTabs = browserTabs
export const $foregroundBrowserTabId = foregroundBrowserTabId
export const $taskTabBindings = taskTabBindings
export const $browserPaneOpen = browserPaneOpen
export const $browserPaneGeometry = browserPaneGeometry
export const $foregroundBrowserTab = computed(
  [$browserTabs, $foregroundBrowserTabId],
  (tabs, id) => tabs.find(tab => tab.id === id) ?? null
)

function randomUuid(): string {
  return globalThis.crypto.randomUUID()
}

function validGeometry(geometry: BrowserGeometry): boolean {
  return (
    Number.isFinite(geometry.x) &&
    Number.isFinite(geometry.y) &&
    Number.isFinite(geometry.width) &&
    geometry.width >= 0 &&
    Number.isFinite(geometry.height) &&
    geometry.height >= 0
  )
}

export function setBrowserPaneOpen(open: boolean): void {
  if ($browserPaneOpen.get() !== open) {
    $browserPaneOpen.set(open)
  }
}

export function openBrowserPane(): void {
  setBrowserPaneOpen(true)
}

export function closeBrowserPane(): void {
  setBrowserPaneOpen(false)
}

export function setBrowserPaneGeometry(geometry: BrowserGeometry): void {
  if (!validGeometry(geometry)) {
    throw new Error('Browser pane geometry must contain finite, non-negative dimensions')
  }

  const current = $browserPaneGeometry.get()

  if (
    current.x === geometry.x &&
    current.y === geometry.y &&
    current.width === geometry.width &&
    current.height === geometry.height
  ) {
    return
  }

  $browserPaneGeometry.set({ ...geometry })
}

function requireTab(tabId: BrowserTabId): BrowserTab {
  const tab = $browserTabs.get().find(candidate => candidate.id === tabId)

  if (!tab) {
    throw new Error(`Unknown browser tab: ${tabId}`)
  }

  return tab
}

export function createBrowserTab(input: CreateBrowserTabInput): BrowserTab {
  if (!validGeometry(input.geometry)) {
    throw new Error('Browser tab geometry must contain finite, non-negative dimensions')
  }

  const privateTab = input.private === true

  const tab: BrowserTab = {
    createdAt: Date.now(),
    geometry: { ...input.geometry },
    id: `browser:${randomUuid()}`,
    private: privateTab,
    privatePartition: privateTab ? `hermes-browser-private:v1:${randomUuid()}` : undefined,
    profile: normalizeProfileKey(input.profile),
    recovery: { attempts: 0, state: 'stable', windowStartedAt: Date.now() },
    restoreId: !privateTab && !input.resource ? randomUuid() : undefined,
    resource: input.resource,
    surfaceEpoch: browserSurfaceEpoch,
    title: '',
    url: input.url,
    workspaceId: input.workspaceId
  }

  $browserTabs.set([...$browserTabs.get(), tab])

  if (input.foreground) {
    selectBrowserTab(tab.id)
  }

  return tab
}

export function selectBrowserTab(tabId: BrowserTabId | null): void {
  if (tabId !== null) {
    requireTab(tabId)
  }

  browserFocusIntentRevision += 1

  if ($foregroundBrowserTabId.get() !== tabId) {
    $foregroundBrowserTabId.set(tabId)
  }
}

export function currentBrowserFocusIntentRevision(): number {
  return browserFocusIntentRevision
}

export function setBrowserTabGeometry(tabId: BrowserTabId, geometry: BrowserGeometry): void {
  if (!validGeometry(geometry)) {
    throw new Error('Browser tab geometry must contain finite, non-negative dimensions')
  }

  const current = $browserTabs.get()
  const index = current.findIndex(tab => tab.id === tabId)

  if (index === -1) {
    throw new Error(`Unknown browser tab: ${tabId}`)
  }

  const existing = current[index]

  if (
    existing.geometry.x === geometry.x &&
    existing.geometry.y === geometry.y &&
    existing.geometry.width === geometry.width &&
    existing.geometry.height === geometry.height
  ) {
    return
  }

  const next = [...current]
  next[index] = { ...existing, geometry: { ...geometry } }
  $browserTabs.set(next)
}

export function setBrowserTabTitle(tabId: BrowserTabId, title: string): void {
  const current = $browserTabs.get()
  const index = current.findIndex(tab => tab.id === tabId)
  if (index === -1) {
    throw new Error(`Unknown browser tab: ${tabId}`)
  }
  const nextTitle = String(title || '').slice(0, 256)
  if (current[index].title === nextTitle) {
    return
  }
  const next = [...current]
  next[index] = { ...current[index], title: nextTitle }
  $browserTabs.set(next)
}

export function setBrowserTabUrl(tabId: BrowserTabId, url: string): void {
  const current = $browserTabs.get()
  const index = current.findIndex(tab => tab.id === tabId)

  if (index === -1) {
    throw new Error(`Unknown browser tab: ${tabId}`)
  }

  if (current[index].url === url) {
    return
  }

  const next = [...current]
  next[index] = { ...current[index], url }
  $browserTabs.set(next)
}

export function closeBrowserTab(tabId: BrowserTabId): void {
  const current = $browserTabs.get()

  if (!current.some(tab => tab.id === tabId)) {
    return
  }

  $browserTabs.set(current.filter(tab => tab.id !== tabId))

  if ($foregroundBrowserTabId.get() === tabId) {
    $foregroundBrowserTabId.set(null)
  }

  const bindings = $taskTabBindings.get()
  const nextBindings = Object.fromEntries(Object.entries(bindings).filter(([, binding]) => binding.tabId !== tabId))

  if (Object.keys(nextBindings).length !== Object.keys(bindings).length) {
    $taskTabBindings.set(nextBindings)
  }
}

function reconstructBrowserTabWithReason(
  tabId: BrowserTabId,
  reason: 'crash-recovery' | 'intentional-hand-back'
): ReconstructedBrowserTab | null {
  const current = $browserTabs.get()
  const index = current.findIndex(tab => tab.id === tabId)

  if (index === -1) {
    return null
  }

  const retired = current[index]
  const now = Date.now()
  const attempts = retired.recovery.attempts

  if (reason === 'crash-recovery' && attempts >= BROWSER_RECOVERY_LIMIT) {
    const next = [...current]
    next[index] = { ...retired, recovery: { ...retired.recovery, state: 'failed' } }
    $browserTabs.set(next)

    return null
  }

  const tab: BrowserTab = {
    ...retired,
    createdAt: now,
    id: `browser:${randomUuid()}`,
    privatePartition: retired.private ? `hermes-browser-private:v1:${randomUuid()}` : undefined,
    recovery:
      reason === 'crash-recovery'
        ? {
            attempts: attempts + 1,
            state: 'active',
            windowStartedAt: attempts === 0 ? now : retired.recovery.windowStartedAt
          }
        : retired.recovery,
    restoredFromTabId: retired.id,
    surfaceEpoch: randomUuid()
  }

  const nextTabs = [...current]
  nextTabs[index] = tab

  const bindings = $taskTabBindings.get()
  const retiredBinding = Object.values(bindings).find(binding => binding.tabId === tabId)

  let binding: TaskTabBinding | undefined
  let nextBindings = bindings

  if (retiredBinding) {
    const generation = Math.max(latestTaskGenerations.get(retiredBinding.taskId) ?? 0, retiredBinding.generation) + 1
    binding = { generation, tabId: tab.id, taskId: retiredBinding.taskId }
    latestTaskGenerations.set(retiredBinding.taskId, generation)
    nextBindings = { ...bindings, [retiredBinding.taskId]: binding }
  }

  $browserTabs.set(nextTabs)

  if ($foregroundBrowserTabId.get() === tabId) {
    $foregroundBrowserTabId.set(tab.id)
  }

  if (nextBindings !== bindings) {
    $taskTabBindings.set(nextBindings)
  }

  return { binding, tab }
}

export function reconstructBrowserTab(tabId: BrowserTabId): ReconstructedBrowserTab | null {
  return reconstructBrowserTabWithReason(tabId, 'crash-recovery')
}

/** Fresh identity for explicit control hand-back, independent of crash retry limits. */
export function reconstructBrowserTabForHandBack(tabId: BrowserTabId): ReconstructedBrowserTab | null {
  return reconstructBrowserTabWithReason(tabId, 'intentional-hand-back')
}

export function markBrowserTabRecoveryStable(tabId: BrowserTabId): void {
  const current = $browserTabs.get()
  const index = current.findIndex(tab => tab.id === tabId)

  if (index === -1 || current[index].recovery.state !== 'active') {
    return
  }

  const next = [...current]
  next[index] = {
    ...current[index],
    recovery: { attempts: 0, state: 'stable', windowStartedAt: Date.now() }
  }
  $browserTabs.set(next)
}

export function retryBrowserTabRecovery(tabId: BrowserTabId): ReconstructedBrowserTab | null {
  const current = $browserTabs.get()
  const index = current.findIndex(tab => tab.id === tabId)

  if (index === -1 || current[index].recovery.state !== 'failed') {
    return null
  }

  const next = [...current]
  next[index] = {
    ...current[index],
    recovery: { attempts: 0, state: 'stable', windowStartedAt: Date.now() }
  }
  $browserTabs.set(next)

  return reconstructBrowserTab(tabId)
}

export function bindAutomationTask(taskId: string, tabId: BrowserTabId): TaskTabBinding {
  requireTab(tabId)

  const bindings = $taskTabBindings.get()
  const existing = bindings[taskId]

  if (existing?.tabId === tabId) {
    return existing
  }

  const tabOwner = Object.values(bindings).find(binding => binding.tabId === tabId && binding.taskId !== taskId)

  if (tabOwner) {
    throw new Error(`Browser tab ${tabId} is already bound to task ${tabOwner.taskId}`)
  }

  const generation = (latestTaskGenerations.get(taskId) ?? 0) + 1
  const binding: TaskTabBinding = { generation, tabId, taskId }

  latestTaskGenerations.set(taskId, generation)
  $taskTabBindings.set({ ...bindings, [taskId]: binding })

  return binding
}

export function unbindAutomationTask(taskId: string, expectedGeneration?: number): boolean {
  const bindings = $taskTabBindings.get()
  const existing = bindings[taskId]

  if (!existing || (expectedGeneration !== undefined && existing.generation !== expectedGeneration)) {
    return false
  }

  const next = { ...bindings }
  delete next[taskId]
  $taskTabBindings.set(next)

  return true
}

export function resolveAutomationTask(taskId: string, expectedGeneration: number): TaskTabResolution {
  const binding = $taskTabBindings.get()[taskId]

  if (!binding) {
    const latest = latestTaskGenerations.get(taskId)

    return latest !== undefined && expectedGeneration <= latest
      ? { currentGeneration: latest, status: 'stale' }
      : { status: 'unbound' }
  }

  if (binding.generation !== expectedGeneration) {
    return { currentGeneration: binding.generation, status: 'stale' }
  }

  const tab = $browserTabs.get().find(candidate => candidate.id === binding.tabId)

  return tab ? { binding, status: 'bound', tab } : { currentGeneration: binding.generation, status: 'stale' }
}

/** Clears live renderer state during a hard re-home or window teardown. */
export function clearBrowserTabs(): void {
  $browserTabs.set([])
  $foregroundBrowserTabId.set(null)
  $taskTabBindings.set({})
  browserSurfaceEpoch = randomUuid()
  browserFocusIntentRevision += 1
}

export function restoreBrowserTabs(
  profile: string,
  descriptors: readonly BrowserRestoreDescriptor[],
  selectedRestoreId: null | string,
  geometry: BrowserGeometry = { height: 720, width: 1024, x: 0, y: 0 }
): readonly BrowserTab[] {
  const normalizedProfile = normalizeProfileKey(profile)
  const existing = $browserTabs.get()
  const knownRestoreIds = new Set(existing.map(tab => tab.restoreId).filter(Boolean))
  const restored = descriptors.flatMap(descriptor => {
    if (knownRestoreIds.has(descriptor.restoreId)) {
      return []
    }

    try {
      const parsed = new URL(descriptor.url)
      if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) {
        return []
      }
    } catch {
      return []
    }

    knownRestoreIds.add(descriptor.restoreId)
    const tab: BrowserTab = {
      createdAt: Date.now(),
      geometry: { ...geometry },
      id: `browser:${randomUuid()}`,
      private: false,
      profile: normalizedProfile,
      recovery: { attempts: 0, state: 'stable', windowStartedAt: Date.now() },
      restoreId: descriptor.restoreId,
      restoredFromTabId: descriptor.restoredFromTabId?.startsWith('browser:')
        ? (descriptor.restoredFromTabId as BrowserTabId)
        : undefined,
      surfaceEpoch: browserSurfaceEpoch,
      title: descriptor.title,
      url: descriptor.url,
      workspaceId: descriptor.workspaceId
    }

    return [tab]
  })

  if (restored.length === 0) {
    return []
  }
  $browserTabs.set([...existing, ...restored])
  const selected = restored.find(tab => tab.restoreId === selectedRestoreId)
  if (selected) {
    $foregroundBrowserTabId.set(selected.id)
  }
  return restored
}

export function clearBrowserTabsForProfile(profile: string): void {
  const normalized = normalizeProfileKey(profile)
  for (const tab of [...$browserTabs.get()]) {
    if (tab.profile === normalized) {
      closeBrowserTab(tab.id)
    }
  }
}

export function clearBrowserWorkspace(profile: string, workspaceId: string): void {
  const normalized = normalizeProfileKey(profile)
  for (const tab of [...$browserTabs.get()]) {
    if (tab.profile === normalized && tab.workspaceId === workspaceId) {
      closeBrowserTab(tab.id)
    }
  }
}
