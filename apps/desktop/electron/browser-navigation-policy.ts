import net from 'node:net'

import {
  isPrivateAddress,
  SensitiveNavigationPolicy,
  type SnpProvenance,
  type SnpResult,
  type SnpScope
} from './browser-sensitive-navigation-policy'

export const BROWSER_NAVIGATION_POLICY_REVISION = 'd-022-snp-v1'

const NAVIGATION_SOURCES = new Set<BrowserNavigationSource>([
  'cdp-navigate', 'cdp-reload', 'guest-frame', 'guest-main-frame',
  'guest-redirect', 'network-request', 'postcondition', 'trusted-activation'
])
const BLOCKING_REASONS = new Set(['navigation-source-invalid', 'scheme-forbidden', 'url-canonicalization-failed', 'url-invalid'])

export type BrowserNavigationSource =
  | 'cdp-navigate' | 'cdp-reload' | 'guest-frame' | 'guest-main-frame'
  | 'guest-redirect' | 'network-request' | 'postcondition' | 'trusted-activation'

export interface BrowserNavigationDecision {
  asciiHost?: string
  categoryCodes: readonly string[]
  classification: 'ordinary' | 'sensitive' | 'unknown'
  disposition: 'allow' | 'block' | 'require-grant'
  effectiveGate: 'per-navigation' | 'task-site-consent'
  exceptionEligible: boolean
  originKey?: string
  provenance: readonly SnpProvenance[]
  pslVersion: string
  reason: string
  reasonCodes: readonly string[]
  revision: string
  scheme?: string
  siteKey?: string
  unicodeHost?: string
  urlParserVersion: string
}

export interface BrowserNavigationContext {
  resolveHost?: (hostname: string) => Promise<readonly string[]>
  scope?: SnpScope
  source: BrowserNavigationSource
}

export interface BrowserDebuggerDecision extends BrowserNavigationDecision {
  hermesCode?: 'CDP_METHOD_BLOCKED' | 'NAVIGATION_GRANT_REQUIRED' | 'NAVIGATION_POLICY_BLOCKED'
}

function fromSnp(result: SnpResult): BrowserNavigationDecision {
  const block = result.reasonCodes.some(reason => BLOCKING_REASONS.has(reason))
  return Object.freeze({
    ...result,
    classification: result.disposition,
    disposition: block ? 'block' : result.disposition === 'ordinary' ? 'allow' : 'require-grant',
    reason: result.reasonCodes[0] ?? 'policy-evaluation-failed',
    revision: BROWSER_NAVIGATION_POLICY_REVISION
  })
}

function failed(reason: string, disposition: BrowserNavigationDecision['disposition'] = 'block'): BrowserNavigationDecision {
  return Object.freeze({
    categoryCodes: Object.freeze([]),
    classification: 'unknown',
    disposition,
    effectiveGate: 'per-navigation',
    exceptionEligible: false,
    provenance: Object.freeze([{ source: 'fail-closed' as const }]),
    pslVersion: 'unavailable',
    reason,
    reasonCodes: Object.freeze([reason]),
    revision: BROWSER_NAVIGATION_POLICY_REVISION,
    urlParserVersion: 'whatwg-url-uts46-v1'
  })
}

export const isPrivateBrowserAddress = isPrivateAddress

export class BrowserNavigationPolicy {
  readonly sensitive: SensitiveNavigationPolicy

  constructor(sensitive = new SensitiveNavigationPolicy()) {
    this.sensitive = sensitive
  }

  canonicalize(rawUrl: unknown): URL | null {
    if (typeof rawUrl !== 'string' || !rawUrl || rawUrl.length > 16_384) {return null}
    try {return new URL(rawUrl)} catch {return null}
  }

  canonicalIdentity(rawUrl: unknown) {
    const parsed = this.canonicalize(rawUrl)
    return parsed ? this.sensitive.identity(parsed) : null
  }

  lexicalDecision(rawUrl: unknown, source: BrowserNavigationSource, scope?: SnpScope): BrowserNavigationDecision {
    if (!NAVIGATION_SOURCES.has(source)) {return failed('navigation-source-invalid')}
    return fromSnp(this.sensitive.classify(rawUrl, scope))
  }

  async evaluate(rawUrl: unknown, context: BrowserNavigationContext): Promise<BrowserNavigationDecision> {
    const lexical = this.lexicalDecision(rawUrl, context.source, context.scope)
    if (lexical.disposition === 'block') {return lexical}
    const parsed = this.canonicalize(rawUrl)
    if (!parsed) {return failed('url-invalid')}
    const hostname = parsed.hostname.replace(/^\[|\]$/g, '')
    if (isPrivateBrowserAddress(hostname) || net.isIP(hostname)) {return lexical}
    if (!context.resolveHost) {return failed('resolver-unavailable')}

    try {
      const addresses = await context.resolveHost(parsed.hostname)
      if (!Array.isArray(addresses) || addresses.length === 0) {return failed('dns-unresolved')}
      if (addresses.some(address => typeof address !== 'string' || isPrivateBrowserAddress(address))) {
        return failed('dns-private-address')
      }
    } catch {
      return failed('dns-failed')
    }
    return lexical
  }

  async evaluateDebuggerCommand(
    method: string,
    params: Record<string, unknown>,
    currentUrl: string,
    resolveHost: BrowserNavigationContext['resolveHost'],
    scope?: SnpScope
  ): Promise<BrowserDebuggerDecision> {
    if (method === 'Page.navigateToHistoryEntry') {
      return { ...failed('raw-history-entry-forbidden'), hermesCode: 'CDP_METHOD_BLOCKED' }
    }
    let target: unknown
    let source: BrowserNavigationSource
    if (method === 'Page.navigate') {
      if (Object.keys(params).some(key => key !== 'url') || typeof params.url !== 'string' || !params.url) {
        return { ...failed('navigate-arguments-invalid'), hermesCode: 'NAVIGATION_POLICY_BLOCKED' }
      }
      target = params.url
      source = 'cdp-navigate'
    } else if (method === 'Page.reload') {
      if (
        Object.keys(params).some(key => key !== 'ignoreCache' && key !== 'loaderId') ||
        ('ignoreCache' in params && typeof params.ignoreCache !== 'boolean') ||
        ('loaderId' in params && typeof params.loaderId !== 'string')
      ) {
        return { ...failed('reload-arguments-invalid'), hermesCode: 'NAVIGATION_POLICY_BLOCKED' }
      }
      target = currentUrl
      source = 'cdp-reload'
    } else {
      return {
        ...failed('non-navigation-method', 'allow'),
        classification: 'ordinary',
        effectiveGate: 'task-site-consent'
      }
    }
    const result = await this.evaluate(target, { resolveHost, scope, source })
    return {
      ...result,
      hermesCode: result.disposition === 'require-grant'
        ? 'NAVIGATION_GRANT_REQUIRED'
        : result.disposition === 'block' ? 'NAVIGATION_POLICY_BLOCKED' : undefined
    }
  }
}

export const browserNavigationPolicy = new BrowserNavigationPolicy()

/** True only when local policy proves the destination ordinary. */
export function isAllowedBrowserNavigation(rawUrl: unknown): boolean {
  return browserNavigationPolicy.lexicalDecision(rawUrl, 'guest-main-frame').disposition === 'allow'
}
