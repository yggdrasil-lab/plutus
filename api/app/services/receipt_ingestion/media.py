"""Image normalization, WebP compression, MIME inference, and PDF text extraction."""

from __future__ import annotations

import io
import logging
import mimetypes
from typing import Any, Optional

logger = logging.getLogger(__name__)

MIN_RECEIPT_ATTACHMENT_BYTES = 15 * 1024
MIN_PAYSLIP_PDF_BYTES = 512

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass


def infer_receipt_mime_type(
    filename: Optional[str],
    content_type: Optional[str],
) -> str:
    """Resolve an accurate MIME type from upload headers or filename extension."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct and ct != "application/octet-stream":
        return ct
    if filename:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in ("heic", "heif"):
            return f"image/{ext}"
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            return guessed
    return "application/octet-stream"


def _normalize_image_rgb(img: Any) -> Any:
    """Apply EXIF orientation and convert an image to RGB for WebP/JPEG encoding."""
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        background = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        background.paste(rgba, mask=rgba.split()[3])
        return background
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def compress_receipt_image(
    content: bytes,
    mime_type: str,
    max_dimension: int = 1400,
    quality: int = 75,
) -> tuple[bytes, str]:
    """Downscale and WebP-compress a processed receipt image to minimize DB storage."""
    if not mime_type.startswith("image/"):
        return content, mime_type

    try:
        from PIL import Image

        with Image.open(io.BytesIO(content)) as raw_img:
            img = _normalize_image_rgb(raw_img)
            if max(img.size) > max_dimension:
                img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            img.save(out, format="WEBP", quality=quality, method=4)
            compressed = out.getvalue()
            if len(compressed) < len(content):
                return compressed, "image/webp"
            return content, mime_type
    except Exception as exc:
        logger.warning(
            "Receipt image compression skipped (%s); retaining original bytes.", exc
        )
        return content, mime_type


def prepare_image_for_llm(
    content: bytes,
    mime_type: str,
    max_dimension: int = 2048,
) -> tuple[bytes, str]:
    """Normalize image format (e.g. HEIC to JPEG) and cap resolution for LLM vision."""
    if not mime_type.startswith("image/"):
        return content, mime_type

    needs_transcode = mime_type not in ("image/jpeg", "image/png", "image/webp")
    try:
        from PIL import Image

        with Image.open(io.BytesIO(content)) as raw_img:
            if not needs_transcode and max(raw_img.size) <= max_dimension:
                return content, mime_type
            img = _normalize_image_rgb(raw_img)
            if max(img.size) > max_dimension:
                img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=88, optimize=True)
            return out.getvalue(), "image/jpeg"
    except Exception as exc:
        logger.warning("Failed to normalize image for LLM (%s); sending raw bytes.", exc)
        return content, mime_type


def extract_pdf_text(content: bytes) -> str:
    """Extract readable text from an uploaded PDF document using pdfplumber."""
    try:
        import pdfplumber

        pages: list[str] = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(text.strip())
        return "\n\n".join(pages)
    except Exception as exc:
        logger.warning("PDF text extraction failed: %s", exc)
        return ""
