"""Transactions and spending router."""

from datetime import date, timedelta
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.models import Account, Transaction
from app.schemas import TransactionCreate, TransactionRead, TransactionUpdate
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


@router.get("", response_model=list[TransactionRead])
def list_transactions(
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    category: Optional[str] = Query(default=None),
    tag: Optional[str] = Query(default=None),
    period: Optional[str] = Query(
        default=None,
        description="'week', 'last_week', 'month', 'last_month', or 'all'",
    ),
    account_id: Optional[int] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """List transactions with optional date range, period, account_id, and category filters."""
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

    txs = db.scalars(stmt.order_by(desc(Transaction.transaction_date), desc(Transaction.id)).limit(limit)).all()
    if tag:
        target_tag = tag.strip().lower().lstrip("#").replace(" ", "-")
        txs = [
            tx
            for tx in txs
            if target_tag in json.loads(tx.tags or "[]")
        ]
    linked_ids = {tx.linked_transaction_id for tx in txs if tx.linked_transaction_id is not None}
    linked_map: dict[int, Transaction] = {}
    if linked_ids:
        linked_rows = db.scalars(
            select(Transaction)
            .options(joinedload(Transaction.account))
            .where(Transaction.id.in_(linked_ids))
        ).all()
        linked_map = {row.id: row for row in linked_rows}

    for tx in txs:
        tx.account_name = tx.account.name if tx.account else None
        tx.has_receipt = bool(tx.receipt)
        partner = linked_map.get(tx.linked_transaction_id) if tx.linked_transaction_id else None
        if partner is not None:
            tx.linked_account_name = partner.account.name if partner.account else None
            tx.linked_amount = float(partner.amount)
            tx.linked_currency = partner.currency
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
