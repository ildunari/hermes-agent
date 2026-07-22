import type { BrowserActivityRepositoryManager } from './browser-activity-repository'
import type { BrowserStateRepositoryManager } from './browser-state-repository'

export interface BrowserMetadataRepairResult {
  activity: boolean
  epoch: string
  metadata: boolean
  ok: boolean
  restoreEnabled: boolean
}

/** Coordinates both live SQLite handles before replacing their shared database. */
export function repairBrowserProfileMetadata(
  profile: string,
  mode: 'reset-metadata' | 'retry',
  state: BrowserStateRepositoryManager,
  activity: BrowserActivityRepositoryManager
): BrowserMetadataRepairResult {
  let repository
  try {repository = state.forProfile(profile)} catch {
    return { activity: false, epoch: '', metadata: false, ok: false, restoreEnabled: false }
  }

  activity.closeProfile(profile)
  const metadata = repository.repair(mode)
  let activityReopened = false
  try {activityReopened = activity.forProfile(profile).open()} catch {activityReopened = false}
  const snapshot = repository.snapshot()
  return {
    activity: activityReopened,
    epoch: metadata && !snapshot.degraded ? snapshot.epoch : '',
    metadata,
    ok: metadata && activityReopened && !snapshot.degraded,
    restoreEnabled: metadata && !snapshot.degraded && repository.restoreEnabled()
  }
}
