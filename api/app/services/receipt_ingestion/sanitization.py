"""Email sanitization and currency, order-number, and merchant-key normalizers."""

from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.config import settings
from app.services.email_utils import (
    extract_and_strip_forwarded_header,
    html_to_plain_text,
    strip_invisible_unicode,
)
from app.services.receipt_ingestion._types import SanitizedEmailContent

_MAX_SANITIZED_PROMPT_CHARS = 48000
_MIN_RICH_HTML_CHARS = 60
_TRACKING_URL_RE = re.compile(r"https?://\S{40,}", re.IGNORECASE)
_CURRENCY_EXACT_PREFIXES: dict[str, str] = {
    "S$": "SGD",
    "SG$": "SGD",
    "NZ$": "NZD",
    "AU$": "AUD",
    "A$": "AUD",
    "US$": "USD",
}
_CURRENCY_SYMBOL_CODES: tuple[tuple[str, str], ...] = (
    ("₱", "PHP"),
    ("€", "EUR"),
    ("£", "GBP"),
)
_MERCHANT_STOPWORDS = frozenset(
    {
        "the",
        "pty",
        "ltd",
        "inc",
        "limited",
        "co",
        "corp",
        "llc",
        "card",
        "transaction",
        "issued",
        "by",
        "payment",
        "purchase",
        "pos",
        "eftpos",
        "online",
        "store",
        "shop",
        "marketplace",
        "com",
        "www",
        "nz",
        "au",
        "sg",
        "us",
        "uk",
        "at",
        "to",
        "of",
        "in",
        "on",
    }
)


def _clean_receipt_text_lines(raw: str) -> str:
    """Normalize whitespace and strip invisible spacers and long tracking URLs."""
    cleaned = strip_invisible_unicode(raw)
    cleaned = _TRACKING_URL_RE.sub("", cleaned)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in cleaned.splitlines()]
    return "\n".join(line for line in lines if line)


def _select_sanitized_body(
    *,
    html_clean: str,
    text_clean: str,
    raw_excerpt: Optional[str],
) -> str:
    if html_clean and len(html_clean) >= _MIN_RICH_HTML_CHARS:
        return html_clean
    if text_clean:
        return text_clean
    if html_clean:
        return html_clean
    if raw_excerpt:
        return _clean_receipt_text_lines(raw_excerpt)
    return ""


def _format_header_lines(
    *,
    clean_subject: str,
    from_address: Optional[str],
    fallback_date: date,
) -> list[str]:
    headers: list[str] = []
    if clean_subject:
        headers.append(f"Subject: {clean_subject}")
    if from_address and from_address.strip():
        headers.append(f"From: {from_address.strip()}")
    headers.append(f"Email Date: {fallback_date.isoformat()}")
    return headers


def sanitize_receipt_email_content(
    *,
    subject: str,
    from_address: Optional[str],
    received_at: Optional[datetime],
    text_body: Optional[str],
    html_excerpt: Optional[str],
    raw_excerpt: Optional[str],
    timezone_name: Optional[str] = None,
) -> SanitizedEmailContent:
    """Sanitize forwarded receipt email HTML/text into prompt text and local fallback date."""
    local_tz = ZoneInfo(timezone_name or settings.timezone)
    html_clean = ""
    if html_excerpt and html_excerpt.strip():
        html_clean = _clean_receipt_text_lines(
            html_to_plain_text(html_excerpt, preserve_annotations=True)
        )

    raw_source = (text_body or raw_excerpt or "").strip()
    clean_subject, stripped_text, ref_local_dt = extract_and_strip_forwarded_header(
        subject or "", raw_source or html_clean, received_at, target_tz=local_tz
    )
    fallback_date = ref_local_dt.date()

    if html_clean:
        _, stripped_html, ref_html_dt = extract_and_strip_forwarded_header(
            subject or "", html_clean, received_at, target_tz=local_tz
        )
        if not raw_source:
            fallback_date = ref_html_dt.date()
        html_clean = _clean_receipt_text_lines(stripped_html)

    body_section = _select_sanitized_body(
        html_clean=html_clean,
        text_clean=_clean_receipt_text_lines(stripped_text),
        raw_excerpt=raw_excerpt,
    )
    if not body_section:
        return SanitizedEmailContent(prompt_text="", fallback_date=fallback_date)

    headers = _format_header_lines(
        clean_subject=clean_subject,
        from_address=from_address,
        fallback_date=fallback_date,
    )
    combined = "\n".join(headers) + "\n\n" + body_section
    return SanitizedEmailContent(
        prompt_text=combined[:_MAX_SANITIZED_PROMPT_CHARS],
        fallback_date=fallback_date,
    )


def normalize_receipt_currency(raw_currency: Any, *, fallback: str = "NZD") -> str:
    """Normalize a parsed currency string to a 3-letter uppercase ISO currency code."""
    fb = (fallback or "NZD").strip().upper()[:3]
    if len(fb) != 3 or not fb.isalpha():
        fb = "NZD"
    if raw_currency is None:
        return fb
    cleaned = str(raw_currency).strip().upper()
    if cleaned in _CURRENCY_EXACT_PREFIXES:
        return _CURRENCY_EXACT_PREFIXES[cleaned]
    for symbol, code in _CURRENCY_SYMBOL_CODES:
        if symbol in cleaned:
            return code
    match = re.search(r"\b([A-Z]{3})\b", cleaned)
    if match:
        return match.group(1)
    return fb


def normalize_order_number(raw_order: Any) -> Optional[str]:
    """Normalize an optional purchase order or pay-period identifier."""
    if raw_order is None:
        return None
    cleaned = str(raw_order).strip()
    if not cleaned or cleaned.lower() in ("null", "none", "n/a"):
        return None
    return cleaned[:128]


def normalize_merchant_key(merchant: str) -> str:
    """Normalize merchant names for semantic receipt deduplication."""
    tokens = [
        tok
        for tok in re.findall(r"[a-z0-9]+", (merchant or "").lower())
        if tok not in _MERCHANT_STOPWORDS
    ]
    return " ".join(tokens) or re.sub(r"[^a-z0-9]+", " ", (merchant or "").lower()).strip()


def resolve_order_date(raw_date: Any, fallback_date: date) -> date:
    """Parse an ISO YYYY-MM-DD date string or return the provided fallback date."""
    try:
        return date.fromisoformat(str(raw_date or ""))
    except ValueError:
        return fallback_date
