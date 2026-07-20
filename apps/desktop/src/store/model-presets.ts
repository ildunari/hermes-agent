import { atom } from 'nanostores'

import { persistString, storedString } from '@/lib/storage'

import { notifyError } from './notifications'
import { setCurrentFastMode, setCurrentReasoningEffort } from './session'
import { sessionTileDelegate } from './session-states'

const STORAGE_KEY = 'hermes.desktop.model-presets'

/** Per-model reasoning/fast preset, remembered globally across sessions and
 *  re-applied to the session whenever that model is selected. Unset dimensions
 *  fall back to the Hermes default (medium effort, no fast). */
export interface ModelPreset {
  effort?: string
  fast?: boolean
}

type RequestGateway = <T>(method: string, params?: Record<string, unknown>) => Promise<T>

/** Stable `provider::model` key (matches the visibility-store format). */
function normalizePresetProvider(provider: string): string {
  const key = provider.trim().toLowerCase()

  if (key === 'vibe' || key === 'vibe-proxy' || key === 'vibe_proxy') {
    return 'vibeproxy'
  }

  return key || provider
}

/** Stable `provider::model` key (matches the visibility-store format). */
export const modelPresetKey = (provider: string, model: string): string => `${normalizePresetProvider(provider)}::${model}`

function normalizePresetMap(presets: Record<string, ModelPreset>): Record<string, ModelPreset> {
  const next: Record<string, ModelPreset> = {}

  for (const [key, preset] of Object.entries(presets)) {
    const [provider, ...modelParts] = key.split('::')
    const normalizedKey = modelParts.length > 0 ? modelPresetKey(provider, modelParts.join('::')) : key
    next[normalizedKey] = { ...next[normalizedKey], ...preset }
  }

  return next
}

function load(): Record<string, ModelPreset> {
  const raw = storedString(STORAGE_KEY)

  if (!raw) {
    return {}
  }

  try {
    const parsed = JSON.parse(raw)

    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return {}
    }

    const normalized = normalizePresetMap(parsed as Record<string, ModelPreset>)

    if (JSON.stringify(normalized) !== JSON.stringify(parsed)) {
      persistString(STORAGE_KEY, JSON.stringify(normalized))
    }

    return normalized
  } catch {
    return {}
  }
}

export const $modelPresets = atom<Record<string, ModelPreset>>(load())

export function getModelPreset(provider: string, model: string): ModelPreset {
  return $modelPresets.get()[modelPresetKey(provider, model)] ?? {}
}

/** Merge a partial preset for one model and persist. */
export function setModelPreset(provider: string, model: string, patch: ModelPreset): void {
  const key = modelPresetKey(provider, model)
  const next = { ...$modelPresets.get(), [key]: { ...$modelPresets.get()[key], ...patch } }

  $modelPresets.set(next)
  persistString(STORAGE_KEY, JSON.stringify(next))
}

/** Apply a model's preset to the composer, then push it to a live session.
 *  `undefined` skips that dimension; values are capability-gated upstream.
 *  Without a session the local draft still needs the preset, but must not call
 *  `config.set`: that falls back to persistent profile config when no session
 *  matches and would rewrite the user's defaults.
 *
 *  `primary: false` scopes the optimistic write to the tile's session slice —
 *  a tile's picker must not clobber the primary composer's effort/fast. */
export async function applyModelPreset(
  { effort, fast }: ModelPreset,
  ctx: { failMessage: string; primary?: boolean; request: RequestGateway; sessionId: null | string }
): Promise<void> {
  if (ctx.primary ?? true) {
    if (effort !== undefined) {
      setCurrentReasoningEffort(effort)
    }

    if (fast !== undefined) {
      setCurrentFastMode(fast)
    }
  } else if (ctx.sessionId) {
    sessionTileDelegate()?.updateSession(ctx.sessionId, state => ({
      ...state,
      ...(effort !== undefined ? { reasoningEffort: effort } : {}),
      ...(fast !== undefined ? { fast } : {})
    }))
  }

  if (!ctx.sessionId) {
    return
  }

  try {
    if (effort !== undefined) {
      await ctx.request('config.set', { key: 'reasoning', session_id: ctx.sessionId, value: effort })
    }

    if (fast !== undefined) {
      await ctx.request('config.set', { key: 'fast', session_id: ctx.sessionId, value: fast ? 'fast' : 'normal' })
    }
  } catch (err) {
    notifyError(err, ctx.failMessage)
  }
}
