import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuSub,
  DropdownMenuSubTrigger
} from '@/components/ui/dropdown-menu'
import { $modelPresets, getModelPreset } from '@/store/model-presets'
import { $activeSessionId } from '@/store/session'

import { type FastControl, ModelEditSubmenu, normalizeReasoningEffort } from './model-edit-submenu'

// Radix calls these on open; jsdom doesn't implement them.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

beforeEach(() => {
  $modelPresets.set({})
  $activeSessionId.set(null)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

// Render the submenu inside an open menu/sub so its content (switches) mounts.
function renderSubmenu(opts: {
  fastControl: FastControl
  provider?: string
  reasoning: boolean
  reasoningEfforts?: string[]
  requestGateway: () => Promise<unknown>
}) {
  return render(
    <DropdownMenu open>
      <DropdownMenuContent>
        <DropdownMenuSub open>
          <DropdownMenuSubTrigger>edit</DropdownMenuSubTrigger>
          <ModelEditSubmenu
            effort="medium"
            fastControl={opts.fastControl}
            isActive
            model="m1"
            onSelectModel={vi.fn()}
            provider={opts.provider ?? 'p1'}
            reasoning={opts.reasoning}
            reasoningEfforts={opts.reasoningEfforts}
            requestGateway={opts.requestGateway as never}
          />
        </DropdownMenuSub>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

describe('ModelEditSubmenu model-aware effort options', () => {
  it('folds saved aliases onto the distinct Codex wire levels', () => {
    const supported = ['low', 'medium', 'high', 'xhigh', 'max']

    expect(normalizeReasoningEffort('minimal', supported)).toBe('low')
    expect(normalizeReasoningEffort('ultra', supported)).toBe('max')
  })

  it('preserves provider-neutral levels without a capability list', () => {
    expect(normalizeReasoningEffort('minimal')).toBe('minimal')
    expect(normalizeReasoningEffort('ultra')).toBe('ultra')
  })

  it('preserves explicit thinking-off for session writes', () => {
    expect(normalizeReasoningEffort('none', ['low', 'medium', 'high'])).toBe('none')
  })

  it('shows the Codex-supported levels without duplicate minimal or ultra choices', () => {
    renderSubmenu({
      fastControl: { kind: 'none' },
      provider: 'openai-codex',
      reasoning: true,
      reasoningEfforts: ['low', 'medium', 'high', 'xhigh', 'max'],
      requestGateway: vi.fn().mockResolvedValue({})
    })

    expect(screen.queryByText('Minimal')).toBeNull()
    expect(screen.getByText('Light')).toBeTruthy()
    expect(screen.getByText('Medium')).toBeTruthy()
    expect(screen.getByText('Extra High')).toBeTruthy()
    expect(screen.getByText('Max')).toBeTruthy()
    expect(screen.queryByText('Ultra')).toBeNull()
  })
})

// Regression: editing the active row before a live session exists must stay
// preset-only — the gateway's config.set falls back to global config when no
// session matches, so it must not be called. (Caught in the second review.)
describe('ModelEditSubmenu no-session guard', () => {
  it('param fast: records the preset but skips the gateway without a session', () => {
    const requestGateway = vi.fn().mockResolvedValue({})
    renderSubmenu({ fastControl: { kind: 'param', on: false }, reasoning: false, requestGateway })

    fireEvent.click(screen.getByRole('switch'))

    expect(getModelPreset('p1', 'm1').fast).toBe(true)
    expect(requestGateway).not.toHaveBeenCalled()
  })

  it('reasoning: records the preset but skips the gateway without a session', () => {
    const requestGateway = vi.fn().mockResolvedValue({})
    renderSubmenu({ fastControl: { kind: 'none' }, reasoning: true, requestGateway })

    // Thinking starts on (medium); toggling it off routes through patchReasoning.
    fireEvent.click(screen.getByRole('switch'))

    expect(getModelPreset('p1', 'm1').effort).toBe('none')
    expect(requestGateway).not.toHaveBeenCalled()
  })

  it('param fast: pushes to the gateway once a session is active', async () => {
    const requestGateway = vi.fn().mockResolvedValue({})
    $activeSessionId.set('sess1')
    renderSubmenu({ fastControl: { kind: 'param', on: false }, reasoning: false, requestGateway })

    fireEvent.click(screen.getByRole('switch'))

    expect(requestGateway).toHaveBeenCalledWith('config.set', { key: 'fast', session_id: 'sess1', value: 'fast' })
  })
})
