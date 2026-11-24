"""Payslip email ingestion, normalization, field application, and inflow matching."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import Receipt
from app.services.ai_gateway import AiGateway
from app.services.receipt_ingestion._types import (
    PayslipEmailIngestionResult,
    PdfTextExtractor,
    ReceiptParseError,
    ReceiptValidationError,
    StoredReceiptMedia,
)
from app.services.receipt_ingestion.matching import (
    find_semantic_duplicate_receipt,
    load_receipt_snapshot,
    match_receipt_to_transaction,
)
from app.services.receipt_ingestion.media import (
    MIN_PAYSLIP_PDF_BYTES,
    compress_stored_media,
    extract_from_valid_attachments,
    parse_single_attachment_doc,
)
from app.services.receipt_ingestion.prompts import (
    parse_payslip_text_with_llm,
    parse_payslip_with_llm,
)
from app.services.receipt_ingestion.sanitization import (
    normalize_order_number,
    normalize_receipt_currency,
    resolve_order_date,
    sanitize_receipt_email_content,
)


@dataclass(frozen=True)
class _NormalizedPayslip:
    """Validated structured payslip fields ready for Receipt persistence."""

    employer: str
    pay_date: date
    net_pay: float
    currency: str
    tax_amount: Optional[float]
    pay_period_ref: Optional[str]
    notes: Optional[str]


@dataclass(frozen=True)
class _PayslipAttachmentOutcome:
    """Outcome of attachment-first payslip extraction including fallback media."""

    payslip: Optional[_NormalizedPayslip]
    stored_media: Optional[StoredReceiptMedia]
    fallback_attachment: Optional[StoredReceiptMedia]
    raw_text: Optional[str]


def apply_parsed_payslip_fields(
    receipt: Receipt,
    parsed: Optional[dict[str, Any]],
    db: Session,
) -> bool:
    """Populate header fields on an existing payslip Receipt while clearing line items."""
    normalized = _normalize_parsed_payslip(
        parsed,
        fallback_date=receipt.receipt_date or date.today(),
        fallback_currency=receipt.currency or "NZD",
    )
    if normalized is None:
        return False

    receipt.document_type = "payslip"
    receipt.receipt_date = normalized.pay_date
    receipt.merchant = normalized.employer
    receipt.total_amount = normalized.net_pay
    receipt.currency = normalized.currency
    if normalized.pay_period_ref:
        receipt.order_number = normalized.pay_period_ref
    receipt.tax_amount = normalized.tax_amount
    receipt.notes = normalized.notes
    receipt.is_processed = True
    if receipt.items:
        receipt.items.clear()
        db.flush()
    return True


def ingest_payslip_email(
    db: Session,
    payload: Any,
    *,
    ai_gateway: Optional[AiGateway] = None,
    pdf_text_extractor: Optional[PdfTextExtractor] = None,
) -> PayslipEmailIngestionResult:
    """Ingest a payslip email (attachment-first, zero items/products, inflow matching)."""
    sanitized = sanitize_receipt_email_content(
        subject=payload.subject or "", from_address=payload.from_address,
        received_at=payload.received_at, text_body=payload.text_body,
        html_excerpt=payload.html_excerpt, raw_excerpt=payload.raw_excerpt,
    )
    outcome = _extract_payslip_from_attachments(
        payload.attachments, sanitized.fallback_date,
        ai_gateway=ai_gateway, pdf_text_extractor=pdf_text_extractor,
    )
    normalized, stored_media, raw_text = outcome.payslip, outcome.stored_media, outcome.raw_text
    if normalized is None:
        normalized, raw_text = _extract_payslip_from_email_body(
            sanitized.prompt_text, sanitized.fallback_date, ai_gateway=ai_gateway,
        )
        if stored_media is None and outcome.fallback_attachment is not None:
            stored_media = compress_stored_media(outcome.fallback_attachment)

    existing_dup = find_semantic_duplicate_receipt(
        db, merchant=normalized.employer, receipt_date=normalized.pay_date,
        total_amount=normalized.net_pay, currency=normalized.currency,
        order_number=normalized.pay_period_ref, document_type="payslip",
    )
    if existing_dup is not None:
        return PayslipEmailIngestionResult(
            receipt=load_receipt_snapshot(db, existing_dup.receipt_id), deduplicated=True
        )

    receipt_id = _persist_payslip_receipt(
        db, normalized=normalized, message_id=payload.message_id,
        raw_text=raw_text, stored_media=stored_media,
    )
    return PayslipEmailIngestionResult(receipt=load_receipt_snapshot(db, receipt_id))


def _clean_payslip_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned or cleaned.lower() in ("null", "none", "n/a"):
        return None
    return cleaned


def _derive_payslip_period_fields(
    parsed: dict[str, Any],
) -> tuple[Optional[str], Optional[str]]:
    start_str = _clean_payslip_text(parsed.get("pay_period_start"))
    end_str = _clean_payslip_text(parsed.get("pay_period_end"))
    label = _clean_payslip_text(parsed.get("pay_period_label"))

    range_str = (
        f"{start_str} to {end_str}" if (start_str and end_str) else (end_str or start_str)
    )
    pay_period_ref = normalize_order_number(range_str or label)

    if label and range_str and label.lower() != range_str.lower():
        notes = f"Pay period: {label} ({range_str})"
    elif label or range_str:
        notes = f"Pay period: {label or range_str}"
    else:
        notes = None
    return pay_period_ref, notes


def _parse_optional_tax(raw_tax: Any) -> Optional[float]:
    if raw_tax is None or str(raw_tax).strip() == "":
        return None
    try:
        parsed_tax = round(float(raw_tax), 2)
    except (TypeError, ValueError):
        return None
    if math.isfinite(parsed_tax) and parsed_tax >= 0:
        return parsed_tax
    return None


def _normalize_parsed_payslip(
    parsed: Any,
    *,
    fallback_date: date,
    fallback_currency: str = "NZD",
) -> Optional[_NormalizedPayslip]:
    if not isinstance(parsed, dict):
        return None
    employer = str(parsed.get("employer") or "").strip()[:150]
    if not employer:
        return None

    try:
        net_pay = round(float(parsed.get("net_pay")), 2)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(net_pay) or net_pay <= 0:
        return None

    pay_period_ref, notes = _derive_payslip_period_fields(parsed)
    return _NormalizedPayslip(
        employer=employer,
        pay_date=resolve_order_date(parsed.get("pay_date"), fallback_date),
        net_pay=net_pay,
        currency=normalize_receipt_currency(parsed.get("currency"), fallback=fallback_currency),
        tax_amount=_parse_optional_tax(parsed.get("tax_amount")),
        pay_period_ref=pay_period_ref,
        notes=notes,
    )


def _parse_single_payslip_attachment(
    raw_bytes: bytes,
    filename: Optional[str],
    mime_type: str,
    *,
    ai_gateway: Optional[AiGateway] = None,
    pdf_text_extractor: Optional[PdfTextExtractor] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    return parse_single_attachment_doc(
        raw_bytes,
        filename,
        mime_type,
        text_parser=lambda txt: parse_payslip_text_with_llm(
            txt, raise_on_error=False, gateway=ai_gateway
        ),
        fallback_parser=lambda b, f, m: parse_payslip_with_llm(
            b, f, m, gateway=ai_gateway, pdf_text_extractor=pdf_text_extractor
        ),
        pdf_vision_fallback=lambda b: parse_payslip_with_llm(
            b,
            filename,
            "application/pdf",
            gateway=ai_gateway,
            pdf_text_extractor=lambda _: "",
        ),
        pdf_text_extractor=pdf_text_extractor,
    )


def _extract_payslip_from_attachments(
    attachments: list[Any],
    fallback_date: date,
    *,
    ai_gateway: Optional[AiGateway] = None,
    pdf_text_extractor: Optional[PdfTextExtractor] = None,
) -> _PayslipAttachmentOutcome:
    normalized, stored_media, fallback_att, raw_text = extract_from_valid_attachments(
        attachments,
        min_pdf_bytes=MIN_PAYSLIP_PDF_BYTES,
        attachment_parser=lambda b, f, m: _parse_single_payslip_attachment(
            b, f, m, ai_gateway=ai_gateway, pdf_text_extractor=pdf_text_extractor
        ),
        normalizer=lambda parsed: _normalize_parsed_payslip(parsed, fallback_date=fallback_date),
    )
    return _PayslipAttachmentOutcome(
        payslip=normalized,
        stored_media=stored_media,
        fallback_attachment=fallback_att,
        raw_text=raw_text,
    )


def _extract_payslip_from_email_body(
    sanitized_text: str,
    fallback_date: date,
    *,
    ai_gateway: Optional[AiGateway] = None,
) -> tuple[_NormalizedPayslip, str]:
    if not sanitized_text:
        raise ReceiptValidationError(
            "Payslip email contains no readable payslip content or valid attachment."
        )
    parsed_body = parse_payslip_text_with_llm(
        sanitized_text, raise_on_error=True, gateway=ai_gateway
    )
    normalized = _normalize_parsed_payslip(parsed_body, fallback_date=fallback_date)
    if normalized is None:
        raise ReceiptParseError(
            "Payslip extraction failed: missing valid employer and positive net_pay."
        )
    return normalized, sanitized_text[:32000]


def _persist_payslip_receipt(
    db: Session,
    *,
    normalized: _NormalizedPayslip,
    message_id: str,
    raw_text: Optional[str],
    stored_media: Optional[StoredReceiptMedia],
) -> int:
    receipt = Receipt(
        document_type="payslip",
        receipt_date=normalized.pay_date,
        merchant=normalized.employer,
        total_amount=normalized.net_pay,
        currency=normalized.currency,
        tax_amount=normalized.tax_amount,
        order_number=normalized.pay_period_ref,
        source_email_message_id=message_id[:512],
        status="unlinked",
        is_processed=True,
        raw_text=raw_text,
        notes=normalized.notes,
        image_data=stored_media.data if stored_media else None,
        image_mime_type=stored_media.mime_type if stored_media else None,
        image_filename=(
            stored_media.filename[:255] if stored_media and stored_media.filename else None
        ),
    )
    db.add(receipt)
    db.commit()
    db.refresh(receipt)
    match_receipt_to_transaction(db, receipt.id)
    return receipt.id
