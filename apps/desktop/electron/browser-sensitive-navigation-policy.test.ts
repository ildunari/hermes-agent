import { describe, expect, it } from 'vitest'

import {
  SensitiveNavigationPolicy,
  SNP_EXCEPTION_MAX_IDLE_MS,
  SNP_POLICY_VERSION,
  type SnpScope
} from './browser-sensitive-navigation-policy'

const scope = (overrides: Partial<SnpScope> = {}): SnpScope => ({
  guestGeneration: 'guest-1',
  profile: 'default',
  tabId: 'tab-1',
  taskGeneration: 7,
  taskId: 'task-1',
  workspaceId: 'workspace-1',
  ...overrides
})

describe('SensitiveNavigationPolicy canonical identity', () => {
  it.each([
    ['HTTPS://EXAMPLE.COM.:443/a?secret=1#hidden', 'https://example.com', 'https://example.com', 'example.com'],
    ['https://sub.example.com:8443/a', 'https://sub.example.com:8443', 'https://example.com', 'sub.example.com'],
    ['https://faß.de/a', 'https://xn--fa-hia.de', 'https://xn--fa-hia.de', 'xn--fa-hia.de'],
    ['https://%65xample.com/a', 'https://example.com', 'https://example.com', 'example.com'],
    ['http://0x7f.1:8080/a', 'http://127.0.0.1:8080', 'http://127.0.0.1:8080', '127.0.0.1'],
    ['https://[2001:0db8::1]:443/a', 'https://[2001:db8::1]', 'https://[2001:db8::1]', '2001:db8::1'],
    ['https://a.b.example.co.uk/a', 'https://a.b.example.co.uk', 'https://example.co.uk', 'a.b.example.co.uk'],
    ['https://a.blogspot.com/a', 'https://a.blogspot.com', 'https://a.blogspot.com', 'a.blogspot.com'],
    ['https://www.city.kawasaki.jp/a', 'https://www.city.kawasaki.jp', 'https://city.kawasaki.jp', 'www.city.kawasaki.jp'],
    ['https://localhost:9443/a', 'https://localhost:9443', 'https://localhost:9443', 'localhost'],
    ['https://printer.local/a', 'https://printer.local', 'https://printer.local', 'printer.local'],
    ['https://co.uk/a', 'https://co.uk', 'https://co.uk', 'co.uk']
  ])('canonicalizes %s with WHATWG/UTS46 and bundled PSL', (url, originKey, siteKey, asciiHost) => {
    const result = new SensitiveNavigationPolicy().classify(url)
    expect(result).toMatchObject({ asciiHost, originKey, siteKey })
    expect(JSON.stringify(result)).not.toContain('secret=1')
    expect(JSON.stringify(result)).not.toContain('hidden')
  })

  it.each([
    'https://a\u200Db.com/',
    'https://xn--/',
    'https://[::1',
    'https://example.com:99999/',
    'not a url'
  ])('fails invalid UTS46/URL input closed without reflecting it: %s', url => {
    const result = new SensitiveNavigationPolicy().classify(url)
    expect(result).toMatchObject({ disposition: 'unknown', effectiveGate: 'per-navigation', reasonCodes: ['url-invalid'] })
    expect(JSON.stringify(result)).not.toContain(url)
  })

  it('honors PSL wildcard and exception boundaries without suffix widening', () => {
    const policy = new SensitiveNavigationPolicy()
    expect(policy.classify('https://foo.kawasaki.jp').siteKey).toBe('https://foo.kawasaki.jp')
    expect(policy.classify('https://bar.foo.kawasaki.jp').siteKey).toBe('https://bar.foo.kawasaki.jp')
    expect(policy.classify('https://a.city.kawasaki.jp').siteKey).toBe('https://city.kawasaki.jp')
  })
})

describe('SensitiveNavigationPolicy precedence and failure behavior', () => {
  it('keeps hard URL credentials above an otherwise eligible exception', () => {
    let now = 1_000
    const policy = new SensitiveNavigationPolicy({ now: () => now, randomId: () => 'fixed' })
    expect(policy.createException({ ...scope(), expiresAt: now + 60_000, url: 'https://account.example.com/start' })).not.toBeNull()
    expect(policy.classify('https://user:password@account.example.com/secret', scope())).toMatchObject({
      disposition: 'sensitive', exceptionEligible: false,
      reasonCodes: ['credential-bearing-navigation']
    })
    now += 1
    expect(policy.classify('https://account.example.com/next', scope()).disposition).toBe('ordinary')
  })

  it('applies user exact-origin and explicit site scope only within the profile', () => {
    const policy = new SensitiveNavigationPolicy({ randomId: () => 'rule' })
    expect(policy.markSensitive({ profile: 'default', scope: 'origin', url: 'https://login.example.com:8443/path?x=1' })).not.toBeNull()
    expect(policy.classify('https://login.example.com:8443/other', scope()).disposition).toBe('sensitive')
    expect(policy.classify('https://login.example.com/other', scope()).disposition).toBe('unknown')
    expect(policy.classify('https://login.example.com:8443/other', scope({ profile: 'other' })).disposition).toBe('unknown')
    expect(policy.markSensitive({ profile: 'default', ruleId: 'site-rule', scope: 'site', url: 'https://account.example.net' })).not.toBeNull()
    expect(policy.classify('https://billing.example.net', scope()).disposition).toBe('sensitive')
    expect(policy.classify('http://billing.example.net', scope()).reasonCodes).not.toContain('user-marked-sensitive')
    expect(policy.markSensitive({ profile: 'default', scope: 'site', url: 'https://co.uk' })).toBeNull()
  })

  it('propagates trusted observations across sibling subdomains and invalidates exceptions', () => {
    const policy = new SensitiveNavigationPolicy({ now: () => 5_000, randomId: () => 'obs' })
    expect(policy.createException({ ...scope(), expiresAt: 60_000, url: 'https://login.example.org' })).not.toBeNull()
    expect(policy.observeSensitive({
      profile: 'default', signal: 'password-control', url: 'https://login.example.org', workspaceId: 'workspace-1'
    })).not.toBeNull()
    expect(policy.classify('https://account.example.org/settings', scope())).toMatchObject({
      disposition: 'sensitive', reasonCodes: ['password-control-observed']
    })
    expect(policy.classify('https://account.example.org/settings', scope({ workspaceId: 'other' })).disposition).toBe('unknown')
  })

  it('allows an advisory HTTP rule to be downgraded only by an exact scoped exception', () => {
    const policy = new SensitiveNavigationPolicy({ now: () => 10_000, randomId: () => 'http' })
    const before = policy.classify('http://news.example.com', scope())
    expect(before).toMatchObject({ disposition: 'sensitive', exceptionEligible: true, reasonCodes: ['insecure-http-advisory'] })
    expect(policy.createException({ ...scope(), expiresAt: 70_000, url: 'http://news.example.com' })).not.toBeNull()
    expect(policy.classify('http://cdn.example.com', scope()).disposition).toBe('ordinary')
    expect(policy.classify('https://cdn.example.com', scope()).disposition).toBe('unknown')
  })

  it.each([
    ['profile', { profile: 'other' }], ['workspace', { workspaceId: 'other' }],
    ['task', { taskId: 'other' }], ['tab', { tabId: 'other' }],
    ['task generation', { taskGeneration: 8 }], ['guest generation', { guestGeneration: 'other' }]
  ])('does not widen an exception across %s', (_label, mismatch) => {
    const policy = new SensitiveNavigationPolicy({ now: () => 1_000, randomId: () => 'scope' })
    policy.createException({ ...scope(), expiresAt: 60_000, url: 'https://example.com' })
    expect(policy.classify('https://sub.example.com', scope(mismatch as Partial<SnpScope>)).disposition).toBe('unknown')
  })

  it('expires exceptions and refuses permanent or overlong exceptions', () => {
    let now = 1_000
    const policy = new SensitiveNavigationPolicy({ now: () => now, randomId: () => 'expiry' })
    expect(policy.createException({ ...scope(), expiresAt: now + SNP_EXCEPTION_MAX_IDLE_MS + 1, url: 'https://example.com' })).toBeNull()
    expect(policy.createException({ ...scope(), expiresAt: now + 10, url: 'https://example.com' })).not.toBeNull()
    now += 11
    expect(policy.classify('https://example.com', scope()).disposition).toBe('unknown')
  })

  it.each([
    ['missing policy', { policyAsset: null }, 'policy-asset-missing'],
    ['unknown policy version', { policyAsset: { advisoryHttpRuleId: 'x', credentialRuleId: 'y', version: '99' } }, 'policy-version-incompatible'],
    ['missing PSL', { pslAsset: null }, 'psl-asset-missing'],
    ['corrupt PSL', { pslAsset: { checksum: '0', rules: 'com', version: 'bad' } }, 'psl-version-incompatible']
  ])('fails closed for %s', (_label, options, reason) => {
    const result = new SensitiveNavigationPolicy(options).classify('https://sub.example.com/path?canary=raw')
    expect(result).toMatchObject({ disposition: 'unknown', effectiveGate: 'per-navigation', exceptionEligible: false })
    expect(result.reasonCodes).toContain(reason)
    expect(JSON.stringify(result)).not.toContain('canary=raw')
  })

  it('turns evaluator exceptions into stable fail-closed output', () => {
    const policy = new SensitiveNavigationPolicy({ now: () => {throw new Error('boom')} })
    expect(policy.classify('https://example.com/private?token=secret')).toMatchObject({
      disposition: 'unknown', effectiveGate: 'per-navigation', reasonCodes: ['policy-evaluation-failed']
    })
  })

  it('exports only normalized user-rule metadata and revokes by profile', () => {
    const policy = new SensitiveNavigationPolicy({ now: () => 42, randomId: () => 'metadata' })
    const rule = policy.markSensitive({ profile: 'default', scope: 'origin', url: 'https://example.com/path?q=secret#fragment' })!
    expect(policy.exportSensitiveRules('default')).toEqual([{
      createdAt: 42, key: 'https://example.com', policyVersion: SNP_POLICY_VERSION,
      profile: 'default', ruleId: rule.ruleId, scope: 'origin'
    }])
    expect(JSON.stringify(policy.exportSensitiveRules('default'))).not.toContain('secret')
    expect(policy.revokeSensitiveRule('other', rule.ruleId)).toBe(false)
    expect(policy.revokeSensitiveRule('default', rule.ruleId)).toBe(true)
  })
})
