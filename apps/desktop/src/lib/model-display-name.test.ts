import { describe, expect, it } from 'vitest'

import { displayModelName, displayProviderModel, displayProviderName } from './model-display-name'

describe('model display names', () => {
  it('turns raw model IDs into readable names without changing the saved ID', () => {
    expect(displayModelName('claude-opus-4-8')).toBe('Opus 4.8')
    expect(displayModelName('claude-opus-4-8', { provider: 'vibeproxy' })).toBe('Opus 4.8')
    expect(displayModelName('claude-sonnet-5')).toBe('Sonnet 5')
    expect(displayModelName('claude-haiku-4-5-20251001')).toBe('Haiku 4.5')
    expect(displayModelName('claude-haiku-4-5-20251001', { provider: 'vibeproxy' })).toBe('Haiku 4.5')
    expect(displayModelName('gpt-5.4-mini')).toBe('GPT 5.4 Mini')
    expect(displayModelName('z-ai/glm-5v-turbo')).toBe('GLM 5V Turbo')
    expect(displayModelName('gpt-5.6-sol')).toBe('GPT 5.6 Sol')
    expect(displayModelName('gpt-5.6-terra')).toBe('GPT 5.6 Terra')
    expect(displayModelName('grok-4.5')).toBe('Grok 4.5')
    expect(displayModelName('grok-composer-2.5-fast')).toBe('Grok Composer 2.5 Fast')
  })

  it('cleans provider IDs and custom provider slugs', () => {
    expect(displayProviderName('vibeproxy')).toBe('CLI Proxy')
    expect(displayProviderName('openai-codex', 'Company Gateway')).toBe('Company Gateway')
    expect(displayProviderName('openai-codex')).toBe('Codex')
    expect(displayProviderName('custom:atomic')).toBe('Atomic')
    expect(displayProviderName('atomic')).toBe('Atomic')
    expect(displayProviderName('xai')).toBe('xAI API')
    expect(displayProviderName('xai-oauth')).toBe('xAI')
  })

  it('combines readable provider and model labels', () => {
    expect(displayProviderModel('vibeproxy', 'claude-opus-4-8')).toBe('CLI Proxy · Opus 4.8')
  })
})
