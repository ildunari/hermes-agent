import type { RuntimeRouting, RuntimeRoutingIdentity } from '@/types/hermes'

const RUNTIME_ROUTING_STATES = new Set<RuntimeRouting['state']>([
  'fallback_activated',
  'finished',
  'primary_restored',
  'started'
])

function routingRecord(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined
}

function routingIdentity(value: unknown): RuntimeRoutingIdentity | undefined {
  const identity = routingRecord(value)

  if (typeof identity?.model !== 'string' || typeof identity.provider !== 'string') {
    return undefined
  }

  const model = identity.model.trim()
  const provider = identity.provider.trim()

  return model && provider ? { model, provider } : undefined
}

/** Parse backend-owned routing data at the renderer boundary.
 *
 * Runtime and Desktop can update independently, so this deliberately accepts
 * `unknown`, recognizes only schema v1, and returns a fresh normalized object.
 * Callers must never store an unchecked payload received over RPC.
 */
export function parseRuntimeRouting(value: unknown): RuntimeRouting | undefined {
  const routing = routingRecord(value)
  const fallback = routingRecord(routing?.fallback)
  const selected = routingIdentity(routing?.selected)
  const runtime = routingIdentity(routing?.runtime)
  const state = routing?.state
  const reason = fallback?.reason
  const chainIndex = fallback?.chain_index

  if (
    routing?.schema_version !== 1 ||
    typeof state !== 'string' ||
    !RUNTIME_ROUTING_STATES.has(state as RuntimeRouting['state']) ||
    !selected ||
    !runtime ||
    typeof fallback?.active !== 'boolean' ||
    typeof reason !== 'string' ||
    !reason.trim() ||
    typeof chainIndex !== 'number' ||
    !Number.isSafeInteger(chainIndex) ||
    chainIndex < 0
  ) {
    return undefined
  }

  return {
    schema_version: 1,
    state: state as RuntimeRouting['state'],
    selected,
    runtime,
    fallback: { active: fallback.active, reason: reason.trim(), chain_index: chainIndex }
  }
}

export function sameRuntimeIdentity(left: RuntimeRoutingIdentity, right: RuntimeRoutingIdentity): boolean {
  return left.model === right.model && left.provider === right.provider
}

export function isRuntimeFallback(routing: RuntimeRouting | undefined): routing is RuntimeRouting {
  return Boolean(routing?.fallback.active && !sameRuntimeIdentity(routing.runtime, routing.selected))
}