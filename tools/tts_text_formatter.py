"""Prepare assistant text for spoken text-to-speech playback.

The visible assistant transcript can be full of markdown, file paths, media tags,
and implementation-report bullets.  This module derives a transient spoken
version for read-aloud / voice playback without mutating the stored message.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Any, Callable, Optional


_MEDIA_LINE_RE = re.compile(r"(?im)^\s*(?:\[\[audio_as_voice\]\]\s*)?MEDIA:\S+\s*$")
_MEDIA_REF_RE = re.compile(r"\bMEDIA:\S+", re.I)
_MEDIA_TOKEN_RE = re.compile(r"\[\[(?:audio_as_voice|as_document)\]\]")
_FENCED_CODE_RE = re.compile(r"```[\s\S]*?(?:```|$)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_URL_RE = re.compile(r"\bhttps?://\S+", re.I)
_TTS_AUDIO_PATH_RE = re.compile(r"(?:/Users/|~/)[^\s`'\")\]]*tts_\d{8}_\d{6}\.(?:mp3|wav|ogg|opus|flac)", re.I)
_ABSOLUTE_PATH_RE = re.compile(r"(?:/Users/[^\s`'\")\]]+|~/(?:[^\s`'\")\]]+))")
_TTS_FILENAME_RE = re.compile(r"\btts_\d{8}_\d{6}\.(?:mp3|wav|ogg|opus|flac)\b", re.I)
_WHITESPACE_RE = re.compile(r"[ \t]+")

_SPOKEN_PROMPT = """Rewrite assistant text for spoken text-to-speech playback.

Return only the spoken text. Preserve factual claims, numbers, names, outcomes,
and caveats. Do not add new facts. Make it sound like a concise human spoken
reply, not a markdown report.

Remove or paraphrase anything that should not be read aloud: markdown bullets,
tables, code fences, MEDIA tags, audio_as_voice markers, raw URLs, long file
paths, generated filenames, and implementation log noise. Prefer one to three
natural paragraphs. Expand awkward technical forms only when it helps speech.

Assistant text:
{text}
"""


def deterministic_spoken_cleanup(text: str, *, max_chars: int = 4000) -> str:
    """Return a safe deterministic spoken version of *text*.

    This is deliberately conservative: it removes presentation artifacts and
    path/media noise but does not invent summaries. It is used before any model
    rewrite and as the fallback when the model formatter is unavailable.
    """
    if not isinstance(text, str):
        return ""

    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _MEDIA_LINE_RE.sub(" ", cleaned)
    cleaned = _MEDIA_REF_RE.sub(" ", cleaned)
    cleaned = _MEDIA_TOKEN_RE.sub(" ", cleaned)
    cleaned = _FENCED_CODE_RE.sub(" ", cleaned)
    cleaned = _MARKDOWN_LINK_RE.sub(r"\1", cleaned)
    cleaned = _INLINE_CODE_RE.sub(r"\1", cleaned)
    cleaned = _URL_RE.sub(" a link ", cleaned)
    cleaned = _TTS_AUDIO_PATH_RE.sub("the generated audio file", cleaned)
    cleaned = _TTS_FILENAME_RE.sub("the generated audio file", cleaned)
    cleaned = _ABSOLUTE_PATH_RE.sub("the generated file", cleaned)

    lines: list[str] = []
    for raw_line in cleaned.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        # Drop markdown table separators and low-value code-ish rows.
        if re.fullmatch(r"[:\-\s|]+", line):
            continue
        line = re.sub(r"^#{1,6}\s+", "", line)
        line = re.sub(r"^>\s*", "", line)
        line = re.sub(r"^\s*(?:[-+*]|\d+[.)]|\[[ xX]\])\s+", "", line)
        line = line.replace("|", ", ")
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        line = re.sub(r"\*(.+?)\*", r"\1", line)
        line = re.sub(r"[_~]", "", line)
        line = _WHITESPACE_RE.sub(" ", line).strip(" -")
        if line:
            lines.append(line)

    cleaned = ". ".join(lines)
    cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)
    cleaned = re.sub(r"(?:\.\s*){2,}", ". ", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()

    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:") + "."
    return cleaned


def _score_candidate(text: str) -> int:
    if not text:
        return -100
    lowered = text.lower()
    score = 0
    bad_markers = ["media:", "[[audio_as_voice]]", "/users/", "```", "|---"]
    score -= 25 * sum(marker in lowered for marker in bad_markers)
    score -= 10 * len(_TTS_FILENAME_RE.findall(text))
    score += min(len(text), 700) // 70
    score += min(text.count("."), 8)
    if "the generated file" in lowered:
        score += 1
    return score


def _run_candidate(name: str, cmd: list[str], timeout: float) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        out = (proc.stdout or "").strip()
        out = re.sub(r"^```(?:text)?\s*|\s*```$", "", out, flags=re.I).strip().strip('"').strip()
        return {
            "name": name,
            "ok": proc.returncode == 0 and bool(out),
            "text": out,
            "score": _score_candidate(out),
        }
    except Exception as exc:  # noqa: BLE001 - formatter failure must not block TTS
        return {"name": name, "ok": False, "text": "", "score": -100, "error": repr(exc)}


def model_spoken_rewrite(
    text: str,
    *,
    timeout: float = 14.0,
    runner: Optional[Callable[[str, list[str], float], dict[str, Any]]] = None,
) -> str:
    """Try a quick LLM rewrite and return an empty string on failure.

    Uses local CLIs when available. This keeps the model route outside the core
    tool schema and preserves deterministic fallback behavior.
    """
    prompt = _SPOKEN_PROMPT.format(text=text)
    candidates: list[tuple[str, list[str]]] = []

    hermes = shutil.which("hermes")
    if hermes:
        candidates.append((
            "codex-spark",
            [
                hermes,
                "-z",
                prompt,
                "-m",
                "gpt-5.3-codex-spark",
                "--provider",
                "openai-codex",
                "--cli",
                "--ignore-rules",
                "chat",
            ],
        ))

    claude = shutil.which("claude")
    if claude:
        candidates.append((
            "claude-haiku",
            [claude, "-p", prompt, "--model", "haiku", "--max-turns", "1"],
        ))

    if not candidates:
        return ""

    run = runner or (lambda name, cmd, limit: _run_candidate(name, cmd, limit))
    per_candidate_timeout = max(1.0, timeout / max(len(candidates), 1))
    results = [run(name, cmd, per_candidate_timeout) for name, cmd in candidates]
    good = [r for r in results if r.get("ok") and r.get("text")]
    if not good:
        return ""
    best = max(good, key=lambda r: int(r.get("score", 0)))
    return deterministic_spoken_cleanup(str(best.get("text") or ""))


def prepare_spoken_text(
    text: str,
    *,
    source: str = "read-aloud",
    rewrite: str = "auto",
    timeout: float = 14.0,
    model_enabled: bool = False,
    runner: Optional[Callable[[str, list[str], float], dict[str, Any]]] = None,
) -> str:
    """Prepare text for read-aloud/voice TTS.

    ``rewrite`` accepts ``off`` (deterministic only), ``on`` (try model, fallback),
    or ``auto``. Model rewriting is additionally gated by ``model_enabled`` so
    Desktop read-aloud never shells assistant text into subprocess argv unless a
    user explicitly opts into that experimental route. Chatterbox's own wrapper
    may still apply its configured provider-local formatter after this cleanup.
    """
    cleaned = deterministic_spoken_cleanup(text)
    if not cleaned:
        return ""

    mode = (rewrite or "auto").strip().lower()
    src = (source or "").strip().lower()
    should_rewrite = model_enabled and (
        mode in {"on", "true", "1", "yes"}
        or (mode == "auto" and src in {"read-aloud", "voice-conversation"})
    )
    if not should_rewrite:
        return cleaned

    rewritten = model_spoken_rewrite(cleaned, timeout=timeout, runner=runner)
    return rewritten or cleaned
