"""Programmatic QA checks for rendered rich card images."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageStat

from .schema import CardQAResult, MessageCardSpec


def qa_rendered_card(image_path: str | Path, spec: MessageCardSpec, *, platform: str = "") -> CardQAResult:
    warnings: list[str] = []
    path = Path(image_path)
    if not path.exists() or not path.is_file():
        return CardQAResult(ok=False, warnings=["rendered image does not exist"])
    size = path.stat().st_size
    if size <= 0:
        return CardQAResult(ok=False, warnings=["rendered image is empty"], bytes=size)
    if size > 9_000_000:
        warnings.append("rendered image is large for chat delivery")
    try:
        with Image.open(path) as image:
            width, height = image.size
            if width < 360 or height < 220:
                warnings.append("rendered image dimensions are too small")
            if width > 2600 or height > 5200:
                warnings.append("rendered image dimensions may exceed platform limits")
            extrema = image.convert("RGB").getextrema()
            flat = all(lo == hi for lo, hi in extrema)
            stat = ImageStat.Stat(image.convert("L"))
            stddev = stat.stddev[0] if isinstance(stat.stddev, list) else float(stat.stddev)
            if flat or stddev < 1.0:
                warnings.append("rendered image appears blank or near-uniform")
    except Exception as exc:
        return CardQAResult(ok=False, warnings=[f"rendered image could not be opened: {type(exc).__name__}"], bytes=size)
    return CardQAResult(ok=not any("blank" in w or "too small" in w for w in warnings), warnings=warnings, width=width, height=height, bytes=size)
