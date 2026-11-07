"""Machine-to-machine integrations router for Shortcuts, Siri, and document ingestion."""

from datetime import date, timedelta
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
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Account, AssetSnapshot, Transaction
from app.schemas import QuickLogRequest, StatementParseResult, TransactionRead
from app.services.jev_client import classify_transaction_jev
from app.services.statement_parser import (
    auto_categorize,
    generate_transaction_hash,
    parse_csv_statement,
    parse_pdf_statement,
)

router = APIRouter(prefix="/integrations", tags=["Integrations"])


def verify_integration_token(
    authorization: Optional[str] = Header(None),
    secret: Optional[str] = Query(None),
    x_authentik_username: Optional[str] = Header(None, alias="X-authentik-username"),
    x_authentik_email: Optional[str] = Header(None, alias="X-authentik-email"),
) -> bool:
    """Validate bearer token from Authorization header, secret query parameter, or Authentik SSO forwarded session."""
    if x_authentik_username or x_authentik_email:
        # Authenticated through Traefik Authentik forward-auth proxy
        return True

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

    # Category resolution via Jev System One or rule-based fallback
    category = payload.category
    notes = payload.notes
    if not category or category == "General":
        jev_cat, is_anomaly = classify_transaction_jev(payload.merchant, payload.amount, payload.notes)
        if jev_cat and jev_cat != "General":
            category = jev_cat
        else:
            category = auto_categorize(payload.merchant)
        if is_anomaly:
            anomaly_tag = "[Anomaly Detected]"
            notes = f"{notes} {anomaly_tag}".strip() if notes else anomaly_tag

    tx = Transaction(
        transaction_date=tx_date,
        account_id=target_account.id,
        amount=spend_amount,
        currency="NZD",
        merchant=payload.merchant,
        category=category,
        raw_description=f"QuickLog: {payload.merchant}",
        raw_hash=raw_hash,
        notes=notes,
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return tx


@router.get("/sample-statement", response_class=PlainTextResponse)
def get_sample_statement(
    institution: str = Query(default="ASB"),
    _authorized: bool = Depends(verify_integration_token),
):
    """Generate a realistic dynamic sample CSV statement for demonstration and testing."""
    today = date.today()
    d1 = (today - timedelta(days=1)).strftime("%Y/%m/%d")
    d2 = (today - timedelta(days=2)).strftime("%Y/%m/%d")
    d3 = (today - timedelta(days=3)).strftime("%Y/%m/%d")
    d4 = (today - timedelta(days=4)).strftime("%Y/%m/%d")
    d5 = (today - timedelta(days=5)).strftime("%Y/%m/%d")

    csv_lines = [
        "Date,Unique Id,Tran Type,Cheque Number,Payee,Memo,Amount,Balance",
        f"{d1},1001,DEBIT,,PAK N SAVE ALBANY,Weekly Groceries,-142.50,4250.00",
        f"{d2},1002,DEBIT,,SPOTIFY AUCKLAND,Family Plan,-18.99,4392.50",
        f"{d3},1003,DEBIT,,BP CONNECT TAKAPUNA,Fuel,-85.00,4411.49",
        f"{d4},1004,DEBIT,,CAFE MIMOSA TAKAPUNA,Morning Coffee,-6.50,4496.49",
        f"{d5},1005,CREDIT,,EMPLOYER SALARY,Fortnightly Pay,2850.00,4502.99",
    ]
    return "\n".join(csv_lines)


@router.post("/statement", response_model=StatementParseResult)
async def upload_statement(
    file: UploadFile = File(...),
    institution: str = Query(default="auto", description="Institution name, or 'auto' for automatic content detection"),
    commit: bool = Query(default=False, description="Whether to commit extracted transactions directly to database"),
    account_id: Optional[int] = Query(default=None),
    closing_balance: Optional[float] = Query(default=None, description="Optional manual closing balance override"),
    tier: Optional[str] = Query(default=None, description="Asset tier: cash, growth, or locked"),
    account_type: Optional[str] = Query(default=None, description="Account type: checking, savings, brokerage, kiwisaver"),
    account_name: Optional[str] = Query(default=None, description="Custom account name"),
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

    if closing_balance is not None:
        parse_result.closing_balance = closing_balance
        if parse_result.end_date is None:
            parse_result.end_date = date.today()

    if commit:
        # Effective institution: use caller override if specific, else use auto-detected from statement
        target_institution = (
            institution if institution and institution.lower() not in ("auto", "unknown")
            else parse_result.institution if parse_result.institution != "Unknown"
            else "Bank"
        )

        account = None
        if account_id:
            account = db.get(Account, account_id)
        if not account and target_institution:
            stmt = select(Account).where(
                Account.institution.ilike(f"%{target_institution}%"),
                Account.is_active == True,
            )
            if account_name:
                stmt = stmt.where(Account.name.ilike(f"%{account_name}%"))
            account = db.scalar(stmt)

        if not account:
            inst_lower = target_institution.lower()
            resolved_tier = tier or (
                "locked" if "simplicity" in inst_lower or "kiwi" in inst_lower
                else "growth" if any(k in inst_lower for k in ("sharesies", "kernel", "milford", "investnow", "hatch", "broker"))
                else "cash"
            )
            resolved_type = account_type or (
                "checking" if resolved_tier == "cash"
                else "brokerage" if resolved_tier == "growth"
                else "kiwisaver"
            )
            resolved_name = account_name or (
                f"{target_institution} {parse_result.account_name}" if parse_result.account_name
                else f"{target_institution} {resolved_type.capitalize()}"
            )
            expected_return = (
                0.07 if resolved_tier == "growth"
                else 0.065 if resolved_tier == "locked"
                else 0.0
            )

            account = Account(
                name=resolved_name,
                account_type=resolved_type,
                tier=resolved_tier,
                institution=target_institution,
                currency=parse_result.currency,
                expected_annual_return=expected_return,
                is_active=True,
            )
            db.add(account)
            db.commit()
            db.refresh(account)

        target_account_id = account.id

        # 1. Commit transactions with deduplication
        for ptx in parse_result.transactions:
            raw_hash = generate_transaction_hash(
                tx_date=ptx.transaction_date,
                amount=ptx.amount,
                merchant=ptx.merchant,
                account_id=target_account_id,
            )
            existing = db.scalar(select(Transaction).where(Transaction.raw_hash == raw_hash))
            if not existing:
                tx_cat = ptx.category
                if not tx_cat or tx_cat == "General":
                    jev_cat, _ = classify_transaction_jev(ptx.merchant, ptx.amount)
                    tx_cat = jev_cat if jev_cat and jev_cat != "General" else auto_categorize(ptx.merchant)
                tx = Transaction(
                    transaction_date=ptx.transaction_date,
                    account_id=target_account_id,
                    amount=ptx.amount,
                    currency=parse_result.currency,
                    merchant=ptx.merchant,
                    category=tx_cat,
                    raw_description=ptx.raw_description,
                    raw_hash=raw_hash,
                )
                db.add(tx)

        # 2. If closing balance is detected, commit an asset snapshot anchor
        if parse_result.closing_balance is not None and parse_result.end_date is not None:
            bal_nzd = parse_result.closing_balance
            existing_snap = db.scalar(
                select(AssetSnapshot).where(
                    AssetSnapshot.account_id == target_account_id,
                    AssetSnapshot.snapshot_date == parse_result.end_date,
                )
            )
            if existing_snap:
                existing_snap.balance = parse_result.closing_balance
                existing_snap.balance_nzd = bal_nzd
                existing_snap.source = "statement_pdf" if filename.endswith(".pdf") else "statement_csv"
            else:
                snap = AssetSnapshot(
                    account_id=target_account_id,
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
