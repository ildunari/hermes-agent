import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, findByText, fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { DropdownMenu, DropdownMenuContent } from '@/components/ui/dropdown-menu'
import { $activeSessionId, $currentModel, $currentProvider, $currentReasoningEffort } from '@/store/session'
import { $sessionStates } from '@/store/session-states'

import { ModelMenuPanel } from './model-menu-panel'

// Radix calls these on open; jsdom doesn't implement them.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

const getGlobalModelOptions = vi.fn()

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: (...args: unknown[]) => getGlobalModelOptions(...args),
  setApiRequestProfile: vi.fn()
}))

// MoA presets now arrive as the catalog's virtual `moa` provider row (the same
// payload a remote gateway's model.options returns), not the /api/model/moa
// REST config.
const MOA_PROVIDER = { models: ['default', 'BeastMode'], name: 'Mixture of Agents', slug: 'moa' }

beforeEach(() => {
  $activeSessionId.set('runtime-1')
  $currentModel.set('')
  $currentProvider.set('')
  $currentReasoningEffort.set('')
  $sessionStates.set({})
  getGlobalModelOptions.mockResolvedValue({ providers: [MOA_PROVIDER] })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function renderPanel(onSelectModel = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  const content = render(
    <QueryClientProvider client={client}>
      <DropdownMenu open>
        <DropdownMenuContent>
          <ModelMenuPanel onSelectModel={onSelectModel} requestGateway={vi.fn() as never} />
        </DropdownMenuContent>
      </DropdownMenu>
    </QueryClientProvider>
  )

  return { onSelectModel, content }
}

describe('ModelMenuPanel model presets', () => {
  it('preserves the current reasoning effort when switching to a model without a saved preset', async () => {
    $currentReasoningEffort.set('high')
    const provider = {
      capabilities: { 'claude-fable-5': { fast: false, reasoning: true } },
      models: ['claude-fable-5'],
      name: 'VibeProxy',
      slug: 'vibeproxy'
    }
    getGlobalModelOptions.mockResolvedValue({ providers: [provider] })
    const calls: { method: string; params?: Record<string, unknown> }[] = []
    const requestGateway = vi.fn(async <T,>(method: string, params?: Record<string, unknown>) => {
      calls.push({ method, params })

      return {} as T
    })
    const onSelectModel = vi.fn(async () => true)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <DropdownMenu open>
          <DropdownMenuContent>
            <ModelMenuPanel onSelectModel={onSelectModel} requestGateway={requestGateway as never} />
          </DropdownMenuContent>
        </DropdownMenu>
      </QueryClientProvider>
    )

    const row = await findByText(document.body, /Fable 5/i)
    fireEvent.click(row)

    expect(onSelectModel).toHaveBeenCalledWith({ model: 'claude-fable-5', provider: 'vibeproxy' })
    await waitFor(() =>
      expect(calls).toEqual([
        {
          method: 'config.set',
          params: { key: 'reasoning', session_id: 'runtime-1', value: 'high' }
        }
      ])
    )
  })
})

describe('ModelMenuPanel MoA presets', () => {
  it('selecting a MoA preset switches PERSISTENTLY via onSelectModel (not the one-shot dispatch)', async () => {
    const { content, onSelectModel } = renderPanel()

    // moaOptions is async (useQuery) — wait for the preset row to mount.
    const row = await content.findByText('MoA: BeastMode')
    fireEvent.click(row)

    // #54670: must route through the persistent model-switch path
    // i.e. onSelectModel with provider 'moa' (which session-scopes live-session
    // switches), NOT a one-shot command.dispatch that reverts after a turn.
    expect(onSelectModel).toHaveBeenCalledWith({ model: 'BeastMode', provider: 'moa' })
  })

  it('shows the check on the preset that matches the current moa selection', async () => {
    $currentProvider.set('moa')
    $currentModel.set('BeastMode')
    const { content } = renderPanel()

    const row = await content.findByText('MoA: BeastMode')
    // The check codicon renders as a sibling within the same row item.
    const item = row.closest('[role="menuitem"]') ?? row.parentElement
    expect(item?.querySelector('.codicon-check')).not.toBeNull()
  })

  it('keeps the virtual moa provider out of the main model groups (presets section only)', async () => {
    const { content } = renderPanel()

    await content.findByText('MoA: BeastMode')

    // The provider group header would read "Mixture of Agents"; the presets
    // section header reads "MoA presets". Only the latter should exist.
    // Radix DropdownMenu portals its content to document.body, so assert
    // against the body (not content.container) to see the rendered items.

    // eslint-disable-next-line no-restricted-globals
    expect(document.body.textContent).toContain('MoA presets')
    // eslint-disable-next-line no-restricted-globals
    expect(document.body.textContent).not.toContain('Mixture of Agents')
  })

  it('renders presets from the catalog even before a session exists', async () => {
    $activeSessionId.set('')
    const { onSelectModel, content } = renderPanel()

    const row = await content.findByText('MoA: BeastMode')
    fireEvent.click(row)

    // Pre-session picks are UI state shipped on the next session.create — the
    // row must not be disabled and must still route through onSelectModel.
    expect(onSelectModel).toHaveBeenCalledWith({ model: 'BeastMode', provider: 'moa' })
  })
})

describe('ModelMenuPanel runtime routing', () => {
  it('shows provider identity for a same-model cross-provider fallback', async () => {
    $sessionStates.set({
      'runtime-1': {
        runtimeRouting: {
          schema_version: 1,
          state: 'finished',
          selected: { model: 'shared-model', provider: 'openai' },
          runtime: { model: 'shared-model', provider: 'anthropic' },
          fallback: { active: true, reason: 'rate_limit', chain_index: 0 }
        }
      } as ClientSessionState
    })
    const { content } = renderPanel()
    expect(await content.findByText(/OpenAI: shared-model/)).toBeTruthy()
    expect(await content.findByText(/Anthropic: shared-model/)).toBeTruthy()
  })
})
