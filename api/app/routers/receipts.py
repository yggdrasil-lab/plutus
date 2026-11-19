"""Router for receipt management, line-item extraction, cross-domain statistics, and transaction reconciliation."""

from datetime import date
import json
from typing import Any, Optional
from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Receipt, ReceiptItem, Transaction
from app.schemas import (
    ProductStatisticsResponse,
    ReceiptCreate,
    ReceiptItemCreate,
    ReceiptRead,
)
from app.services.receipt_service import (
    apply_receipt_discounts_with_jev,
    classify_line_item,
    compress_receipt_image,
    compute_product_statistics,
    extract_tags_for_line_item,
    get_or_create_product,
    infer_receipt_mime_type,
    match_receipt_to_transaction,
    parse_receipt_with_llm,
    reconcile_unlinked_receipts,
)

router = APIRouter(prefix="/receipts", tags=["Receipts & Itemized Tracking"])


@router.get("", response_model=list[ReceiptRead])
def list_receipts(
    status: Optional[str] = Query(default=None, description="'unlinked', 'matched', or 'verified'"),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """List receipts ordered by receipt_date descending."""
    stmt = select(Receipt)
    if status:
        stmt = stmt.where(Receipt.status == status)
    return db.scalars(stmt.order_by(Receipt.receipt_date.desc(), Receipt.id.desc()).limit(limit)).all()


@router.post("", response_model=ReceiptRead, status_code=status.HTTP_201_CREATED)
def create_receipt(
    payload: ReceiptCreate,
    db: Session = Depends(get_db),
):
    """Create a structured receipt with itemized line items and attempt auto-matching."""
    receipt = Receipt(
        receipt_date=payload.receipt_date,
        merchant=payload.merchant.strip(),
        total_amount=payload.total_amount,
        currency=payload.currency,
        tax_amount=payload.tax_amount,
        status=payload.status,
        notes=payload.notes,
    )
    db.add(receipt)
    db.flush()

    for item in payload.items:
        cat = item.category
        dom = item.domain
        ptype = item.product_type
        vol = item.volume_ml

        if not dom or not ptype:
            auto_cat, auto_dom, auto_ptype, auto_vol = classify_line_item(item.item_name)
            cat = cat or auto_cat
            dom = dom or auto_dom
            ptype = ptype or auto_ptype
            vol = vol or auto_vol

        tags_list = item.tags or extract_tags_for_line_item(item.item_name, dom, ptype)
        prod = get_or_create_product(
            db=db,
            item_name=item.item_name.strip(),
            brand=item.brand.strip() if item.brand else None,
            category=cat,
            domain=dom,
            tags=tags_list,
            volume_ml=vol,
        )

        r_item = ReceiptItem(
            receipt_id=receipt.id,
            product_id=prod.id,
            item_name=item.item_name.strip(),
            brand=item.brand.strip() if item.brand else None,
            quantity=item.quantity,
            unit_price=item.unit_price,
            total_price=item.total_price,
            category=cat or "General",
            domain=dom,
            product_type=ptype,
            tags=json.dumps(tags_list),
            volume_ml=vol,
            notes=item.notes,
        )
        db.add(r_item)

    db.commit()
    db.refresh(receipt)

    # Attempt auto-match against ledger transactions
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
    receipt.tax_amount = float(parsed["tax_amount"]) if parsed.get("tax_amount") is not None else None
    receipt.is_processed = True
    receipt.notes = None

    receipt.items.clear()
    db.flush()

    resolved_items = apply_receipt_discounts_with_jev(parsed.get("items", []))
    for it in resolved_items:
        item_name = str(it.get("item_name") or "Item").strip()
        total_price = float(it.get("total_price", 0.0) or 0.0)
        cat, dom, ptype, vol = classify_line_item(item_name)
        it_cat = it.get("category") or cat
        it_dom = it.get("domain") or dom
        it_ptype = it.get("product_type") or ptype
        it_vol = it.get("volume_ml") or vol
        it_tags = it.get("tags") or extract_tags_for_line_item(item_name, it_dom, it_ptype)

        prod_id: Optional[int] = None
        if total_price >= 0:
            prod = get_or_create_product(
                db=db,
                item_name=item_name,
                brand=it.get("brand"),
                category=it_cat,
                domain=it_dom,
                tags=it_tags,
                volume_ml=it_vol,
            )
            prod_id = prod.id

        r_item = ReceiptItem(
            receipt_id=receipt.id,
            product_id=prod_id,
            item_name=item_name,
            brand=it.get("brand"),
            quantity=float(it.get("quantity", 1.0) or 1.0),
            unit_price=float(it.get("unit_price", 0.0) or 0.0),
            discount_amount=float(it["discount_amount"]) if it.get("discount_amount") is not None else None,
            total_price=total_price,
            category=it_cat,
            domain=it_dom,
            product_type=it_ptype,
            tags=json.dumps(it_tags),
            volume_ml=it_vol,
            notes=it.get("notes"),
        )
        db.add(r_item)


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
    """Re-run Hydrogen Vision parsing on an unprocessed stored receipt and compress on success."""
    receipt = db.get(Receipt, receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    if not receipt.image_data:
        raise HTTPException(status_code=400, detail="Receipt has no stored image to reprocess")

    mime_type = receipt.image_mime_type or infer_receipt_mime_type(receipt.image_filename, None)
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
    if receipt.status == "unlinked":
        match_receipt_to_transaction(receipt, db)
    return receipt


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
    receipt = db.get(Receipt, receipt_id)
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
    """Explicitly link a receipt to a specific transaction."""
    receipt = db.get(Receipt, receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")

    tx = db.get(Transaction, transaction_id)
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")

    receipt.transaction_id = tx.id
    receipt.status = "matched"
    db.commit()
    db.refresh(receipt)
    return receipt


@router.post("/reconcile")
def reconcile_receipts(
    db: Session = Depends(get_db),
):
    """Trigger retroactive matching between unlinked receipts and bank transactions."""
    matched = reconcile_unlinked_receipts(db)
    return {"status": "ok", "matched_count": matched}
