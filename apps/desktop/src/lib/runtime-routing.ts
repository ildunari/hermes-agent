import type { RuntimeRouting, RuntimeRoutingIdentity } from '@/types/hermes'

export function sameRuntimeIdentity(left: RuntimeRoutingIdentity, right: RuntimeRoutingIdentity): boolean {
  return left.model === right.model && left.provider === right.provider
}

export function isRuntimeFallback(routing: RuntimeRouting | undefined): routing is RuntimeRouting {
  return Boolean(routing?.fallback.active && !sameRuntimeIdentity(routing.runtime, routing.selected))
}