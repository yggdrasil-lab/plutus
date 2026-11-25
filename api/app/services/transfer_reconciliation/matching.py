"""Database orchestration and single-commit reconciliation for transfer legs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import TYPE_CHECKING, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Transaction
from app.services.fx_service import FxRateFetcher, get_exchange_rate, has_exact_exchange_rate
from app.services.transfer_reconciliation.constants import (
    CRYPTO_CURRENCIES,
    MATCHABLE_TRANSFER_CATEGORIES,
    TRANSFER_MATCH_WINDOW_DAYS,
    fx_rate_precision,
)
from app.services.transfer_reconciliation.keywords import (
    TRANSFER_KEYWORD_TOKENS,
    is_transfer_like,
)
from app.services.transfer_reconciliation.primitives import link_transfer_pair
from app.services.transfer_reconciliation.scoring import (
    TransferCandidate,
    TransferMatchDecision,
    compute_implied_crypto_fee,
    score_transfer_match,
)

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement

RateResolver = Callable[..., Optional[tuple[float, str]]]


def build_reconciliation_candidate_filter() -> ColumnElement[bool]:
    """Build SQLAlchemy OR filter matching transfer category or any transfer keyword token."""
    clauses: list[ColumnElement[bool]] = [Transaction.category.ilike("%transfer%")]
    for token in TRANSFER_KEYWORD_TOKENS:
        pattern = f"%{token}%"
        clauses.append(Transaction.merchant.ilike(pattern))
        clauses.append(Transaction.raw_description.ilike(pattern))
    return or_(*clauses)


def has_exact_pair_rate(
    db: Session,
    *,
    rate_date: date,
    from_currency: str,
    to_currency: str,
) -> bool:
    """Return True if an exact-date rate exists directly, inverted, or via NZD."""
    src = (from_currency or "").strip().upper()
    dst = (to_currency or "").strip().upper()
    if src == dst:
        return True
    if has_exact_exchange_rate(db, rate_date=rate_date, from_currency=src, to_currency=dst):
        return True
    if has_exact_exchange_rate(db, rate_date=rate_date, from_currency=dst, to_currency=src):
        return True
    if src != "NZD" and dst != "NZD":
        return (
            has_exact_exchange_rate(db, rate_date=rate_date, from_currency=src, to_currency="NZD")
            and has_exact_exchange_rate(db, rate_date=rate_date, from_currency=dst, to_currency="NZD")
        )
    return False


def extract_fiat_to_crypto_fee(
    db: Session,
    *,
    outflow: Transaction,
    inflow: Transaction,
    fetcher: Optional[FxRateFetcher] = None,
    rate_lookup: Optional[RateResolver] = None,
) -> Optional[float]:
    """Derive implied fiat spread fee when exact-day crypto market rate is available."""
    if outflow.fee_amount is not None:
        return None
    out_code = (outflow.currency or "").strip().upper()
    in_code = (inflow.currency or "").strip().upper()
    if out_code in CRYPTO_CURRENCIES or in_code not in CRYPTO_CURRENCIES:
        return None
    out_amt = abs(float(outflow.amount))
    in_amt = float(inflow.amount)
    if out_amt <= 0 or in_amt <= 0:
        return None
    resolver = rate_lookup or get_exchange_rate
    market = resolver(
        db, rate_date=inflow.transaction_date, from_currency=in_code, to_currency=out_code, fetcher=fetcher
    )
    if market is None or market[0] <= 0:
        return None
    if not has_exact_pair_rate(
        db, rate_date=inflow.transaction_date, from_currency=in_code, to_currency=out_code
    ):
        return None
    return compute_implied_crypto_fee(out_amount=out_amt, in_amount=in_amt, market_rate=float(market[0]))


def match_transfer_pair(
    tx: Transaction,
    db: Session,
    *,
    fetcher: Optional[FxRateFetcher] = None,
    commit: bool = True,
    rate_lookup: Optional[RateResolver] = None,
) -> Optional[Transaction]:
    """Find and link the best matching transfer counter-leg across accounts."""
    if tx.linked_transaction_id is not None or float(tx.amount) == 0.0:
        return None
    if not is_transfer_like(
        category=tx.category,
        merchant=tx.merchant,
        raw_description=tx.raw_description,
    ):
        return None
    candidates = _query_match_candidates(tx, db)
    if not candidates:
        return None
    resolver = rate_lookup or get_exchange_rate
    decision = _score_db_candidates(tx, candidates, db, fetcher=fetcher, rate_lookup=resolver)
    if decision is None:
        return None
    partner = next(cand for cand in candidates if cand.id == decision.partner_id)
    _apply_match_decision(
        db,
        tx=tx,
        partner=partner,
        decision=decision,
        fetcher=fetcher,
        rate_lookup=resolver,
    )
    if commit:
        db.commit()
        db.refresh(tx)
        db.refresh(partner)
    else:
        db.flush()
    return partner


def reconcile_transfers(
    db: Session,
    *,
    fetcher: Optional[FxRateFetcher] = None,
    rate_lookup: Optional[RateResolver] = None,
) -> int:
    """Evaluate all unlinked transfer candidates and persist matches in one commit."""
    stmt = (
        select(Transaction)
        .where(Transaction.linked_transaction_id.is_(None))
        .where(build_reconciliation_candidate_filter())
        .order_by(Transaction.transaction_date.asc())
    )
    candidates = db.scalars(stmt).all()
    matched_count = 0
    for tx in candidates:
        if tx.linked_transaction_id is not None:
            continue
        partner = match_transfer_pair(
            tx,
            db,
            fetcher=fetcher,
            commit=False,
            rate_lookup=rate_lookup,
        )
        if partner is not None:
            matched_count += 1
    if matched_count > 0:
        db.commit()
    return matched_count


def _query_match_candidates(tx: Transaction, db: Session) -> list[Transaction]:
    """Load opposing-sign unlinked transfer-eligible transactions within the settlement window."""
    date_min = tx.transaction_date - timedelta(days=TRANSFER_MATCH_WINDOW_DAYS)
    date_max = tx.transaction_date + timedelta(days=TRANSFER_MATCH_WINDOW_DAYS)
    sign_filter = Transaction.amount > 0 if float(tx.amount) < 0 else Transaction.amount < 0
    stmt = (
        select(Transaction)
        .where(Transaction.id != tx.id)
        .where(sign_filter)
        .where(Transaction.linked_transaction_id.is_(None))
        .where(Transaction.transaction_date >= date_min)
        .where(Transaction.transaction_date <= date_max)
    )
    return [
        cand
        for cand in db.scalars(stmt).all()
        if (cand.category or "").strip().lower() in MATCHABLE_TRANSFER_CATEGORIES
    ]


def _to_transfer_candidate(tx: Transaction) -> TransferCandidate:
    """Convert an ORM Transaction row into an immutable scoring snapshot."""
    return TransferCandidate(
        id=int(tx.id),
        account_id=int(tx.account_id),
        transaction_date=tx.transaction_date,
        amount=float(tx.amount),
        currency=tx.currency or "",
        category=tx.category or "",
        merchant=tx.merchant or "",
        raw_description=tx.raw_description,
        exchange_rate=float(tx.exchange_rate) if tx.exchange_rate is not None else None,
        fee_amount=float(tx.fee_amount) if tx.fee_amount is not None else None,
    )


def _score_db_candidates(
    tx: Transaction,
    candidates: list[Transaction],
    db: Session,
    *,
    fetcher: Optional[FxRateFetcher],
    rate_lookup: RateResolver,
) -> Optional[TransferMatchDecision]:
    """Run pure transfer scoring using a DB-backed FX rate closure."""
    anchor = _to_transfer_candidate(tx)
    snapshots = [_to_transfer_candidate(cand) for cand in candidates]

    def _lookup_market_rate(rate_date: date, from_currency: str, to_currency: str) -> Optional[float]:
        resolved = rate_lookup(
            db,
            rate_date=rate_date,
            from_currency=from_currency,
            to_currency=to_currency,
            fetcher=fetcher,
        )
        return float(resolved[0]) if resolved is not None and resolved[0] > 0 else None

    return score_transfer_match(anchor, snapshots, lookup_market_rate=_lookup_market_rate)


def _apply_match_decision(
    db: Session,
    *,
    tx: Transaction,
    partner: Transaction,
    decision: TransferMatchDecision,
    fetcher: Optional[FxRateFetcher],
    rate_lookup: RateResolver,
) -> None:
    """Apply FX rates, implied fees, and symmetric link mutations from a winning decision."""
    outflow, inflow = (tx, partner) if decision.outflow_id == tx.id else (partner, tx)
    if decision.is_same_currency:
        outflow.exchange_rate = 1.0
        inflow.exchange_rate = 1.0
        if decision.outflow_fee_amount is not None:
            outflow.fee_amount = decision.outflow_fee_amount
    else:
        _apply_cross_currency_updates(
            db,
            outflow=outflow,
            inflow=inflow,
            decision=decision,
            fetcher=fetcher,
            rate_lookup=rate_lookup,
        )
    link_transfer_pair(tx, partner, promote_general_category=True)


def _apply_cross_currency_updates(
    db: Session,
    *,
    outflow: Transaction,
    inflow: Transaction,
    decision: TransferMatchDecision,
    fetcher: Optional[FxRateFetcher],
    rate_lookup: RateResolver,
) -> None:
    """Apply cross-currency crypto spread fee extraction and reporting FX rates."""
    out_code = (outflow.currency or "").strip().upper()
    in_code = (inflow.currency or "").strip().upper()
    outflow_rate = decision.outflow_exchange_rate
    if out_code not in CRYPTO_CURRENCIES and in_code in CRYPTO_CURRENCIES:
        outflow_rate = _apply_fiat_to_crypto_fee(
            db,
            outflow=outflow,
            inflow=inflow,
            out_code=out_code,
            default_rate=outflow_rate,
            fetcher=fetcher,
            rate_lookup=rate_lookup,
        )
    if in_code in CRYPTO_CURRENCIES:
        market_crypto_nzd = rate_lookup(
            db,
            rate_date=inflow.transaction_date,
            from_currency=in_code,
            to_currency="NZD",
            fetcher=fetcher,
        )
        if market_crypto_nzd is not None and market_crypto_nzd[0] > 0:
            inflow.exchange_rate = round(float(market_crypto_nzd[0]), 8)
        elif outflow_rate is not None and out_code == "NZD":
            inflow.exchange_rate = outflow_rate
    if out_code not in CRYPTO_CURRENCIES and outflow_rate is not None:
        outflow.exchange_rate = outflow_rate


def _apply_fiat_to_crypto_fee(
    db: Session,
    *,
    outflow: Transaction,
    inflow: Transaction,
    out_code: str,
    default_rate: Optional[float],
    fetcher: Optional[FxRateFetcher],
    rate_lookup: RateResolver,
) -> Optional[float]:
    """Extract implied fiat->crypto fee if available and recompute net outflow rate."""
    extracted_fee = extract_fiat_to_crypto_fee(
        db,
        outflow=outflow,
        inflow=inflow,
        fetcher=fetcher,
        rate_lookup=rate_lookup,
    )
    if extracted_fee is None:
        return default_rate
    outflow.fee_amount = extracted_fee
    out_amt = abs(float(outflow.amount))
    in_amt = float(inflow.amount)
    net_out = out_amt - extracted_fee if out_amt > extracted_fee else out_amt
    if out_code == "NZD":
        return round(net_out / in_amt, 8)
    implied_after_fee = in_amt / net_out
    return round(implied_after_fee, fx_rate_precision(implied_after_fee))
