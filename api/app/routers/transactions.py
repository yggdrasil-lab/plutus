"""Transactions and spending router."""

from datetime import date, timedelta
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.models import Account, Transaction
from app.schemas import (
    TagCategoryBreakdownItem,
    TagSpendingSummaryItem,
    TagSpendingSummaryResponse,
    TransactionCreate,
    TransactionRead,
    TransactionUpdate,
)
from app.services.burn_service import TRANSFER_CATEGORIES, to_nzd_amount
from app.services.period_service import resolve_period_dates
from app.services.receipt_service import reconcile_unlinked_receipts
from app.services.statement_parser import generate_transaction_hash
from app.services.transfer_service import reconcile_transfers

router = APIRouter(prefix="/transactions", tags=["Transactions"])


def _normalize_tags(raw_tags: Optional[list[str]]) -> list[str]:
    if not raw_tags:
        return []
    seen: list[str] = []
    for item in raw_tags:
        cleaned = str(item).strip().lower().lstrip("#").replace(" ", "-")
        if cleaned and cleaned not in seen:
            seen.append(cleaned[:40])
    return seen


def _parse_stored_tags(raw_tags: Optional[str]) -> list[str]:
    """Safely parse and normalize JSON-encoded transaction tags."""
    if not raw_tags:
        return []
    try:
        parsed = json.loads(raw_tags)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return _normalize_tags(parsed)


@router.get("", response_model=list[TransactionRead])
def list_transactions(
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    category: Optional[str] = Query(default=None),
    tag: Optional[str] = Query(default=None),
    period: Optional[str] = Query(
        default=None,
        description="'week', 'last_week', 'month', 'last_month', 'annual', or 'all'",
    ),
    account_id: Optional[int] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """List transactions with optional date range, period, account_id, category, and tag filters."""
    start_date, end_date = resolve_period_dates(period, start_date, end_date)

    stmt = select(Transaction).options(
        joinedload(Transaction.account),
        joinedload(Transaction.receipt),
    )
    if account_id is not None:
        stmt = stmt.where(Transaction.account_id == account_id)
    if start_date:
        stmt = stmt.where(Transaction.transaction_date >= start_date)
    if end_date:
        stmt = stmt.where(Transaction.transaction_date <= end_date)
    if category:
        stmt = stmt.where(func.lower(Transaction.category) == category.strip().lower())

    ordered_stmt = stmt.order_by(desc(Transaction.transaction_date), desc(Transaction.id))
    if tag:
        target_tag = tag.strip().lower().lstrip("#").replace(" ", "-")
        rows = db.scalars(
            ordered_stmt.where(Transaction.tags.is_not(None), Transaction.tags != "[]")
        ).all()
        txs = [tx for tx in rows if target_tag in _parse_stored_tags(tx.tags)][:limit]
    else:
        txs = db.scalars(ordered_stmt.limit(limit)).all()
    for tx in txs:
        tx.account_name = tx.account.name if tx.account else None
        tx.has_receipt = bool(tx.receipt)
    return txs


@router.patch("/{transaction_id}", response_model=TransactionRead)
def update_transaction(
    transaction_id: int,
    payload: TransactionUpdate,
    db: Session = Depends(get_db),
):
    """Update category, merchant, or notes of an existing transaction."""
    tx = db.scalar(
        select(Transaction)
        .options(joinedload(Transaction.account), joinedload(Transaction.receipt))
        .where(Transaction.id == transaction_id)
    )
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if payload.category is not None:
        cat = payload.category.strip()
        if not cat:
            raise HTTPException(status_code=400, detail="Category name cannot be empty")
        tx.category = cat
        if cat.lower() not in ("transfers", "transfer"):
            if tx.linked_transaction_id is not None:
                partner = db.get(Transaction, tx.linked_transaction_id)
                if partner is not None:
                    partner.linked_transaction_id = None
                    partner.exchange_rate = None
                    partner.fee_amount = None
            tx.linked_transaction_id = None
            tx.exchange_rate = None
            tx.fee_amount = None

    if payload.merchant is not None:
        m = payload.merchant.strip()
        if m:
            tx.merchant = m

    if payload.notes is not None:
        tx.notes = payload.notes

    if payload.tags is not None:
        tx.tags = json.dumps(_normalize_tags(payload.tags))

    if "recurrence" in payload.model_fields_set:
        tx.recurrence = payload.recurrence

    db.commit()
    db.refresh(tx)
    tx.account_name = tx.account.name if tx.account else None
    tx.has_receipt = bool(tx.receipt)
    return tx


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
        .options(joinedload(Transaction.account), joinedload(Transaction.receipt))
        .where(Transaction.raw_hash == raw_hash)
    )
    if existing:
        existing.account_name = account.name
        existing.has_receipt = bool(existing.receipt)
        return existing

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
        tags=json.dumps(_normalize_tags(payload.tags)),
        recurrence=payload.recurrence,
        notes=payload.notes,
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)

    # Automatically reconcile any unlinked receipts and internal/cross-currency transfers
    reconcile_unlinked_receipts(db)
    reconcile_transfers(db)
    db.refresh(tx)

    tx.account_name = account.name
    tx.has_receipt = bool(tx.receipt)
    return tx


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
    """Get aggregated spending by category for a given date window."""
    stmt = (
        select(
            Transaction.category,
            func.sum(Transaction.amount).label("total_amount"),
            func.count(Transaction.id).label("count"),
        )
        .where(
            Transaction.transaction_date >= start_date,
            Transaction.transaction_date <= end_date,
        )
        .group_by(Transaction.category)
        .order_by(func.sum(Transaction.amount))
    )
    results = db.execute(stmt).all()
    return [
        {
            "category": row.category,
            "total_amount": float(row.total_amount or 0.0),
            "count": row.count,
        }
        for row in results
    ]


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
    """Aggregate NZD expense totals and per-category breakdowns for every transaction tag."""
    effective_period = None if (start_date is not None or end_date is not None) else period
    start_date, end_date = resolve_period_dates(effective_period, start_date, end_date)

    stmt = select(Transaction).where(
        Transaction.amount < 0,
        func.lower(Transaction.category).not_in(TRANSFER_CATEGORIES),
        Transaction.tags.is_not(None),
        Transaction.tags != "[]",
    )
    if start_date:
        stmt = stmt.where(Transaction.transaction_date >= start_date)
    if end_date:
        stmt = stmt.where(Transaction.transaction_date <= end_date)

    rows = db.scalars(stmt.order_by(desc(Transaction.transaction_date), desc(Transaction.id))).all()

    total_tagged_spend = 0.0
    tag_map: dict[str, dict] = {}
    for tx in rows:
        tx_tags = _parse_stored_tags(tx.tags)
        if not tx_tags:
            continue
        amt_nzd = to_nzd_amount(tx.amount, tx.currency, tx.exchange_rate)
        cat_name = (tx.category or "General").strip() or "General"
        total_tagged_spend += amt_nzd

        for tag_key in tx_tags:
            bucket = tag_map.setdefault(
                tag_key,
                {
                    "spend": 0.0,
                    "count": 0,
                    "first_date": tx.transaction_date,
                    "last_date": tx.transaction_date,
                    "categories": {},
                },
            )
            bucket["spend"] += amt_nzd
            bucket["count"] += 1
            if tx.transaction_date < bucket["first_date"]:
                bucket["first_date"] = tx.transaction_date
            if tx.transaction_date > bucket["last_date"]:
                bucket["last_date"] = tx.transaction_date

            cat_bucket = bucket["categories"].setdefault(cat_name, {"spend": 0.0, "count": 0})
            cat_bucket["spend"] += amt_nzd
            cat_bucket["count"] += 1

    tag_items: list[TagSpendingSummaryItem] = []
    for tag_key, bucket in tag_map.items():
        tag_total = round(bucket["spend"], 2)
        cat_items = [
            TagCategoryBreakdownItem(
                category=cat_name,
                total_spend=round(cat_data["spend"], 2),
                transaction_count=cat_data["count"],
                percentage=round(cat_data["spend"] / tag_total * 100.0, 1) if tag_total > 0 else 0.0,
            )
            for cat_name, cat_data in bucket["categories"].items()
        ]
        cat_items.sort(key=lambda item: (-item.total_spend, item.category))
        tag_items.append(
            TagSpendingSummaryItem(
                tag=tag_key,
                total_spend=tag_total,
                transaction_count=bucket["count"],
                first_date=bucket["first_date"],
                last_date=bucket["last_date"],
                categories=cat_items,
            )
        )

    tag_items.sort(key=lambda item: (-item.total_spend, item.tag))
    return TagSpendingSummaryResponse(
        period=period or "custom",
        start_date=start_date,
        end_date=end_date,
        total_tagged_spend=round(total_tagged_spend, 2),
        tags=tag_items,
    )
