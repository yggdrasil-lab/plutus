"""Service for reconciling internal transfers, cross-border remittances, foreign exchange, and crypto conversions."""

from datetime import timedelta
import logging
import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Transaction

logger = logging.getLogger(__name__)

TRANSFER_KEYWORDS = re.compile(
    r"\b(?:transfer|tfr|trsf|wire|swift|telegraphic|remit|remittance|internal|"
    r"wise|transferwise|currencyfair|sharesies|dbs|posb|bdo|bpi|unionbank|metrobank|"
    r"paynow|instapay|pesonet|crypto|bitcoin|btc|sats|satoshis|binance|easycrypto|"
    r"coinbase|kraken|fx|conversion|exchange|cross-border)\b",
    re.I,
)


def match_transfer_pair(tx: Transaction, db: Session) -> Optional[Transaction]:
    """Finds matching counter-leg for an account transfer.

    Supports:
    1. Same-currency transfers (e.g. ASB -> Sharesies, checking -> savings).
    2. Multi-currency and cross-border transfers (e.g. ASB NZD -> DBS SGD/USD, DBS -> BPI/BDO PHP, NZD/USD -> Bitcoin).
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

    # Multi-day window for international wire settlement, remittances, or crypto block confirmations
    date_min = tx.transaction_date - timedelta(days=4)
    date_max = tx.transaction_date + timedelta(days=4)

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
                    if 0 < diff <= 50.0:
                        cand_outflow.fee_amount = diff
                break

        # 2. Multi-Currency (FX / Remittance / Crypto) Match
        else:
            desc_text = f"{cand_outflow.merchant} {cand_outflow.raw_description or ''} {cand_inflow.merchant} {cand_inflow.raw_description or ''}".lower()
            has_transfer_signal = (
                cand_outflow.category.lower() in ("transfers", "transfer")
                or cand_inflow.category.lower() in ("transfers", "transfer")
                or TRANSFER_KEYWORDS.search(desc_text) is not None
            )

            rate = float(cand_outflow.exchange_rate or cand_inflow.exchange_rate or 0.0)
            fee = float(cand_outflow.fee_amount or cand_inflow.fee_amount or 0.0)
            net_out = out_amt - fee if out_amt > fee else out_amt

            if rate > 0:
                expected_in = round(net_out * rate, 4)
                if in_amt > 0 and abs(expected_in - in_amt) / in_amt <= 0.08:
                    best_match = cand
                    cand_outflow.exchange_rate = rate
                    cand_inflow.exchange_rate = rate
                    break

            if net_out > 0:
                implied_rate = in_amt / net_out
                # Handle arbitrary currency pairs (fiat ratios and high/low crypto fractional rates)
                if has_transfer_signal and implied_rate > 0:
                    best_match = cand
                    precision = 8 if implied_rate < 0.01 else 6 if implied_rate < 1.0 else 4
                    cand_outflow.exchange_rate = round(implied_rate, precision)
                    cand_inflow.exchange_rate = round(implied_rate, precision)
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
            | (Transaction.merchant.ilike("%transfer%"))
            | (Transaction.raw_description.ilike("%transfer%"))
            | (Transaction.merchant.ilike("%wire%"))
            | (Transaction.merchant.ilike("%remit%"))
            | (Transaction.merchant.ilike("%fx%"))
            | (Transaction.merchant.ilike("%dbs%"))
            | (Transaction.merchant.ilike("%bdo%"))
            | (Transaction.merchant.ilike("%bpi%"))
            | (Transaction.merchant.ilike("%unionbank%"))
            | (Transaction.merchant.ilike("%crypto%"))
            | (Transaction.merchant.ilike("%bitcoin%"))
            | (Transaction.merchant.ilike("%btc%"))
            | (Transaction.merchant.ilike("%wise%"))
            | (Transaction.merchant.ilike("%conversion%"))
            | (Transaction.raw_description.ilike("%wire%"))
            | (Transaction.raw_description.ilike("%remit%"))
            | (Transaction.raw_description.ilike("%crypto%"))
            | (Transaction.raw_description.ilike("%bitcoin%"))
            | (Transaction.raw_description.ilike("%btc%"))
            | (Transaction.raw_description.ilike("%dbs%"))
            | (Transaction.raw_description.ilike("%bdo%"))
            | (Transaction.raw_description.ilike("%bpi%"))
            | (Transaction.raw_description.ilike("%unionbank%"))
            | (Transaction.raw_description.ilike("%wise%"))
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
