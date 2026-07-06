"""Renderer client for Hermes rich message cards."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

from .qa import qa_rendered_card
from .schema import CardRenderResult, MessageCardSpec
from .validate import fallback_markdown

logger = logging.getLogger(__name__)
_RENDER_SEMAPHORE = threading.Semaphore(int(os.getenv("HERMES_RICH_CARD_RENDER_CONCURRENCY", "2") or "2"))
_RENDERER_CACHE_VERSION = "visual-defaults-v2"


def render_card(
    spec: MessageCardSpec,
    *,
    platform: str = "generic",
    profile_home: Path | None = None,
    fallback: str | None = None,
    repairs: list[str] | None = None,
) -> CardRenderResult:
    """Render a validated card spec to a profile-aware PNG cache path.

    Uses the Message UI renderer when the Node dependencies are installed. If the
    renderer errors, callers receive a safe fallback with no stack trace.
    """
    profile_home = Path(profile_home or get_hermes_home())
    card_hash = _card_hash(spec, platform)
    day_dir = profile_home / "cache" / "rich_cards" / card_hash[:10]
    day_dir.mkdir(parents=True, exist_ok=True)
    base = day_dir / f"card_{card_hash[:16]}"
    png_path = base.with_suffix(".png")
    svg_path = base.with_suffix(".svg")
    spec_path = base.with_suffix(".json")
    manifest_path = base.with_suffix(".qa.json")
    fallback_text = fallback_markdown(spec, fallback)
    alt = _alt_text(spec)

    if png_path.exists() and png_path.stat().st_size > 0:
        qa = qa_rendered_card(png_path, spec, platform=platform)
        if qa.ok:
            return CardRenderResult(ok=True, image_path=str(png_path), svg_path=str(svg_path) if svg_path.exists() else None, fallback_markdown=fallback_text, alt=alt, repairs=repairs or [], warnings=qa.warnings, qa=qa)
        logger.warning("Cached rich-card image failed QA; re-rendering %s", png_path)
        try:
            png_path.unlink(missing_ok=True)
            svg_path.unlink(missing_ok=True)
        except OSError:
            pass

    spec_path.write_text(json.dumps(spec.model_dump(mode="json", exclude_none=True), indent=2), encoding="utf-8")
    script = Path(__file__).resolve().parents[2] / "scripts" / "rich_cards" / "render-card.mjs"
    timeout = float(os.getenv("HERMES_RICH_CARD_RENDER_TIMEOUT", "10") or "10")
    with _RENDER_SEMAPHORE:
        try:
            proc = subprocess.run(
                ["node", str(script), "--input", str(spec_path), "--output", str(png_path), "--svg", str(svg_path), "--platform", platform],
                cwd=str(Path(__file__).resolve().parents[2]),
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CardRenderResult(ok=False, fallback_markdown=fallback_text, alt=alt, repairs=repairs or [], error="message card renderer timed out", correct_example=_correct_example(spec.kind))
        except Exception as exc:
            logger.warning("Rich card renderer failed to launch: %s", exc)
            return CardRenderResult(ok=False, fallback_markdown=fallback_text, alt=alt, repairs=repairs or [], error="message card renderer unavailable", correct_example=_correct_example(spec.kind))

    if proc.returncode != 0:
        err = _safe_error(proc.stderr or proc.stdout or "renderer failed")
        return CardRenderResult(ok=False, fallback_markdown=fallback_text, alt=alt, repairs=repairs or [], error=err, correct_example=_correct_example(spec.kind))
    if not png_path.exists() or png_path.stat().st_size <= 0:
        return CardRenderResult(ok=False, fallback_markdown=fallback_text, alt=alt, repairs=repairs or [], error="renderer did not produce a PNG", correct_example=_correct_example(spec.kind))

    qa = qa_rendered_card(png_path, spec, platform=platform)
    manifest = {
        "ok": qa.ok,
        "warnings": qa.warnings,
        "image_path": str(png_path),
        "svg_path": str(svg_path) if svg_path.exists() else None,
        "platform": platform,
        "spec_path": str(spec_path),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if not qa.ok:
        return CardRenderResult(ok=False, image_path=None, svg_path=str(svg_path) if svg_path.exists() else None, fallback_markdown=fallback_text, alt=alt, warnings=qa.warnings, repairs=repairs or [], error="rendered card failed programmatic QA", qa=qa, correct_example=_correct_example(spec.kind))
    return CardRenderResult(ok=True, image_path=str(png_path), svg_path=str(svg_path) if svg_path.exists() else None, fallback_markdown=fallback_text, alt=alt, warnings=qa.warnings, repairs=repairs or [], qa=qa)


def cleanup_rich_card_cache(max_age_hours: int = 168, *, profile_home: Path | None = None) -> int:
    """Delete rendered rich-card cache files older than max_age_hours."""
    cache_dir = Path(profile_home or get_hermes_home()) / "cache" / "rich_cards"
    if not cache_dir.exists():
        return 0
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for path in list(cache_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass
    for directory in sorted((p for p in cache_dir.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    return removed


def _card_hash(spec: MessageCardSpec, platform: str) -> str:
    payload = json.dumps({"renderer_cache_version": _RENDERER_CACHE_VERSION, "platform": platform, "spec": spec.model_dump(mode="json", exclude_none=True)}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _alt_text(spec: MessageCardSpec) -> str:
    title = f"{spec.title} " if spec.title else ""
    if spec.kind in {"table", "comparison", "status"} and spec.columns and spec.rows:
        return f"{title}{spec.kind} card with {len(spec.rows)} rows and {len(spec.columns)} columns".strip()
    if spec.kind == "chart" and spec.chart:
        series_count = len(spec.chart.series)
        label_count = len(spec.chart.labels or [])
        return f"{title}{spec.chart.type} chart with {series_count} series and {label_count} labels".strip()
    if spec.kind == "metric_grid" and spec.metrics:
        return f"{title}metric grid with {len(spec.metrics)} metrics".strip()
    if spec.kind == "receipt":
        item_count = len(spec.items or spec.rows or [])
        return f"{title}receipt card with {item_count} items".strip()
    return f"{title}{spec.kind} card".strip()


def _safe_error(text: str) -> str:
    lines = (text or "").strip().splitlines()[0:2]
    return " ".join(line[:240] for line in lines) or "renderer failed"


def _correct_example(kind: Any) -> dict[str, Any]:
    if str(kind) == "chart":
        return {"kind": "chart", "chart": {"type": "bar", "labels": ["A", "B"], "series": [{"name": "Value", "values": [10, 20]}]}}
    return {"kind": "table", "columns": ["Name", "Value"], "rows": [["A", "10"], ["B", "20"]]}
