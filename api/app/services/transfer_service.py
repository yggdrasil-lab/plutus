"""Service for reconciling internal transfers, cross-border remittances, foreign exchange, and crypto conversions."""

from datetime import date, timedelta
import logging
import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Transaction
from app.services.fx_service import FxRateFetcher, get_exchange_rate, has_exact_exchange_rate
from app.services.transfer_reconciliation import CRYPTO_CURRENCIES, unlink_transfer_pair

logger = logging.getLogger(__name__)

TRANSFER_KEYWORDS = re.compile(
    r"\b(?:transfer|tfr|trsf|wire|swift|telegraphic|remit|remittance|internal|"
    r"wise|transferwise|currencyfair|sharesies|dbs|posb|bdo|bpi|unionbank|metrobank|"
    r"paynow|instapay|pesonet|crypto|bitcoin|btc|sats|satoshis|binance|easycrypto|"
    r"lightning|coinbase|kraken|swyftx|fx|conversion|exchange|cross-border)\b",
    re.I,
)
CRYPTO_COUNTERPARTY_KEYWORDS = re.compile(
    r"\b(?:crypto|bitcoin|btc|sats|satoshis|binance|easycrypto|lightning|coinbase|kraken|swyftx)\b",
    re.I,
)

NON_TRANSFER_NOISE = re.compile(
    r"\b(?:service charge|base rewards|cashcare|bank fee|account fee)\b",
    re.I,
)
SUBACCOUNT_TAG_RE = re.compile(r"\[([^\]]+)\]\s*$")


def _extract_subaccount_tag(raw_description: Optional[str]) -> Optional[str]:
    """Extract trailing `[Subaccount]` tag from statement descriptions if present."""
    if not raw_description:
        return None
    match = SUBACCOUNT_TAG_RE.search(raw_description.strip())
    return match.group(1).strip().lower() if match else None

HIGH_DENOM_FIAT = {"PHP", "JPY", "IDR", "KRW", "VND"}
MIN_IMPLIED_CRYPTO_FEE_FIAT = 0.50
MIN_IMPLIED_CRYPTO_FEE_RATIO = 0.001
MAX_IMPLIED_CRYPTO_FEE_RATIO = 0.12


def _has_exact_pair_rate(
    db: Session,
    *,
    rate_date: date,
    from_currency: str,
    to_currency: str,
) -> bool:
    """Return True if an exact-date rate exists for (from_currency, to_currency) directly, inverted, or via NZD."""
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


def _extract_fiat_to_crypto_fee(
    db: Session,
    *,
    outflow: Transaction,
    inflow: Transaction,
    fetcher: Optional[FxRateFetcher] = None,
) -> Optional[float]:
    """Derive implied fiat conversion/spread fee when exact-day crypto market rate is available."""
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

    market = get_exchange_rate(
        db,
        rate_date=inflow.transaction_date,
        from_currency=in_code,
        to_currency=out_code,
        fetcher=fetcher,
    )
    if market is None or market[0] <= 0:
        return None
    if not _has_exact_pair_rate(
        db,
        rate_date=inflow.transaction_date,
        from_currency=in_code,
        to_currency=out_code,
    ):
        return None

    implied_fee = round(out_amt - (in_amt * float(market[0])), 2)
    if implied_fee < MIN_IMPLIED_CRYPTO_FEE_FIAT:
        return None
    fee_ratio = implied_fee / out_amt
    if fee_ratio < MIN_IMPLIED_CRYPTO_FEE_RATIO or fee_ratio > MAX_IMPLIED_CRYPTO_FEE_RATIO:
        return None
    return implied_fee


def _is_plausible_offline_fx_rate(
    from_currency: str,
    to_currency: str,
    implied_rate: float,
    *,
    counterparty_text: str = "",
) -> bool:
    """Return True if an implied FX rate is plausible when no market rate is available."""
    src = (from_currency or "").upper()
    dst = (to_currency or "").upper()
    if src in CRYPTO_CURRENCIES or dst in CRYPTO_CURRENCIES:
        # Never guess fiat<->crypto pairs offline unless the fiat leg explicitly names a crypto platform.
        return CRYPTO_COUNTERPARTY_KEYWORDS.search(counterparty_text or "") is not None
    if src in HIGH_DENOM_FIAT or dst in HIGH_DENOM_FIAT:
        return 0.005 <= implied_rate <= 200.0
    return 0.3 <= implied_rate <= 3.5


def _is_transfer_like(tx: Transaction) -> bool:
    """Return True if a transaction is categorized as a transfer or contains transfer keywords."""
    tx_cat = (tx.category or "").strip().lower()
    if tx_cat not in ("general", "transfers", "transfer"):
        return False
    merchant_text = f"{tx.merchant or ''} {tx.raw_description or ''}"
    if NON_TRANSFER_NOISE.search(merchant_text):
        return False
    return bool(
        tx_cat in ("transfers", "transfer")
        or TRANSFER_KEYWORDS.search(tx.merchant or "") is not None
        or (tx.raw_description and TRANSFER_KEYWORDS.search(tx.raw_description) is not None)
    )


def match_transfer_pair(tx: Transaction, db: Session) -> Optional[Transaction]:
    """Finds matching counter-leg for an account transfer.

    Supports:
    1. Same-currency transfers (e.g. ASB -> Sharesies, checking -> savings).
    2. Multi-currency and cross-border transfers (e.g. ASB NZD -> DBS SGD/USD, DBS -> BPI/BDO PHP, NZD/USD -> Bitcoin).
    """
    if tx.linked_transaction_id is not None:
        return None

    if not _is_transfer_like(tx):
        return None

    # Multi-day window for international wire settlement, remittances, or crypto block confirmations
    date_min = tx.transaction_date - timedelta(days=4)
    date_max = tx.transaction_date + timedelta(days=4)

    if tx.amount < 0:
        stmt = (
            select(Transaction)
            .where(Transaction.id != tx.id)
            .where(Transaction.amount > 0)
            .where(Transaction.linked_transaction_id.is_(None))
            .where(Transaction.transaction_date >= date_min)
            .where(Transaction.transaction_date <= date_max)
        )
    else:
        stmt = (
            select(Transaction)
            .where(Transaction.id != tx.id)
            .where(Transaction.amount < 0)
            .where(Transaction.linked_transaction_id.is_(None))
            .where(Transaction.transaction_date >= date_min)
            .where(Transaction.transaction_date <= date_max)
        )

    candidates = [
        cand
        for cand in db.scalars(stmt).all()
        if (cand.category or "").strip().lower() in ("general", "transfers", "transfer")
    ]
    if not candidates or float(tx.amount) == 0.0:
        return None

    candidates.sort(key=lambda c: abs((c.transaction_date - tx.transaction_date).days))

    outflow_tx = tx if tx.amount < 0 else None
    inflow_tx = tx if tx.amount > 0 else None

    best_match: Optional[Transaction] = None

    # Pass 1: Same-Currency Exact Match (always preferred over cross-currency)
    for cand in candidates:
        cand_outflow = cand if cand.amount < 0 else outflow_tx
        cand_inflow = cand if cand.amount > 0 else inflow_tx
        if not cand_outflow or not cand_inflow:
            continue

        if cand_outflow.currency != cand_inflow.currency:
            continue

        out_amt = abs(float(cand_outflow.amount))
        in_amt = float(cand_inflow.amount)

        if cand_outflow.account_id == cand_inflow.account_id:
            if abs((cand_outflow.transaction_date - cand_inflow.transaction_date).days) > 1:
                continue
            if not (_is_transfer_like(cand_outflow) and _is_transfer_like(cand_inflow)):
                continue
            if abs(out_amt - in_amt) > 0.05:
                continue
            sub_out = _extract_subaccount_tag(cand_outflow.raw_description)
            sub_in = _extract_subaccount_tag(cand_inflow.raw_description)
            if sub_out and sub_in and sub_out == sub_in:
                continue

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

    # Pass 2: Multi-Currency (FX / Remittance / Crypto) Best-Scored Match
    if not best_match:
        best_score: Optional[tuple[int, float]] = None
        best_candidate_outflow_rate: Optional[float] = None
        best_candidate_out: Optional[Transaction] = None
        best_candidate_in: Optional[Transaction] = None

        for cand in candidates:
            cand_outflow = cand if cand.amount < 0 else outflow_tx
            cand_inflow = cand if cand.amount > 0 else inflow_tx
            if not cand_outflow or not cand_inflow:
                continue

            if cand_outflow.account_id == cand_inflow.account_id:
                continue

            if cand_outflow.currency == cand_inflow.currency:
                continue
            if not _is_transfer_like(cand):
                continue

            out_amt = abs(float(cand_outflow.amount))
            in_amt = float(cand_inflow.amount)
            out_code = cand_outflow.currency.upper()
            in_code = cand_inflow.currency.upper()
            desc_out = f"{cand_outflow.merchant} {cand_outflow.raw_description or ''}"
            desc_in = f"{cand_inflow.merchant} {cand_inflow.raw_description or ''}"
            desc_text = f"{desc_out} {desc_in}".lower()
            has_transfer_signal = (
                cand_outflow.category.lower() in ("transfers", "transfer")
                or cand_inflow.category.lower() in ("transfers", "transfer")
                or TRANSFER_KEYWORDS.search(desc_text) is not None
            )
            if not has_transfer_signal:
                continue

            fee = float(cand_outflow.fee_amount or 0.0)
            net_out = out_amt - fee if out_amt > fee else out_amt
            if net_out <= 0 or in_amt <= 0:
                continue

            is_fiat_pair = out_code not in CRYPTO_CURRENCIES and in_code not in CRYPTO_CURRENCIES
            if is_fiat_pair and (out_amt < 1.0 or in_amt < 1.0):
                continue

            implied_rate = in_amt / net_out
            date_diff = abs((cand_inflow.transaction_date - cand_outflow.transaction_date).days)
            crypto_leg_date = (
                cand_inflow.transaction_date
                if in_code in CRYPTO_CURRENCIES
                else cand_outflow.transaction_date
                if out_code in CRYPTO_CURRENCIES
                else tx.transaction_date
            )

            # Explicit pre-populated rate on cand_outflow only matches direct in/out semantics
            # when converting into NZD (since single-tx exchange_rate represents currency -> NZD).
            out_rate = float(cand_outflow.exchange_rate or 0.0)
            if out_rate > 0 and in_code == "NZD":
                expected_in = round(net_out * out_rate, 4)
                rel_diff = abs(expected_in - in_amt) / in_amt
                if rel_diff <= 0.08:
                    score = (date_diff, rel_diff)
                    if best_score is None or score < best_score:
                        best_score = score
                        best_match = cand
                        best_candidate_outflow_rate = out_rate
                        best_candidate_out = cand_outflow
                        best_candidate_in = cand_inflow
                    continue

            market = get_exchange_rate(
                db,
                rate_date=crypto_leg_date,
                from_currency=out_code,
                to_currency=in_code,
            )
            if market is not None and market[0] > 0:
                rel_diff = abs(implied_rate - market[0]) / market[0]
                if rel_diff > 0.12:
                    continue
            else:
                fiat_desc = desc_out if in_code in CRYPTO_CURRENCIES else desc_in
                if not _is_plausible_offline_fx_rate(
                    out_code,
                    in_code,
                    implied_rate,
                    counterparty_text=fiat_desc,
                ):
                    continue
                rel_diff = 0.0

            score = (date_diff, rel_diff)
            if best_score is None or score < best_score:
                best_score = score
                best_match = cand
                best_candidate_out = cand_outflow
                best_candidate_in = cand_inflow
                if in_code in CRYPTO_CURRENCIES and out_code == "NZD":
                    # Quote fiat->crypto NZD leg in NZD per 1 unit crypto (e.g. 122849.82 NZD/BTC)
                    best_candidate_outflow_rate = round(net_out / in_amt, 8)
                else:
                    precision = 8 if implied_rate < 0.01 else 6 if implied_rate < 1.0 else 4
                    best_candidate_outflow_rate = round(implied_rate, precision)

        if best_match and best_candidate_out and best_candidate_in:
            in_code = best_candidate_in.currency.upper()
            out_code = best_candidate_out.currency.upper()
            if out_code not in CRYPTO_CURRENCIES and in_code in CRYPTO_CURRENCIES:
                extracted_fee = _extract_fiat_to_crypto_fee(
                    db,
                    outflow=best_candidate_out,
                    inflow=best_candidate_in,
                )
                if extracted_fee is not None:
                    best_candidate_out.fee_amount = extracted_fee
                    out_amt = abs(float(best_candidate_out.amount))
                    in_amt = float(best_candidate_in.amount)
                    net_out = out_amt - extracted_fee if out_amt > extracted_fee else out_amt
                    if out_code == "NZD":
                        best_candidate_outflow_rate = round(net_out / in_amt, 8)
                    else:
                        implied_after_fee = in_amt / net_out
                        precision = 8 if implied_after_fee < 0.01 else 6 if implied_after_fee < 1.0 else 4
                        best_candidate_outflow_rate = round(implied_after_fee, precision)
            if in_code in CRYPTO_CURRENCIES:
                market_crypto_nzd = get_exchange_rate(
                    db,
                    rate_date=best_candidate_in.transaction_date,
                    from_currency=in_code,
                    to_currency="NZD",
                )
                if market_crypto_nzd is not None and market_crypto_nzd[0] > 0:
                    best_candidate_in.exchange_rate = round(float(market_crypto_nzd[0]), 8)
                elif best_candidate_outflow_rate is not None and out_code == "NZD":
                    best_candidate_in.exchange_rate = best_candidate_outflow_rate
            if out_code not in CRYPTO_CURRENCIES and best_candidate_outflow_rate is not None:
                best_candidate_out.exchange_rate = best_candidate_outflow_rate

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
            | (Transaction.merchant.ilike("%lightning%"))
            | (Transaction.merchant.ilike("%wise%"))
            | (Transaction.merchant.ilike("%conversion%"))
            | (Transaction.raw_description.ilike("%lightning%"))
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


def _sync_crypto_reporting_rate(
    db: Session,
    tx: Transaction,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> int:
    """Refresh missing, inverted, or stale crypto-to-NZD reporting rate on a single leg."""
    code = (tx.currency or "").strip().upper()
    needs_exact_check = (
        not has_exact_exchange_rate(
            db, rate_date=tx.transaction_date, from_currency=code, to_currency="NZD"
        )
        or tx.exchange_rate is None
        or float(tx.exchange_rate) < 1.0
    )
    resolved_nzd = get_exchange_rate(
        db,
        rate_date=tx.transaction_date,
        from_currency=code,
        to_currency="NZD",
        fetcher=fetcher,
    )
    if resolved_nzd is None or resolved_nzd[0] <= 0:
        return 0
    target_rate = round(float(resolved_nzd[0]), 8)
    cur_rate = float(tx.exchange_rate or 0.0)
    needs_update = (
        cur_rate <= 0
        or cur_rate < 1.0
        or abs(cur_rate - target_rate) / target_rate > 0.001
        or needs_exact_check
    )
    if needs_update and abs(cur_rate - target_rate) > 1e-8:
        tx.exchange_rate = target_rate
        return 1
    return 0


def _should_unlink_crypto_leg(
    db: Session,
    tx: Transaction,
    partner: Transaction,
    outflow: Transaction,
    inflow: Transaction,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> bool:
    """Return True if a linked crypto transfer violates market spread or counterparty guards."""
    out_code = (outflow.currency or "").strip().upper()
    in_code = (inflow.currency or "").strip().upper()
    out_amt = abs(float(outflow.amount))
    fee = float(outflow.fee_amount or 0.0)
    net_out = out_amt - fee if out_amt > fee else out_amt
    in_amt = float(inflow.amount)
    implied_rate = (in_amt / net_out) if (net_out > 0 and in_amt > 0) else 0.0
    market = get_exchange_rate(
        db,
        rate_date=tx.transaction_date,
        from_currency=out_code,
        to_currency=in_code,
        fetcher=fetcher,
    )
    if market is not None and market[0] > 0 and implied_rate > 0:
        return abs(implied_rate - market[0]) / market[0] > 0.12
    if out_code != "NZD" and in_code != "NZD":
        desc_partner = f"{partner.merchant or ''} {partner.raw_description or ''}"
        return CRYPTO_COUNTERPARTY_KEYWORDS.search(desc_partner) is None
    return False


def _backfill_nzd_crypto_fee(
    db: Session,
    outflow: Transaction,
    inflow: Transaction,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> int:
    """Backfill implied conversion spread fee and effective NZD/crypto rate on paired NZD leg."""
    out_amt = abs(float(outflow.amount))
    fee = float(outflow.fee_amount or 0.0)
    net_out = out_amt - fee if out_amt > fee else out_amt
    in_amt = float(inflow.amount)
    if net_out <= 0 or in_amt <= 0:
        return 0
    updates = 0
    extracted_fee = _extract_fiat_to_crypto_fee(
        db,
        outflow=outflow,
        inflow=inflow,
        fetcher=fetcher,
    )
    if extracted_fee is not None:
        outflow.fee_amount = extracted_fee
        net_out = out_amt - extracted_fee if out_amt > extracted_fee else out_amt
        updates += 1
    expected_nzd_per_crypto = round(net_out / in_amt, 8)
    if abs(float(outflow.exchange_rate or 0.0) - expected_nzd_per_crypto) > 1e-4:
        outflow.exchange_rate = expected_nzd_per_crypto
        updates += 1
    return updates


def _repair_linked_crypto_leg(
    db: Session,
    tx: Transaction,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> int:
    """Repair or unlink one linked crypto transfer leg using canonical unlink primitives."""
    if tx.linked_transaction_id is None:
        return 0
    partner = db.get(Transaction, tx.linked_transaction_id)
    if partner is None:
        unlink_transfer_pair(db, tx, fetcher=fetcher)
        return 1
    outflow = partner if partner.amount < 0 else tx
    inflow = tx if tx.amount > 0 else partner
    out_code = (outflow.currency or "").strip().upper()
    in_code = (inflow.currency or "").strip().upper()
    if out_code == in_code:
        return 0
    if _should_unlink_crypto_leg(db, tx, partner, outflow, inflow, fetcher=fetcher):
        unlink_transfer_pair(db, tx, fetcher=fetcher)
        return 1
    if out_code == "NZD" and in_code in CRYPTO_CURRENCIES:
        return _backfill_nzd_crypto_fee(db, outflow, inflow, fetcher=fetcher)
    return 0


def repair_crypto_transfers_and_rates(
    db: Session,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> int:
    """Repair inverted/fallback crypto FX rates and unlink mismatched crypto transfer legs."""
    crypto_rows = [
        tx
        for tx in db.scalars(select(Transaction).order_by(Transaction.transaction_date.asc())).all()
        if (tx.currency or "").strip().upper() in CRYPTO_CURRENCIES
    ]
    if not crypto_rows:
        return 0

    repaired = 0
    for tx in crypto_rows:
        repaired += _sync_crypto_reporting_rate(db, tx, fetcher=fetcher)
        repaired += _repair_linked_crypto_leg(db, tx, fetcher=fetcher)

    if repaired > 0:
        db.commit()
    return repaired
