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
from app.services.ingestion_service import upsert_account_snapshot

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

    target_currency = (
        payload.currency
        if ("currency" in payload.model_fields_set and payload.currency)
        else (account.currency or "NZD")
    )
    return upsert_account_snapshot(
        db,
        account=account,
        snapshot_date=payload.snapshot_date,
        balance=payload.balance,
        currency=target_currency,
        source=payload.source,
        notes=payload.notes,
        exchange_rate=payload.exchange_rate,
        preserve_existing_notes_if_none=False,
    )


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
