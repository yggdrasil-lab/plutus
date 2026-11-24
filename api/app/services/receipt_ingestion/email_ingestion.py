"""Purchase receipt email ingestion (multi-order parsing, semantic dedup, matching)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Receipt
from app.services.ai_gateway import AiGateway
from app.services.receipt_ingestion._types import (
    PdfTextExtractor,
    PurchaseEmailIngestionResult,
    ReceiptParseError,
    ReceiptSnapshot,
    ReceiptValidationError,
    StoredReceiptMedia,
)
from app.services.receipt_ingestion.classification import (
    apply_receipt_discounts_with_jev,
    drafts_from_parsed_items,
    persist_classified_receipt_items,
)
from app.services.receipt_ingestion.matching import (
    find_semantic_duplicate_receipt,
    load_receipt_snapshot,
    match_receipt_to_transaction,
)
from app.services.receipt_ingestion.media import (
    MIN_RECEIPT_ATTACHMENT_BYTES,
    compress_stored_media,
    extract_from_valid_attachments,
    parse_single_attachment_doc,
)
from app.services.receipt_ingestion.prompts import (
    parse_receipt_orders_text_with_llm,
    parse_receipt_with_llm,
)
from app.services.receipt_ingestion.sanitization import (
    normalize_order_number,
    normalize_receipt_currency,
    resolve_order_date,
    sanitize_receipt_email_content,
)


@dataclass(frozen=True)
class _AttachmentParseOutcome:
    """Outcome of attachment-first purchase receipt extraction including fallback media."""

    orders: list[dict[str, Any]]
    stored_media: Optional[StoredReceiptMedia]
    fallback_attachment: Optional[StoredReceiptMedia]
    raw_text: Optional[str]


def ingest_purchase_receipt_email(
    db: Session,
    payload: Any,
    *,
    ai_gateway: Optional[AiGateway] = None,
    pdf_text_extractor: Optional[PdfTextExtractor] = None,
) -> PurchaseEmailIngestionResult:
    """Ingest a purchase receipt email (attachment-first or HTML/text, 1 or N orders)."""
    fallback_date = _resolve_email_fallback_date(
        payload.received_at, ZoneInfo(settings.timezone)
    )
    outcome = _extract_receipt_orders_from_attachments(
        payload.attachments, ai_gateway=ai_gateway, pdf_text_extractor=pdf_text_extractor,
    )
    orders, stored_media, raw_text = outcome.orders, outcome.stored_media, outcome.raw_text
    if not orders:
        orders, fallback_date, raw_text = _extract_orders_from_email_body(
            payload, ai_gateway=ai_gateway
        )
        if orders and stored_media is None and outcome.fallback_attachment is not None:
            stored_media = compress_stored_media(outcome.fallback_attachment)
    if not orders:
        raise ReceiptParseError("Could not extract any purchase orders from receipt email.")

    results = [
        _persist_or_dedup_order(
            db, order=order, fallback_date=fallback_date, message_id=payload.message_id,
            raw_text=raw_text, stored_media=stored_media, ai_gateway=ai_gateway,
        )
        for order in orders
    ]
    snapshots = tuple(snap for snap, _ in results)
    return PurchaseEmailIngestionResult(
        receipts=snapshots,
        receipt_ids=tuple(snap.id for snap in snapshots),
        deduplicated_count=sum(1 for _, was_dup in results if was_dup),
    )


def _resolve_email_fallback_date(received_at: Optional[datetime], local_tz: ZoneInfo) -> date:
    if not received_at:
        return datetime.now(local_tz).date()
    aware = received_at.replace(tzinfo=timezone.utc) if received_at.tzinfo is None else received_at
    return aware.astimezone(local_tz).date()


def _normalize_parsed_orders(parsed: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(parsed, dict):
        return []
    raw_orders = parsed.get("orders")
    if isinstance(raw_orders, list):
        candidates = [o for o in raw_orders if isinstance(o, dict)]
    elif "merchant" in parsed and "total_amount" in parsed:
        candidates = [parsed]
    else:
        return []

    valid: list[dict[str, Any]] = []
    for order in candidates:
        merchant = str(order.get("merchant") or "").strip()
        try:
            total_amt = float(order.get("total_amount"))
        except (TypeError, ValueError):
            continue
        if merchant and total_amt >= 0:
            valid.append(order)
    return valid


def _parse_single_attachment(
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
        text_parser=lambda txt: parse_receipt_orders_text_with_llm(
            txt, raise_on_error=False, gateway=ai_gateway
        ),
        fallback_parser=lambda b, f, m: parse_receipt_with_llm(
            b, f, m, gateway=ai_gateway, pdf_text_extractor=pdf_text_extractor
        ),
        pdf_text_extractor=pdf_text_extractor,
    )


def _extract_receipt_orders_from_attachments(
    attachments: list[Any],
    *,
    ai_gateway: Optional[AiGateway] = None,
    pdf_text_extractor: Optional[PdfTextExtractor] = None,
) -> _AttachmentParseOutcome:
    extracted, stored_media, fallback_att, raw_text = extract_from_valid_attachments(
        attachments,
        min_pdf_bytes=MIN_RECEIPT_ATTACHMENT_BYTES,
        attachment_parser=lambda b, f, m: _parse_single_attachment(
            b, f, m, ai_gateway=ai_gateway, pdf_text_extractor=pdf_text_extractor
        ),
        normalizer=_normalize_parsed_orders,
    )
    return _AttachmentParseOutcome(
        orders=extracted or [],
        stored_media=stored_media,
        fallback_attachment=fallback_att,
        raw_text=raw_text,
    )


def _extract_orders_from_email_body(
    payload: Any,
    *,
    ai_gateway: Optional[AiGateway] = None,
) -> tuple[list[dict[str, Any]], date, str]:
    sanitized = sanitize_receipt_email_content(
        subject=payload.subject or "",
        from_address=payload.from_address,
        received_at=payload.received_at,
        text_body=payload.text_body,
        html_excerpt=payload.html_excerpt,
        raw_excerpt=payload.raw_excerpt,
    )
    if not sanitized.prompt_text:
        raise ReceiptValidationError(
            "Purchase receipt email contains no readable receipt content or valid attachment."
        )
    parsed_body = parse_receipt_orders_text_with_llm(
        sanitized.prompt_text, raise_on_error=True, gateway=ai_gateway
    )
    return (
        _normalize_parsed_orders(parsed_body),
        sanitized.fallback_date,
        sanitized.prompt_text[:32000],
    )


def _persist_or_dedup_order(
    db: Session,
    *,
    order: dict[str, Any],
    fallback_date: date,
    message_id: str,
    raw_text: Optional[str],
    stored_media: Optional[StoredReceiptMedia],
    ai_gateway: Optional[AiGateway] = None,
) -> tuple[ReceiptSnapshot, bool]:
    merchant = str(order.get("merchant") or "Unknown Merchant").strip()[:150]
    order_number = normalize_order_number(order.get("order_number"))
    currency = normalize_receipt_currency(order.get("currency"), fallback="NZD")
    total_amount = round(float(order.get("total_amount", 0.0) or 0.0), 2)
    r_date = resolve_order_date(order.get("receipt_date"), fallback_date)

    existing_dup = find_semantic_duplicate_receipt(
        db, merchant=merchant, receipt_date=r_date, total_amount=total_amount,
        currency=currency, order_number=order_number, document_type="receipt",
    )
    if existing_dup is not None:
        return load_receipt_snapshot(db, existing_dup.receipt_id), True

    tax_amount = (
        round(float(order["tax_amount"]), 2) if order.get("tax_amount") is not None else None
    )
    snapshot = _persist_new_order(
        db, merchant=merchant, r_date=r_date, total_amount=total_amount,
        currency=currency, tax_amount=tax_amount, order_number=order_number,
        message_id=message_id, raw_text=raw_text, stored_media=stored_media,
        raw_items=order.get("items") or [], ai_gateway=ai_gateway,
    )
    return snapshot, False


def _persist_new_order(
    db: Session,
    *,
    merchant: str,
    r_date: date,
    total_amount: float,
    currency: str,
    tax_amount: Optional[float],
    order_number: Optional[str],
    message_id: str,
    raw_text: Optional[str],
    stored_media: Optional[StoredReceiptMedia],
    raw_items: list[Any],
    ai_gateway: Optional[AiGateway],
) -> ReceiptSnapshot:
    receipt = Receipt(
        receipt_date=r_date, merchant=merchant, total_amount=total_amount,
        currency=currency, tax_amount=tax_amount, order_number=order_number,
        source_email_message_id=message_id[:512], status="unlinked",
        is_processed=True, raw_text=raw_text,
        image_data=stored_media.data if stored_media else None,
        image_mime_type=stored_media.mime_type if stored_media else None,
        image_filename=(
            stored_media.filename[:255] if stored_media and stored_media.filename else None
        ),
    )
    db.add(receipt)
    db.flush()
    resolved_drafts = apply_receipt_discounts_with_jev(
        drafts_from_parsed_items(raw_items), gateway=ai_gateway
    )
    persist_classified_receipt_items(
        db, receipt_id=receipt.id, merchant=receipt.merchant,
        drafts=resolved_drafts, gateway=ai_gateway,
    )
    db.commit()
    db.refresh(receipt)
    match_receipt_to_transaction(db, receipt.id)
    return load_receipt_snapshot(db, receipt.id)
