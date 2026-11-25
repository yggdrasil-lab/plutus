"""Shared currency, category, and scoring constants for transfer reconciliation."""

CRYPTO_CURRENCIES: frozenset[str] = frozenset({"BTC", "ETH", "SOL", "USDC", "USDT"})
TRANSFER_CATEGORIES: tuple[str, ...] = ("transfers", "transfer")
MATCHABLE_TRANSFER_CATEGORIES: tuple[str, ...] = ("general", "transfers", "transfer")
HIGH_DENOM_FIAT: frozenset[str] = frozenset({"PHP", "JPY", "IDR", "KRW", "VND"})

MIN_IMPLIED_CRYPTO_FEE_FIAT = 0.50
MIN_IMPLIED_CRYPTO_FEE_RATIO = 0.001
MAX_IMPLIED_CRYPTO_FEE_RATIO = 0.12

TRANSFER_MATCH_WINDOW_DAYS = 4
SAME_CURRENCY_TOLERANCE = 0.05
MAX_SAME_CURRENCY_IMPLIED_FEE = 50.0
EXPLICIT_RATE_MAX_REL_DIFF = 0.08
MARKET_RATE_MAX_REL_DIFF = 0.12


def fx_rate_precision(rate: float) -> int:
    """Return canonical decimal rounding precision for FX rates by magnitude."""
    return 8 if rate < 0.01 else 6 if rate < 1.0 else 4
