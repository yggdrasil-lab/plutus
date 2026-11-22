"""Transactions and spending router."""

from datetime import date
import json
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.models import Account, Loan, Receipt, Transaction, TransactionSplit
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
from app.services.fx_service import get_exchange_rate
from app.services.receipt_service import reconcile_unlinked_receipts
from app.services.rules_engine import normalize_tags
from app.services.spending_projection import (
    TRANSFER_CATEGORIES,
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


def _transaction_load_options(*, include_receipt_items: bool = False):
    """Return consistent eager-load options for TransactionRead hydration."""
    receipt_load = (
        joinedload(Transaction.receipt).joinedload(Receipt.items)
        if include_receipt_items
        else joinedload(Transaction.receipt)
    )
    return (
        joinedload(Transaction.account),
        joinedload(Transaction.counterpart_account),
        joinedload(Transaction.loan),
        joinedload(Transaction.linked_transaction).joinedload(Transaction.account),
        joinedload(Transaction.splits).joinedload(TransactionSplit.loan),
        receipt_load,
    )


def hydrate_transaction(tx: Transaction) -> Transaction:
    """Populate derived account, counterpart, loan, linked-transfer, split, and receipt fields."""
    tx.account_name = tx.account.name if tx.account else None
    tx.counterpart_account_name = (
        tx.counterpart_account.name if tx.counterpart_account else None
    )
    tx.counterpart_account_type = (
        tx.counterpart_account.account_type if tx.counterpart_account else None
    )
    tx.loan_name = tx.loan.name if tx.loan else None
    tx.loan_type = tx.loan.loan_type if tx.loan else None
    linked = tx.linked_transaction
    if linked is not None:
        tx.linked_account_name = linked.account.name if linked.account else None
        tx.linked_amount = float(linked.amount)
        tx.linked_currency = linked.currency
    else:
        tx.linked_account_name = None
        tx.linked_amount = None
        tx.linked_currency = None
    splits = getattr(tx, "splits", None) or []
    for split in splits:
        split.loan_name = split.loan.name if split.loan else None
        split.loan_type = split.loan.loan_type if split.loan else None
    tx.is_split = len(splits) >= 2
    tx.has_receipt = bool(tx.receipt)
    tx.receipt_id = tx.receipt.id if tx.receipt else None
    tx.receipt_document_type = tx.receipt.document_type if tx.receipt else None
    return tx


def _resolve_unlinked_fx_rate(db: Session, leg: Transaction) -> Optional[float]:
    """Return None for NZD or resolved currency->NZD rate for unlinked non-NZD transactions."""
    currency = (leg.currency or "NZD").strip().upper()
    if currency == "NZD":
        return None
    resolved = get_exchange_rate(
        db,
        rate_date=leg.transaction_date,
        from_currency=currency,
        to_currency="NZD",
    )
    if resolved is not None and resolved[0] > 0:
        return round(float(resolved[0]), 8)
    return None


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


def _apply_category_update(db: Session, tx: Transaction, raw_category: str) -> None:
    """Apply category change and unlink paired transfer legs when moving off Transfers."""
    cat = raw_category.strip()
    if not cat:
        raise HTTPException(status_code=400, detail="Category name cannot be empty")
    was_transfer_or_linked = (
        (tx.category or "").strip().lower() in TRANSFER_CATEGORIES
        or tx.linked_transaction_id is not None
    )
    tx.category = cat
    if cat.lower() in TRANSFER_CATEGORIES or not was_transfer_or_linked:
        return
    if tx.linked_transaction_id is not None:
        partner = db.get(Transaction, tx.linked_transaction_id)
        if partner is not None:
            partner.linked_transaction_id = None
            partner.fee_amount = None
            partner.exchange_rate = _resolve_unlinked_fx_rate(db, partner)
    tx.linked_transaction_id = None
    tx.fee_amount = None
    tx.exchange_rate = _resolve_unlinked_fx_rate(db, tx)


def _apply_counterpart_account_update(
    db: Session, tx: Transaction, payload: TransactionUpdate
) -> None:
    """Validate and apply manual counterpart_account_id updates without creating mirror rows."""
    if "counterpart_account_id" not in payload.model_fields_set:
        return
    target_id = payload.counterpart_account_id
    if target_id is None:
        tx.counterpart_account_id = None
        return
    if db.get(Account, target_id) is None:
        raise HTTPException(status_code=404, detail="Counterpart account not found")
    if target_id == tx.account_id:
        raise HTTPException(
            status_code=400,
            detail="Counterpart account cannot match transaction account",
        )
    tx.counterpart_account_id = target_id


def _apply_loan_update(db: Session, tx: Transaction, payload: TransactionUpdate) -> None:
    """Validate and apply loan_id and loan_payment_amount updates."""
    old_loan_id = tx.loan_id
    if "loan_id" in payload.model_fields_set:
        if payload.loan_id is not None and db.get(Loan, payload.loan_id) is None:
            raise HTTPException(status_code=404, detail="Loan not found")
        effective_loan_id = payload.loan_id
    else:
        effective_loan_id = old_loan_id

    if "loan_payment_amount" in payload.model_fields_set:
        if payload.loan_payment_amount is None:
            tx.loan_payment_amount = None
        else:
            if effective_loan_id is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="loan_payment_amount requires an active loan_id.",
                )
            if float(payload.loan_payment_amount) > abs(float(tx.amount)):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="loan_payment_amount cannot exceed the transaction amount.",
                )
            tx.loan_payment_amount = float(payload.loan_payment_amount)
    elif "loan_id" in payload.model_fields_set and (
        payload.loan_id is None or payload.loan_id != old_loan_id
    ):
        tx.loan_payment_amount = None

    if "loan_id" in payload.model_fields_set:
        tx.loan_id = payload.loan_id


@router.patch("/{transaction_id}", response_model=TransactionRead)
def update_transaction(
    transaction_id: int,
    payload: TransactionUpdate,
    db: Session = Depends(get_db),
):
    """Update category, merchant, notes, tags, recurrence, counterpart_account_id, or loan link."""
    tx = db.scalar(
        select(Transaction)
        .options(*_transaction_load_options())
        .where(Transaction.id == transaction_id)
    )
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if payload.category is not None:
        _apply_category_update(db, tx, payload.category)
    if payload.merchant is not None and payload.merchant.strip():
        tx.merchant = payload.merchant.strip()
    if payload.notes is not None:
        tx.notes = payload.notes
    if payload.tags is not None:
        tx.tags = json.dumps(normalize_tags(payload.tags))
    if "recurrence" in payload.model_fields_set:
        tx.recurrence = payload.recurrence

    _apply_counterpart_account_update(db, tx, payload)
    _apply_loan_update(db, tx, payload)

    db.commit()
    hydrated = db.scalar(
        select(Transaction)
        .execution_options(populate_existing=True)
        .options(*_transaction_load_options())
        .where(Transaction.id == transaction_id)
    )
    return hydrate_transaction(hydrated or tx)


@router.put("/{transaction_id}/splits", response_model=TransactionRead)
def replace_transaction_splits(
    transaction_id: int,
    payload: TransactionSplitsUpdate,
    db: Session = Depends(get_db),
):
    """Replace or clear manual split lines on an unlinked transaction."""
    tx = db.scalar(
        select(Transaction)
        .options(*_transaction_load_options())
        .where(Transaction.id == transaction_id)
    )
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.linked_transaction_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Linked transfers cannot be split.",
        )

    if not payload.splits:
        tx.splits.clear()
        db.commit()
        hydrated = db.scalar(
            select(Transaction)
            .options(*_transaction_load_options())
            .where(Transaction.id == transaction_id)
        )
        return hydrate_transaction(hydrated or tx)

    if len(payload.splits) < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least 2 split lines are required when splitting a transaction.",
        )

    signed_sum = round(sum(float(item.amount) for item in payload.splits), 2)
    tx_signed = round(float(tx.amount), 2)
    tx_abs = round(abs(float(tx.amount)), 2)
    is_signed_match = abs(signed_sum - tx_signed) <= 0.01
    is_legacy_unsigned = (
        float(tx.amount) < 0
        and all(float(item.amount) > 0 for item in payload.splits)
        and abs(signed_sum - tx_abs) <= 0.01
    )
    if not is_signed_match and not is_legacy_unsigned:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Signed split amounts ({signed_sum:.2f}) must sum to "
                f"transaction amount ({tx_signed:.2f})."
            ),
        )

    prepared_splits: list[TransactionSplit] = []
    for item in payload.splits:
        cat_clean = (item.category or "").strip()
        if not cat_clean:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Split category cannot be empty.",
            )
        if len(cat_clean) > 50:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Split category cannot exceed 50 characters.",
            )
        if item.loan_id is not None:
            if db.get(Loan, item.loan_id) is None:
                raise HTTPException(status_code=404, detail="Loan not found")
            if cat_clean.lower() != "debt & loans":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="loan_id is only permitted when split category is 'Debt & Loans'.",
                )
        notes_clean = item.notes.strip() if item.notes is not None else None
        if notes_clean == "":
            notes_clean = None
        effective_amount = round(
            -float(item.amount) if is_legacy_unsigned else float(item.amount),
            2,
        )
        prepared_splits.append(
            TransactionSplit(
                amount=effective_amount,
                category=cat_clean,
                tags=json.dumps(normalize_tags(item.tags)),
                loan_id=item.loan_id,
                notes=notes_clean,
            )
        )

    tx.splits.clear()
    db.flush()
    tx.splits.extend(prepared_splits)
    tx.loan_id = None
    tx.loan_payment_amount = None
    db.commit()

    hydrated = db.scalar(
        select(Transaction)
        .options(*_transaction_load_options())
        .where(Transaction.id == transaction_id)
    )
    return hydrate_transaction(hydrated or tx)


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

    existing = db.scalar(
        select(Transaction)
        .options(*_transaction_load_options())
        .where(Transaction.raw_hash == raw_hash)
    )
    if existing:
        return hydrate_transaction(existing)

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

    hydrated = db.scalar(
        select(Transaction)
        .options(*_transaction_load_options())
        .where(Transaction.id == tx.id)
    )
    return hydrate_transaction(hydrated or tx)


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
