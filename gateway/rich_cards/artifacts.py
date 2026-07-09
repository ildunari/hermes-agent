"""Parse explicit rich-card artifact fences and render them into ordered segments."""

from __future__ import annotations

import json
import logging
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_hermes_home

from .renderer import render_card
from .schema import CardRenderResult, MessageCardSpec
from .validate import fallback_markdown, validate_and_repair

logger = logging.getLogger(__name__)
_ALLOWED_INFOS = {"message-card", "card", "chart-card"}
_FENCE_RE = re.compile(r"(?P<fence>```+|~~~+)\s*(?P<info>[^\n`]*)\n(?P<body>.*?)(?P=fence)", re.DOTALL)
_MEDIA_RE = re.compile(r"MEDIA:\s*(?P<path>`[^`]+`|'[^']+'|\"[^\"]+\"|\S+)")
_CARD_FALLBACKS: OrderedDict[str, str] = OrderedDict()
_CARD_ALTS: OrderedDict[str, str] = OrderedDict()
_CARD_FORCE_DOCUMENTS: OrderedDict[str, bool] = OrderedDict()
_CARD_CACHE_MAX = 512
try:
    _CARD_CACHE_MAX = max(64, int(os.getenv("HERMES_RICH_CARD_FALLBACK_CACHE_MAX", "512") or "512"))
except ValueError:
    _CARD_CACHE_MAX = 512


@dataclass(frozen=True)
class CardArtifact:
    start: int
    end: int
    info: str
    body: str
    original: str


@dataclass(frozen=True)
class TextSegment:
    markdown: str


@dataclass(frozen=True)
class MediaSegment:
    path: Path
    is_voice: bool = False
    alt: str = ""
    fallback_markdown: str = ""
    force_document: bool = False


MessageSegment = TextSegment | MediaSegment


def find_card_artifacts(text: str) -> list[CardArtifact]:
    artifacts: list[CardArtifact] = []
    for match in _FENCE_RE.finditer(text or ""):
        info = (match.group("info") or "").strip().split()[0].lower()
        if info not in _ALLOWED_INFOS:
            continue
        body = match.group("body")
        if info == "chart-card" and "kind" not in body:
            body = "kind: chart\n" + body
        artifacts.append(CardArtifact(match.start(), match.end(), info, body, match.group(0)))
    return artifacts


def parse_card_body(body: str, *, info: str = "message-card") -> tuple[dict[str, Any] | None, str | None]:
    raw = (body or "").strip()
    if not raw:
        return None, "message-card block is empty"
    try:
        if raw.startswith("{"):
            parsed = json.loads(raw)
        else:
            parsed = yaml.safe_load(raw)
    except Exception as exc:
        return None, f"could not parse message-card YAML/JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, "message-card body must parse to an object"
    if info == "chart-card" and "kind" not in parsed:
        parsed["kind"] = "chart"
    return parsed, None


def render_rich_cards_in_response(
    text: str,
    *,
    platform: str = "generic",
    profile_home: Path | None = None,
    markdown_table_auto: bool = False,
) -> str:
    """Render explicit card artifacts to MEDIA tags, preserving fallbacks on failure."""
    rendered, _results = render_rich_cards_with_results(text, platform=platform, profile_home=profile_home, markdown_table_auto=markdown_table_auto)
    return rendered


def render_rich_cards_with_results(
    text: str,
    *,
    platform: str = "generic",
    profile_home: Path | None = None,
    markdown_table_auto: bool = False,
) -> tuple[str, list[CardRenderResult]]:
    profile_home = Path(profile_home or get_hermes_home())
    if markdown_table_auto:
        try:
            from .markdown_tables import render_markdown_tables_in_response
            text, table_results = render_markdown_tables_in_response(text, platform=platform, profile_home=profile_home)
        except Exception as exc:
            logger.debug("Markdown table auto-capture failed; preserving source: %s", exc)
            table_results = []
    else:
        table_results = []
    artifacts = find_card_artifacts(text)
    if not artifacts:
        return text, table_results

    out: list[str] = []
    last = 0
    results: list[CardRenderResult] = list(table_results)
    for artifact in artifacts:
        out.append(text[last:artifact.start])
        parsed, parse_error = parse_card_body(artifact.body, info=artifact.info)
        if parse_error or parsed is None:
            logger.warning("Message-card parse failed: %s", parse_error)
            out.append(f"[message card unavailable: {parse_error or 'invalid message-card body'}]")
            last = artifact.end
            continue
        spec, repairs, error, example = validate_and_repair(parsed)
        if spec is None:
            logger.warning("Message-card validation failed: %s", error)
            readable_fallback = fallback_markdown(parsed)
            out.append(readable_fallback)
            results.append(CardRenderResult(ok=False, fallback_markdown=readable_fallback, alt="invalid message card", repairs=repairs, error=error, correct_example=example))
            last = artifact.end
            continue
        result = render_card(spec, platform=platform, profile_home=profile_home, repairs=repairs)
        results.append(result)
        if result.ok and result.image_path:
            replacement = f"MEDIA:{result.image_path}"
            _remember_card_media(
                result.image_path,
                fallback_markdown=result.fallback_markdown,
                alt=result.alt,
                force_document=spec.delivery.force_document,
            )
            caption = spec.delivery.caption
            if caption:
                replacement = f"{caption}\n{replacement}"
            if spec.delivery.include_fallback_after:
                replacement = f"{replacement}\n\n{result.fallback_markdown}"
            out.append(replacement)
        else:
            logger.warning("Message-card render failed; preserving fallback: %s", result.error)
            out.append(result.fallback_markdown or "[message card unavailable]")
        last = artifact.end
    out.append(text[last:])
    return _normalize_blank_lines("".join(out)), results


def response_to_ordered_segments(text: str) -> list[MessageSegment]:
    """Split rendered response text into text/media segments in source order."""
    segments: list[MessageSegment] = []
    last = 0
    for match in _MEDIA_RE.finditer(text or ""):
        before = text[last:match.start()]
        if before.strip():
            cleaned_before = _strip_delivery_directives(_normalize_blank_lines(before).strip())
            if cleaned_before:
                segments.append(TextSegment(cleaned_before))
        raw_path = match.group("path").strip()
        if len(raw_path) >= 2 and raw_path[0] == raw_path[-1] and raw_path[0] in "`\"'":
            raw_path = raw_path[1:-1].strip()
        raw_path = raw_path.lstrip("`\"'").rstrip("`\"',.;:)}]")
        if raw_path:
            expanded = Path(raw_path).expanduser()
            key = str(expanded)
            fallback = _CARD_FALLBACKS.get(key, "")
            alt = _CARD_ALTS.get(key, "")
            force_document = _CARD_FORCE_DOCUMENTS.get(key, False)
            segments.append(MediaSegment(expanded, alt=alt, fallback_markdown=fallback, force_document=force_document))
        last = match.end()
    after = text[last:]
    if after.strip():
        cleaned_after = _strip_delivery_directives(_normalize_blank_lines(after).strip())
        if cleaned_after:
            segments.append(TextSegment(cleaned_after))
    return segments


def _strip_delivery_directives(text: str) -> str:
    return text.replace("[[audio_as_voice]]", "").replace("[[as_document]]", "").strip()


def _remember_card_media(
    image_path: str,
    *,
    fallback_markdown: str,
    alt: str,
    force_document: bool = False,
) -> None:
    key = str(Path(image_path))
    _bounded_set(_CARD_FALLBACKS, key, fallback_markdown or "")
    _bounded_set(_CARD_ALTS, key, alt or "")
    _bounded_set(_CARD_FORCE_DOCUMENTS, key, bool(force_document))


def _bounded_set(cache: OrderedDict[str, Any], key: str, value: Any) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _CARD_CACHE_MAX:
        cache.popitem(last=False)


def markdown_table_auto_enabled(platform: str = "generic") -> bool:
    """Off-by-default rollout flag for conservative markdown table capture."""
    value = os.getenv("HERMES_RICH_CARD_TABLE_AUTO", "").strip().lower()
    if value not in {"1", "true", "yes", "on"}:
        return False
    platform_value = os.getenv("HERMES_RICH_CARD_TABLE_AUTO_PLATFORMS", "telegram,bluebubbles,discord").strip()
    if not platform_value or platform_value == "*":
        return True
    allowed = {part.strip().lower() for part in platform_value.split(",") if part.strip()}
    return (platform or "generic").lower() in allowed


def _normalize_blank_lines(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()
