export interface BrowserProfileDeletionResult {
  metadata: boolean
  ok: boolean
  permissions: boolean
  siteData: boolean
}

/** Runs independent deletion scopes without allowing one failure to block another. */
export async function deleteLocalBrowserProfileData(
  clearSiteData: () => Promise<{ permissions: boolean; siteData: boolean }>,
  deleteMetadata: () => boolean
): Promise<BrowserProfileDeletionResult> {
  let site = { permissions: false, siteData: false }
  try {site = await clearSiteData()} catch { /* metadata deletion must still run */ }
  let metadata = false
  try {metadata = deleteMetadata()} catch { /* report the failed scope */ }
  return {
    metadata,
    ok: site.permissions && site.siteData && metadata,
    permissions: site.permissions,
    siteData: site.siteData
  }
}
