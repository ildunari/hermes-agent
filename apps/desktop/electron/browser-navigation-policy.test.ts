import { describe, expect, it, vi } from 'vitest'

import {
  BROWSER_NAVIGATION_POLICY_REVISION,
  BrowserNavigationPolicy,
  type BrowserNavigationSource,
  isPrivateBrowserAddress
} from './browser-navigation-policy'

const publicResolver = async () => ['93.184.216.34', '2606:2800:220:1:248:1893:25c8:1946']

describe('BrowserNavigationPolicy D-022 integration', () => {
  it.each<BrowserNavigationSource>([
    'cdp-navigate', 'cdp-reload', 'guest-frame', 'guest-main-frame',
    'guest-redirect', 'network-request', 'postcondition', 'trusted-activation'
  ])('treats an unseen public destination as gated unknown from %s', async source => {
    const result = await new BrowserNavigationPolicy().evaluate('https://example.com/path', {
      resolveHost: publicResolver,
      source
    })
    expect(result).toMatchObject({
      classification: 'unknown',
      disposition: 'require-grant',
      effectiveGate: 'per-navigation',
      reasonCodes: ['no-rule-proves-ordinary'],
      revision: BROWSER_NAVIGATION_POLICY_REVISION,
      scheme: 'https',
      siteKey: 'https://example.com'
    })
  })

  it.each([
    'about:blank', 'blob:https://example.com/id', 'chrome://settings', 'data:text/html,hi',
    'file:///etc/passwd', 'javascript:location="https://example.com"', 'view-source:https://example.com'
  ])('blocks forbidden scheme before DNS: %s', async url => {
    const resolveHost = vi.fn(publicResolver)
    const result = await new BrowserNavigationPolicy().evaluate(url, { resolveHost, source: 'trusted-activation' })
    expect(result).toMatchObject({ disposition: 'block', reason: 'scheme-forbidden' })
    expect(resolveHost).not.toHaveBeenCalled()
  })

  it('classifies credentials as sensitive and local destinations as exact-origin unknown', async () => {
    const policy = new BrowserNavigationPolicy()
    await expect(policy.evaluate('https://user:pass@example.com/', {
      resolveHost: publicResolver,
      source: 'cdp-navigate'
    })).resolves.toMatchObject({
      classification: 'sensitive', disposition: 'require-grant', reason: 'credential-bearing-navigation'
    })
    await expect(policy.evaluate('http://192.168.1.20:8080/', {
      resolveHost: publicResolver,
      source: 'guest-main-frame'
    })).resolves.toMatchObject({
      classification: 'unknown', disposition: 'require-grant',
      reason: 'local-or-private-unknown', siteKey: 'http://192.168.1.20:8080'
    })
  })

  it('keeps the DNS rebinding floor fail closed independently of SNP consent', async () => {
    const policy = new BrowserNavigationPolicy()
    const context = { source: 'network-request' as const }
    await expect(policy.evaluate('https://example.com', context)).resolves.toMatchObject({ disposition: 'block', reason: 'resolver-unavailable' })
    await expect(policy.evaluate('https://example.com', { ...context, resolveHost: async () => {throw new Error('dns down')} })).resolves.toMatchObject({ disposition: 'block', reason: 'dns-failed' })
    await expect(policy.evaluate('https://example.com', { ...context, resolveHost: async () => [] })).resolves.toMatchObject({ disposition: 'block', reason: 'dns-unresolved' })
    await expect(policy.evaluate('https://example.com', { ...context, resolveHost: async () => ['93.184.216.34', '10.0.0.7'] })).resolves.toMatchObject({ disposition: 'block', reason: 'dns-private-address' })
  })

  it('pre-dispatch checks method-specific navigation shape and requires grants for unknown URLs', async () => {
    const policy = new BrowserNavigationPolicy()
    await expect(policy.evaluateDebuggerCommand('Page.navigate', { url: 'https://example.com/' }, 'https://current.example/', publicResolver)).resolves.toMatchObject({
      disposition: 'require-grant', hermesCode: 'NAVIGATION_GRANT_REQUIRED'
    })
    await expect(policy.evaluateDebuggerCommand('Page.navigate', { frameId: 'untrusted', url: 'https://example.com/' }, 'https://current.example/', publicResolver)).resolves.toMatchObject({
      disposition: 'block', hermesCode: 'NAVIGATION_POLICY_BLOCKED', reason: 'navigate-arguments-invalid'
    })
    await expect(policy.evaluateDebuggerCommand('Page.navigateToHistoryEntry', { entryId: 4 }, 'https://current.example/', publicResolver)).resolves.toMatchObject({
      disposition: 'block', hermesCode: 'CDP_METHOD_BLOCKED', reason: 'raw-history-entry-forbidden'
    })
    await expect(policy.evaluateDebuggerCommand('Page.reload', { ignoreCache: true }, 'file:///etc/passwd', publicResolver)).resolves.toMatchObject({
      disposition: 'block', hermesCode: 'NAVIGATION_POLICY_BLOCKED', reason: 'scheme-forbidden'
    })
  })
})

describe('private address classifier', () => {
  it.each([
    '0.0.0.0', '10.1.2.3', '100.64.0.1', '127.0.0.1', '169.254.1.1',
    '172.16.0.1', '192.168.1.1', '198.18.0.1', '224.0.0.1',
    '::', '::1', 'fc00::1', 'fd12::1', 'fe80::1', '::ffff:127.0.0.1',
    'localhost', 'sub.localhost', 'printer.local'
  ])('classifies %s as private', address => expect(isPrivateBrowserAddress(address)).toBe(true))

  it.each(['8.8.8.8', '93.184.216.34', '2001:4860:4860::8888', 'example.com'])(
    'classifies %s as non-private', address => expect(isPrivateBrowserAddress(address)).toBe(false)
  )
})
