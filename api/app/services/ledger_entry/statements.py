"""Bank statement upload, account resolution, transaction commit, and balance anchoring."""

from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account
from app.services.ledger_entry._types import (
    LedgerEntryBadRequestError,
    StatementUploadInput,
    StatementUploadResult,
)
from app.services.statement_parser import (
    StatementParseResult,
    parse_csv_statement,
    parse_pdf_statement,
)

_LOCKED_KEYWORDS: tuple[str, ...] = ("simplicity", "kiwi")
_GROWTH_KEYWORDS: tuple[str, ...] = (
    "sharesies",
    "kernel",
    "milford",
    "investnow",
    "hatch",
    "broker",
)
_TIER_DEFAULTS: dict[str, tuple[str, float]] = {
    "cash": ("checking", 0.0),
    "growth": ("brokerage", 0.07),
    "locked": ("kiwisaver", 0.065),
}


def _resolve_target_institution(institution: str, parsed_institution: str) -> str:
    """Select explicit caller institution when provided, else auto-detected institution or 'Bank'."""
    if institution and institution.lower() not in ("auto", "unknown"):
        return institution
    if parsed_institution and parsed_institution != "Unknown":
        return parsed_institution
    return "Bank"


def _resolve_account_tier(target_institution: str, explicit_tier: Optional[str]) -> str:
    """Resolve explicit tier or infer cash/growth/locked from institution keywords."""
    if explicit_tier:
        return explicit_tier
    inst_lower = target_institution.lower()
    if any(keyword in inst_lower for keyword in _LOCKED_KEYWORDS):
        return "locked"
    if any(keyword in inst_lower for keyword in _GROWTH_KEYWORDS):
        return "growth"
    return "cash"


def _resolve_type_and_return(
    resolved_tier: str,
    explicit_type: Optional[str],
) -> tuple[str, float]:
    """Resolve default account type and expected annual return from tier metadata table."""
    default_type, expected_return = _TIER_DEFAULTS.get(resolved_tier, ("checking", 0.0))
    return (explicit_type or default_type), expected_return


def _create_statement_account(
    db: Session,
    *,
    parse_result: StatementParseResult,
    target_institution: str,
    tier: Optional[str],
    account_type: Optional[str],
    account_name: Optional[str],
) -> Account:
    """Create and persist a new Account derived from statement metadata and overrides."""
    resolved_tier = _resolve_account_tier(target_institution, tier)
    resolved_type, expected_return = _resolve_type_and_return(resolved_tier, account_type)
    resolved_name = account_name or (
        f"{target_institution} {parse_result.account_name}"
        if parse_result.account_name
        else f"{target_institution} {resolved_type.capitalize()}"
    )
    created = Account(
        name=resolved_name,
        account_type=resolved_type,
        tier=resolved_tier,
        institution=target_institution,
        currency=parse_result.currency,
        expected_annual_return=expected_return,
        is_active=True,
    )
    db.add(created)
    db.commit()
    db.refresh(created)
    return created


def _find_or_create_statement_account(
    db: Session,
    *,
    parse_result: StatementParseResult,
    target_institution: str,
    account_id: Optional[int],
    tier: Optional[str],
    account_type: Optional[str],
    account_name: Optional[str],
) -> tuple[Account, bool]:
    """Resolve explicit or matching active account, or auto-create a new account."""
    if account_id:
        existing_by_id = db.get(Account, account_id)
        if existing_by_id is not None:
            return existing_by_id, False

    if target_institution:
        stmt = select(Account).where(
            Account.institution.ilike(f"%{target_institution}%"),
            Account.is_active == True,
        )
        if account_name:
            stmt = stmt.where(Account.name.ilike(f"%{account_name}%"))
        matched = db.scalar(stmt)
        if matched is not None:
            return matched, False

    created = _create_statement_account(
        db,
        parse_result=parse_result,
        target_institution=target_institution,
        tier=tier,
        account_type=account_type,
        account_name=account_name,
    )
    return created, True


def commit_parsed_statement(
    db: Session,
    *,
    parse_result: StatementParseResult,
    filename: str,
    institution: str = "auto",
    account_id: Optional[int] = None,
    tier: Optional[str] = None,
    account_type: Optional[str] = None,
    account_name: Optional[str] = None,
) -> StatementUploadResult:
    """Resolve/create the target account, ingest normalized transactions, and anchor closing snapshot."""
    from app.services.ingestion_service import (
        NormalizedTx,
        ingest_normalized_transactions,
        upsert_account_snapshot,
    )

    target_institution = _resolve_target_institution(institution, parse_result.institution)
    account, account_created = _find_or_create_statement_account(
        db,
        parse_result=parse_result,
        target_institution=target_institution,
        account_id=account_id,
        tier=tier,
        account_type=account_type,
        account_name=account_name,
    )

    normalized_items = [
        NormalizedTx(
            transaction_date=ptx.transaction_date,
            amount=ptx.amount,
            currency=ptx.currency or parse_result.currency,
            merchant=ptx.merchant,
            category=ptx.category,
            raw_description=ptx.raw_description,
            exchange_rate=ptx.exchange_rate,
            fee_amount=ptx.fee_amount,
        )
        for ptx in parse_result.transactions
    ]
    ingest_normalized_transactions(db, account, normalized_items)

    snapshot_upserted = False
    if parse_result.closing_balance is not None and parse_result.end_date is not None:
        normalized_filename = filename.lower()
        snap_source = "statement_pdf" if normalized_filename.endswith(".pdf") else "statement_csv"
        upsert_account_snapshot(
            db,
            account=account,
            snapshot_date=parse_result.end_date,
            balance=parse_result.closing_balance,
            currency=parse_result.currency,
            source=snap_source,
            notes=f"Auto-anchored from {normalized_filename}",
        )
        snapshot_upserted = True

    return StatementUploadResult(
        parse_result=parse_result,
        committed=True,
        account_id=account.id,
        account_created=account_created,
        snapshot_upserted=snapshot_upserted,
    )


def process_statement_upload(
    db: Session,
    payload: StatementUploadInput,
) -> StatementUploadResult:
    """Parse a PDF or CSV statement upload, apply overrides, and optionally commit to the ledger."""
    filename = (payload.filename or "").lower()
    if filename.endswith(".pdf"):
        parse_result = parse_pdf_statement(payload.content, institution=payload.institution)
    elif filename.endswith(".csv"):
        text_content = payload.content.decode("utf-8", errors="replace")
        parse_result = parse_csv_statement(text_content, institution=payload.institution)
    else:
        raise LedgerEntryBadRequestError(
            "Unsupported file format. Please upload a .pdf or .csv statement."
        )

    if payload.closing_balance is not None:
        parse_result.closing_balance = payload.closing_balance
        if parse_result.end_date is None:
            parse_result.end_date = date.today()

    if payload.currency and payload.currency.strip():
        parse_result.currency = payload.currency.strip().upper()

    if not payload.commit:
        return StatementUploadResult(parse_result=parse_result, committed=False)

    return commit_parsed_statement(
        db,
        parse_result=parse_result,
        filename=filename,
        institution=payload.institution,
        account_id=payload.account_id,
        tier=payload.tier,
        account_type=payload.account_type,
        account_name=payload.account_name,
    )
