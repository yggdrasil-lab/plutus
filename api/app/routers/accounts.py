"""Account and asset snapshot management router."""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Account, AssetSnapshot
from app.schemas import (
    AccountCreate,
    AccountRead,
    AssetSnapshotCreate,
    AssetSnapshotRead,
)

router = APIRouter(prefix="/accounts", tags=["Accounts"])


@router.get("", response_model=list[AccountRead])
def list_accounts(
    is_active: Optional[bool] = Query(default=True),
    db: Session = Depends(get_db),
):
    """List financial accounts, optionally filtered by active status."""
    stmt = select(Account)
    if is_active is not None:
        stmt = stmt.where(Account.is_active == is_active)
    return db.scalars(stmt.order_by(Account.tier, Account.name)).all()


@router.post("", response_model=AccountRead, status_code=status.HTTP_201_CREATED)
def create_account(
    payload: AccountCreate,
    db: Session = Depends(get_db),
):
    """Create a new financial account or asset bucket."""
    account = Account(**payload.model_dump())
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@router.get("/{account_id}", response_model=AccountRead)
def get_account(account_id: int, db: Session = Depends(get_db)):
    """Retrieve an account by ID."""
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


@router.post("/{account_id}/snapshots", response_model=AssetSnapshotRead, status_code=status.HTTP_201_CREATED)
def record_asset_snapshot(
    account_id: int,
    payload: AssetSnapshotCreate,
    db: Session = Depends(get_db),
):
    """Record or update point-in-time valuation for an account balance."""
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    balance_nzd = payload.balance * payload.exchange_rate

    # Check for existing snapshot on the same date for idempotency
    existing = db.scalar(
        select(AssetSnapshot).where(
            AssetSnapshot.account_id == account_id,
            AssetSnapshot.snapshot_date == payload.snapshot_date,
        )
    )

    if existing:
        existing.balance = payload.balance
        existing.currency = payload.currency
        existing.exchange_rate = payload.exchange_rate
        existing.balance_nzd = balance_nzd
        existing.source = payload.source
        existing.notes = payload.notes
        db.commit()
        db.refresh(existing)
        return existing

    snapshot = AssetSnapshot(
        account_id=account_id,
        snapshot_date=payload.snapshot_date,
        balance=payload.balance,
        currency=payload.currency,
        exchange_rate=payload.exchange_rate,
        balance_nzd=balance_nzd,
        source=payload.source,
        notes=payload.notes,
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


@router.get("/{account_id}/snapshots", response_model=list[AssetSnapshotRead])
def list_account_snapshots(
    account_id: int,
    limit: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
):
    """List historical snapshots for a given account."""
    stmt = (
        select(AssetSnapshot)
        .where(AssetSnapshot.account_id == account_id)
        .order_by(desc(AssetSnapshot.snapshot_date))
        .limit(limit)
    )
    return db.scalars(stmt).all()
