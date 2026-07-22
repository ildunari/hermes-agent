import { describe, expect, it, vi } from 'vitest'

import { BrowserConsentAuthority } from './browser-consent'

const scope = {
  category: 'permission' as const,
  guestGeneration: 'guest-a',
  operationId: 'operation-a',
  permission: 'camera',
  profile: 'coding',
  site: 'https://example.test',
  tabId: 'tab-a',
  taskGeneration: 7,
  taskId: 'task-a'
}

describe('BrowserConsentAuthority', () => {
  it('mints cryptographic correlated ids and resolves out of order by exact id and host', async () => {
    const prompts: any[] = []
    const authority = new BrowserConsentAuthority({ present: (hostId, prompt) => prompts.push({ hostId, prompt }) })
    const first = authority.request(11, scope)
    const second = authority.request(11, { ...scope, operationId: 'operation-b' })

    expect(prompts).toHaveLength(2)
    expect(prompts[0].prompt.consentId).toMatch(/^[A-Za-z0-9_-]{43}$/)
    expect(prompts[1].prompt.consentId).not.toBe(prompts[0].prompt.consentId)
    expect(authority.resolve(99, { consentId: prompts[1].prompt.consentId, decision: 'allow' })).toEqual({
      error: 'browser-consent-stale',
      ok: false
    })
    expect(authority.resolve(11, { consentId: prompts[1].prompt.consentId, decision: 'allow' })).toEqual({ ok: true })
    expect(authority.resolve(11, { consentId: prompts[0].prompt.consentId, decision: 'deny' })).toEqual({ ok: true })

    await expect(second).resolves.toMatchObject({ consentId: prompts[1].prompt.consentId, reason: 'allow' })
    await expect(first).resolves.toMatchObject({ consentId: prompts[0].prompt.consentId, reason: 'deny' })
    expect(authority.resolve(11, { consentId: prompts[1].prompt.consentId, decision: 'allow' })).toEqual({
      error: 'browser-consent-stale',
      ok: false
    })
  })

  it('expires once and revokes exact generation scopes without accepting stale responses', async () => {
    vi.useFakeTimers()
    const prompts: any[] = []
    const authority = new BrowserConsentAuthority({ present: (_hostId, prompt) => prompts.push(prompt) })
    const expiring = authority.request(11, scope, 25)

    await vi.advanceTimersByTimeAsync(26)
    await expect(expiring).resolves.toMatchObject({ reason: 'expired' })
    expect(authority.resolve(11, { consentId: prompts[0].consentId, decision: 'allow' })).toEqual({
      error: 'browser-consent-stale',
      ok: false
    })

    const retained = authority.request(11, { ...scope, guestGeneration: 'guest-b' })
    const revoked = authority.request(11, { ...scope, operationId: 'operation-c' })
    authority.revoke(prompt => prompt.guestGeneration === 'guest-a')

    await expect(revoked).resolves.toMatchObject({ reason: 'revoked' })
    expect(authority.resolve(11, { consentId: prompts[1].consentId, decision: 'allow' })).toEqual({ ok: true })
    await expect(retained).resolves.toMatchObject({ reason: 'allow' })
    vi.useRealTimers()
  })

  it('publishes exact revocation dismissal before rejecting a late allow', async () => {
    const settled = vi.fn()
    const prompts: any[] = []

    const authority = new BrowserConsentAuthority({
      present: (_hostId, prompt) => prompts.push(prompt),
      settled
    })

    const pending = authority.request(11, scope)

    authority.revoke(prompt => prompt.consentId === prompts[0].consentId)

    expect(settled).toHaveBeenCalledExactlyOnceWith(11, {
      consentId: prompts[0].consentId,
      reason: 'revoked'
    })
    expect(authority.resolve(11, { consentId: prompts[0].consentId, decision: 'allow' })).toEqual({
      error: 'browser-consent-stale',
      ok: false
    })
    await expect(pending).resolves.toEqual({ consentId: prompts[0].consentId, reason: 'revoked' })
  })

  it('allows task-ordinary only for an eligible navigation and never for an action gate', async () => {
    const prompts: any[] = []
    const authority = new BrowserConsentAuthority({ present: (_hostId, prompt) => prompts.push(prompt) })
    const navigationPolicy = {
      categoryCodes: [], classification: 'unknown' as const, exceptionEligible: true,
      policyVersion: 'd-022-snp-v1', provenance: [{ source: 'fail-closed' }],
      pslVersion: 'psl-v1', reasonCodes: ['no-rule-proves-ordinary'], urlParserVersion: 'whatwg-v1'
    }
    const navigation = authority.request(11, { ...scope, category: 'navigation', navigationPolicy })
    expect(authority.resolve(11, {
      consentId: prompts[0].consentId,
      decision: 'ordinary-for-task'
    })).toEqual({ ok: true })
    await expect(navigation).resolves.toMatchObject({ reason: 'ordinary-for-task' })

    const action = authority.request(11, { ...scope, category: 'destructive-action' })
    expect(authority.resolve(11, {
      consentId: prompts[1].consentId,
      decision: 'ordinary-for-task'
    })).toEqual({ error: 'browser-consent-decision-not-eligible', ok: false })
    expect(authority.resolve(11, { consentId: prompts[1].consentId, decision: 'deny' })).toEqual({ ok: true })
    await expect(action).resolves.toMatchObject({ reason: 'deny' })
  })
})
