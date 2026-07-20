import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import type { ChatBarState } from '@/app/chat/composer/types'
import type { ClientSessionState } from '@/app/types'
import { $activeSessionId, $currentModel, setCurrentModel, setCurrentModelSource } from '@/store/session'
import { $sessionStates } from '@/store/session-states'

import { ModelPill } from './model-pill'

const modelState = (over: Partial<ChatBarState['model']> = {}): ChatBarState['model'] => ({
  canSwitch: true,
  model: 'gpt-6',
  provider: 'openai',
  ...over
})

afterEach(() => {
  cleanup()
  $activeSessionId.set(null)
  setCurrentModel('')
  setCurrentModelSource('')
  $sessionStates.set({})
})

// #62055: a manual composer pick is sticky and silently overrides the
// Settings → Model default for every NEW chat. The pill must say so.
describe('ModelPill pinned-override badge', () => {
  it('shows the pin dot on a draft running a manual pick', () => {
    setCurrentModel('deepseek/deepseek-v4-flash')
    setCurrentModelSource('manual')
    $activeSessionId.set(null)

    render(<ModelPill disabled={false} model={modelState()} />)

    expect(screen.getByTestId('model-pinned-dot')).toBeTruthy()
  })

  it('stays quiet when the composer reflects the profile default', () => {
    setCurrentModel('google/gemma-4-26b-a4b-it:free')
    setCurrentModelSource('default')
    $activeSessionId.set(null)

    render(<ModelPill disabled={false} model={modelState()} />)

    expect(screen.queryByTestId('model-pinned-dot')).toBeNull()
  })

  it('stays quiet on a live session (footer shows that session, not the pin)', () => {
    setCurrentModel('deepseek/deepseek-v4-flash')
    setCurrentModelSource('manual')
    $activeSessionId.set('live-1')

    render(<ModelPill disabled={false} model={modelState()} />)

    expect(screen.queryByTestId('model-pinned-dot')).toBeNull()
  })

  it('is exercised in both render paths', () => {
    setCurrentModel('deepseek/deepseek-v4-flash')
    setCurrentModelSource('manual')
    $activeSessionId.set(null)

    // Fallback (no live menu) path.
    const { unmount } = render(<ModelPill disabled={false} model={modelState()} />)
    expect(screen.getByTestId('model-pinned-dot')).toBeTruthy()
    unmount()

    // Live-menu (dropdown) path.
    render(<ModelPill disabled={false} model={modelState({ modelMenuContent: <div /> })} />)
    expect(screen.getByTestId('model-pinned-dot')).toBeTruthy()
    expect($currentModel.get()).toBe('deepseek/deepseek-v4-flash')
  })
})

describe('ModelPill runtime routing', () => {
  const routing = (state: 'fallback_activated' | 'finished') => ({
    schema_version: 1 as const,
    state,
    selected: { model: 'primary-model', provider: 'openai' },
    runtime: { model: 'backup-model', provider: 'anthropic' },
    fallback: { active: true, reason: 'rate_limit', chain_index: 0 }
  })

  it('shows running fallback without changing selected intent', () => {
    setCurrentModel('primary-model')
    $activeSessionId.set('live-1')
    $sessionStates.set({ 'live-1': { runtimeRouting: routing('fallback_activated') } as ClientSessionState })
    render(<ModelPill disabled={false} model={modelState()} />)
    expect(screen.getByRole('button').textContent).toContain('Primary Model')
    expect(screen.getByRole('button').textContent).toContain('· Running ·')
    expect(screen.getByRole('button').textContent).toContain('Backup Model')
    expect($currentModel.get()).toBe('primary-model')
  })

  it('labels the runtime that produced the last response', () => {
    setCurrentModel('primary-model')
    $activeSessionId.set('live-1')
    $sessionStates.set({ 'live-1': { runtimeRouting: routing('finished') } as ClientSessionState })
    render(<ModelPill disabled={false} model={modelState()} />)
    expect(screen.getByRole('button').textContent).toContain('Last response ·')
    expect(screen.getByRole('button').textContent).toContain('Backup Model')
  })

  it('keeps fallback visible in compact mode with full accessible detail', () => {
    setCurrentModel('primary-model')
    $activeSessionId.set('live-1')
    $sessionStates.set({ 'live-1': { runtimeRouting: routing('finished') } as ClientSessionState })
    render(<ModelPill compact disabled={false} model={modelState()} />)

    expect(screen.getByTestId('model-fallback-indicator')).toBeTruthy()
    const button = screen.getByRole('button')
    expect(button.getAttribute('aria-label')).toContain('Last response')
    expect(button.getAttribute('aria-label')).toContain('anthropic: backup-model')
    expect(button.getAttribute('aria-label')).toContain('openai: primary-model')
  })

  it('shows provider-only fallback and exposes routing in the non-dropdown accessible name', () => {
    setCurrentModel('shared-model')
    $activeSessionId.set('live-1')
    const providerFallback = routing('fallback_activated')
    providerFallback.selected = { model: 'shared-model', provider: 'openai' }
    providerFallback.runtime = { model: 'shared-model', provider: 'anthropic' }
    $sessionStates.set({ 'live-1': { runtimeRouting: providerFallback } as ClientSessionState })
    render(<ModelPill disabled={false} model={modelState()} />)

    const button = screen.getByRole('button')
    expect(button.textContent).toContain('Shared Model')
    expect(button.textContent).toContain('· Running · Anthropic: Shared Model')
    expect(button.getAttribute('aria-label')).toContain('anthropic: shared-model')
    expect(button.getAttribute('aria-label')).toContain('openai: shared-model')
  })
})
