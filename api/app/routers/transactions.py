"""Transactions and spending router."""

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.models import Account, Transaction
from app.schemas import TransactionCreate, TransactionRead, TransactionUpdate
from app.services.receipt_service import reconcile_unlinked_receipts
from app.services.statement_parser import generate_transaction_hash
from app.services.transfer_service import reconcile_transfers

router = APIRouter(prefix="/transactions", tags=["Transactions"])


@router.get("", response_model=list[TransactionRead])
def list_transactions(
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    category: Optional[str] = Query(default=None),
    period: Optional[str] = Query(default=None, description="'week', 'month', 'last_month', or 'all'"),
    account_id: Optional[int] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """List transactions with optional date range, period, account_id, and category filters."""
    if period:
        today = date.today()
        if period == "week":
            start_date = today - timedelta(days=today.weekday())
            end_date = start_date + timedelta(days=6)
        elif period == "month":
            start_date = date(today.year, today.month, 1)
            end_date = today
        elif period == "last_month":
            first_this_month = date(today.year, today.month, 1)
            end_date = first_this_month - timedelta(days=1)
            start_date = date(end_date.year, end_date.month, 1)
        elif period == "all":
            start_date = None
            end_date = None
        else:
            raise HTTPException(status_code=400, detail="Invalid period. Choose 'week', 'month', 'last_month', or 'all'.")

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

    if payload.merchant is not None:
        m = payload.merchant.strip()
        if m:
            tx.merchant = m

    if payload.notes is not None:
        tx.notes = payload.notes

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
