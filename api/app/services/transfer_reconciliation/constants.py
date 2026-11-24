"""Shared currency and category constants for transfer reconciliation."""

CRYPTO_CURRENCIES: frozenset[str] = frozenset({"BTC", "ETH", "SOL", "USDC", "USDT"})
TRANSFER_CATEGORIES: tuple[str, ...] = ("transfers", "transfer")
