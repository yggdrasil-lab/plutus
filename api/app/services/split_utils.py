"""Shared helpers for resolving signed manual transaction split amounts."""

from typing import Sequence

from app.models import TransactionSplit


def resolve_signed_split_amount(
    split: TransactionSplit,
    tx_splits: Sequence[TransactionSplit],
    parent_amount: float,
) -> float:
    """Return a split line's signed amount, normalizing legacy unsigned positive splits on negative parents."""
    return split.signed_amount(parent_amount=parent_amount, siblings=tx_splits)
