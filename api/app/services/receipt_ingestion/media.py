"""Image normalization, WebP compression, MIME inference, and PDF text extraction."""

from __future__ import annotations

import base64
from collections.abc import Callable, Sequence
import io
import logging
import mimetypes
from typing import Any, Optional

from app.services.receipt_ingestion._types import PdfTextExtractor, StoredReceiptMedia

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


def get_pdf_text_extractor() -> PdfTextExtractor:
    """FastAPI dependency provider returning the canonical PDF text extractor."""
    return extract_pdf_text


def compress_stored_media(media: StoredReceiptMedia) -> StoredReceiptMedia:
    """Compress image attachment bytes to WebP while preserving PDF bytes verbatim."""
    comp_bytes, comp_mime = compress_receipt_image(media.data, media.mime_type)
    return StoredReceiptMedia(data=comp_bytes, mime_type=comp_mime, filename=media.filename)


def decode_valid_email_attachment(
    att: Any,
    *,
    min_pdf_bytes: int = MIN_RECEIPT_ATTACHMENT_BYTES,
    min_image_bytes: int = MIN_RECEIPT_ATTACHMENT_BYTES,
) -> Optional[tuple[bytes, str]]:
    """Decode base64 attachment and enforce PDF/image MIME and minimum byte thresholds."""
    if not getattr(att, "content_base64", None):
        return None
    try:
        raw_bytes = base64.b64decode(att.content_base64, validate=False)
    except Exception:
        return None
    mime_type = infer_receipt_mime_type(
        getattr(att, "filename", None),
        getattr(att, "content_type", None),
    )
    if mime_type == "application/pdf":
        return (raw_bytes, mime_type) if len(raw_bytes) >= min_pdf_bytes else None
    if mime_type.startswith("image/"):
        return (raw_bytes, mime_type) if len(raw_bytes) >= min_image_bytes else None
    return None


def parse_single_attachment_doc(
    raw_bytes: bytes,
    filename: Optional[str],
    mime_type: str,
    *,
    text_parser: Callable[[str], Optional[dict[str, Any]]],
    fallback_parser: Callable[[bytes, Optional[str], str], Optional[dict[str, Any]]],
    pdf_vision_fallback: Optional[Callable[[bytes], Optional[dict[str, Any]]]] = None,
    pdf_text_extractor: Optional[PdfTextExtractor] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Parse one PDF or image attachment once via text-first then vision/fallback parser."""
    if mime_type != "application/pdf":
        return fallback_parser(raw_bytes, filename, mime_type), None
    extractor = pdf_text_extractor or extract_pdf_text
    pdf_text = extractor(raw_bytes)
    raw_excerpt = pdf_text[:32000] if pdf_text else None
    if pdf_text.strip():
        parsed_att = text_parser(pdf_text)
        if parsed_att:
            return parsed_att, raw_excerpt
    if pdf_vision_fallback is not None:
        return pdf_vision_fallback(raw_bytes), raw_excerpt
    if pdf_text.strip():
        return fallback_parser(raw_bytes, filename, mime_type), raw_excerpt
    return None, raw_excerpt


def extract_from_valid_attachments(
    attachments: Sequence[Any],
    *,
    min_pdf_bytes: int,
    attachment_parser: Callable[
        [bytes, Optional[str], str], tuple[Optional[dict[str, Any]], Optional[str]]
    ],
    normalizer: Callable[[Optional[dict[str, Any]]], Any],
) -> tuple[Any, Optional[StoredReceiptMedia], Optional[StoredReceiptMedia], Optional[str]]:
    """Extract normalized payload from valid attachments and retain first fallback media."""
    fallback_attachment: Optional[StoredReceiptMedia] = None
    raw_text_for_record: Optional[str] = None

    for att in attachments:
        decoded = decode_valid_email_attachment(
            att, min_pdf_bytes=min_pdf_bytes, min_image_bytes=MIN_RECEIPT_ATTACHMENT_BYTES
        )
        if decoded is None:
            continue
        raw_bytes, mime_type = decoded
        media = StoredReceiptMedia(raw_bytes, mime_type, getattr(att, "filename", None))
        if fallback_attachment is None:
            fallback_attachment = media

        parsed_att, pdf_excerpt = attachment_parser(
            raw_bytes, getattr(att, "filename", None), mime_type
        )
        if pdf_excerpt:
            raw_text_for_record = pdf_excerpt
        normalized = normalizer(parsed_att)
        if normalized:
            return normalized, compress_stored_media(media), fallback_attachment, raw_text_for_record

    return None, None, fallback_attachment, raw_text_for_record
