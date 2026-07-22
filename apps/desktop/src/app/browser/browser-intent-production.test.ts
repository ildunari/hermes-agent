import { beforeEach, describe, expect, it } from 'vitest'

import { $notifications, clearNotifications } from '@/store/notifications'
import { $activeProfile } from '@/store/profile'
import { $activeSessionId, $connection, $currentCwd, $selectedStoredSessionId } from '@/store/session'

import {
  authorizeExplicitSystemPreviewIntent,
  completeExplicitBrowserResourceIntent,
  openExplicitBrowserIntent,
  openExplicitBrowserResourceIntent
} from './browser-intent-production'
import { $browserTabs, $foregroundBrowserTabId, clearBrowserTabs } from './browser-store'

const geometry = { height: 600, width: 900, x: 0, y: 0 }

describe('production browser intent routing', () => {
  beforeEach(() => {
    clearBrowserTabs()
    clearNotifications()
    $activeProfile.set('coding')
    $activeSessionId.set('session-current')
    $selectedStoredSessionId.set(null)
    $currentCwd.set('/workspace')
    $connection.set({ mode: 'local' } as never)
  })

  it('derives scope and executes a direct transcript click in the foreground', () => {
    const result = openExplicitBrowserIntent({ geometry, source: 'transcript-link', targetRef: 'https://example.test/path' })

    expect(result).toMatchObject({
      applied: true,
      resolution: {
        activation: 'execute',
        focus: true,
        kind: 'browser-tab',
        scope: { profile: 'coding', workspaceId: 'session-current' },
        targetRef: 'https://example.test/path'
      }
    })
    expect($browserTabs.get()).toHaveLength(1)
    expect($foregroundBrowserTabId.get()).toBe($browserTabs.get()[0].id)
  })

  it('keeps background and scheduled sources inert at the production entry point', () => {
    for (const source of ['background-result', 'scheduled-result'] as const) {
      expect(openExplicitBrowserIntent({ geometry, source, targetRef: 'https://example.test/' })).toMatchObject({
        applied: false,
        resolution: { activation: 'offer', focus: false, kind: 'browser-tab' }
      })
    }

    expect($browserTabs.get()).toEqual([])
  })

  it('derives remote loopback topology and refuses it until P5.2 supplies an exact grant', () => {
    $connection.set({ mode: 'remote' } as never)

    const result = openExplicitBrowserIntent({
      geometry,
      source: 'transcript-link',
      target: 'preview',
      targetRef: 'http://127.0.0.1:4173/'
    })

    expect(result).toEqual({ applied: false, resolution: { kind: 'blocked', reason: 'grant-required' } })
    expect($browserTabs.get()).toEqual([])
  })

  it('classifies every remote loopback alias as grant-bound before opening a tab', () => {
    $connection.set({ mode: 'remote' } as never)

    for (const targetRef of [
      'http://127.1.2.3:4173/',
      'http://[::1]:4173/',
      'http://[::ffff:127.0.0.9]:4173/',
      'http://[::]:4173/',
      'http://[::0]:4173/',
      'http://[::ffff:0.0.0.0]:4173/',
      'http://localhost.:4173/',
      'http://preview.localhost:4173/'
    ]) {
      expect(openExplicitBrowserIntent({ geometry, source: 'transcript-link', target: 'preview', targetRef })).toEqual({
        applied: false,
        resolution: { kind: 'blocked', reason: 'grant-required' }
      })
    }

    expect($browserTabs.get()).toEqual([])
    expect($notifications.get()[0]).toMatchObject({ kind: 'warning', title: 'Link not opened' })
  })

  it('reserves resource work without focus, then revalidates scope before applying the opaque grant', () => {
    const tab = openExplicitBrowserResourceIntent({
      geometry,
      kind: 'artifact',
      scopeSnapshot: { profile: 'coding', workspaceId: 'session-current' },
      sourceSessionId: 'session-source',
      target: '/workspace/report.pdf'
    })

    expect(tab).not.toBeNull()
    expect(tab?.url).toBe('')
    expect($foregroundBrowserTabId.get()).toBeNull()

    expect(
      completeExplicitBrowserResourceIntent(
        tab!,
        `hermes-artifact://g-${'l'.repeat(32)}/report.pdf`
      )
    ).toMatchObject({ applied: true, resolution: { kind: 'artifact-tab' } })
    expect($foregroundBrowserTabId.get()).toBe(tab?.id)

    clearBrowserTabs()
    const stale = openExplicitBrowserResourceIntent({
      geometry,
      kind: 'artifact',
      scopeSnapshot: { profile: 'coding', workspaceId: 'session-current' },
      sourceSessionId: 'session-source',
      target: '/workspace/report.pdf'
    })!
    $activeSessionId.set('session-new')

    expect(
      completeExplicitBrowserResourceIntent(stale, `hermes-artifact://g-${'m'.repeat(32)}/report.pdf`)
    ).toEqual({ applied: false, resolution: { kind: 'blocked', reason: 'scope-mismatch' } })
    expect($browserTabs.get()).toEqual([])
    expect($notifications.get()[0]).toMatchObject({
      kind: 'warning',
      title: 'Resource not opened'
    })
  })

  it('does not steal focus when the user selects another tab while a resource grant is minting', () => {
    openExplicitBrowserIntent({ geometry, source: 'transcript-link', targetRef: 'https://first.test/' })
    const tab = openExplicitBrowserResourceIntent({
      geometry,
      kind: 'artifact',
      scopeSnapshot: { profile: 'coding', workspaceId: 'session-current' },
      sourceSessionId: 'session-source',
      target: '/workspace/report.pdf'
    })!
    openExplicitBrowserIntent({ geometry, source: 'transcript-link', targetRef: 'https://second.test/' })
    const foreground = $foregroundBrowserTabId.get()

    expect(
      completeExplicitBrowserResourceIntent(tab, `hermes-artifact://g-${'n'.repeat(32)}/report.pdf`)
    ).toMatchObject({ applied: true, resolution: { focus: false, kind: 'artifact-tab' } })
    expect($foregroundBrowserTabId.get()).toBe(foreground)
  })

  it('reserves remote preview grants only for Studio loopback targets', () => {
    $connection.set({ mode: 'remote' } as never)
    const scopeSnapshot = { profile: 'coding', workspaceId: 'session-current' }

    expect(
      openExplicitBrowserResourceIntent({
        geometry,
        kind: 'preview',
        scopeSnapshot,
        sourceSessionId: 'session-current',
        target: 'http://127.0.0.1:4173/app/'
      })
    ).not.toBeNull()
    expect(
      openExplicitBrowserResourceIntent({
        geometry,
        kind: 'preview',
        scopeSnapshot,
        sourceSessionId: 'session-current',
        target: 'https://example.test/'
      })
    ).toBeNull()
  })

  it('rejects an async click when its captured profile or workspace is no longer current', () => {
    const scopeSnapshot = { profile: 'coding', workspaceId: 'session-current' }
    $activeSessionId.set('session-new')

    expect(
      openExplicitBrowserIntent({
        geometry,
        scopeSnapshot,
        source: 'transcript-link',
        targetRef: 'https://example.test/'
      })
    ).toEqual({ applied: false, resolution: { kind: 'blocked', reason: 'scope-mismatch' } })
    expect($browserTabs.get()).toEqual([])
  })

  it('authorizes a local explicit file disposition only while its captured scope remains current', () => {
    const scopeSnapshot = { profile: 'coding', workspaceId: 'session-current' }

    expect(authorizeExplicitSystemPreviewIntent(scopeSnapshot, 'file:///workspace/preview.html')).toEqual({
      kind: 'external-offer',
      reason: 'explicit-system-disposition-required'
    })

    $activeSessionId.set('session-new')
    expect(authorizeExplicitSystemPreviewIntent(scopeSnapshot, 'file:///workspace/preview.html')).toEqual({
      kind: 'blocked',
      reason: 'scope-mismatch'
    })
  })

  it('keeps remote file dispositions grant-bound instead of opening a same-path local file', () => {
    $connection.set({ mode: 'remote' } as never)

    expect(
      authorizeExplicitSystemPreviewIntent(
        { profile: 'coding', workspaceId: 'session-current' },
        'file:///studio/workspace/preview.html'
      )
    ).toEqual({ kind: 'blocked', reason: 'grant-required' })
  })

  it('allows local loopback and fails closed on invalid targets', () => {
    expect(openExplicitBrowserIntent({
      geometry,
      source: 'transcript-link',
      target: 'preview',
      targetRef: 'http://localhost:4173/'
    }).applied).toBe(true)

    clearBrowserTabs()

    expect(openExplicitBrowserIntent({
      geometry,
      source: 'transcript-link',
      targetRef: 'javascript:alert(1)'
    })).toEqual({ applied: false, resolution: { kind: 'blocked', reason: 'invalid-target' } })
    expect($browserTabs.get()).toEqual([])
  })
})