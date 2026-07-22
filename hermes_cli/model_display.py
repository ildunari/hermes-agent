"""Human-friendly display labels for model identifiers."""

from __future__ import annotations

import re

_DATE_TOKEN_RE = re.compile(r"^(?:20\d{6}|20\d{2}[._-]?\d{2}[._-]?\d{2})$")
_VERSION_TOKEN_RE = re.compile(r"^\d+(?:\.\d+)?$")

# Antigravity / Cloud Code Assist exposes each Gemini model as effort-suffixed
# route IDs (e.g. ``gemini-3.1-pro-low``, ``gemini-3.6-flash-high``). The
# suffix is the wire tier, not a distinct model, and the reasoning-effort
# picker already controls thinking depth — so it must not leak into the name
# shown to the user. This strips the trailing effort tier for display only;
# the raw route ID (used for routing/persistence) is never changed.
_GEMINI_EFFORT_SUFFIX_RE = re.compile(
    r"^(gemini-.+?)-(?:extra-low|low|medium|high)$", re.IGNORECASE
)


def _strip_gemini_effort_suffix(model_id: str) -> str:
    match = _GEMINI_EFFORT_SUFFIX_RE.match(model_id)
    return match.group(1) if match else model_id

_TOKEN_CASES = {
    "ai": "AI",
    "api": "API",
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "codex": "Codex",
    "composer": "Composer",
    "deepseek": "DeepSeek",
    "flash": "Flash",
    "fable": "Fable",
    "fast": "Fast",
    "gemini": "Gemini",
    "glm": "GLM",
    "gpt": "GPT",
    "grok": "Grok",
    "haiku": "Haiku",
    "highspeed": "Highspeed",
    "luna": "Luna",
    "mini": "Mini",
    "opus": "Opus",
    "pro": "Pro",
    "qwopus": "Qwopus",
    "sonnet": "Sonnet",
    "sol": "Sol",
    "spark": "Spark",
    "terra": "Terra",
    "turbo": "Turbo",
    "vision": "Vision",
    "xai": "xAI",
}


def prettify_model_label(model_id: str) -> str:
    """Return a compact button label for raw model IDs.

    Keeps the raw ID untouched for routing; this is display-only. Examples:
    ``claude-opus-4-5-20251101`` -> ``Claude Opus 4.5`` and
    ``gpt-5.3-codex-spark`` -> ``GPT 5.3 Codex Spark``.
    """
    raw = str(model_id or "").strip()
    if not raw:
        return ""

    # Drop provider namespaces like ``anthropic/claude-...``.
    short = raw.rsplit("/", 1)[-1]
    # Display-only: drop the Antigravity Gemini effort-tier suffix so the
    # reasoning selector — not the model name — carries the effort.
    short = _strip_gemini_effort_suffix(short)
    # Split common separators but keep existing decimal versions intact.
    tokens = [t for t in re.split(r"[-_:\s]+", short) if t]
    while tokens and _DATE_TOKEN_RE.match(tokens[-1]):
        tokens.pop()
    if not tokens:
        return short

    out: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        lower = token.lower()

        # Collapse adjacent numeric version fragments: 4 8 -> 4.8.
        if token.isdigit() and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            out.append(f"{token}.{tokens[i + 1]}")
            i += 2
            continue

        # Preserve compact v-prefixed major versions: v4 -> V4.
        if re.match(r"^v\d+(?:\.\d+)?$", lower):
            out.append("V" + token[1:])
        elif _VERSION_TOKEN_RE.match(token):
            out.append(token)
        elif lower in _TOKEN_CASES:
            out.append(_TOKEN_CASES[lower])
        else:
            out.append(token[:1].upper() + token[1:])
        i += 1

    return " ".join(out)
