"""Shared image normalization helpers for model vision inputs.

Providers that accept image input generally support JPEG/PNG/WebP, but not
HEIC/HEIF. iPhone uploads often arrive as HEIC bytes, sometimes with a
misleading .jpg filename or a data:image/heic URL. Normalize those to JPEG
before the model/provider sees them.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_dir

logger = logging.getLogger(__name__)

_HEIF_BRANDS = {
    b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis",
    b"mif1", b"msf1",
}
_AVIF_BRANDS = {b"avif", b"avis"}
_UNIVERSALLY_SUPPORTED_MIMES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp",
})


def sniff_image_mime_from_bytes(raw: bytes) -> Optional[str]:
    """Return an image MIME type from magic bytes, or None if unknown."""
    if not raw:
        return None
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw.startswith(b"BM"):
        return "image/bmp"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if len(raw) >= 12 and raw[4:8] == b"ftyp":
        major_brand = raw[8:12].lower()
        compatible_brands = raw[16:128].lower()
        if major_brand in _AVIF_BRANDS or any(brand in compatible_brands for brand in _AVIF_BRANDS):
            return "image/avif"
        if major_brand in _HEIF_BRANDS or any(brand in compatible_brands for brand in _HEIF_BRANDS):
            # Treat HEIF-family still images as image/heic for routing. The
            # converter handles both HEIC and generic HEIF containers.
            return "image/heic"
    if raw[:4] in {b"II*\x00", b"MM\x00*"}:
        return "image/tiff"
    if raw[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    head = raw[:512].lstrip().lower()
    if (head.startswith(b"<?xml") or head.startswith(b"<svg")) and b"<svg" in head:
        return "image/svg+xml"
    return None


def detect_image_mime_type(image_path: Path) -> Optional[str]:
    """Return a MIME type when *image_path* looks like a supported image."""
    with image_path.open("rb") as f:
        header = f.read(128)

    sniffed = sniff_image_mime_from_bytes(header)
    if sniffed:
        return sniffed

    if image_path.suffix.lower() == ".svg":
        head = image_path.read_text(encoding="utf-8", errors="ignore")[:4096].lower()
        if "<svg" in head:
            return "image/svg+xml"
    return None


def is_heic_mime(mime_type: str | None) -> bool:
    return str(mime_type or "").strip().lower() in {"image/heic", "image/heif", "image/heif-sequence"}


def _vision_temp_dir() -> Path:
    temp_dir = get_hermes_dir("cache/vision", "temp_vision_images")
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir


def convert_heic_to_jpeg_for_vision(image_path: Path) -> Path:
    """Convert a HEIC/HEIF image into a provider-safe image file.

    Backwards-compatible name: callers historically imported this as a JPEG
    converter. The preferred output is now PNG so decoded pixels are preserved
    losslessly for providers that reject HEIC/HEIF. JPEG is only a last-ditch
    fallback when PNG conversion is unavailable.

    macOS ships `sips`, which can decode Apple/iPhone HEIC without extra Python
    wheels. On other hosts, or if `sips` fails, we try optional `pillow_heif`.
    The returned file is temporary and the caller owns cleanup.
    """
    temp_dir = _vision_temp_dir()
    png_path = temp_dir / f"heic_{uuid.uuid4().hex}.png"

    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["sips", "-s", "format", "png", str(image_path), "--out", str(png_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            if detect_image_mime_type(png_path) == "image/png":
                return png_path
        except Exception as exc:
            logger.debug("sips HEIC→PNG conversion failed for %s: %s", image_path, exc)
            png_path.unlink(missing_ok=True)

    try:
        import pillow_heif  # type: ignore
        from PIL import Image

        pillow_heif.register_heif_opener()
        with Image.open(image_path) as img:
            if img.mode not in {"RGB", "RGBA", "L", "LA", "P"}:
                img = img.convert("RGBA")
            img.save(png_path, format="PNG", optimize=False)
        if detect_image_mime_type(png_path) == "image/png":
            return png_path
    except Exception as exc:
        logger.debug("pillow_heif HEIC→PNG conversion failed for %s: %s", image_path, exc)
        png_path.unlink(missing_ok=True)

    # Compatibility fallback: better a viewable image than a hard failure on
    # hosts with partial HEIC decoders that can only emit JPEG.
    jpg_path = temp_dir / f"heic_{uuid.uuid4().hex}.jpg"
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["sips", "-s", "format", "jpeg", str(image_path), "--out", str(jpg_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            if detect_image_mime_type(jpg_path) == "image/jpeg":
                return jpg_path
        except Exception as exc:
            logger.debug("sips HEIC→JPEG fallback failed for %s: %s", image_path, exc)
            jpg_path.unlink(missing_ok=True)

    raise ValueError(
        "HEIC/HEIF image detected but could not be converted to PNG for vision analysis."
    )


def transcode_image_to_png_for_vision(image_path: Path) -> Path:
    """Transcode a non-universal raster image into a PNG file providers accept."""
    out_path = _vision_temp_dir() / f"vision_{uuid.uuid4().hex}.png"
    try:
        from PIL import Image
    except ImportError as exc:
        raise ValueError(
            "Image format is not accepted directly by all vision providers and "
            "Pillow is not installed for PNG transcoding."
        ) from exc

    try:
        import pillow_heif  # type: ignore

        pillow_heif.register_heif_opener()
    except Exception:
        pass
    try:
        import pillow_avif  # type: ignore  # noqa: F401
    except Exception:
        pass

    try:
        with Image.open(image_path) as img:
            if img.mode not in {"RGB", "RGBA", "L", "LA", "P"}:
                img = img.convert("RGBA")
            img.save(out_path, format="PNG", optimize=False)
        if detect_image_mime_type(out_path) == "image/png":
            return out_path
    except Exception as exc:
        out_path.unlink(missing_ok=True)
        raise ValueError(
            "Image format is not accepted directly by all vision providers and "
            "could not be transcoded to PNG."
        ) from exc

    out_path.unlink(missing_ok=True)
    raise ValueError("Image transcoding did not produce a valid PNG.")


def normalize_image_file_for_vision(
    image_path: Path,
    mime_type: str | None = None,
) -> tuple[Path, str, bool]:
    """Return a provider-safe image path, MIME type, and cleanup flag.

    HEIC/HEIF converts to JPEG because OpenAI/Codex-style vision inputs support
    JPEG reliably while rejecting HEIC. Other raster formats that many providers
    reject (AVIF, TIFF, BMP, ICO) transcode to PNG with Pillow. SVG is detected
    and rejected unless a future rasterizer is added.
    """
    detected = mime_type or detect_image_mime_type(image_path)
    if not detected:
        raise ValueError("Only real image files are supported for vision analysis.")
    if is_heic_mime(detected):
        converted = convert_heic_to_jpeg_for_vision(image_path)
        converted_mime = detect_image_mime_type(converted) or "image/png"
        return converted, converted_mime, True
    detected = detected.strip().lower()
    if detected in _UNIVERSALLY_SUPPORTED_MIMES:
        return image_path, detected, False
    if detected == "image/svg+xml":
        raise ValueError("SVG images are not supported for native vision input.")
    return transcode_image_to_png_for_vision(image_path), "image/png", True


def file_to_data_url(image_path: Path, mime_type: str | None = None) -> str:
    data = image_path.read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    mime = mime_type or detect_image_mime_type(image_path) or "image/jpeg"
    return f"data:{mime};base64,{encoded}"


def normalize_image_data_url_for_vision(data_url: str) -> tuple[str, bool]:
    """Convert HEIC/HEIF data URLs to lossless PNG data URLs when possible.

    Returns ``(data_url, changed)``. Non-HEIC image data URLs are returned
    unchanged. The function also sniffs bytes, so mislabeled
    ``data:image/jpeg`` payloads containing HEIC are still normalized.
    """
    if not isinstance(data_url, str):
        return data_url, False
    header, sep, payload = data_url.partition(",")
    if not sep or not header.lower().startswith("data:image/"):
        return data_url, False

    mime = ""
    if header.lower().startswith("data:"):
        mime = header[5:].split(";", 1)[0].strip().lower()

    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception:
        # Let the provider/reporting path surface malformed data URLs normally.
        return data_url, False

    sniffed = sniff_image_mime_from_bytes(raw)
    if not (is_heic_mime(mime) or is_heic_mime(sniffed)):
        return data_url, False

    temp_dir = _vision_temp_dir()
    src = temp_dir / f"heic_data_url_{uuid.uuid4().hex}.heic"
    converted: Path | None = None
    try:
        src.write_bytes(raw)
        converted = convert_heic_to_jpeg_for_vision(src)
        converted_mime = detect_image_mime_type(converted) or "image/png"
        return file_to_data_url(converted, converted_mime), True
    finally:
        src.unlink(missing_ok=True)
        if converted is not None:
            converted.unlink(missing_ok=True)
