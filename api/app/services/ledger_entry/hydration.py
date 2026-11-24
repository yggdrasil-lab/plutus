"""Read-only SQLAlchemy eager-loading and detached snapshot hydration for transactions."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import Receipt, Transaction, TransactionSplit
from app.services.ledger_entry._types import (
    HydratedManualSplit,
    HydratedTransaction,
    LedgerEntryNotFoundError,
)
from app.services.ledger_entry.tags import parse_rule_tags


def transaction_load_options(*, include_receipt_items: bool = False):
    """Return consistent eager-load options for transaction snapshot hydration."""
    receipt_load = (
        joinedload(Transaction.receipt).joinedload(Receipt.items)
        if include_receipt_items
        else joinedload(Transaction.receipt)
    )
    return (
        joinedload(Transaction.account),
        joinedload(Transaction.counterpart_account),
        joinedload(Transaction.loan),
        joinedload(Transaction.linked_transaction).joinedload(Transaction.account),
        joinedload(Transaction.splits).joinedload(TransactionSplit.loan),
        receipt_load,
    )


def _hydrate_split(split: TransactionSplit) -> HydratedManualSplit:
    """Project one ORM TransactionSplit row into an immutable HydratedManualSplit."""
    return HydratedManualSplit(
        id=split.id,
        transaction_id=split.transaction_id,
        amount=float(split.amount),
        category=split.category,
        tags=tuple(parse_rule_tags(split.tags)),
        loan_id=split.loan_id,
        loan_name=split.loan.name if split.loan else None,
        loan_type=split.loan.loan_type if split.loan else None,
        notes=split.notes,
    )


def _extract_linked_fields(
    tx: Transaction,
) -> tuple[Optional[str], Optional[float], Optional[str]]:
    """Extract linked transfer account name, amount, and currency read-only."""
    linked = tx.linked_transaction
    if linked is None:
        return None, None, None
    account_name = linked.account.name if linked.account else None
    return account_name, float(linked.amount), linked.currency


def hydrate_transaction(tx: Transaction) -> HydratedTransaction:
    """Project an ORM Transaction into a detached HydratedTransaction without mutating ORM state."""
    splits_tuple = tuple(_hydrate_split(s) for s in (getattr(tx, "splits", None) or ()))
    linked_account_name, linked_amount, linked_currency = _extract_linked_fields(tx)
    receipt = tx.receipt
    return HydratedTransaction(
        id=tx.id,
        transaction_date=tx.transaction_date,
        account_id=tx.account_id,
        account_name=tx.account.name if tx.account else None,
        amount=float(tx.amount),
        currency=tx.currency,
        merchant=tx.merchant,
        category=tx.category,
        raw_description=tx.raw_description,
        notes=tx.notes,
        linked_transaction_id=tx.linked_transaction_id,
        linked_account_name=linked_account_name,
        linked_amount=linked_amount,
        linked_currency=linked_currency,
        counterpart_account_id=tx.counterpart_account_id,
        counterpart_account_name=(
            tx.counterpart_account.name if tx.counterpart_account else None
        ),
        counterpart_account_type=(
            tx.counterpart_account.account_type if tx.counterpart_account else None
        ),
        loan_id=tx.loan_id,
        loan_name=tx.loan.name if tx.loan else None,
        loan_type=tx.loan.loan_type if tx.loan else None,
        loan_payment_amount=(
            float(tx.loan_payment_amount) if tx.loan_payment_amount is not None else None
        ),
        exchange_rate=float(tx.exchange_rate) if tx.exchange_rate is not None else None,
        fee_amount=float(tx.fee_amount) if tx.fee_amount is not None else None,
        raw_hash=tx.raw_hash,
        tags=tuple(parse_rule_tags(tx.tags)),
        recurrence=tx.recurrence,
        has_receipt=bool(receipt),
        receipt_id=receipt.id if receipt else None,
        receipt_document_type=receipt.document_type if receipt else None,
        splits=splits_tuple,
        is_split=len(splits_tuple) >= 2,
        created_at=tx.created_at,
    )


def load_hydrated_transaction(
    db: Session,
    transaction_id: int,
    *,
    populate_existing: bool = False,
) -> HydratedTransaction:
    """Load an eager-joined transaction by ID and return its detached HydratedTransaction."""
    stmt = (
        select(Transaction)
        .options(*transaction_load_options())
        .where(Transaction.id == transaction_id)
    )
    if populate_existing:
        stmt = stmt.execution_options(populate_existing=True)
    tx = db.scalar(stmt)
    if tx is None:
        raise LedgerEntryNotFoundError("Transaction not found")
    return hydrate_transaction(tx)
