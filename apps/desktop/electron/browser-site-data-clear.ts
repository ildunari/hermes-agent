/** Reports the authoritative storage phase independently from later cache maintenance. */
export async function runBrowserSiteDataClear(
  clearStorage: () => Promise<void>,
  finishMaintenance: () => Promise<void> | void
): Promise<boolean> {
  let siteData = false
  try {
    await clearStorage()
    siteData = true
    await finishMaintenance()
  } catch { /* retain the phase that already completed */ }
  return siteData
}
