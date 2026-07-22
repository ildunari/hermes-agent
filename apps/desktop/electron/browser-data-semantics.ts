export const BROWSER_SITE_STORAGE_TYPES = Object.freeze([
  'cookies',
  'filesystem',
  'indexdb',
  'localstorage',
  'shadercache',
  'serviceworkers',
  'cachestorage'
] as const)

export interface BrowserDataClearPlan {
  clearAuthCache: boolean
  clearHttpCache: boolean
  origin?: string
  storages: readonly typeof BROWSER_SITE_STORAGE_TYPES[number][]
}

/**
 * Site sign-out is exact-origin and must not become a profile-wide HTTP/auth
 * cache clear. Omitting origin is the separately destructive all-site-data
 * operation.
 */
export function browserDataClearPlan(origin?: string): BrowserDataClearPlan | null {
  if (origin == null) {
    return {
      clearAuthCache: true,
      clearHttpCache: true,
      storages: BROWSER_SITE_STORAGE_TYPES
    }
  }

  try {
    const parsed = new URL(origin)
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.origin !== origin || parsed.username || parsed.password) {
      return null
    }

    return {
      clearAuthCache: false,
      clearHttpCache: false,
      origin: parsed.origin,
      storages: BROWSER_SITE_STORAGE_TYPES
    }
  } catch {
    return null
  }
}
