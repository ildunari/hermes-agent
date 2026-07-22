import type { GatewayEventPayload } from '@/lib/chat-messages'

import {
  $taskTabBindings,
  bindAutomationTask,
  createBrowserTab,
  resolveAutomationTask
} from './browser-store'
import {
  $browserSupervision,
  type BrowserOperation,
  superviseBrowserTask,
  updateBrowserOperation
} from './browser-supervision'

const BROWSER_TOOLS = new Set([
  'browser_back',
  'browser_cdp',
  'browser_click',
  'browser_console',
  'browser_dialog',
  'browser_get_images',
  'browser_navigate',
  'browser_press',
  'browser_scroll',
  'browser_snapshot',
  'browser_type',
  'browser_vision'
])

const SNAPSHOT_TOOLS = new Set(['browser_console', 'browser_get_images', 'browser_snapshot', 'browser_vision'])
const NAVIGATION_TOOLS = new Set(['browser_back', 'browser_navigate'])

function operationForTool(name: string): BrowserOperation {
  if (SNAPSHOT_TOOLS.has(name)) {
    return 'snapshot'
  }

  if (NAVIGATION_TOOLS.has(name)) {
    return 'navigate'
  }

  return 'action'
}

function opaqueOwner(payload: GatewayEventPayload | undefined): string {
  const toolId = payload?.tool_id || payload?.tool_call_id

  return typeof toolId === 'string' && toolId ? toolId : 'agent'
}

/**
 * Production bridge from the authenticated gateway tool stream to renderer
 * browser authority. It carries no URL, arguments, result, or page content.
 */
export function applyBrowserToolLifecycle(
  phase: 'complete' | 'start',
  sessionId: string,
  profile: string,
  payload: GatewayEventPayload | undefined
): boolean {
  const name = typeof payload?.name === 'string' ? payload.name : ''

  if (!sessionId || !profile || !BROWSER_TOOLS.has(name)) {
    return false
  }

  const existing = $taskTabBindings.get()[sessionId]

  if (existing) {
    const resolved = resolveAutomationTask(sessionId, existing.generation)
    const record = $browserSupervision.get()[sessionId]

    if (
      resolved.status !== 'bound' ||
      resolved.tab.profile !== profile ||
      (record && record.profile !== profile)
    ) {
      return false
    }

    if (phase === 'complete') {
      return record?.ownerId === opaqueOwner(payload)
        ? updateBrowserOperation(sessionId, existing.generation, 'idle')
        : false
    }

    const operation = operationForTool(name)
    const ownerId = opaqueOwner(payload)

    superviseBrowserTask(existing, {
      operation,
      ownerId,
      profile,
      sessionId
    })
    updateBrowserOperation(sessionId, existing.generation, operation, ownerId)

    return true
  }

  if (phase === 'complete') {
    return false
  }

  // The attachment page stays internal and blank until the browser tool's
  // authenticated Page.navigate arrives. Creating this background tab never
  // changes foreground selection; P5 owns explicit-intent focusing policy.
  const operation = operationForTool(name)

  const tab = createBrowserTab({
    geometry: { height: 720, width: 1024, x: 0, y: 0 },
    profile,
    url: '',
    workspaceId: sessionId
  })

  const binding = bindAutomationTask(sessionId, tab.id)

  superviseBrowserTask(binding, {
    operation,
    ownerId: opaqueOwner(payload),
    profile,
    sessionId
  })

  return true
}
