"""Router for receipt management, line-item extraction, cross-domain statistics, and transaction reconciliation."""

from datetime import datetime
import json
from typing import NoReturn, Optional
from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.db import get_db
from app.models import Receipt, ReceiptItem, Transaction
from app.services.ai_gateway import AiGateway, get_ai_gateway
from app.services.product_catalog import (
    ProductStatisticsSnapshot,
    compute_product_statistics,
)
from app.services.ledger_entry import normalize_tags
from app.schemas import (
    ProductStatisticItem,
    ProductStatisticsResponse,
    ReceiptCreate,
    ReceiptItemRead,
    ReceiptItemUpdate,
    ReceiptRead,
    TransactionCandidateRead,
)
from app.services.receipt_ingestion import (
    ManualReceiptInput,
    PdfTextExtractor,
    ReceiptIngestionError,
    ReceiptItemDraft,
    ReceiptItemSnapshot,
    ReceiptNotFoundError,
    ReceiptParseError,
    ReceiptSnapshot,
    ReceiptUpstreamError,
    ReceiptValidationError,
    TransactionCandidateSnapshot,
    create_manual_receipt,
    find_receipt_transaction_candidates,
    get_pdf_text_extractor,
    reconcile_unlinked_receipts,
    reprocess_receipt_media,
    upload_receipt_media,
)

router = APIRouter(prefix="/receipts", tags=["Receipts & Itemized Tracking"])

_EXCEPTION_STATUS_MAP: tuple[tuple[type[ReceiptIngestionError], int], ...] = (
    (ReceiptNotFoundError, status.HTTP_404_NOT_FOUND),
    (ReceiptValidationError, status.HTTP_400_BAD_REQUEST),
    (ReceiptParseError, status.HTTP_422_UNPROCESSABLE_ENTITY),
    (ReceiptUpstreamError, status.HTTP_503_SERVICE_UNAVAILABLE),
)


def _raise_http_error(exc: ReceiptIngestionError) -> NoReturn:
    for exc_type, http_status in _EXCEPTION_STATUS_MAP:
        if isinstance(exc, exc_type):
            raise HTTPException(status_code=http_status, detail=str(exc)) from exc
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _to_receipt_item_read(
    item: ReceiptItemSnapshot,
    fallback_created_at: datetime,
) -> ReceiptItemRead:
    return ReceiptItemRead(
        id=item.id,
        receipt_id=item.receipt_id,
        product_id=item.product_id,
        item_name=item.item_name,
        brand=item.brand,
        quantity=item.quantity,
        unit_price=item.unit_price,
        discount_amount=item.discount_amount,
        total_price=item.total_price,
        category=item.category,
        domain=item.domain,
        product_type=item.product_type,
        tags=list(item.tags),
        volume_ml=item.volume_ml,
        notes=item.notes,
        inherited_tags=list(item.inherited_tags),
        created_at=item.created_at or fallback_created_at,
    )


def _to_receipt_read(snapshot: ReceiptSnapshot) -> ReceiptRead:
    return ReceiptRead(
        id=snapshot.id,
        transaction_id=snapshot.transaction_id,
        document_type=snapshot.document_type,
        receipt_date=snapshot.receipt_date,
        merchant=snapshot.merchant,
        total_amount=snapshot.total_amount,
        currency=snapshot.currency,
        tax_amount=snapshot.tax_amount,
        order_number=snapshot.order_number,
        source_email_message_id=snapshot.source_email_message_id,
        status=snapshot.status,
        image_path=snapshot.image_path,
        image_mime_type=snapshot.image_mime_type,
        image_filename=snapshot.image_filename,
        has_image=snapshot.has_image,
        is_processed=snapshot.is_processed,
        raw_text=snapshot.raw_text,
        notes=snapshot.notes,
        items=[_to_receipt_item_read(item, snapshot.created_at) for item in snapshot.items],
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
    )


def _to_candidate_read(candidate: TransactionCandidateSnapshot) -> TransactionCandidateRead:
    return TransactionCandidateRead(
        transaction_id=candidate.transaction_id,
        transaction_date=candidate.transaction_date,
        merchant=candidate.merchant,
        raw_description=candidate.raw_description,
        amount=candidate.amount,
        currency=candidate.currency,
        account_id=candidate.account_id,
        account_name=candidate.account_name,
        original_amount=candidate.original_amount,
        original_currency=candidate.original_currency,
        estimated_receipt_amount=candidate.estimated_receipt_amount,
        confidence=candidate.confidence,
        match_reason=candidate.match_reason,
    )


def _to_manual_receipt_input(payload: ReceiptCreate) -> ManualReceiptInput:
    drafts = tuple(
        ReceiptItemDraft(
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
            explicit_tags=tuple(item.tags),
            notes=item.notes,
        )
        for item in payload.items
    )
    return ManualReceiptInput(
        receipt_date=payload.receipt_date,
        merchant=payload.merchant,
        total_amount=payload.total_amount,
        document_type=payload.document_type,
        currency=payload.currency,
        tax_amount=payload.tax_amount,
        order_number=payload.order_number,
        status=payload.status,
        notes=payload.notes,
        items=drafts,
    )


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


@router.post("", response_model=ReceiptRead, status_code=status.HTTP_201_CREATED)
def create_receipt(
    payload: ReceiptCreate,
    db: Session = Depends(get_db),
    ai_gateway: AiGateway = Depends(get_ai_gateway),
):
    """Create a structured receipt or payslip and attempt auto-matching."""
    try:
        snapshot = create_manual_receipt(
            db,
            _to_manual_receipt_input(payload),
            ai_gateway=ai_gateway,
        )
    except ReceiptIngestionError as exc:
        _raise_http_error(exc)
    return _to_receipt_read(snapshot)


@router.post("/upload", response_model=ReceiptRead, status_code=status.HTTP_201_CREATED)
async def upload_receipt(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    ai_gateway: AiGateway = Depends(get_ai_gateway),
    pdf_text_extractor: PdfTextExtractor = Depends(get_pdf_text_extractor),
):
    """Upload a receipt photo/PDF/text, parse with Vision/Text LLM, and store in DB."""
    content = await file.read()
    try:
        snapshot = upload_receipt_media(
            db,
            content=content,
            filename=file.filename,
            content_type=file.content_type,
            ai_gateway=ai_gateway,
            pdf_text_extractor=pdf_text_extractor,
        )
    except ReceiptIngestionError as exc:
        _raise_http_error(exc)
    return _to_receipt_read(snapshot)


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
    ai_gateway: AiGateway = Depends(get_ai_gateway),
    pdf_text_extractor: PdfTextExtractor = Depends(get_pdf_text_extractor),
):
    """Re-run LLM parsing on a stored receipt or payslip and compress image attachments on success."""
    try:
        snapshot = reprocess_receipt_media(
            db,
            receipt_id,
            ai_gateway=ai_gateway,
            pdf_text_extractor=pdf_text_extractor,
        )
    except ReceiptIngestionError as exc:
        _raise_http_error(exc)
    return _to_receipt_read(snapshot)


@router.get("/{receipt_id}/candidates", response_model=list[TransactionCandidateRead])
def list_receipt_candidates(
    receipt_id: int,
    db: Session = Depends(get_db),
):
    """Return top 5 sorted candidate matching transactions for linking to this receipt."""
    try:
        candidates = find_receipt_transaction_candidates(db, receipt_id, limit=5)
    except ReceiptIngestionError as exc:
        _raise_http_error(exc)
    return [_to_candidate_read(candidate) for candidate in candidates]


def _to_product_statistics_response(
    snapshot: ProductStatisticsSnapshot,
) -> ProductStatisticsResponse:
    return ProductStatisticsResponse(
        domain=snapshot.domain,
        total_products=snapshot.total_products,
        total_spend=snapshot.total_spend,
        products=[
            ProductStatisticItem(
                item_name=item.item_name,
                brand=item.brand,
                category=item.category,
                domain=item.domain,
                product_type=item.product_type,
                volume_ml=item.volume_ml,
                purchase_count=item.purchase_count,
                total_spent=item.total_spent,
                unit_price_latest=item.unit_price_latest,
                first_purchased=item.first_purchased,
                last_purchased=item.last_purchased,
                avg_days_between_purchases=item.avg_days_between_purchases,
                estimated_depletion_date=item.estimated_depletion_date,
            )
            for item in snapshot.products
        ],
    )


@router.get("/statistics/products", response_model=ProductStatisticsResponse)
def get_product_statistics(
    domain: Optional[str] = Query(default=None, description="e.g. '02-skin', '01-health', '20-home'"),
    product_type: Optional[str] = Query(default=None, description="e.g. 'vitamin_c_serum', 'protein_powder'"),
    db: Session = Depends(get_db),
):
    """Retrieve granular product purchase statistics, spend volume, and usage longevity."""
    snapshot = compute_product_statistics(db, domain=domain, product_type=product_type)
    return _to_product_statistics_response(snapshot)


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
