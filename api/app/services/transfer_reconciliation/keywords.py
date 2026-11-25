"""Unified transfer keyword patterns, noise guards, and SQLAlchemy candidate filters."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

from app.services.transfer_reconciliation.constants import (
    CRYPTO_CURRENCIES,
    HIGH_DENOM_FIAT,
    MATCHABLE_TRANSFER_CATEGORIES,
    TRANSFER_CATEGORIES,
)

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement

TRANSFER_KEYWORD_TOKENS: tuple[str, ...] = (
    "transfer",
    "tfr",
    "trsf",
    "wire",
    "swift",
    "telegraphic",
    "remit",
    "remittance",
    "internal",
    "wise",
    "transferwise",
    "currencyfair",
    "sharesies",
    "dbs",
    "posb",
    "bdo",
    "bpi",
    "unionbank",
    "metrobank",
    "paynow",
    "instapay",
    "pesonet",
    "crypto",
    "bitcoin",
    "btc",
    "sats",
    "satoshis",
    "binance",
    "easycrypto",
    "lightning",
    "coinbase",
    "kraken",
    "swyftx",
    "fx",
    "conversion",
    "exchange",
    "cross-border",
)

CRYPTO_COUNTERPARTY_KEYWORD_TOKENS: tuple[str, ...] = (
    "crypto",
    "bitcoin",
    "btc",
    "sats",
    "satoshis",
    "binance",
    "easycrypto",
    "lightning",
    "coinbase",
    "kraken",
    "swyftx",
)

NON_TRANSFER_NOISE_TOKENS: tuple[str, ...] = (
    "service charge",
    "base rewards",
    "cashcare",
    "bank fee",
    "account fee",
)


def _compile_word_pattern(tokens: tuple[str, ...]) -> re.Pattern[str]:
    """Compile case-insensitive whole-word alternation regex from token tuple."""
    alternation = "|".join(re.escape(token) for token in tokens)
    return re.compile(rf"\b(?:{alternation})\b", re.I)


TRANSFER_KEYWORDS = _compile_word_pattern(TRANSFER_KEYWORD_TOKENS)
CRYPTO_COUNTERPARTY_KEYWORDS = _compile_word_pattern(CRYPTO_COUNTERPARTY_KEYWORD_TOKENS)
NON_TRANSFER_NOISE = _compile_word_pattern(NON_TRANSFER_NOISE_TOKENS)
SUBACCOUNT_TAG_RE = re.compile(r"\[([^\]]+)\]\s*$")


def extract_subaccount_tag(raw_description: Optional[str]) -> Optional[str]:
    """Extract trailing `[Subaccount]` tag from statement descriptions if present."""
    if not raw_description:
        return None
    match = SUBACCOUNT_TAG_RE.search(raw_description.strip())
    return match.group(1).strip().lower() if match else None


def is_transfer_like(
    *,
    category: Optional[str],
    merchant: Optional[str],
    raw_description: Optional[str],
) -> bool:
    """Return True when category and text signal a transfer without matching bank noise."""
    tx_cat = (category or "").strip().lower()
    if tx_cat not in MATCHABLE_TRANSFER_CATEGORIES:
        return False
    merchant_text = f"{merchant or ''} {raw_description or ''}"
    if NON_TRANSFER_NOISE.search(merchant_text):
        return False
    return bool(
        tx_cat in TRANSFER_CATEGORIES
        or TRANSFER_KEYWORDS.search(merchant or "") is not None
        or (raw_description and TRANSFER_KEYWORDS.search(raw_description) is not None)
    )


def is_plausible_offline_fx_rate(
    from_currency: str,
    to_currency: str,
    implied_rate: float,
    *,
    counterparty_text: str = "",
) -> bool:
    """Return True if an implied FX rate is plausible when no market rate is available."""
    src = (from_currency or "").strip().upper()
    dst = (to_currency or "").strip().upper()
    if src in CRYPTO_CURRENCIES or dst in CRYPTO_CURRENCIES:
        # Never guess fiat<->crypto pairs offline unless the fiat leg names a crypto platform.
        return CRYPTO_COUNTERPARTY_KEYWORDS.search(counterparty_text or "") is not None
    if src in HIGH_DENOM_FIAT or dst in HIGH_DENOM_FIAT:
        return 0.005 <= implied_rate <= 200.0
    return 0.3 <= implied_rate <= 3.5


def build_reconciliation_candidate_filter() -> ColumnElement[bool]:
    """Build SQLAlchemy OR filter matching transfer category or any transfer keyword token."""
    from sqlalchemy import or_

    from app.models import Transaction

    clauses: list[ColumnElement[bool]] = [Transaction.category.ilike("%transfer%")]
    for token in TRANSFER_KEYWORD_TOKENS:
        pattern = f"%{token}%"
        clauses.append(Transaction.merchant.ilike(pattern))
        clauses.append(Transaction.raw_description.ilike(pattern))
    return or_(*clauses)
