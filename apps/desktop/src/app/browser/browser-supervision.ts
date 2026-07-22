import { atom } from 'nanostores'

import type { BrowserTabId, TaskTabBinding } from './browser-store'

const MAX_TIMELINE_ENTRIES = 200

export type BrowserControlState = 'agent' | 'handing-back' | 'local-takeover' | 'paused'
export type BrowserControlReason = 'hand-back' | 'pause' | 'stop' | 'takeover'
export type BrowserOperation = 'action' | 'idle' | 'navigate' | 'snapshot'

export interface BrowserSupervisionRecord {
  generation: number
  operation: BrowserOperation
  ownerId: string
  profile: string
  sessionId: string
  state: BrowserControlState
  tabId: BrowserTabId
  taskId: string
}

export interface BrowserTimelineEntry {
  at: number
  generation: number
  id: string
  reason: BrowserControlReason
  state: BrowserControlState
  tabId: BrowserTabId
  taskId: string
}

interface ExactNativeBinding {
  guestGeneration: string
  tabId: string
  taskGeneration: number
  taskId: string
}

interface FreshHandBackIdentity {
  binding: TaskTabBinding
  surfaceEpoch: string
}

const supervision = atom<Readonly<Record<string, BrowserSupervisionRecord>>>({})
const timeline = atom<readonly BrowserTimelineEntry[]>([])
const timelineDegraded = atom(false)
const handBackBarriers = new Map<string, { generation: number; surfaceEpoch: string; tabId: BrowserTabId }>()

export const $browserSupervision = supervision
export const $browserTimeline = timeline
export const $browserTimelineDegraded = timelineDegraded

function setTimelineEntry(entry: BrowserTimelineEntry): void {
  const existing = $browserTimeline.get()

  if (existing.some(candidate => candidate.id === entry.id)) {return}

  $browserTimeline.set([...existing, entry].sort((left, right) => left.at - right.at).slice(-MAX_TIMELINE_ENTRIES))
}

function timelineFromRow(row: BrowserActivityPersistedRow): BrowserTimelineEntry | null {
  if (!row.operationId || !['hand-back', 'pause', 'stop', 'takeover'].includes(row.category)) {return null}

  return {
    at: row.createdAt,
    generation: row.bindingGeneration,
    id: row.eventId,
    reason: row.category as BrowserControlReason,
    state: row.category === 'takeover' ? 'local-takeover' : row.category === 'hand-back' ? 'handing-back' : 'paused',
    tabId: row.tabIncarnationId as BrowserTabId,
    taskId: row.operationId
  }
}

export async function hydrateBrowserTimeline(profile: string, workspaceId?: string): Promise<void> {
  const activity = window.hermesDesktop?.browserActivity

  if (!activity) {
    timelineDegraded.set(true)

    return
  }

  const result = await activity.list({ limit: MAX_TIMELINE_ENTRIES, profile, workspaceId })
  timelineDegraded.set(result.degraded)

  for (const row of result.rows) {
    const entry = timelineFromRow(row)

    if (entry) {setTimelineEntry(entry)}
  }
}

export async function deleteBrowserSessionActivity(profile: string, sessionId: string): Promise<void> {
  try {
    const result = await window.hermesDesktop.browserActivity.deleteSession({ profile, sessionId })

    if (!result.ok) {throw new Error('browser activity deletion failed')}
  } catch {
    timelineDegraded.set(true)
  }
}

function appendTimeline(record: BrowserSupervisionRecord, reason: BrowserControlReason): void {
  const entry: BrowserTimelineEntry = {
    at: Date.now(),
    generation: record.generation,
    id: globalThis.crypto.randomUUID(),
    reason,
    state: record.state,
    tabId: record.tabId,
    taskId: record.taskId
  }

  const activity = window.hermesDesktop?.browserActivity

  if (!activity) {
    timelineDegraded.set(true)
    setTimelineEntry(entry)

    return
  }

  void activity.append({
    event: {
      bindingGeneration: record.generation,
      category: reason,
      certainty: 'completed',
      operationId: record.taskId,
      phase: 'completed',
      reasonCode: 'retired',
      sessionId: record.sessionId,
      sessionLineageId: record.sessionId,
      source: 'human',
      tabIncarnationId: record.tabId,
      workspaceId: record.sessionId
    },
    profile: record.profile
  }).then(result => {
    timelineDegraded.set(!result.ok)
    const persisted = result.row ? timelineFromRow(result.row) : null

    setTimelineEntry(persisted ?? entry)
  }).catch(() => {
    timelineDegraded.set(true)
    setTimelineEntry(entry)
  })
}

function replaceExact(
  taskId: string,
  expectedGeneration: number,
  update: (record: BrowserSupervisionRecord) => BrowserSupervisionRecord
): BrowserSupervisionRecord | null {
  const current = $browserSupervision.get()
  const record = current[taskId]

  if (!record || record.generation !== expectedGeneration) {
    return null
  }

  const next = update(record)
  $browserSupervision.set({ ...current, [taskId]: next })

  return next
}

export function superviseBrowserTask(
  binding: TaskTabBinding,
  identity: Pick<BrowserSupervisionRecord, 'operation' | 'ownerId' | 'profile' | 'sessionId'>
): BrowserSupervisionRecord {
  const existing = $browserSupervision.get()[binding.taskId]

  if (existing && binding.generation <= existing.generation) {
    return existing
  }

  const record: BrowserSupervisionRecord = {
    ...identity,
    generation: binding.generation,
    state: 'agent',
    tabId: binding.tabId,
    taskId: binding.taskId
  }

  $browserSupervision.set({ ...$browserSupervision.get(), [binding.taskId]: record })

  return record
}

export function updateBrowserOperation(
  taskId: string,
  expectedGeneration: number,
  operation: BrowserOperation,
  ownerId?: string
): boolean {
  const next = replaceExact(taskId, expectedGeneration, record =>
    record.state === 'agent' ? { ...record, operation, ownerId: ownerId ?? record.ownerId } : record
  )

  return Boolean(next?.state === 'agent' && next.operation === operation)
}

export async function requestLocalBrowserControl(
  request: ExactNativeBinding,
  state: 'local-takeover' | 'paused',
  revoke: (request: ExactNativeBinding) => Promise<{ ok: boolean; retired: boolean }>
): Promise<boolean> {
  const current = $browserSupervision.get()[request.taskId]

  if (!current || current.generation !== request.taskGeneration || current.tabId !== request.tabId) {
    return false
  }

  if (current.state === state) {
    return true
  }

  // Pause has already retired native authority. Moving from that acknowledged
  // state into local takeover must not attempt to retire the same generation
  // twice and misreport the second idempotent acknowledgement as a failure.
  if (current.state === 'paused' && state === 'local-takeover') {
    const next = replaceExact(request.taskId, request.taskGeneration, record => ({ ...record, state }))

    if (!next || next.tabId !== request.tabId) {
      return false
    }

    appendTimeline(next, 'takeover')

    return true
  }

  const acknowledgement = await revoke(request)

  if (!acknowledgement.ok || !acknowledgement.retired) {
    return false
  }

  const next = replaceExact(request.taskId, request.taskGeneration, record => ({ ...record, state }))

  if (!next || next.tabId !== request.tabId) {
    return false
  }

  appendTimeline(next, state === 'paused' ? 'pause' : 'takeover')

  return true
}

export function beginBrowserHandBack(
  taskId: string,
  expectedGeneration: number,
  reconstruct: () => FreshHandBackIdentity | null
): boolean {
  const current = $browserSupervision.get()[taskId]

  if (!current || current.generation !== expectedGeneration || current.state === 'agent') {
    return false
  }

  const fresh = reconstruct()

  if (!fresh || fresh.binding.taskId !== taskId || fresh.binding.generation <= expectedGeneration) {
    return false
  }

  const next: BrowserSupervisionRecord = {
    ...current,
    generation: fresh.binding.generation,
    state: 'handing-back',
    tabId: fresh.binding.tabId
  }

  $browserSupervision.set({ ...$browserSupervision.get(), [taskId]: next })
  handBackBarriers.set(taskId, {
    generation: fresh.binding.generation,
    surfaceEpoch: fresh.surfaceEpoch,
    tabId: fresh.binding.tabId
  })
  appendTimeline(next, 'hand-back')

  return true
}

/** Called only after main observes a successful fresh snapshot for the reconstructed guest. */
export function completeBrowserHandBack(
  taskId: string,
  generation: number,
  tabId: BrowserTabId,
  surfaceEpoch: string
): boolean {
  const barrier = handBackBarriers.get(taskId)

  if (
    !barrier ||
    barrier.generation !== generation ||
    barrier.tabId !== tabId ||
    barrier.surfaceEpoch !== surfaceEpoch
  ) {
    return false
  }

  const next = replaceExact(taskId, generation, record =>
    record.state === 'handing-back' && record.tabId === tabId ? { ...record, state: 'agent' } : record
  )

  if (!next || next.state !== 'agent') {
    return false
  }

  handBackBarriers.delete(taskId)

  return true
}

export function isBrowserHandBackPending(
  taskId: string,
  generation: number,
  tabId: BrowserTabId,
  surfaceEpoch: string
): boolean {
  const barrier = handBackBarriers.get(taskId)

  return Boolean(
    barrier &&
      barrier.generation === generation &&
      barrier.tabId === tabId &&
      barrier.surfaceEpoch === surfaceEpoch
  )
}

export async function stopSupervisedBrowser(
  request: ExactNativeBinding,
  stop: (request: ExactNativeBinding) => Promise<{ ok: boolean; retired: boolean }>
): Promise<boolean> {
  const current = $browserSupervision.get()[request.taskId]

  if (!current || current.generation !== request.taskGeneration || current.tabId !== request.tabId) {
    return false
  }

  const acknowledgement = await stop(request)
  const latest = $browserSupervision.get()[request.taskId]

  if (
    !acknowledgement.ok ||
    !acknowledgement.retired ||
    latest?.generation !== request.taskGeneration ||
    latest.tabId !== request.tabId
  ) {
    return false
  }

  appendTimeline({ ...latest, state: 'paused' }, 'stop')
  const next = { ...$browserSupervision.get() }
  delete next[request.taskId]
  $browserSupervision.set(next)
  handBackBarriers.delete(request.taskId)

  return true
}

export function clearBrowserSupervision(): void {
  $browserSupervision.set({})
  $browserTimeline.set([])
  timelineDegraded.set(false)
  handBackBarriers.clear()
}
