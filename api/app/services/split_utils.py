"""Shared helpers for resolving signed manual transaction split amounts."""

from typing import Sequence

from app.models import TransactionSplit


def resolve_signed_split_amount(
    split: TransactionSplit,
    tx_splits: Sequence[TransactionSplit],
    parent_amount: float,
) -> float:
    """Return a split line's signed amount, normalizing legacy unsigned positive splits on negative parents."""
    raw = float(split.amount)
    if float(parent_amount) < 0 and tx_splits and all(float(s.amount) > 0 for s in tx_splits):
        return -raw
    return raw
