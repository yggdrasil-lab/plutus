"""Manual creation, media upload, and media reprocessing lifecycles for receipts."""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import Receipt
from app.services.ai_gateway import AiGateway
from app.services.receipt_ingestion._types import (
    ManualReceiptInput,
    PdfTextExtractor,
    ReceiptNotFoundError,
    ReceiptParseError,
    ReceiptSnapshot,
    ReceiptValidationError,
)
from app.services.receipt_ingestion.classification import (
    apply_receipt_discounts_with_jev,
    drafts_from_parsed_items,
    persist_classified_receipt_items,
)
from app.services.receipt_ingestion.matching import (
    load_receipt_snapshot,
    match_receipt_to_transaction,
)
from app.services.receipt_ingestion.media import (
    compress_receipt_image,
    infer_receipt_mime_type,
)
from app.services.receipt_ingestion.payslip_ingestion import apply_parsed_payslip_fields
from app.services.receipt_ingestion.prompts import (
    parse_payslip_with_llm,
    parse_receipt_with_llm,
)
from app.services.receipt_ingestion.sanitization import (
    normalize_order_number,
    normalize_receipt_currency,
    resolve_order_date,
)

_VALID_DOCUMENT_TYPES = frozenset({"receipt", "payslip"})


def create_manual_receipt(
    db: Session,
    payload: ManualReceiptInput,
    *,
    ai_gateway: Optional[AiGateway] = None,
) -> ReceiptSnapshot:
    """Create a manual receipt or payslip without semantic dedup or discount rewriting."""
    document_type, merchant = _validate_manual_input(payload)
    receipt = Receipt(
        document_type=document_type,
        receipt_date=payload.receipt_date,
        merchant=merchant[:150],
        total_amount=float(payload.total_amount),
        currency=normalize_receipt_currency(payload.currency, fallback="NZD"),
        tax_amount=payload.tax_amount,
        order_number=normalize_order_number(payload.order_number),
        status=payload.status,
        notes=payload.notes,
    )
    db.add(receipt)
    db.flush()
    if document_type != "payslip" and payload.items:
        persist_classified_receipt_items(
            db,
            receipt_id=receipt.id,
            merchant=receipt.merchant,
            drafts=payload.items,
            gateway=ai_gateway,
        )
    db.commit()
    db.refresh(receipt)
    match_receipt_to_transaction(db, receipt.id)
    return load_receipt_snapshot(db, receipt.id)


def upload_receipt_media(
    db: Session,
    *,
    content: bytes,
    filename: Optional[str],
    content_type: Optional[str],
    ai_gateway: Optional[AiGateway] = None,
    pdf_text_extractor: Optional[PdfTextExtractor] = None,
) -> ReceiptSnapshot:
    """Upload a receipt photo, PDF, or text file, parse with LLM, and persist."""
    if not content:
        raise ReceiptValidationError("Uploaded receipt file is empty")

    mime_type = infer_receipt_mime_type(filename, content_type)
    raw_text = _decode_text_preview(content, mime_type)
    parsed = parse_receipt_with_llm(
        content, filename, mime_type,
        db=db, raise_on_error=False,
        gateway=ai_gateway, pdf_text_extractor=pdf_text_extractor,
    )
    if _is_valid_parsed_receipt(parsed):
        assert parsed is not None
        receipt_id = _persist_processed_upload(
            db, content=content, filename=filename, mime_type=mime_type,
            raw_text=raw_text, parsed=parsed, ai_gateway=ai_gateway,
        )
        match_receipt_to_transaction(db, receipt_id)
        return load_receipt_snapshot(db, receipt_id)

    receipt_id = _persist_unprocessed_upload(
        db, content=content, filename=filename, mime_type=mime_type, raw_text=raw_text,
    )
    return load_receipt_snapshot(db, receipt_id)


def reprocess_receipt_media(
    db: Session,
    receipt_id: int,
    *,
    ai_gateway: Optional[AiGateway] = None,
    pdf_text_extractor: Optional[PdfTextExtractor] = None,
) -> ReceiptSnapshot:
    """Re-run LLM parsing on stored receipt or payslip media and compress images."""
    receipt = db.get(Receipt, receipt_id)
    if receipt is None:
        raise ReceiptNotFoundError(f"Receipt {receipt_id} not found")
    if not receipt.image_data:
        raise ReceiptValidationError("Receipt has no stored image to reprocess")

    mime_type = receipt.image_mime_type or infer_receipt_mime_type(receipt.image_filename, None)
    if receipt.document_type == "payslip":
        _reprocess_payslip(
            db,
            receipt,
            mime_type=mime_type,
            ai_gateway=ai_gateway,
            pdf_text_extractor=pdf_text_extractor,
        )
    else:
        _reprocess_purchase_receipt(
            db,
            receipt,
            mime_type=mime_type,
            ai_gateway=ai_gateway,
            pdf_text_extractor=pdf_text_extractor,
        )

    compressed_bytes, compressed_mime = compress_receipt_image(receipt.image_data, mime_type)
    receipt.image_data = compressed_bytes
    receipt.image_mime_type = compressed_mime
    db.commit()
    db.expire(receipt)
    if receipt.status in ("unlinked", "needs_review"):
        match_receipt_to_transaction(db, receipt.id)
    return load_receipt_snapshot(db, receipt.id)


def _validate_manual_input(payload: ManualReceiptInput) -> tuple[str, str]:
    doc_type = (payload.document_type or "receipt").strip().lower()
    if doc_type not in _VALID_DOCUMENT_TYPES:
        raise ReceiptValidationError(
            f"Unsupported document_type '{payload.document_type}'."
        )
    merchant = (payload.merchant or "").strip()
    if not merchant:
        raise ReceiptValidationError("Merchant cannot be empty")
    if float(payload.total_amount) < 0:
        raise ReceiptValidationError("Total amount cannot be negative")
    return doc_type, merchant


def _decode_text_preview(content: bytes, mime_type: str) -> Optional[str]:
    if not mime_type.startswith("text/"):
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_valid_parsed_receipt(parsed: Optional[dict[str, Any]]) -> bool:
    return bool(parsed and "merchant" in parsed and "total_amount" in parsed)


def _apply_parsed_receipt_fields(
    receipt: Receipt,
    parsed: dict[str, Any],
    db: Session,
    *,
    ai_gateway: Optional[AiGateway] = None,
) -> None:
    receipt.receipt_date = resolve_order_date(parsed.get("receipt_date"), date.today())
    receipt.merchant = str(parsed.get("merchant") or "Unknown Merchant").strip()[:150]
    receipt.total_amount = float(parsed.get("total_amount", 0.0) or 0.0)
    receipt.currency = normalize_receipt_currency(
        parsed.get("currency"), fallback=receipt.currency or "NZD"
    )
    order_num = normalize_order_number(parsed.get("order_number"))
    if order_num:
        receipt.order_number = order_num
    receipt.tax_amount = (
        float(parsed["tax_amount"]) if parsed.get("tax_amount") is not None else None
    )
    receipt.is_processed = True
    receipt.notes = None

    if receipt.items:
        receipt.items.clear()
        db.flush()

    drafts = drafts_from_parsed_items(parsed.get("items") or [])
    resolved_drafts = apply_receipt_discounts_with_jev(drafts, gateway=ai_gateway)
    persist_classified_receipt_items(
        db,
        receipt_id=receipt.id,
        merchant=receipt.merchant,
        drafts=resolved_drafts,
        gateway=ai_gateway,
    )


def _persist_processed_upload(
    db: Session,
    *,
    content: bytes,
    filename: Optional[str],
    mime_type: str,
    raw_text: Optional[str],
    parsed: dict[str, Any],
    ai_gateway: Optional[AiGateway] = None,
) -> int:
    stored_bytes, stored_mime = compress_receipt_image(content, mime_type)
    receipt = Receipt(
        receipt_date=date.today(),
        merchant="Pending",
        total_amount=0.0,
        raw_text=raw_text,
        image_data=stored_bytes,
        image_mime_type=stored_mime,
        image_filename=filename[:255] if filename else None,
        is_processed=True,
        status="unlinked",
    )
    db.add(receipt)
    db.flush()
    _apply_parsed_receipt_fields(receipt, parsed, db, ai_gateway=ai_gateway)
    db.commit()
    db.refresh(receipt)
    return receipt.id


def _persist_unprocessed_upload(
    db: Session,
    *,
    content: bytes,
    filename: Optional[str],
    mime_type: str,
    raw_text: Optional[str],
) -> int:
    receipt = Receipt(
        receipt_date=date.today(),
        merchant=(filename or "Unprocessed Receipt")[:150],
        total_amount=0.0,
        raw_text=raw_text,
        image_data=content,
        image_mime_type=mime_type,
        image_filename=filename[:255] if filename else None,
        is_processed=False,
        status="unlinked",
        notes="Pending vision processing (original quality preserved)",
    )
    db.add(receipt)
    db.commit()
    db.refresh(receipt)
    return receipt.id


def _reprocess_payslip(
    db: Session,
    receipt: Receipt,
    *,
    mime_type: str,
    ai_gateway: Optional[AiGateway],
    pdf_text_extractor: Optional[PdfTextExtractor],
) -> None:
    parsed = parse_payslip_with_llm(
        receipt.image_data or b"",
        receipt.image_filename,
        mime_type,
        raise_on_error=True,
        gateway=ai_gateway,
        pdf_text_extractor=pdf_text_extractor,
    )
    if not apply_parsed_payslip_fields(receipt, parsed, db):
        raise ReceiptParseError("Payslip parsing failed; original document preserved.")


def _reprocess_purchase_receipt(
    db: Session,
    receipt: Receipt,
    *,
    mime_type: str,
    ai_gateway: Optional[AiGateway],
    pdf_text_extractor: Optional[PdfTextExtractor],
) -> None:
    parsed = parse_receipt_with_llm(
        receipt.image_data or b"",
        receipt.image_filename,
        mime_type,
        db=db,
        raise_on_error=True,
        gateway=ai_gateway,
        pdf_text_extractor=pdf_text_extractor,
    )
    if not _is_valid_parsed_receipt(parsed):
        raise ReceiptParseError("Receipt vision parsing failed; original image preserved.")
    assert parsed is not None
    _apply_parsed_receipt_fields(receipt, parsed, db, ai_gateway=ai_gateway)
