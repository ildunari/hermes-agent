const PROVIDER_LABELS: Record<string, string> = {
  'alibaba-coding-plan': 'Alibaba Cloud Coding Plan',
  'azure-foundry': 'Azure Foundry',
  bedrock: 'AWS Bedrock',
  'copilot-acp': 'GitHub Copilot ACP',
  copilot: 'GitHub Copilot',
  custom: 'Custom Endpoint',
  deepseek: 'DeepSeek',
  gemini: 'Google AI Studio',
  huggingface: 'Hugging Face',
  'kimi-coding-cn': 'Kimi Moonshot China',
  'kimi-coding': 'Kimi / Moonshot',
  lmstudio: 'LM Studio',
  minimax: 'MiniMax',
  'minimax-cn': 'MiniMax China',
  'minimax-oauth': 'MiniMax OAuth',
  moa: 'Mixture Of Agents',
  novita: 'Novita AI',
  'ollama-cloud': 'Ollama Cloud',
  'openai-api': 'OpenAI API',
  'openai-codex': 'Codex',
  opencode: 'OpenCode',
  'opencode-go': 'OpenCode Go',
  'opencode-zen': 'OpenCode Zen',
  openrouter: 'OpenRouter',
  'qwen-oauth': 'Qwen OAuth Portal',
  stepfun: 'StepFun Step Plan',
  'tencent-tokenhub': 'Tencent TokenHub',
  vibeproxy: 'CLI Proxy',
  xai: 'xAI API',
  'xai-oauth': 'xAI',
  zai: 'zAI'
}

const WORD_LABELS: Record<string, string> = {
  acp: 'ACP',
  ai: 'AI',
  api: 'API',
  aws: 'AWS',
  cn: 'China',
  codex: 'Codex',
  composer: 'Composer',
  deepseek: 'DeepSeek',
  fable: 'Fable',
  fast: 'Fast',
  flash: 'Flash',
  glm: 'GLM',
  gmi: 'GMI',
  gpt: 'GPT',
  gpu: 'GPU',
  grok: 'Grok',
  haiku: 'Haiku',
  jan: 'Jan',
  kimi: 'Kimi',
  lm: 'LM',
  luna: 'Luna',
  m3: 'M3',
  mini: 'Mini',
  minimax: 'MiniMax',
  mimo: 'MiMo',
  nim: 'NIM',
  oauth: 'OAuth',
  openai: 'OpenAI',
  opencode: 'OpenCode',
  openrouter: 'OpenRouter',
  opus: 'Opus',
  pro: 'Pro',
  qwopus: 'Qwopus',
  qwen: 'Qwen',
  rtx: 'RTX',
  sol: 'Sol',
  sonnet: 'Sonnet',
  spark: 'Spark',
  terra: 'Terra',
  tokenhub: 'TokenHub',
  turbo: 'Turbo',
  v: 'V',
  vibeproxy: 'Vibe Proxy',
  xai: 'xAI',
  zai: 'Z.AI'
}

function titleWord(word: string): string {
  const lower = word.toLowerCase()

  if (!lower) {
    return ''
  }

  if (WORD_LABELS[lower]) {
    return WORD_LABELS[lower]
  }

  if (/^\d+(?:\.\d+)*$/.test(lower)) {
    return lower
  }

  if (/^\d+[a-z]$/.test(lower)) {
    return lower.toUpperCase()
  }

  return lower.charAt(0).toUpperCase() + lower.slice(1)
}

function collapseVersionTokens(words: string[], provider?: string): string[] {
  const out: string[] = []

  for (const word of words) {
    const previous = out[out.length - 1]

    if (/^\d+$/.test(word) && /^\d+$/.test(previous ?? '')) {
      out[out.length - 1] = `${previous}.${word}`

      continue
    }

    out.push(word)
  }

  return out
}

export function displayProviderName(provider: string, fallback?: string): string {
  const explicitLabel = (fallback || '').trim()

  if (explicitLabel) {
    return explicitLabel
  }

  const raw = provider.trim()
  const normalized = raw.toLowerCase()

  if (!raw) {
    return ''
  }

  if (PROVIDER_LABELS[normalized]) {
    return PROVIDER_LABELS[normalized]
  }

  const withoutCustom = normalized.startsWith('custom:') ? raw.slice('custom:'.length) : raw
  const cleaned = withoutCustom
    .replace(/[._-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()

  return cleaned.split(' ').filter(Boolean).map(titleWord).join(' ')
}

export function displayModelName(model: string, options: { provider?: string } = {}): string {
  const raw = (model || '').trim()

  if (!raw) {
    return ''
  }

  const visible = raw.includes('/') ? raw.split('/').filter(Boolean).pop() || raw : raw
  // Antigravity / Cloud Code Assist Gemini ids carry an effort-tier suffix
  // (gemini-3.1-pro-low, gemini-3.5-flash-extra-low). It is the wire tier, not
  // a distinct model, and the reasoning selector owns effort — strip it for
  // display only. The raw id passed to callbacks/persistence is untouched.
  const withoutEffort = visible.replace(/^(gemini-.+?)-(?:extra-low|low|medium|high)$/i, '$1')
  const withoutDatePin = withoutEffort.replace(/[-._]\d{8}$/, '')
  const cleaned = withoutDatePin
    .replace(/[._-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  const words = collapseVersionTokens(cleaned.split(' '), options.provider).filter(Boolean)

  // Claude model IDs are already shown under a Claude/VibeProxy provider row.
  // Keep the visible model name tight: "Opus 4.8", "Sonnet 5", "Haiku 4.5".
  if (words[0]?.toLowerCase() === 'claude' && words.length > 1) {
    words.shift()
  }

  return words.map(titleWord).join(' ')
}

export function displayProviderModel(provider: string, model: string, providerFallback?: string): string {
  return [displayProviderName(provider, providerFallback), displayModelName(model, { provider })]
    .filter(Boolean)
    .join(' · ')
}
