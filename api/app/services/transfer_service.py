"""Service for reconciling internal and cross-currency (Wise) account transfers."""

from datetime import timedelta
import logging
import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Transaction

logger = logging.getLogger(__name__)

TRANSFER_KEYWORDS = re.compile(
    r"\b(?:transfer|tfr|trsf|wise|transferwise|currencyfair|sharesies|internal)\b",
    re.I,
)


def match_transfer_pair(tx: Transaction, db: Session) -> Optional[Transaction]:
    """Finds matching counter-leg for an account transfer.

    Supports:
    1. Same-currency transfers (e.g. ASB -> Sharesies, checking -> savings).
    2. Multi-currency transfers (e.g. ASB NZD -> Wise USD, Wise EUR -> ASB NZD).
    """
    if tx.linked_transaction_id is not None:
        return None

    is_transfer = (
        tx.category.lower() in ("transfers", "transfer")
        or TRANSFER_KEYWORDS.search(tx.merchant) is not None
        or (tx.raw_description and TRANSFER_KEYWORDS.search(tx.raw_description) is not None)
    )
    if not is_transfer:
        return None

    date_min = tx.transaction_date - timedelta(days=3)
    date_max = tx.transaction_date + timedelta(days=3)

    if tx.amount < 0:
        stmt = (
            select(Transaction)
            .where(Transaction.id != tx.id)
            .where(Transaction.account_id != tx.account_id)
            .where(Transaction.amount > 0)
            .where(Transaction.linked_transaction_id.is_(None))
            .where(Transaction.transaction_date >= date_min)
            .where(Transaction.transaction_date <= date_max)
        )
    else:
        stmt = (
            select(Transaction)
            .where(Transaction.id != tx.id)
            .where(Transaction.account_id != tx.account_id)
            .where(Transaction.amount < 0)
            .where(Transaction.linked_transaction_id.is_(None))
            .where(Transaction.transaction_date >= date_min)
            .where(Transaction.transaction_date <= date_max)
        )

    candidates = db.scalars(stmt).all()
    if not candidates:
        return None

    outflow_tx = tx if tx.amount < 0 else None
    inflow_tx = tx if tx.amount > 0 else None

    best_match: Optional[Transaction] = None

    for cand in candidates:
        cand_outflow = cand if cand.amount < 0 else outflow_tx
        cand_inflow = cand if cand.amount > 0 else inflow_tx

        out_amt = abs(float(cand_outflow.amount))
        in_amt = float(cand_inflow.amount)

        # 1. Same-Currency Match
        if cand_outflow.currency == cand_inflow.currency:
            fee = float(cand_outflow.fee_amount or 0.0)
            net_out = out_amt - fee
            if abs(net_out - in_amt) <= 0.05 or abs(out_amt - in_amt) <= 0.05:
                best_match = cand
                cand_outflow.exchange_rate = 1.0
                cand_inflow.exchange_rate = 1.0
                if abs(out_amt - in_amt) > 0.05 and not cand_outflow.fee_amount:
                    diff = round(out_amt - in_amt, 2)
                    if 0 < diff <= 30.0:
                        cand_outflow.fee_amount = diff
                break

        # 2. Multi-Currency (Wise FX) Match
        else:
            desc_text = f"{cand_outflow.merchant} {cand_outflow.raw_description or ''} {cand_inflow.merchant} {cand_inflow.raw_description or ''}".lower()
            has_fx_signal = any(k in desc_text for k in ["wise", "transferwise", "conversion", "fx", "currency", "exchange"])

            rate = float(cand_outflow.exchange_rate or cand_inflow.exchange_rate or 0.0)
            fee = float(cand_outflow.fee_amount or cand_inflow.fee_amount or 0.0)

            if rate > 0:
                expected_in = round((out_amt - fee) * rate, 2)
                if abs(expected_in - in_amt) <= 1.0:
                    best_match = cand
                    cand_outflow.exchange_rate = rate
                    cand_inflow.exchange_rate = rate
                    break

            implied_rate = in_amt / out_amt
            if has_fx_signal and 0.01 <= implied_rate <= 1000.0:
                best_match = cand
                cand_outflow.exchange_rate = round(implied_rate, 4)
                cand_inflow.exchange_rate = round(implied_rate, 4)
                break

    if best_match:
        tx.linked_transaction_id = best_match.id
        best_match.linked_transaction_id = tx.id
        if tx.category == "General":
            tx.category = "Transfers"
        if best_match.category == "General":
            best_match.category = "Transfers"
        db.commit()
        db.refresh(tx)
        db.refresh(best_match)
        return best_match

    return None


def reconcile_transfers(db: Session) -> int:
    """Evaluates all unlinked transfer candidates and pairs matching legs across accounts."""
    stmt = (
        select(Transaction)
        .where(Transaction.linked_transaction_id.is_(None))
        .where(
            (Transaction.category.ilike("%transfer%"))
            | (Transaction.merchant.ilike("%wise%"))
            | (Transaction.merchant.ilike("%transferwise%"))
            | (Transaction.raw_description.ilike("%wise%"))
            | (Transaction.raw_description.ilike("%transfer%"))
        )
        .order_by(Transaction.transaction_date.asc())
    )
    candidates = db.scalars(stmt).all()
    matched_count = 0

    for tx in candidates:
        if tx.linked_transaction_id is not None:
            continue
        partner = match_transfer_pair(tx, db)
        if partner:
            matched_count += 1

    return matched_count
