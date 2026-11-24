"""Transactions and spending router."""

from datetime import date
import json
from typing import Literal, NoReturn, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Account, Transaction
from app.schemas import (
    TagCategoryBreakdownItem,
    TagSpendingSummaryItem,
    TagSpendingSummaryResponse,
    TransactionCreate,
    TransactionRead,
    TransactionSplitRead,
    TransactionSplitsUpdate,
    TransactionUpdate,
)
from app.services.ledger_entry import (
    UNSET,
    LedgerEntryError,
    LedgerEntryNotFoundError,
    LedgerEntryValidationError,
    ManualSplitLineInput,
    TransactionPatch,
    load_hydrated_transaction,
    normalize_tags,
    replace_manual_splits,
    update_transaction as update_ledger_transaction,
)
from app.services.receipt_ingestion import reconcile_unlinked_receipts
from app.services.spending_projection import (
    InvalidWindowError,
    ProjectedSplit,
    ProjectedTransaction,
    ReportingWindow,
    TransactionQuery,
    project_transactions,
    project_window,
)
from app.services.statement_parser import generate_transaction_hash
from app.services.transfer_service import reconcile_transfers

router = APIRouter(prefix="/transactions", tags=["Transactions"])


def _raise_ledger_http_error(exc: LedgerEntryError) -> NoReturn:
    """Translate LedgerEntry domain exceptions into HTTP responses."""
    if isinstance(exc, LedgerEntryNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, LedgerEntryValidationError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _projected_split_to_read(split: ProjectedSplit) -> TransactionSplitRead:
    """Adapt a frozen ProjectedSplit snapshot into a TransactionSplitRead DTO."""
    return TransactionSplitRead(
        id=split.id,
        transaction_id=split.transaction_id,
        amount=split.amount,
        category=split.category,
        tags=list(split.tags),
        loan_id=split.loan_id,
        loan_name=split.loan_name,
        loan_type=split.loan_type,
        notes=split.notes,
    )


def _projected_to_transaction_read(item: ProjectedTransaction) -> TransactionRead:
    """Adapt a frozen ProjectedTransaction snapshot into a TransactionRead DTO."""
    return TransactionRead(
        id=item.id, transaction_date=item.transaction_date, account_id=item.account_id,
        account_name=item.account_name, amount=item.amount, currency=item.currency,
        merchant=item.merchant, category=item.category, raw_description=item.raw_description,
        notes=item.notes, linked_transaction_id=item.linked_transaction_id,
        linked_account_name=item.linked_account_name, linked_amount=item.linked_amount,
        linked_currency=item.linked_currency, counterpart_account_id=item.counterpart_account_id,
        counterpart_account_name=item.counterpart_account_name,
        counterpart_account_type=item.counterpart_account_type, loan_id=item.loan_id,
        loan_name=item.loan_name, loan_type=item.loan_type,
        loan_payment_amount=item.loan_payment_amount, exchange_rate=item.exchange_rate,
        fee_amount=item.fee_amount, raw_hash=item.raw_hash, tags=list(item.tags),
        recurrence=item.recurrence, has_receipt=item.has_receipt, receipt_id=item.receipt_id,
        receipt_document_type=item.receipt_document_type, is_receipt_split=item.is_receipt_split,
        splits=[_projected_split_to_read(s) for s in item.splits], is_split=item.is_split,
        is_amortized_commitment=item.is_amortized_commitment,
        amortized_period_amount_nzd=item.amortized_period_amount_nzd,
        amortized_cadence=item.amortized_cadence,
        excluded_from_period_budget=item.excluded_from_period_budget,
        created_at=item.created_at,
    )


@router.get("", response_model=list[TransactionRead])
def list_transactions(
    response: Response,
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    category: Optional[str] = Query(default=None),
    include_subclasses: bool = Query(default=False, description="Match child categories inheriting from category"),
    include_amortized: bool = Query(
        default=False,
        description="Include active amortized recurring commitments and flag period-excluded items when inspecting a named weekly/monthly category",
    ),
    tag: Optional[str] = Query(default=None),
    period: Optional[str] = Query(
        default=None,
        description="'week', 'last_week', 'month', 'last_month', 'annual', or 'all'",
    ),
    account_id: Optional[int] = Query(default=None),
    loan_id: Optional[int] = Query(default=None),
    flow: Optional[Literal["inflow", "outflow"]] = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """List transactions with optional flow projection, date range, period, account_id, loan_id, category, and tag filters."""
    query = TransactionQuery(
        window=ReportingWindow(period=period, start_date=start_date, end_date=end_date),
        category=category, tag=tag, flow=flow, include_subclasses=include_subclasses,
        include_amortized=include_amortized, account_id=account_id, loan_id=loan_id,
        limit=limit, offset=offset,
    )
    try:
        projection = project_transactions(db, query)
    except InvalidWindowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response.headers["X-Total-Count"] = str(projection.total_count)
    return [_projected_to_transaction_read(item) for item in projection.items]


@router.patch("/{transaction_id}", response_model=TransactionRead)
def update_transaction(
    transaction_id: int,
    payload: TransactionUpdate,
    db: Session = Depends(get_db),
):
    """Update category, merchant, notes, tags, recurrence, counterpart_account_id, or loan link."""
    fields = payload.model_fields_set
    patch = TransactionPatch(
        category=payload.category if "category" in fields else UNSET,
        merchant=payload.merchant if "merchant" in fields else UNSET,
        notes=payload.notes if "notes" in fields else UNSET,
        tags=(
            (tuple(payload.tags) if payload.tags is not None else None)
            if "tags" in fields
            else UNSET
        ),
        recurrence=payload.recurrence if "recurrence" in fields else UNSET,
        counterpart_account_id=(
            payload.counterpart_account_id if "counterpart_account_id" in fields else UNSET
        ),
        loan_id=payload.loan_id if "loan_id" in fields else UNSET,
        loan_payment_amount=(
            payload.loan_payment_amount if "loan_payment_amount" in fields else UNSET
        ),
    )
    try:
        return update_ledger_transaction(db, transaction_id, patch)
    except LedgerEntryError as exc:
        _raise_ledger_http_error(exc)


@router.put("/{transaction_id}/splits", response_model=TransactionRead)
def replace_transaction_splits(
    transaction_id: int,
    payload: TransactionSplitsUpdate,
    db: Session = Depends(get_db),
):
    """Replace or clear manual split lines on an unlinked transaction."""
    split_inputs = tuple(
        ManualSplitLineInput(
            amount=item.amount,
            category=item.category,
            tags=tuple(item.tags),
            loan_id=item.loan_id,
            notes=item.notes,
        )
        for item in payload.splits
    )
    try:
        return replace_manual_splits(db, transaction_id, split_inputs)
    except LedgerEntryError as exc:
        _raise_ledger_http_error(exc)


@router.post("", response_model=TransactionRead, status_code=status.HTTP_201_CREATED)
def create_transaction(
    payload: TransactionCreate,
    db: Session = Depends(get_db),
):
    """Record an individual transaction with deterministic hash deduplication."""
    account = db.get(Account, payload.account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    raw_hash = generate_transaction_hash(
        tx_date=payload.transaction_date,
        amount=payload.amount,
        merchant=payload.merchant,
        account_id=payload.account_id,
    )

    existing_id = db.scalar(select(Transaction.id).where(Transaction.raw_hash == raw_hash))
    if existing_id is not None:
        return load_hydrated_transaction(db, existing_id)

    tx = Transaction(
        transaction_date=payload.transaction_date,
        account_id=payload.account_id,
        amount=payload.amount,
        currency=payload.currency,
        merchant=payload.merchant,
        category=payload.category,
        raw_description=payload.raw_description or payload.merchant,
        raw_hash=raw_hash,
        linked_transaction_id=payload.linked_transaction_id,
        exchange_rate=payload.exchange_rate,
        fee_amount=payload.fee_amount,
        tags=json.dumps(normalize_tags(payload.tags or [])),
        recurrence=payload.recurrence,
        notes=payload.notes,
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)

    # Automatically reconcile any unlinked receipts and internal/cross-currency transfers
    reconcile_unlinked_receipts(db)
    reconcile_transfers(db)

    return load_hydrated_transaction(db, tx.id, populate_existing=True)


@router.post("/reconcile-transfers")
def trigger_transfer_reconciliation(db: Session = Depends(get_db)):
    """Trigger automatic transfer reconciliation across accounts."""
    matched = reconcile_transfers(db)
    return {"status": "ok", "matched_transfers": matched}


@router.get("/summary/categories")
def get_category_summary(
    start_date: date = Query(...),
    end_date: date = Query(...),
    db: Session = Depends(get_db),
):
    """Get aggregated Reporting Currency spending by category for a given date window."""
    try:
        projection = project_window(
            db,
            ReportingWindow(start_date=start_date, end_date=end_date),
        )
    except InvalidWindowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    items = [
        {
            "category": item.category,
            "total_amount": 0.0 if item.total_spend == 0 else round(-item.total_spend, 2),
            "count": item.transaction_count,
        }
        for item in projection.categories
        if not item.is_transfer and item.transaction_count > 0
    ]
    items.sort(key=lambda row: (row["total_amount"], row["category"]))
    return items


@router.get("/summary/tags", response_model=TagSpendingSummaryResponse)
def get_tag_spending_summary(
    period: Optional[str] = Query(
        default="all",
        description="'week', 'last_week', 'month', 'last_month', 'annual', or 'all'",
    ),
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    db: Session = Depends(get_db),
):
    """Aggregate NZD expense totals and per-category breakdowns for every tag."""
    effective_period = None if (start_date is not None or end_date is not None) else period
    try:
        projection = project_window(
            db,
            ReportingWindow(period=effective_period, start_date=start_date, end_date=end_date),
        )
    except InvalidWindowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return TagSpendingSummaryResponse(
        period=projection.period,
        start_date=projection.start_date,
        end_date=projection.end_date,
        total_tagged_spend=projection.total_tagged_spend,
        tags=[
            TagSpendingSummaryItem(
                tag=t.tag,
                total_spend=t.total_spend,
                transaction_count=t.transaction_count,
                first_date=t.first_date,
                last_date=t.last_date,
                categories=[TagCategoryBreakdownItem(**vars(c)) for c in t.categories],
            )
            for t in projection.tags
        ],
    )
