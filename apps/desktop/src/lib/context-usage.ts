import type { ContextBreakdown, UsageStats } from '@/types/hermes'

export function contextSegmentPercent(tokens: number, contextMax: number): number {
  if (!Number.isFinite(tokens) || !Number.isFinite(contextMax) || tokens <= 0 || contextMax <= 0) {
    return 0
  }

  return Math.max(0, Math.min(100, (tokens / contextMax) * 100))
}

export function resolveContextUsage(currentUsage: UsageStats, breakdown: ContextBreakdown | null) {
  return {
    contextMax: currentUsage.context_max ?? breakdown?.context_max ?? 0,
    contextPercent: currentUsage.context_percent ?? breakdown?.context_percent ?? 0,
    contextUsed: currentUsage.context_used ?? breakdown?.context_used ?? 0
  }
}
