"""DBS Singapore transaction email alert parser for Cloudflare Email Worker."""

from datetime import date, datetime
import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account
from app.schemas import DbsAlertPayload, DbsAlertResult
from app.services.ingestion_service import (
    ExternalAccountSpec,
    NormalizedTx,
    get_or_create_external_account,
    ingest_normalized_transactions,
)

AMOUNT_PATTERN = re.compile(
    r"(?:SGD|S\$|USD|NZD)\s*([0-9,]+\.[0-9]{2})",
    re.IGNORECASE,
)
CURRENCY_PATTERN = re.compile(r"\b(SGD|USD|NZD)\b|S\$", re.IGNORECASE)
MERCHANT_PATTERN = re.compile(
    r"(?:\bat\b|\bto\b|\bfrom\b)\s+([A-Za-z0-9 &.,'/\-]{2,60}?)(?:\s+\bon\b|\s+via\b|\s+ref|\.|,|\n|$)",
    re.IGNORECASE,
)
DATE_PATTERN = re.compile(
    r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(?:\s+\d{4})?)",
    re.IGNORECASE,
)

CREDIT_KEYWORDS = ("received", "credited", "incoming", "refund", "deposit")


def _parse_alert_date(text: str) -> date:
    """Extract transaction date from DBS alert body, defaulting to today."""
    match = DATE_PATTERN.search(text)
    if not match:
        return date.today()
    raw = match.group(1).strip()
    for fmt in ("%d %b %Y", "%d %b"):
        try:
            parsed = datetime.strptime(raw, fmt)
            if fmt == "%d %b":
                return parsed.replace(year=date.today().year).date()
            return parsed.date()
        except ValueError:
            continue
    return date.today()


def parse_dbs_alert_text(subject: str, text: str) -> NormalizedTx:
    """Parse amount, currency, merchant, and date from a DBS alert email."""
    combined = f"{subject}\n{text}"

    amt_match = AMOUNT_PATTERN.search(combined)
    if not amt_match:
        raise ValueError("Could not extract transaction amount from DBS alert email.")

    raw_amount = float(amt_match.group(1).replace(",", ""))
    cur_match = CURRENCY_PATTERN.search(combined)
    if cur_match:
        token = cur_match.group(0).upper()
        currency = "SGD" if token == "S$" else token
    else:
        currency = "SGD"

    lowered = combined.lower()
    is_credit = any(kw in lowered for kw in CREDIT_KEYWORDS)
    signed_amount = abs(raw_amount) if is_credit else -abs(raw_amount)

    merchant_match = MERCHANT_PATTERN.search(text) or MERCHANT_PATTERN.search(subject)
    merchant = merchant_match.group(1).strip() if merchant_match else subject.strip()[:150] or "DBS Transaction"

    return NormalizedTx(
        transaction_date=_parse_alert_date(combined),
        amount=signed_amount,
        currency=currency,
        merchant=merchant,
        category="General",
        raw_description=f"{subject} | {text[:240]}".strip(),
        notes="dbs_email_alert",
    )


def ingest_dbs_alert(db: Session, payload: DbsAlertPayload) -> DbsAlertResult:
    """Parse and commit a forwarded DBS alert email into Plutus."""
    normalized = parse_dbs_alert_text(payload.subject, payload.text)

    account: Optional[Account] = None
    if payload.account_name:
        account = db.scalar(
            select(Account).where(
                Account.name.ilike(f"%{payload.account_name}%"),
                Account.is_active == True,
            )
        )
    if not account:
        account = db.scalar(
            select(Account).where(
                Account.institution.ilike("%DBS%"),
                Account.currency == normalized.currency,
                Account.is_active == True,
            )
        )
    if not account:
        account = get_or_create_external_account(
            db,
            ExternalAccountSpec(
                provider="dbs_email",
                external_id=f"dbs-{normalized.currency.lower()}",
                name=payload.account_name or f"DBS Singapore ({normalized.currency})",
                institution="DBS",
                account_type="checking",
                tier="cash",
                currency=normalized.currency,
            ),
        )

    created_count, skipped_count, created_txs = ingest_normalized_transactions(
        db, account, [normalized]
    )
    if created_count == 0 and skipped_count > 0:
        return DbsAlertResult(status="duplicate", transaction=None, duplicate=True)

    return DbsAlertResult(
        status="created",
        transaction=created_txs[0] if created_txs else None,
        duplicate=False,
    )
