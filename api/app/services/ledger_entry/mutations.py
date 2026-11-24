"""Transaction update and manual split replacement lifecycle mutations."""

from __future__ import annotations

from collections.abc import Sequence
import json
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, Loan, Transaction, TransactionSplit
from app.services.ledger_entry._types import (
    MAX_SPLIT_CATEGORY_LENGTH,
    TRANSFER_CATEGORIES,
    UNSET,
    HydratedTransaction,
    LedgerEntryBadRequestError,
    LedgerEntryNotFoundError,
    LedgerEntryValidationError,
    ManualSplitLineInput,
    TransactionPatch,
    _UnsetType,
)
from app.services.ledger_entry.hydration import (
    load_hydrated_transaction,
    transaction_load_options,
)
from app.services.ledger_entry.tags import normalize_tags
from app.services.transfer_reconciliation import unlink_transfer_pair


def _load_mutable_transaction(db: Session, transaction_id: int) -> Transaction:
    """Load an ORM Transaction with eager relations or raise LedgerEntryNotFoundError."""
    tx = db.scalar(
        select(Transaction)
        .options(*transaction_load_options())
        .where(Transaction.id == transaction_id)
    )
    if tx is None:
        raise LedgerEntryNotFoundError("Transaction not found")
    return tx


def _apply_category_update(db: Session, tx: Transaction, raw_category: str) -> None:
    """Apply category change and unlink paired transfer legs when moving off Transfers."""
    cat = raw_category.strip()
    if not cat:
        raise LedgerEntryBadRequestError("Category name cannot be empty")
    was_transfer_or_linked = (
        (tx.category or "").strip().lower() in TRANSFER_CATEGORIES
        or tx.linked_transaction_id is not None
    )
    tx.category = cat
    if cat.lower() in TRANSFER_CATEGORIES or not was_transfer_or_linked:
        return
    unlink_transfer_pair(db, tx, restore_self_when_unlinked=True)


def _apply_counterpart_account_update(
    db: Session,
    tx: Transaction,
    target_id: int | None | _UnsetType,
) -> None:
    """Validate and apply counterpart_account_id updates without creating mirror rows."""
    if isinstance(target_id, _UnsetType):
        return
    if target_id is None:
        tx.counterpart_account_id = None
        return
    if db.get(Account, target_id) is None:
        raise LedgerEntryNotFoundError("Counterpart account not found")
    if target_id == tx.account_id:
        raise LedgerEntryBadRequestError("Counterpart account cannot match transaction account")
    tx.counterpart_account_id = target_id


def _apply_loan_update(
    db: Session,
    tx: Transaction,
    loan_id: int | None | _UnsetType,
    loan_payment_amount: float | None | _UnsetType,
) -> None:
    """Validate and apply tri-state loan_id and loan_payment_amount updates."""
    old_loan_id = tx.loan_id
    loan_id_set = not isinstance(loan_id, _UnsetType)
    if loan_id_set:
        if loan_id is not None and db.get(Loan, loan_id) is None:
            raise LedgerEntryNotFoundError("Loan not found")
        effective_loan_id = loan_id
    else:
        effective_loan_id = old_loan_id

    if not isinstance(loan_payment_amount, _UnsetType):
        _set_loan_payment_amount(tx, loan_payment_amount, effective_loan_id)
    elif loan_id_set and (loan_id is None or loan_id != old_loan_id):
        tx.loan_payment_amount = None

    if loan_id_set:
        tx.loan_id = loan_id


def _set_loan_payment_amount(
    tx: Transaction,
    loan_payment_amount: Optional[float],
    effective_loan_id: Optional[int],
) -> None:
    """Validate and assign loan_payment_amount against active loan and transaction amount."""
    if loan_payment_amount is None:
        tx.loan_payment_amount = None
        return
    payment_value = float(loan_payment_amount)
    if payment_value <= 0:
        raise LedgerEntryValidationError("loan_payment_amount must be positive.")
    if effective_loan_id is None:
        raise LedgerEntryValidationError("loan_payment_amount requires an active loan_id.")
    if payment_value > abs(float(tx.amount)):
        raise LedgerEntryValidationError("loan_payment_amount cannot exceed the transaction amount.")
    tx.loan_payment_amount = payment_value


def update_transaction(
    db: Session,
    transaction_id: int,
    patch: TransactionPatch,
) -> HydratedTransaction:
    """Apply a tri-state TransactionPatch and return the refreshed HydratedTransaction."""
    tx = _load_mutable_transaction(db, transaction_id)
    if not isinstance(patch.category, _UnsetType) and patch.category is not None:
        _apply_category_update(db, tx, patch.category)
    if not isinstance(patch.merchant, _UnsetType) and patch.merchant is not None and patch.merchant.strip():
        tx.merchant = patch.merchant.strip()
    if not isinstance(patch.notes, _UnsetType):
        tx.notes = patch.notes
    if not isinstance(patch.tags, _UnsetType) and patch.tags is not None:
        tx.tags = json.dumps(normalize_tags(patch.tags))
    if not isinstance(patch.recurrence, _UnsetType):
        tx.recurrence = patch.recurrence

    _apply_counterpart_account_update(db, tx, patch.counterpart_account_id)
    _apply_loan_update(db, tx, patch.loan_id, patch.loan_payment_amount)

    db.commit()
    return load_hydrated_transaction(db, tx.id, populate_existing=True)


def _validate_split_amounts(
    tx: Transaction,
    splits: Sequence[ManualSplitLineInput],
) -> bool:
    """Validate split count, non-zero amounts, and sum parity; return legacy unsigned flag."""
    if len(splits) < 2:
        raise LedgerEntryValidationError(
            "At least 2 split lines are required when splitting a transaction."
        )
    for item in splits:
        if float(item.amount) == 0.0:
            raise LedgerEntryValidationError("Split amount cannot be zero.")

    signed_sum = round(sum(float(item.amount) for item in splits), 2)
    tx_signed = round(float(tx.amount), 2)
    tx_abs = round(abs(float(tx.amount)), 2)
    is_signed_match = abs(signed_sum - tx_signed) <= 0.01
    is_legacy_unsigned = (
        float(tx.amount) < 0
        and all(float(item.amount) > 0 for item in splits)
        and abs(signed_sum - tx_abs) <= 0.01
    )
    if not is_signed_match and not is_legacy_unsigned:
        raise LedgerEntryValidationError(
            f"Signed split amounts ({signed_sum:.2f}) must sum to "
            f"transaction amount ({tx_signed:.2f})."
        )
    return is_legacy_unsigned


def _build_split_row(
    db: Session,
    item: ManualSplitLineInput,
    *,
    negate: bool,
) -> TransactionSplit:
    """Validate one split line input and construct an unattached TransactionSplit ORM row."""
    cat_clean = (item.category or "").strip()
    if not cat_clean:
        raise LedgerEntryValidationError("Split category cannot be empty.")
    if len(cat_clean) > MAX_SPLIT_CATEGORY_LENGTH:
        raise LedgerEntryValidationError("Split category cannot exceed 50 characters.")
    if item.loan_id is not None:
        if db.get(Loan, item.loan_id) is None:
            raise LedgerEntryNotFoundError("Loan not found")
        if cat_clean.lower() != "debt & loans":
            raise LedgerEntryValidationError(
                "loan_id is only permitted when split category is 'Debt & Loans'."
            )
    notes_clean = item.notes.strip() if item.notes is not None else None
    if notes_clean == "":
        notes_clean = None
    effective_amount = round(-float(item.amount) if negate else float(item.amount), 2)
    return TransactionSplit(
        amount=effective_amount,
        category=cat_clean,
        tags=json.dumps(normalize_tags(item.tags)),
        loan_id=item.loan_id,
        notes=notes_clean,
    )


def replace_manual_splits(
    db: Session,
    transaction_id: int,
    splits: Sequence[ManualSplitLineInput],
) -> HydratedTransaction:
    """Replace or clear manual split lines on an unlinked transaction."""
    tx = _load_mutable_transaction(db, transaction_id)
    if tx.linked_transaction_id is not None:
        raise LedgerEntryBadRequestError("Linked transfers cannot be split.")

    if not splits:
        tx.splits.clear()
        db.commit()
        return load_hydrated_transaction(db, tx.id, populate_existing=True)

    is_legacy_unsigned = _validate_split_amounts(tx, splits)
    prepared_splits = [
        _build_split_row(db, item, negate=is_legacy_unsigned) for item in splits
    ]

    tx.splits.clear()
    db.flush()
    tx.splits.extend(prepared_splits)
    tx.loan_id = None
    tx.loan_payment_amount = None
    db.commit()
    return load_hydrated_transaction(db, tx.id, populate_existing=True)
