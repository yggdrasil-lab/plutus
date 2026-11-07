"""Router for receipt management, line-item extraction, cross-domain statistics, and transaction reconciliation."""

from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
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
    classify_line_item,
    compute_product_statistics,
    match_receipt_to_transaction,
    parse_receipt_text_with_llm,
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

        r_item = ReceiptItem(
            receipt_id=receipt.id,
            item_name=item.item_name.strip(),
            brand=item.brand.strip() if item.brand else None,
            quantity=item.quantity,
            unit_price=item.unit_price,
            total_price=item.total_price,
            category=cat or "General",
            domain=dom,
            product_type=ptype,
            volume_ml=vol,
            notes=item.notes,
        )
        db.add(r_item)

    db.commit()
    db.refresh(receipt)

    # Attempt auto-match against ledger transactions
    match_receipt_to_transaction(receipt, db)

    return receipt


@router.post("/upload", response_model=ReceiptRead, status_code=status.HTTP_201_CREATED)
async def upload_receipt(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a receipt image or text document, parse structured items with Jev, and store."""
    content = await file.read()
    raw_text = ""
    try:
        raw_text = content.decode("utf-8")
    except UnicodeDecodeError:
        raw_text = f"Receipt file: {file.filename} (binary size: {len(content)} bytes)"

    parsed = parse_receipt_text_with_llm(raw_text)

    if parsed and "merchant" in parsed and "total_amount" in parsed:
        try:
            r_date = date.fromisoformat(parsed.get("receipt_date", date.today().isoformat()))
        except ValueError:
            r_date = date.today()

        receipt = Receipt(
            receipt_date=r_date,
            merchant=parsed.get("merchant", "Unknown Merchant"),
            total_amount=float(parsed.get("total_amount", 0.0)),
            tax_amount=float(parsed.get("tax_amount", 0.0)) if parsed.get("tax_amount") else None,
            raw_text=raw_text,
            status="unlinked",
        )
        db.add(receipt)
        db.flush()

        for it in parsed.get("items", []):
            item_name = it.get("item_name", "Item")
            cat, dom, ptype, vol = classify_line_item(item_name)
            r_item = ReceiptItem(
                receipt_id=receipt.id,
                item_name=item_name,
                brand=it.get("brand"),
                quantity=float(it.get("quantity", 1.0)),
                unit_price=float(it.get("unit_price", 0.0)),
                total_price=float(it.get("total_price", 0.0)),
                category=it.get("category") or cat,
                domain=it.get("domain") or dom,
                product_type=it.get("product_type") or ptype,
                volume_ml=it.get("volume_ml") or vol,
            )
            db.add(r_item)

        db.commit()
        db.refresh(receipt)
        match_receipt_to_transaction(receipt, db)
        return receipt

    # Fallback when LLM is unavailable: create unlinked placeholder
    receipt = Receipt(
        receipt_date=date.today(),
        merchant=file.filename or "Scanned Receipt",
        total_amount=0.0,
        raw_text=raw_text,
        status="unlinked",
    )
    db.add(receipt)
    db.commit()
    db.refresh(receipt)
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
