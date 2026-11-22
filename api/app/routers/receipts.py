"""Router for receipt management, line-item extraction, cross-domain statistics, and transaction reconciliation."""

from datetime import date
import json
from typing import Any, Optional
from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.db import get_db
from app.models import Receipt, ReceiptItem, Transaction
from app.services.rules_engine import normalize_tags
from app.schemas import (
    ProductStatisticsResponse,
    ReceiptCreate,
    ReceiptItemCreate,
    ReceiptItemRead,
    ReceiptItemUpdate,
    ReceiptRead,
    TransactionCandidateRead,
)
from app.services.receipt_service import (
    apply_parsed_payslip_fields,
    apply_receipt_discounts_with_jev,
    compress_receipt_image,
    compute_product_statistics,
    create_classified_receipt_item,
    find_receipt_transaction_candidates,
    infer_receipt_mime_type,
    load_receipt_rule_context,
    match_receipt_to_transaction,
    normalize_order_number,
    normalize_receipt_currency,
    parse_payslip_with_llm,
    parse_receipt_with_llm,
    reconcile_unlinked_receipts,
)

router = APIRouter(prefix="/receipts", tags=["Receipts & Itemized Tracking"])


@router.get("", response_model=list[ReceiptRead])
def list_receipts(
    status: Optional[str] = Query(default=None, description="'unlinked', 'needs_review', 'matched', or 'verified'"),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """List receipts ordered by receipt_date descending."""
    stmt = select(Receipt).options(
        joinedload(Receipt.transaction),
        selectinload(Receipt.items),
    )
    if status:
        stmt = stmt.where(Receipt.status == status)
    return (
        db.scalars(stmt.order_by(Receipt.receipt_date.desc(), Receipt.id.desc()).limit(limit))
        .unique()
        .all()
    )


def _persist_payload_receipt_items(db: Session, receipt: Receipt, items: list[Any]) -> None:
    """Classify and persist line items submitted via ReceiptCreate."""
    active_rules, existing_tags, extra_categories = load_receipt_rule_context(db)
    for item in items:
        create_classified_receipt_item(
            db,
            receipt=receipt,
            item_name=item.item_name,
            brand=item.brand,
            quantity=item.quantity,
            unit_price=item.unit_price,
            discount_amount=item.discount_amount,
            total_price=item.total_price,
            explicit_category=item.category,
            explicit_domain=item.domain,
            explicit_product_type=item.product_type,
            explicit_volume_ml=item.volume_ml,
            explicit_tags=item.tags,
            notes=item.notes,
            active_rules=active_rules,
            existing_tags=existing_tags,
            extra_categories=extra_categories,
        )


@router.post("", response_model=ReceiptRead, status_code=status.HTTP_201_CREATED)
def create_receipt(
    payload: ReceiptCreate,
    db: Session = Depends(get_db),
):
    """Create a structured receipt or payslip and attempt auto-matching."""
    receipt = Receipt(
        document_type=payload.document_type,
        receipt_date=payload.receipt_date,
        merchant=payload.merchant.strip(),
        total_amount=payload.total_amount,
        currency=normalize_receipt_currency(payload.currency, fallback="NZD"),
        tax_amount=payload.tax_amount,
        order_number=normalize_order_number(payload.order_number),
        status=payload.status,
        notes=payload.notes,
    )
    db.add(receipt)
    db.flush()
    if payload.document_type != "payslip" and payload.items:
        _persist_payload_receipt_items(db, receipt, payload.items)
    db.commit()
    db.refresh(receipt)
    match_receipt_to_transaction(receipt, db)
    return receipt


def _apply_parsed_receipt_fields(receipt: Receipt, parsed: dict[str, Any], db: Session) -> None:
    """Populate receipt header fields and itemized line items from parsed LLM JSON."""
    try:
        r_date = date.fromisoformat(parsed.get("receipt_date", date.today().isoformat()))
    except (TypeError, ValueError):
        r_date = date.today()

    receipt.receipt_date = r_date
    receipt.merchant = str(parsed.get("merchant") or "Unknown Merchant").strip()[:150]
    receipt.total_amount = float(parsed.get("total_amount", 0.0) or 0.0)
    receipt.currency = normalize_receipt_currency(
        parsed.get("currency"), fallback=receipt.currency or "NZD"
    )
    order_num = normalize_order_number(parsed.get("order_number"))
    if order_num:
        receipt.order_number = order_num
    receipt.tax_amount = float(parsed["tax_amount"]) if parsed.get("tax_amount") is not None else None
    receipt.is_processed = True
    receipt.notes = None

    receipt.items.clear()
    db.flush()

    active_rules, existing_tags, extra_categories = load_receipt_rule_context(db)
    resolved_items = apply_receipt_discounts_with_jev(parsed.get("items", []))
    for it in resolved_items:
        create_classified_receipt_item(
            db,
            receipt=receipt,
            item_name=str(it.get("item_name") or "Item"),
            brand=it.get("brand"),
            quantity=float(it.get("quantity", 1.0) or 1.0),
            unit_price=float(it.get("unit_price", 0.0) or 0.0),
            discount_amount=(
                float(it["discount_amount"]) if it.get("discount_amount") is not None else None
            ),
            total_price=float(it.get("total_price", 0.0) or 0.0),
            explicit_category=it.get("category"),
            explicit_domain=it.get("domain"),
            explicit_product_type=it.get("product_type"),
            explicit_volume_ml=it.get("volume_ml"),
            explicit_tags=it.get("tags"),
            notes=it.get("notes"),
            active_rules=active_rules,
            existing_tags=existing_tags,
            extra_categories=extra_categories,
        )


@router.post("/upload", response_model=ReceiptRead, status_code=status.HTTP_201_CREATED)
async def upload_receipt(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a receipt photo/PDF/text, parse with Hydrogen Vision, and store in DB.

    Processed images are downscaled and WebP-compressed to save database space.
    Unprocessed receipts retain their original image quality for later reprocessing.
    """
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded receipt file is empty")

    mime_type = infer_receipt_mime_type(file.filename, file.content_type)
    raw_text: Optional[str] = None
    if mime_type.startswith("text/"):
        try:
            raw_text = content.decode("utf-8")
        except UnicodeDecodeError:
            raw_text = None

    parsed = parse_receipt_with_llm(content, file.filename, mime_type)

    if parsed and "merchant" in parsed and "total_amount" in parsed:
        stored_bytes, stored_mime = compress_receipt_image(content, mime_type)
        receipt = Receipt(
            receipt_date=date.today(),
            merchant="Pending",
            total_amount=0.0,
            raw_text=raw_text,
            image_data=stored_bytes,
            image_mime_type=stored_mime,
            image_filename=file.filename,
            is_processed=True,
            status="unlinked",
        )
        db.add(receipt)
        db.flush()
        _apply_parsed_receipt_fields(receipt, parsed, db)
        db.commit()
        db.refresh(receipt)
        match_receipt_to_transaction(receipt, db)
        return receipt

    # Unprocessed fallback: preserve original image quality in DB for manual linking or reprocessing
    receipt = Receipt(
        receipt_date=date.today(),
        merchant=file.filename or "Unprocessed Receipt",
        total_amount=0.0,
        raw_text=raw_text,
        image_data=content,
        image_mime_type=mime_type,
        image_filename=file.filename,
        is_processed=False,
        status="unlinked",
        notes="Pending vision processing (original quality preserved)",
    )
    db.add(receipt)
    db.commit()
    db.refresh(receipt)
    return receipt


@router.get("/{receipt_id}/image")
def get_receipt_image(
    receipt_id: int,
    db: Session = Depends(get_db),
):
    """Serve the stored receipt image or document from the database."""
    receipt = db.get(Receipt, receipt_id)
    if not receipt or not receipt.image_data:
        raise HTTPException(status_code=404, detail="Receipt image not found")

    filename = receipt.image_filename or f"receipt-{receipt_id}"
    return Response(
        content=receipt.image_data,
        media_type=receipt.image_mime_type or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.post("/{receipt_id}/reprocess", response_model=ReceiptRead)
def reprocess_receipt(
    receipt_id: int,
    db: Session = Depends(get_db),
):
    """Re-run LLM parsing on a stored receipt or payslip and compress image attachments on success."""
    receipt = db.get(Receipt, receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    if not receipt.image_data:
        raise HTTPException(status_code=400, detail="Receipt has no stored image to reprocess")

    mime_type = receipt.image_mime_type or infer_receipt_mime_type(receipt.image_filename, None)
    if receipt.document_type == "payslip":
        parsed = parse_payslip_with_llm(receipt.image_data, receipt.image_filename, mime_type)
        if not apply_parsed_payslip_fields(receipt, parsed, db):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Payslip parsing failed; original document preserved.",
            )
    else:
        parsed = parse_receipt_with_llm(receipt.image_data, receipt.image_filename, mime_type)
        if not parsed or "merchant" not in parsed or "total_amount" not in parsed:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Receipt vision parsing failed; original image preserved.",
            )
        _apply_parsed_receipt_fields(receipt, parsed, db)

    compressed_bytes, compressed_mime = compress_receipt_image(receipt.image_data, mime_type)
    receipt.image_data = compressed_bytes
    receipt.image_mime_type = compressed_mime
    db.commit()
    db.refresh(receipt)
    if receipt.status in ("unlinked", "needs_review"):
        match_receipt_to_transaction(receipt, db)
    return receipt


@router.get("/{receipt_id}/candidates", response_model=list[TransactionCandidateRead])
def list_receipt_candidates(
    receipt_id: int,
    db: Session = Depends(get_db),
):
    """Return top 5 sorted candidate matching transactions for linking to this receipt."""
    receipt = db.get(Receipt, receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return find_receipt_transaction_candidates(db, receipt, limit=5)


@router.get("/statistics/products", response_model=ProductStatisticsResponse)
def get_product_statistics(
    domain: Optional[str] = Query(default=None, description="e.g. '02-skin', '01-health', '20-home'"),
    product_type: Optional[str] = Query(default=None, description="e.g. 'vitamin_c_serum', 'protein_powder'"),
    db: Session = Depends(get_db),
):
    """Retrieve granular product purchase statistics, spend volume, and usage longevity."""
    return compute_product_statistics(db, domain=domain, product_type=product_type)


@router.get("/{receipt_id}", response_model=ReceiptRead)
def get_receipt(
    receipt_id: int,
    db: Session = Depends(get_db),
):
    """Retrieve a single receipt with itemized line items."""
    receipt = db.scalar(
        select(Receipt)
        .options(joinedload(Receipt.transaction), selectinload(Receipt.items))
        .where(Receipt.id == receipt_id)
    )
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return receipt


@router.delete("/{receipt_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_receipt(
    receipt_id: int,
    db: Session = Depends(get_db),
):
    """Delete a receipt and its associated line items."""
    receipt = db.get(Receipt, receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    db.delete(receipt)
    db.commit()
    return None


@router.post("/{receipt_id}/link/{transaction_id}", response_model=ReceiptRead)
def link_receipt_to_transaction(
    receipt_id: int,
    transaction_id: int,
    db: Session = Depends(get_db),
):
    """Explicitly link a receipt to a specific transaction, guarding 1:1 uniqueness."""
    receipt = db.get(Receipt, receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")

    tx = db.get(Transaction, transaction_id)
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")

    already_linked = db.scalar(
        select(Receipt).where(
            Receipt.transaction_id == tx.id,
            Receipt.id != receipt.id,
        )
    )
    if already_linked is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Transaction {transaction_id} is already linked to receipt #{already_linked.id}.",
        )

    receipt.transaction_id = tx.id
    receipt.status = "matched"
    db.commit()
    db.refresh(receipt)
    return receipt


@router.patch("/{receipt_id}/items/{item_id}", response_model=ReceiptItemRead)
def update_receipt_item(
    receipt_id: int,
    item_id: int,
    payload: ReceiptItemUpdate,
    db: Session = Depends(get_db),
):
    """Update category, domain, tags, or notes on a single itemized receipt line."""
    item = db.scalar(
        select(ReceiptItem)
        .options(joinedload(ReceiptItem.receipt).joinedload(Receipt.transaction))
        .where(
            ReceiptItem.id == item_id,
            ReceiptItem.receipt_id == receipt_id,
        )
    )
    if not item:
        raise HTTPException(status_code=404, detail="Receipt item not found")

    if payload.category is not None:
        cat = payload.category.strip()
        if not cat:
            raise HTTPException(status_code=400, detail="Category name cannot be empty")
        item.category = cat

    if "domain" in payload.model_fields_set:
        item.domain = payload.domain.strip() if payload.domain else None

    if payload.tags is not None:
        item.tags = json.dumps(normalize_tags(payload.tags))

    if "notes" in payload.model_fields_set:
        item.notes = payload.notes

    db.commit()
    db.refresh(item)
    return item


@router.post("/reconcile")
def reconcile_receipts(
    db: Session = Depends(get_db),
):
    """Trigger retroactive matching between unlinked receipts and bank transactions."""
    matched = reconcile_unlinked_receipts(db)
    return {"status": "ok", "matched_count": matched}
