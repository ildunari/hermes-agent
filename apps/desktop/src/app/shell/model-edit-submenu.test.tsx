import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuSub,
  DropdownMenuSubTrigger
} from '@/components/ui/dropdown-menu'

import { type FastControl, ModelEditSubmenu, normalizeReasoningEffort } from './model-edit-submenu'

// Radix calls these on open; jsdom doesn't implement them.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

// Render the submenu inside an open menu/sub so its content (switches) mounts.
function renderSubmenu(opts: {
  defaultEffort?: string
  effort?: string
  fastControl: FastControl
  isActive?: boolean
  onSelectModel?: (model: string) => void
  onSetOptions: (patch: { effort?: string; fast?: boolean }) => void
  provider?: string
  reasoning: boolean
  reasoningAlwaysOn?: boolean
  reasoningEfforts?: string[]
}) {
  return render(
    <DropdownMenu open>
      <DropdownMenuContent>
        <DropdownMenuSub open>
          <DropdownMenuSubTrigger>edit</DropdownMenuSubTrigger>
          <ModelEditSubmenu
            defaultEffort={opts.defaultEffort ?? 'medium'}
            effort={opts.effort ?? 'medium'}
            fastControl={opts.fastControl}
            isActive={opts.isActive ?? true}
            model="m1"
            onSelectModel={opts.onSelectModel ?? vi.fn()}
            onSetOptions={opts.onSetOptions}
            provider={opts.provider ?? 'p1'}
            reasoning={opts.reasoning}
            reasoningAlwaysOn={opts.reasoningAlwaysOn}
            reasoningEfforts={opts.reasoningEfforts}
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

  it('maps thinking-off to the only effort for an always-on model', () => {
    expect(normalizeReasoningEffort('none', ['max'], true)).toBe('max')
  })

  it('shows the Codex-supported levels without duplicate minimal or ultra choices', () => {
    renderSubmenu({
      fastControl: { kind: 'none' },
      onSetOptions: vi.fn(),
      provider: 'openai-codex',
      reasoning: true,
      reasoningEfforts: ['low', 'medium', 'high', 'xhigh', 'max']
    })

    expect(screen.queryByText('Minimal')).toBeNull()
    expect(screen.getByText('Light')).toBeTruthy()
    expect(screen.getByText('Medium')).toBeTruthy()
    expect(screen.getByText('Extra High')).toBeTruthy()
    expect(screen.getByText('Max')).toBeTruthy()
    expect(screen.queryByText('Ultra')).toBeNull()
  })

  it('shows K3 as always-on with only max selectable', () => {
    const onSetOptions = vi.fn()
    renderSubmenu({
      fastControl: { kind: 'none' },
      onSetOptions,
      provider: 'kimi-coding',
      reasoning: true,
      reasoningAlwaysOn: true,
      reasoningEfforts: ['max']
    })

    expect((screen.getByRole('switch') as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText('Max')).toBeTruthy()
    expect(screen.queryByText('Medium')).toBeNull()
    fireEvent.click(screen.getByRole('switch'))
    expect(onSetOptions).not.toHaveBeenCalled()
  })
})

// The submenu is PURE: it reports edits and never writes to a session, a
// preset store, or the gateway. That's the invariant that lets the same
// component drive a live chat session AND a detached per-task override — if it
// ever writes directly again, picking an effort for a kanban card would reach
// over and change the user's live chat.
describe('ModelEditSubmenu reports edits without performing them', () => {
  it('param fast: reports the toggle', () => {
    const onSetOptions = vi.fn()
    renderSubmenu({ fastControl: { kind: 'param', on: true }, onSetOptions, reasoning: false })

    fireEvent.click(screen.getByRole('switch'))

    expect(onSetOptions).toHaveBeenCalledWith({ fast: false })
  })

  it('thinking: toggling off reports the none level', () => {
    const onSetOptions = vi.fn()
    renderSubmenu({ fastControl: { kind: 'none' }, onSetOptions, reasoning: true })

    // Thinking starts on (medium); toggling it off reports 'none'.
    fireEvent.click(screen.getByRole('switch'))

    expect(onSetOptions).toHaveBeenCalledWith({ effort: 'none' })
  })

  it('thinking: toggling back on restores the row level, not the hardcoded default', () => {
    const onSetOptions = vi.fn()
    renderSubmenu({
      defaultEffort: 'high',
      effort: 'none',
      fastControl: { kind: 'none' },
      onSetOptions,
      reasoning: true
    })

    fireEvent.click(screen.getByRole('switch'))

    expect(onSetOptions).toHaveBeenCalledWith({ effort: 'high' })
  })

  it('variant fast: swaps the model only when the row is active', () => {
    const onSelectModel = vi.fn()
    const onSetOptions = vi.fn()

    renderSubmenu({
      fastControl: { baseId: 'm1', fastId: 'm1-fast', kind: 'variant', on: false },
      isActive: false,
      onSelectModel,
      onSetOptions,
      reasoning: false
    })

    fireEvent.click(screen.getByRole('switch'))

    // Inactive rows stay preference-only — no model switch.
    expect(onSetOptions).toHaveBeenCalledWith({ fast: true })
    expect(onSelectModel).not.toHaveBeenCalled()
  })

  it('variant fast: active row swaps to the -fast sibling', () => {
    const onSelectModel = vi.fn()
    const onSetOptions = vi.fn()

    renderSubmenu({
      fastControl: { baseId: 'm1', fastId: 'm1-fast', kind: 'variant', on: false },
      onSelectModel,
      onSetOptions,
      reasoning: false
    })

    fireEvent.click(screen.getByRole('switch'))

    expect(onSelectModel).toHaveBeenCalledWith('m1-fast')
  })
})
