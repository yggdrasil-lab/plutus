"""Deterministic transaction deduplication hashing primitives."""

from __future__ import annotations

from datetime import date
import hashlib


def generate_transaction_hash(
    tx_date: date,
    amount: float,
    merchant: str,
    account_id: int,
) -> str:
    """Generate deterministic SHA-256 hash for deduplicating identical statement uploads."""
    payload = f"{tx_date.isoformat()}:{amount:.2f}:{merchant.strip().lower()}:{account_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
