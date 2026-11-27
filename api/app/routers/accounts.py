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
    AccountUpdate,
    AssetSnapshotCreate,
    AssetSnapshotRead,
)
from app.services.account_payouts import build_account_reads
from app.services.credit_telemetry import CreditFxResolutionError
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
    accounts = list(db.scalars(stmt.order_by(Account.tier, Account.name)).all())
    try:
        return build_account_reads(db, accounts)
    except CreditFxResolutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.post("", response_model=AccountRead, status_code=status.HTTP_201_CREATED)
def create_account(
    payload: AccountCreate,
    db: Session = Depends(get_db),
):
    """Create a new financial account or asset bucket."""
    account = Account(**payload.model_dump())
    db.add(account)
    db.flush()
    try:
        dto = build_account_reads(db, [account])[0]
    except CreditFxResolutionError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    db.commit()
    db.refresh(account)
    return dto


@router.get("/{account_id}", response_model=AccountRead)
def get_account(account_id: int, db: Session = Depends(get_db)):
    """Retrieve an account by ID."""
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    try:
        return build_account_reads(db, [account])[0]
    except CreditFxResolutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.patch("/{account_id}", response_model=AccountRead)
def update_account(
    account_id: int,
    payload: AccountUpdate,
    db: Session = Depends(get_db),
):
    """Update editable account metadata such as tier, account_type, name, credit_limit, or expected return."""
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    for field in (
        "name",
        "account_type",
        "tier",
        "institution",
        "expected_annual_return",
        "is_active",
    ):
        if field in payload.model_fields_set:
            val = getattr(payload, field)
            if val is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"{field} cannot be null",
                )
            setattr(account, field, val)

    if "credit_limit" in payload.model_fields_set:
        account.credit_limit = payload.credit_limit

    if "account_number_last4" in payload.model_fields_set:
        raw_last4 = payload.account_number_last4
        account.account_number_last4 = raw_last4.strip()[:64] if raw_last4 and raw_last4.strip() else None

    db.flush()
    try:
        dto = build_account_reads(db, [account])[0]
    except CreditFxResolutionError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    db.commit()
    db.refresh(account)
    return dto


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
