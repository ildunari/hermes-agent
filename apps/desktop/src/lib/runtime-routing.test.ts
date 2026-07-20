import { describe, expect, it } from 'vitest'

import { parseRuntimeRouting } from './runtime-routing'

const validRouting = () => ({
  schema_version: 1,
  state: 'fallback_activated',
  selected: { model: ' primary ', provider: ' openai ' },
  runtime: { model: ' backup ', provider: ' anthropic ' },
  fallback: { active: true, reason: ' rate_limit ', chain_index: 0 }
})

describe('parseRuntimeRouting', () => {
  it('normalizes a complete schema-v1 payload into a fresh object', () => {
    const payload = validRouting()

    expect(parseRuntimeRouting(payload)).toEqual({
      schema_version: 1,
      state: 'fallback_activated',
      selected: { model: 'primary', provider: 'openai' },
      runtime: { model: 'backup', provider: 'anthropic' },
      fallback: { active: true, reason: 'rate_limit', chain_index: 0 }
    })
    expect(parseRuntimeRouting(payload)).not.toBe(payload)
  })

  it.each([
    null,
    [],
    { ...validRouting(), schema_version: 2 },
    { ...validRouting(), state: 'future_state' },
    { ...validRouting(), selected: { model: '', provider: 'openai' } },
    { ...validRouting(), runtime: { model: 'backup', provider: 3 } },
    { ...validRouting(), fallback: { active: 'yes', reason: 'rate_limit', chain_index: 0 } },
    { ...validRouting(), fallback: { active: true, reason: '', chain_index: 0 } },
    { ...validRouting(), fallback: { active: true, reason: 'rate_limit', chain_index: -1 } },
    { ...validRouting(), fallback: { active: true, reason: 'rate_limit', chain_index: 0.5 } }
  ])('rejects malformed routing without throwing: %j', payload => {
    expect(() => parseRuntimeRouting(payload)).not.toThrow()
    expect(parseRuntimeRouting(payload)).toBeUndefined()
  })
})
