import crypto from 'node:crypto'
import net from 'node:net'
import { domainToASCII, domainToUnicode } from 'node:url'

import { BUNDLED_PSL_RULES, BUNDLED_PSL_RULES_SHA256, BUNDLED_PSL_VERSION } from './browser-public-suffix-asset'

export const SNP_POLICY_VERSION = '1.0.0'
export const SNP_URL_PARSER_VERSION = 'whatwg-url-uts46-v1'
export const SNP_EXCEPTION_MAX_IDLE_MS = 30 * 60_000

const OBSERVATIONS = Object.freeze({
  'account-security-control': ['account_security', 'account-security-control-observed'],
  'client-certificate': ['credential_authentication', 'client-certificate-observed'],
  fedcm: ['credential_authentication', 'fedcm-observed'],
  'financial-control': ['payment_financial', 'financial-control-observed'],
  'http-auth': ['credential_authentication', 'http-auth-observed'],
  'password-control': ['credential_authentication', 'password-control-observed'],
  'payment-control': ['payment_financial', 'payment-control-observed'],
  'private-data-portal': ['private_user_data_portal', 'private-data-portal-observed'],
  webauthn: ['credential_authentication', 'webauthn-observed']
} as const)

export type SnpObservation = keyof typeof OBSERVATIONS
export type SnpDisposition = 'ordinary' | 'sensitive' | 'unknown'
export interface SnpScope { guestGeneration: string; profile: string; tabId: string; taskGeneration: number; taskId: string; workspaceId: string }
export interface SnpIdentity { asciiHost: string; originKey: string; port: string; registrableDomain: string | null; scheme: 'http' | 'https'; siteKey: string; unicodeHost: string }
export type SnpProvenance =
  | { policyVersion: string; ruleId: string; severity: 'advisory' | 'hard'; source: 'builtin' }
  | { ruleId: string; scope: 'origin' | 'site'; source: 'user' }
  | { ruleId: string; signal: SnpObservation; source: 'observation' }
  | { exceptionId: string; expiresAt: number; source: 'exception' }
  | { source: 'fail-closed' }
export interface SnpResult {
  asciiHost?: string; categoryCodes: readonly string[]; disposition: SnpDisposition
  effectiveGate: 'per-navigation' | 'task-site-consent'; exceptionEligible: boolean
  originKey?: string; provenance: readonly SnpProvenance[]; pslVersion: string
  reasonCodes: readonly string[]; scheme?: string; siteKey?: string; unicodeHost?: string; urlParserVersion: string
}
export interface SnpPolicyAsset { advisoryHttpRuleId: string; credentialRuleId: string; version: string }
export interface SnpPslAsset { checksum: string; rules: string; version: string }
export interface SnpUserRule { createdAt: number; key: string; policyVersion: string; profile: string; ruleId: string; scope: 'origin' | 'site' }
export interface SnpException extends SnpScope { createdAt: number; exceptionId: string; expiresAt: number; lastUsedAt: number; policyVersion: string; pslVersion: string; siteKey: string }
export interface SensitiveNavigationPolicyOptions { now?: () => number; policyAsset?: SnpPolicyAsset | null; pslAsset?: SnpPslAsset | null; randomId?: () => string }

const POLICY = Object.freeze({ advisoryHttpRuleId: 'builtin.insecure-http.v1', credentialRuleId: 'builtin.credential-bearing-url.v1', version: SNP_POLICY_VERSION })
const PSL = Object.freeze({ checksum: BUNDLED_PSL_RULES_SHA256, rules: BUNDLED_PSL_RULES, version: BUNDLED_PSL_VERSION })
const unique = (values: readonly string[]) => Object.freeze([...new Set(values)])
const hostKey = (host: string) => net.isIP(host) === 6 ? `[${host}]` : host
const validScope = (scope?: SnpScope): scope is SnpScope => Boolean(scope?.profile && scope.workspaceId && scope.taskId && scope.tabId && scope.guestGeneration && Number.isSafeInteger(scope.taskGeneration) && scope.taskGeneration > 0)

class PublicSuffixMatcher {
  readonly #exact = new Set<string>()
  readonly #exceptions = new Set<string>()
  readonly #wildcards = new Set<string>()
  constructor(asset: SnpPslAsset) {
    if (asset.version !== BUNDLED_PSL_VERSION) {throw new Error('psl-version-incompatible')}
    const checksum = crypto.createHash('sha256').update(asset.rules).digest('hex')
    if (checksum !== asset.checksum || checksum !== BUNDLED_PSL_RULES_SHA256) {throw new Error('psl-corrupt')}
    for (const raw of asset.rules.split('\n')) {
      const exception = raw.startsWith('!'); const wildcard = raw.startsWith('*.')
      const source = exception ? raw.slice(1) : wildcard ? raw.slice(2) : raw
      const ascii = domainToASCII(source.toLowerCase()).replace(/\.$/u, '')
      if (!ascii || ascii.includes('..')) {throw new Error('psl-corrupt')}
      ;(exception ? this.#exceptions : wildcard ? this.#wildcards : this.#exact).add(ascii)
    }
    if (!this.#exact.has('com') || !this.#exact.has('co.uk')) {throw new Error('psl-corrupt')}
  }
  registrableDomain(host: string): string | null {
    const labels = host.split('.'); if (labels.length < 2) {return null}
    let suffixLength = 1; let exceptionLength = 0
    for (let index = 0; index < labels.length; index += 1) {
      const candidate = labels.slice(index).join('.'); const length = labels.length - index
      if (this.#exceptions.has(candidate)) {exceptionLength = Math.max(exceptionLength, length - 1)}
      if (this.#exact.has(candidate)) {suffixLength = Math.max(suffixLength, length)}
      if (index < labels.length - 1 && this.#wildcards.has(labels.slice(index + 1).join('.'))) {suffixLength = Math.max(suffixLength, length)}
    }
    if (exceptionLength) {suffixLength = exceptionLength}
    return labels.length > suffixLength ? labels.slice(-(suffixLength + 1)).join('.') : null
  }
}

interface ObservationRecord { profile: string; ruleId: string; signal: SnpObservation; siteKey: string; workspaceId: string }

export class SensitiveNavigationPolicy {
  readonly #exceptions = new Map<string, SnpException>()
  readonly #now: () => number
  readonly #observations = new Map<string, ObservationRecord>()
  readonly #policy: SnpPolicyAsset | null
  readonly #policyFailure: string | null
  readonly #psl: PublicSuffixMatcher | null
  readonly #pslFailure: string | null
  readonly #pslVersion: string
  readonly #randomId: () => string
  readonly #userRules = new Map<string, SnpUserRule>()

  constructor(options: SensitiveNavigationPolicyOptions = {}) {
    this.#now = options.now ?? Date.now; this.#randomId = options.randomId ?? crypto.randomUUID
    const policy = options.policyAsset === undefined ? POLICY : options.policyAsset
    this.#policyFailure = !policy ? 'policy-asset-missing' : policy.version !== SNP_POLICY_VERSION || policy.credentialRuleId !== POLICY.credentialRuleId || policy.advisoryHttpRuleId !== POLICY.advisoryHttpRuleId ? 'policy-version-incompatible' : null
    this.#policy = this.#policyFailure ? null : policy
    const pslAsset = options.pslAsset === undefined ? PSL : options.pslAsset
    let psl: PublicSuffixMatcher | null = null; let failure: string | null = null
    if (!pslAsset) {failure = 'psl-asset-missing'} else {
      try {psl = new PublicSuffixMatcher(pslAsset)} catch (error) {failure = error instanceof Error && error.message === 'psl-version-incompatible' ? 'psl-version-incompatible' : 'psl-asset-corrupt'}
    }
    this.#psl = psl; this.#pslFailure = failure; this.#pslVersion = psl ? pslAsset!.version : 'unavailable'
  }

  identity(parsed: URL): SnpIdentity | null {
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {return null}
    const scheme = parsed.protocol.slice(0, -1) as 'http' | 'https'
    const canonicalHost = parsed.hostname.replace(/^\[|\]$/g, '').replace(/\.$/u, '').toLowerCase()
    const family = net.isIP(canonicalHost); const asciiHost = family ? canonicalHost : domainToASCII(canonicalHost)
    if (!asciiHost || (!family && asciiHost !== canonicalHost)) {return null}
    const port = parsed.port; const originKey = `${scheme}://${hostKey(asciiHost)}${port ? `:${port}` : ''}`
    const exactSite = Boolean(family || isPrivateAddress(asciiHost))
    const registrableDomain = !exactSite && this.#psl ? this.#psl.registrableDomain(asciiHost) : null
    return Object.freeze({ asciiHost, originKey, port, registrableDomain, scheme, siteKey: registrableDomain ? `${scheme}://${registrableDomain}` : originKey, unicodeHost: family ? asciiHost : domainToUnicode(asciiHost) })
  }

  markSensitive(input: { profile: string; ruleId?: string; scope: 'origin' | 'site'; url: string }) {
    const identity = this.#identityFromUrl(input.url)
    if (!identity || !this.#policy || !this.#psl || !input.profile || input.scope === 'site' && !identity.registrableDomain) {return null}
    const rule = Object.freeze({ createdAt: this.#now(), key: input.scope === 'origin' ? identity.originKey : identity.siteKey, policyVersion: this.#policy.version, profile: input.profile, ruleId: input.ruleId ?? `user-${this.#randomId()}`, scope: input.scope })
    this.#userRules.set(rule.ruleId, rule); return rule
  }
  revokeSensitiveRule(profile: string, ruleId: string) {const rule = this.#userRules.get(ruleId); return Boolean(rule?.profile === profile && this.#userRules.delete(ruleId))}
  exportSensitiveRules(profile: string) {return Object.freeze([...this.#userRules.values()].filter(rule => rule.profile === profile).map(rule => ({ ...rule })))}

  observeSensitive(input: { profile: string; ruleId?: string; signal: SnpObservation; url: string; workspaceId: string }) {
    const identity = this.#identityFromUrl(input.url)
    if (!identity || !this.#policy || !input.profile || !input.workspaceId || !(input.signal in OBSERVATIONS)) {return null}
    const record = Object.freeze({ profile: input.profile, ruleId: input.ruleId ?? `observation-${this.#randomId()}`, signal: input.signal, siteKey: identity.siteKey, workspaceId: input.workspaceId })
    this.#observations.set(`${record.profile}\0${record.workspaceId}\0${record.siteKey}\0${record.signal}`, record)
    for (const [id, exception] of this.#exceptions) {if (exception.profile === record.profile && exception.workspaceId === record.workspaceId && exception.siteKey === record.siteKey) {this.#exceptions.delete(id)}}
    return record
  }

  createException(input: SnpScope & { expiresAt: number; url: string }) {
    const identity = this.#identityFromUrl(input.url); if (!identity || !validScope(input) || !this.#policy || !this.#psl) {return null}
    const before = this.classify(input.url, input); const now = this.#now()
    if (!before.exceptionEligible || !Number.isSafeInteger(input.expiresAt) ||
      input.expiresAt <= now || input.expiresAt > now + SNP_EXCEPTION_MAX_IDLE_MS) {return null}
    const exception = Object.freeze({ createdAt: now, exceptionId: `exception-${this.#randomId()}`, expiresAt: input.expiresAt, guestGeneration: input.guestGeneration, lastUsedAt: now, policyVersion: this.#policy.version, profile: input.profile, pslVersion: this.#pslVersion, siteKey: identity.siteKey, tabId: input.tabId, taskGeneration: input.taskGeneration, taskId: input.taskId, workspaceId: input.workspaceId })
    this.#exceptions.set(exception.exceptionId, exception); return exception
  }
  revokeException(exceptionId: string) {return this.#exceptions.delete(exceptionId)}

  classify(rawUrl: unknown, scope?: SnpScope): SnpResult {
    let parsed: URL
    try {if (typeof rawUrl !== 'string' || !rawUrl || rawUrl.length > 16_384) {throw new Error()}; parsed = new URL(rawUrl)} catch {return this.#failed(['url-invalid'])}
    const scheme = parsed.protocol.replace(/:$/u, '')
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {return this.#failed(['scheme-forbidden'], { scheme })}
    const identity = this.identity(parsed); if (!identity) {return this.#failed(['url-canonicalization-failed'], { scheme })}
    try {return this.#classifyParsed(parsed, identity, scope)} catch {return this.#failed(['policy-evaluation-failed'], this.#identityFields(identity))}
  }

  #classifyParsed(parsed: URL, identity: SnpIdentity, scope?: SnpScope): SnpResult {
    const now = this.#now(); const reasons: string[] = []; const categories: string[] = []; const provenance: SnpProvenance[] = []
    const assetFailure = this.#policyFailure ?? this.#pslFailure
    if (assetFailure) {reasons.push(assetFailure); provenance.push({ source: 'fail-closed' })}
    if (this.#policy && (parsed.username || parsed.password)) {reasons.push('credential-bearing-navigation'); categories.push('credential_authentication'); provenance.push({ policyVersion: this.#policy.version, ruleId: this.#policy.credentialRuleId, severity: 'hard', source: 'builtin' })}
    if (scope) {
      for (const rule of this.#userRules.values()) {if (rule.profile === scope.profile && (rule.scope === 'origin' ? rule.key === identity.originKey : rule.key === identity.siteKey)) {reasons.push('user-marked-sensitive'); categories.push('user_sensitive'); provenance.push({ ruleId: rule.ruleId, scope: rule.scope, source: 'user' })}}
      for (const observation of this.#observations.values()) {if (observation.profile === scope.profile && observation.workspaceId === scope.workspaceId && observation.siteKey === identity.siteKey) {const [category, reason] = OBSERVATIONS[observation.signal]; reasons.push(reason); categories.push(category); provenance.push({ ruleId: observation.ruleId, signal: observation.signal, source: 'observation' })}}
    }
    const hard = provenance.some(row => row.source === 'user' || row.source === 'observation' || row.source === 'builtin' && row.severity === 'hard')
    if (hard) {return this.#result('sensitive', reasons, categories, provenance, identity, false)}
    const advisory = Boolean(this.#policy && identity.scheme === 'http' && !isPrivateAddress(identity.asciiHost))
    if (advisory) {reasons.push('insecure-http-advisory'); categories.push('insecure_transport'); provenance.push({ policyVersion: this.#policy!.version, ruleId: this.#policy!.advisoryHttpRuleId, severity: 'advisory', source: 'builtin' })}
    let exception: SnpException | null = null
    if (!assetFailure && validScope(scope)) {
      for (const [id, candidate] of this.#exceptions) {
        if (candidate.expiresAt < now || candidate.lastUsedAt + SNP_EXCEPTION_MAX_IDLE_MS < now) {this.#exceptions.delete(id)}
        else if (candidate.profile === scope.profile && candidate.workspaceId === scope.workspaceId && candidate.taskId === scope.taskId && candidate.taskGeneration === scope.taskGeneration && candidate.tabId === scope.tabId && candidate.guestGeneration === scope.guestGeneration && candidate.siteKey === identity.siteKey && candidate.policyVersion === SNP_POLICY_VERSION && candidate.pslVersion === this.#pslVersion) {exception = Object.freeze({ ...candidate, lastUsedAt: now }); this.#exceptions.set(id, exception); break}
      }
    }
    if (exception) {reasons.push('temporary-ordinary-exception'); provenance.push({ exceptionId: exception.exceptionId, expiresAt: exception.expiresAt, source: 'exception' }); return this.#result('ordinary', reasons, categories, provenance, identity, false)}
    if (advisory) {return this.#result('sensitive', reasons, categories, provenance, identity, true)}
    if (isPrivateAddress(identity.asciiHost) || !identity.registrableDomain) {reasons.push('local-or-private-unknown'); categories.push('local_or_private')}
    reasons.push('no-rule-proves-ordinary'); if (!provenance.length) {provenance.push({ source: 'fail-closed' })}
    return this.#result('unknown', reasons, categories, provenance, identity, !assetFailure)
  }

  #result(disposition: SnpDisposition, reasons: string[], categories: string[], provenance: SnpProvenance[], identity: SnpIdentity, exceptionEligible: boolean): SnpResult {
    return Object.freeze({ ...this.#identityFields(identity), categoryCodes: unique(categories), disposition, effectiveGate: disposition === 'ordinary' ? 'task-site-consent' : 'per-navigation', exceptionEligible, provenance: Object.freeze(provenance), reasonCodes: unique(reasons), urlParserVersion: SNP_URL_PARSER_VERSION })
  }
  #failed(reasonCodes: string[], partial: Partial<SnpResult> = {}): SnpResult {return Object.freeze({ categoryCodes: Object.freeze([]), disposition: 'unknown', effectiveGate: 'per-navigation', exceptionEligible: false, provenance: Object.freeze([{ source: 'fail-closed' as const }]), pslVersion: partial.pslVersion ?? 'unavailable', reasonCodes: unique(reasonCodes), urlParserVersion: SNP_URL_PARSER_VERSION, ...partial })}
  #identityFields(identity: SnpIdentity) {return { asciiHost: identity.asciiHost, originKey: identity.originKey, pslVersion: this.#pslVersion, scheme: identity.scheme, siteKey: identity.siteKey, unicodeHost: identity.unicodeHost }}
  #identityFromUrl(rawUrl: string) {try {return this.identity(new URL(rawUrl))} catch {return null}}
}

export function isPrivateAddress(rawHostname: string): boolean {
  const host = rawHostname.toLowerCase().replace(/^\[|\]$/g, '').replace(/\.$/u, '')
  if (!host || host === 'localhost' || host.endsWith('.localhost') || host.endsWith('.local')) {return true}
  if (net.isIP(host) === 4) {const [a, b] = host.split('.').map(Number); return a === 0 || a === 10 || a === 127 || a === 100 && b >= 64 && b <= 127 || a === 169 && b === 254 || a === 172 && b >= 16 && b <= 31 || a === 192 && (b === 0 || b === 168) || a === 198 && (b === 18 || b === 19) || a >= 224}
  if (net.isIP(host) !== 6) {return false}
  const first = Number.parseInt(host.split(':')[0] || '0', 16)
  return host === '::' || host === '::1' || (first & 0xfe00) === 0xfc00 || (first & 0xffc0) === 0xfe80 || /^::ffff:(?:127\.|7f00:|a00:|c0a8:)/iu.test(host)
}
