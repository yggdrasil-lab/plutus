"""Pure transfer match scoring over detached transaction snapshots."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Optional

from app.services.transfer_reconciliation.constants import (
    CRYPTO_CURRENCIES,
    EXPLICIT_RATE_MAX_REL_DIFF,
    MARKET_RATE_MAX_REL_DIFF,
    MATCHABLE_TRANSFER_CATEGORIES,
    MAX_IMPLIED_CRYPTO_FEE_RATIO,
    MAX_SAME_CURRENCY_IMPLIED_FEE,
    MIN_IMPLIED_CRYPTO_FEE_FIAT,
    MIN_IMPLIED_CRYPTO_FEE_RATIO,
    SAME_CURRENCY_TOLERANCE,
    TRANSFER_CATEGORIES,
    fx_rate_precision,
)
from app.services.transfer_reconciliation.keywords import (
    TRANSFER_KEYWORDS,
    extract_subaccount_tag,
    is_plausible_offline_fx_rate,
    is_transfer_like,
)


@dataclass(frozen=True)
class TransferCandidate:
    """Detached immutable snapshot of a transaction leg for pure transfer scoring."""

    id: int
    account_id: int
    transaction_date: date
    amount: float
    currency: str
    category: str
    merchant: str
    raw_description: Optional[str] = None
    exchange_rate: Optional[float] = None
    fee_amount: Optional[float] = None


@dataclass(frozen=True)
class TransferMatchDecision:
    """Pure scoring result describing the winning transfer partner and FX/fee updates."""

    partner_id: int
    outflow_id: int
    inflow_id: int
    is_same_currency: bool
    score: tuple[int, float]
    outflow_exchange_rate: Optional[float] = None
    inflow_exchange_rate: Optional[float] = None
    outflow_fee_amount: Optional[float] = None


MarketRateLookup = Callable[[date, str, str], Optional[float]]


def orient_transfer_legs(
    anchor: TransferCandidate,
    candidate: TransferCandidate,
) -> Optional[tuple[TransferCandidate, TransferCandidate]]:
    """Return (outflow, inflow) when anchor and candidate have opposing non-zero signs."""
    if anchor.id == candidate.id:
        return None
    if anchor.amount < 0 and candidate.amount > 0:
        return anchor, candidate
    if anchor.amount > 0 and candidate.amount < 0:
        return candidate, anchor
    return None


def compute_implied_crypto_fee(
    *,
    out_amount: float,
    in_amount: float,
    market_rate: float,
) -> Optional[float]:
    """Return implied fiat conversion spread fee when positive and within configured bounds."""
    if out_amount <= 0 or in_amount <= 0 or market_rate <= 0:
        return None
    implied_fee = round(out_amount - (in_amount * float(market_rate)), 2)
    if implied_fee < MIN_IMPLIED_CRYPTO_FEE_FIAT:
        return None
    fee_ratio = implied_fee / out_amount
    if fee_ratio < MIN_IMPLIED_CRYPTO_FEE_RATIO or fee_ratio > MAX_IMPLIED_CRYPTO_FEE_RATIO:
        return None
    return implied_fee


def score_same_currency_match(
    anchor: TransferCandidate,
    candidates: Sequence[TransferCandidate],
) -> Optional[TransferMatchDecision]:
    """Select the closest same-currency transfer leg matching tolerance and account rules."""
    if float(anchor.amount) == 0.0 or not _is_candidate_transfer_like(anchor):
        return None
    best: Optional[TransferMatchDecision] = None
    for candidate in candidates:
        decision = _evaluate_same_currency_candidate(anchor, candidate)
        if decision is None:
            continue
        if best is None or decision.score < best.score:
            best = decision
    return best


def score_cross_currency_match(
    anchor: TransferCandidate,
    candidates: Sequence[TransferCandidate],
    *,
    lookup_market_rate: MarketRateLookup,
) -> Optional[TransferMatchDecision]:
    """Select the lowest-error cross-currency transfer leg against market or offline bounds."""
    if float(anchor.amount) == 0.0 or not _is_candidate_transfer_like(anchor):
        return None
    best: Optional[TransferMatchDecision] = None
    for candidate in candidates:
        decision = _evaluate_cross_currency_candidate(
            anchor,
            candidate,
            lookup_market_rate=lookup_market_rate,
        )
        if decision is None:
            continue
        if best is None or decision.score < best.score:
            best = decision
    return best


def score_transfer_match(
    anchor: TransferCandidate,
    candidates: Sequence[TransferCandidate],
    *,
    lookup_market_rate: MarketRateLookup,
) -> Optional[TransferMatchDecision]:
    """Score candidates preferring same-currency exact legs before cross-currency pairs."""
    if float(anchor.amount) == 0.0 or not _is_candidate_transfer_like(anchor):
        return None
    sorted_candidates = sorted(
        candidates,
        key=lambda cand: abs((cand.transaction_date - anchor.transaction_date).days),
    )
    same_currency = score_same_currency_match(anchor, sorted_candidates)
    if same_currency is not None:
        return same_currency
    return score_cross_currency_match(
        anchor,
        sorted_candidates,
        lookup_market_rate=lookup_market_rate,
    )


def _is_candidate_transfer_like(candidate: TransferCandidate) -> bool:
    """Delegate candidate transfer-likeness check to unified keyword rules."""
    return is_transfer_like(
        category=candidate.category,
        merchant=candidate.merchant,
        raw_description=candidate.raw_description,
    )


def _evaluate_same_currency_candidate(
    anchor: TransferCandidate,
    candidate: TransferCandidate,
) -> Optional[TransferMatchDecision]:
    """Evaluate one same-currency candidate against tolerance and same-account guards."""
    legs = orient_transfer_legs(anchor, candidate)
    if legs is None:
        return None
    outflow, inflow = legs
    if (outflow.currency or "").strip().upper() != (inflow.currency or "").strip().upper():
        return None
    if (candidate.category or "").strip().lower() not in MATCHABLE_TRANSFER_CATEGORIES:
        return None
    out_amt = abs(float(outflow.amount))
    in_amt = float(inflow.amount)
    if not _passes_same_account_guards(outflow, inflow, out_amt, in_amt):
        return None
    fee = float(outflow.fee_amount or 0.0)
    net_diff = abs((out_amt - fee) - in_amt)
    gross_diff = abs(out_amt - in_amt)
    if net_diff > SAME_CURRENCY_TOLERANCE and gross_diff > SAME_CURRENCY_TOLERANCE:
        return None
    implied_fee = _compute_same_currency_implied_fee(outflow, out_amt, in_amt)
    date_diff = abs((inflow.transaction_date - outflow.transaction_date).days)
    return TransferMatchDecision(
        partner_id=candidate.id,
        outflow_id=outflow.id,
        inflow_id=inflow.id,
        is_same_currency=True,
        score=(date_diff, min(net_diff, gross_diff)),
        outflow_exchange_rate=1.0,
        inflow_exchange_rate=1.0,
        outflow_fee_amount=implied_fee,
    )


def _passes_same_account_guards(
    outflow: TransferCandidate,
    inflow: TransferCandidate,
    out_amt: float,
    in_amt: float,
) -> bool:
    """Enforce tight window, keywords, tolerance, and distinct subaccount tags for same-account pairs."""
    if outflow.account_id != inflow.account_id:
        return True
    if abs((outflow.transaction_date - inflow.transaction_date).days) > 1:
        return False
    if not (_is_candidate_transfer_like(outflow) and _is_candidate_transfer_like(inflow)):
        return False
    if abs(out_amt - in_amt) > SAME_CURRENCY_TOLERANCE:
        return False
    sub_out = extract_subaccount_tag(outflow.raw_description)
    sub_in = extract_subaccount_tag(inflow.raw_description)
    return not (sub_out and sub_in and sub_out == sub_in)


def _compute_same_currency_implied_fee(
    outflow: TransferCandidate,
    out_amt: float,
    in_amt: float,
) -> Optional[float]:
    """Return implied same-currency fee when within configured cap and unset on outflow."""
    if abs(out_amt - in_amt) <= SAME_CURRENCY_TOLERANCE or outflow.fee_amount is not None:
        return None
    diff = round(out_amt - in_amt, 2)
    return diff if 0 < diff <= MAX_SAME_CURRENCY_IMPLIED_FEE else None


def _evaluate_cross_currency_candidate(
    anchor: TransferCandidate,
    candidate: TransferCandidate,
    *,
    lookup_market_rate: MarketRateLookup,
) -> Optional[TransferMatchDecision]:
    """Evaluate one cross-currency candidate against transfer signals and FX rates."""
    legs = orient_transfer_legs(anchor, candidate)
    if legs is None:
        return None
    outflow, inflow = legs
    if not _passes_cross_currency_guards(candidate, outflow, inflow):
        return None
    scored = _score_cross_currency_rate(
        anchor,
        outflow,
        inflow,
        lookup_market_rate=lookup_market_rate,
    )
    if scored is None:
        return None
    score, outflow_rate = scored
    return TransferMatchDecision(
        partner_id=candidate.id,
        outflow_id=outflow.id,
        inflow_id=inflow.id,
        is_same_currency=False,
        score=score,
        outflow_exchange_rate=outflow_rate,
    )


def _passes_cross_currency_guards(
    candidate: TransferCandidate,
    outflow: TransferCandidate,
    inflow: TransferCandidate,
) -> bool:
    """Validate distinct accounts/currencies, transfer signals, and positive amounts."""
    if outflow.account_id == inflow.account_id:
        return False
    out_code = (outflow.currency or "").strip().upper()
    in_code = (inflow.currency or "").strip().upper()
    if out_code == in_code or not _is_candidate_transfer_like(candidate):
        return False
    if not _has_cross_currency_transfer_signal(outflow, inflow):
        return False
    out_amt = abs(float(outflow.amount))
    in_amt = float(inflow.amount)
    fee = float(outflow.fee_amount or 0.0)
    net_out = out_amt - fee if out_amt > fee else out_amt
    if net_out <= 0 or in_amt <= 0:
        return False
    is_fiat_pair = out_code not in CRYPTO_CURRENCIES and in_code not in CRYPTO_CURRENCIES
    return not (is_fiat_pair and (out_amt < 1.0 or in_amt < 1.0))


def _has_cross_currency_transfer_signal(
    outflow: TransferCandidate,
    inflow: TransferCandidate,
) -> bool:
    """Return True if either leg is categorized as Transfers or carries transfer keywords."""
    if (outflow.category or "").strip().lower() in TRANSFER_CATEGORIES:
        return True
    if (inflow.category or "").strip().lower() in TRANSFER_CATEGORIES:
        return True
    desc_text = (
        f"{outflow.merchant} {outflow.raw_description or ''} "
        f"{inflow.merchant} {inflow.raw_description or ''}"
    ).lower()
    return TRANSFER_KEYWORDS.search(desc_text) is not None


def _score_cross_currency_rate(
    anchor: TransferCandidate,
    outflow: TransferCandidate,
    inflow: TransferCandidate,
    *,
    lookup_market_rate: MarketRateLookup,
) -> Optional[tuple[tuple[int, float], Optional[float]]]:
    """Compute (score, outflow_exchange_rate) using explicit NZD rate, market rate, or offline bounds."""
    out_code = (outflow.currency or "").strip().upper()
    in_code = (inflow.currency or "").strip().upper()
    out_amt = abs(float(outflow.amount))
    fee = float(outflow.fee_amount or 0.0)
    net_out = out_amt - fee if out_amt > fee else out_amt
    in_amt = float(inflow.amount)
    date_diff = abs((inflow.transaction_date - outflow.transaction_date).days)

    explicit = _score_explicit_nzd_rate(
        outflow, out_code, in_code, net_out, in_amt, date_diff
    )
    if explicit is not None:
        return explicit

    rate_date = _resolve_crypto_rate_date(anchor, outflow, inflow, out_code, in_code)
    market_rate = lookup_market_rate(rate_date, out_code, in_code)
    implied_rate = in_amt / net_out
    rel_diff = _validate_market_or_offline_rate(
        outflow, inflow, out_code, in_code, implied_rate, market_rate
    )
    if rel_diff is None:
        return None
    computed = _compute_cross_currency_outflow_rate(
        out_code, in_code, net_out, in_amt, implied_rate
    )
    return (date_diff, rel_diff), computed


def _score_explicit_nzd_rate(
    outflow: TransferCandidate,
    out_code: str,
    in_code: str,
    net_out: float,
    in_amt: float,
    date_diff: int,
) -> Optional[tuple[tuple[int, float], Optional[float]]]:
    """Short-circuit market lookup when a pre-populated NZD outflow rate matches within tolerance."""
    out_rate = float(outflow.exchange_rate or 0.0)
    if out_rate <= 0 or in_code != "NZD":
        return None
    expected_in = round(net_out * out_rate, 4)
    rel_diff = abs(expected_in - in_amt) / in_amt
    if rel_diff > EXPLICIT_RATE_MAX_REL_DIFF:
        return None
    computed = None if out_code in CRYPTO_CURRENCIES else out_rate
    return (date_diff, rel_diff), computed


def _resolve_crypto_rate_date(
    anchor: TransferCandidate,
    outflow: TransferCandidate,
    inflow: TransferCandidate,
    out_code: str,
    in_code: str,
) -> date:
    """Pick the crypto leg date when crypto is involved, otherwise anchor date."""
    if in_code in CRYPTO_CURRENCIES:
        return inflow.transaction_date
    if out_code in CRYPTO_CURRENCIES:
        return outflow.transaction_date
    return anchor.transaction_date


def _validate_market_or_offline_rate(
    outflow: TransferCandidate,
    inflow: TransferCandidate,
    out_code: str,
    in_code: str,
    implied_rate: float,
    market_rate: Optional[float],
) -> Optional[float]:
    """Return relative spread error against market rate, 0.0 if offline-plausible, or None."""
    if market_rate is not None and market_rate > 0:
        rel_diff = abs(implied_rate - market_rate) / market_rate
        return rel_diff if rel_diff <= MARKET_RATE_MAX_REL_DIFF else None
    desc_out = f"{outflow.merchant} {outflow.raw_description or ''}"
    desc_in = f"{inflow.merchant} {inflow.raw_description or ''}"
    fiat_desc = desc_out if in_code in CRYPTO_CURRENCIES else desc_in
    if not is_plausible_offline_fx_rate(
        out_code,
        in_code,
        implied_rate,
        counterparty_text=fiat_desc,
    ):
        return None
    return 0.0


def _compute_cross_currency_outflow_rate(
    out_code: str,
    in_code: str,
    net_out: float,
    in_amt: float,
    implied_rate: float,
) -> Optional[float]:
    """Quote NZD->crypto outflow in NZD/crypto (8dp), otherwise rounded implied rate."""
    if out_code in CRYPTO_CURRENCIES:
        return None
    if in_code in CRYPTO_CURRENCIES and out_code == "NZD":
        return round(net_out / in_amt, 8)
    return round(implied_rate, fx_rate_precision(implied_rate))
