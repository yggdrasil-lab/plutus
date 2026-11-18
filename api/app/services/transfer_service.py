"""Service for reconciling internal transfers, cross-border remittances, foreign exchange, and crypto conversions."""

from datetime import timedelta
import logging
import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Transaction
from app.services.fx_service import FxRateFetcher, get_exchange_rate, has_exact_exchange_rate

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

CRYPTO_CURRENCIES = {"BTC", "ETH", "SOL", "USDC", "USDT"}
HIGH_DENOM_FIAT = {"PHP", "JPY", "IDR", "KRW", "VND"}


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

            fee = float(cand_outflow.fee_amount or cand_inflow.fee_amount or 0.0)
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
        code = (tx.currency or "").strip().upper()
        needs_exact_check = (
            not has_exact_exchange_rate(db, rate_date=tx.transaction_date, from_currency=code, to_currency="NZD")
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
        if resolved_nzd is not None and resolved_nzd[0] > 0:
            target_rate = round(float(resolved_nzd[0]), 8)
            cur_rate = float(tx.exchange_rate or 0.0)
            if cur_rate <= 0 or cur_rate < 1.0 or abs(cur_rate - target_rate) / target_rate > 0.001 or needs_exact_check:
                if abs(cur_rate - target_rate) > 1e-8:
                    tx.exchange_rate = target_rate
                    repaired += 1

        if tx.linked_transaction_id is not None:
            partner = db.get(Transaction, tx.linked_transaction_id)
            if partner is None:
                tx.linked_transaction_id = None
                repaired += 1
                continue

            outflow = partner if partner.amount < 0 else tx
            inflow = tx if tx.amount > 0 else partner
            out_code = (outflow.currency or "").strip().upper()
            in_code = (inflow.currency or "").strip().upper()
            if out_code == in_code:
                continue

            out_amt = abs(float(outflow.amount))
            fee = float(outflow.fee_amount or inflow.fee_amount or 0.0)
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
            unlink = False
            if market is not None and market[0] > 0 and implied_rate > 0:
                if abs(implied_rate - market[0]) / market[0] > 0.12:
                    unlink = True
            elif out_code != "NZD" and in_code != "NZD":
                desc_partner = f"{partner.merchant or ''} {partner.raw_description or ''}"
                if CRYPTO_COUNTERPARTY_KEYWORDS.search(desc_partner) is None:
                    unlink = True

            if unlink:
                tx.linked_transaction_id = None
                partner.linked_transaction_id = None
                if partner.currency.strip().upper() == "NZD":
                    partner.exchange_rate = 1.0
                else:
                    partner_nzd = get_exchange_rate(
                        db,
                        rate_date=partner.transaction_date,
                        from_currency=partner.currency,
                        to_currency="NZD",
                        fetcher=fetcher,
                    )
                    partner.exchange_rate = round(float(partner_nzd[0]), 8) if partner_nzd else None
                repaired += 1
            elif out_code == "NZD" and in_code in CRYPTO_CURRENCIES and net_out > 0 and in_amt > 0:
                expected_nzd_per_crypto = round(net_out / in_amt, 8)
                if abs(float(outflow.exchange_rate or 0.0) - expected_nzd_per_crypto) > 1e-4:
                    outflow.exchange_rate = expected_nzd_per_crypto
                    repaired += 1

    if repaired > 0:
        db.commit()
    return repaired
