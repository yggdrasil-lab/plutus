"""Machine-to-machine integrations router for Shortcuts, Siri, and document ingestion."""

from datetime import date
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Account, AssetSnapshot, Transaction
from app.schemas import QuickLogRequest, StatementParseResult, TransactionRead
from app.services.statement_parser import (
    generate_transaction_hash,
    parse_csv_statement,
    parse_pdf_statement,
)

router = APIRouter(prefix="/integrations", tags=["Integrations"])


def verify_integration_token(
    authorization: Optional[str] = Header(None),
    secret: Optional[str] = Query(None),
) -> bool:
    """Validate bearer token from Authorization header or secret query parameter."""
    valid_tokens = [t for t in (settings.mcp_token, settings.webhook_secret) if t]
    if not valid_tokens:
        # Development mode without configured tokens
        return True

    candidate = None
    if authorization and authorization.lower().startswith("bearer "):
        candidate = authorization[7:].strip()
    elif secret:
        candidate = secret.strip()

    if candidate and candidate in valid_tokens:
        return True

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing integration token",
    )


@router.post("/quick-log", response_model=TransactionRead, status_code=status.HTTP_201_CREATED)
def quick_log_expense(
    payload: QuickLogRequest,
    db: Session = Depends(get_db),
    _authorized: bool = Depends(verify_integration_token),
):
    """Frictionless quick expense entry from iOS Shortcuts, Siri, or Apple Pay notifications."""
    # Find target account by name, or fallback to first checking account, or create default
    target_account = None
    if payload.account_name:
        target_account = db.scalar(
            select(Account).where(
                Account.name.ilike(f"%{payload.account_name}%"),
                Account.is_active == True,
            )
        )

    if not target_account:
        target_account = db.scalar(
            select(Account).where(
                Account.account_type == "checking",
                Account.is_active == True,
            )
        )

    if not target_account:
        # Create default checking account if none exists
        target_account = Account(
            name="Primary Checking",
            account_type="checking",
            tier="cash",
            institution="Bank",
            currency="NZD",
            expected_annual_return=0.0,
            is_active=True,
        )
        db.add(target_account)
        db.commit()
        db.refresh(target_account)

    tx_date = payload.transaction_date or date.today()
    spend_amount = -abs(payload.amount)  # Stored as negative for spending

    raw_hash = generate_transaction_hash(
        tx_date=tx_date,
        amount=spend_amount,
        merchant=payload.merchant,
        account_id=target_account.id,
    )

    existing = db.scalar(select(Transaction).where(Transaction.raw_hash == raw_hash))
    if existing:
        return existing

    tx = Transaction(
        transaction_date=tx_date,
        account_id=target_account.id,
        amount=spend_amount,
        currency="NZD",
        merchant=payload.merchant,
        category=payload.category,
        raw_description=f"QuickLog: {payload.merchant}",
        raw_hash=raw_hash,
        notes=payload.notes,
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return tx


@router.post("/statement", response_model=StatementParseResult)
async def upload_statement(
    file: UploadFile = File(...),
    institution: str = Query(default="Unknown"),
    commit: bool = Query(default=False, description="Whether to commit extracted transactions directly to database"),
    account_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
    _authorized: bool = Depends(verify_integration_token),
):
    """Upload and parse an official bank statement PDF or CSV file."""
    content = await file.read()
    filename = (file.filename or "").lower()

    if filename.endswith(".pdf"):
        parse_result = parse_pdf_statement(content, institution=institution)
    elif filename.endswith(".csv"):
        text_content = content.decode("utf-8", errors="replace")
        parse_result = parse_csv_statement(text_content, institution=institution)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported file format. Please upload a .pdf or .csv statement.",
        )

    if commit and account_id:
        account = db.get(Account, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Target account for commit not found")

        # 1. Commit transactions with deduplication
        for ptx in parse_result.transactions:
            raw_hash = generate_transaction_hash(
                tx_date=ptx.transaction_date,
                amount=ptx.amount,
                merchant=ptx.merchant,
                account_id=account_id,
            )
            existing = db.scalar(select(Transaction).where(Transaction.raw_hash == raw_hash))
            if not existing:
                tx = Transaction(
                    transaction_date=ptx.transaction_date,
                    account_id=account_id,
                    amount=ptx.amount,
                    currency=parse_result.currency,
                    merchant=ptx.merchant,
                    category=ptx.category,
                    raw_description=ptx.raw_description,
                    raw_hash=raw_hash,
                )
                db.add(tx)

        # 2. If closing balance is detected, commit an asset snapshot anchor
        if parse_result.closing_balance is not None and parse_result.end_date is not None:
            bal_nzd = parse_result.closing_balance
            existing_snap = db.scalar(
                select(AssetSnapshot).where(
                    AssetSnapshot.account_id == account_id,
                    AssetSnapshot.snapshot_date == parse_result.end_date,
                )
            )
            if existing_snap:
                existing_snap.balance = parse_result.closing_balance
                existing_snap.balance_nzd = bal_nzd
                existing_snap.source = "statement_pdf" if filename.endswith(".pdf") else "statement_csv"
            else:
                snap = AssetSnapshot(
                    account_id=account_id,
                    snapshot_date=parse_result.end_date,
                    balance=parse_result.closing_balance,
                    currency=parse_result.currency,
                    exchange_rate=1.0,
                    balance_nzd=bal_nzd,
                    source="statement_pdf" if filename.endswith(".pdf") else "statement_csv",
                    notes=f"Auto-anchored from {filename}",
                )
                db.add(snap)

        db.commit()

    return parse_result
