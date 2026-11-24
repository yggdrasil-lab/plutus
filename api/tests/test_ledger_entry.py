"""SQLite :memory: interface tests for the deep Ledger Entry package."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path
import sys

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base
from app.models import (
    Account,
    CategorizationRule,
    ExchangeRate,
    Loan,
    Receipt,
    ReceiptItem,
    Transaction,
    TransactionSplit,
)
from app.schemas import TransactionRead
from app.services.ledger_entry import (
    HydratedManualSplit,
    HydratedTransaction,
    LedgerEntryBadRequestError,
    LedgerEntryNotFoundError,
    LedgerEntryValidationError,
    ManualSplitLineInput,
    TransactionPatch,
    collect_existing_tags,
    hydrate_transaction,
    load_hydrated_transaction,
    normalize_tags,
    parse_rule_tags,
    replace_manual_splits,
    strip_redundant_tags,
    transaction_load_options,
    update_transaction,
)


def _new_memory_session_factory() -> tuple[sessionmaker[Session], object]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine), engine


@pytest.fixture()
def db_session() -> Iterator[Session]:
    """Provide an isolated SQLite :memory: session."""
    session_factory, engine = _new_memory_session_factory()
    db = session_factory()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def _create_account(
    db: Session,
    *,
    name: str,
    account_type: str = "checking",
    currency: str = "NZD",
) -> Account:
    account = Account(
        name=name,
        institution="Test Bank",
        account_type=account_type,
        tier="cash",
        currency=currency,
        is_active=True,
    )
    db.add(account)
    db.flush()
    return account


def _create_loan(
    db: Session,
    *,
    name: str = "Alex Tan Personal Loan",
    loan_type: str = "lent",
) -> Loan:
    loan = Loan(
        name=name,
        lender="Alex Tan",
        loan_type=loan_type,
        principal_amount=1000.0,
        annual_interest_rate=0.0,
        term_months=12,
        monthly_payment=100.0,
        start_date=date(2026, 1, 1),
        currency="NZD",
        is_active=True,
    )
    db.add(loan)
    db.flush()
    return loan


def _create_tx(
    db: Session,
    *,
    account_id: int,
    raw_hash: str,
    amount: float = -100.0,
    currency: str = "NZD",
    merchant: str = "Northside Market",
    category: str = "Groceries",
    tags: str = '["weekly-shop"]',
    notes: str | None = "Initial note",
    recurrence: str | None = "weekly",
    counterpart_account_id: int | None = None,
    loan_id: int | None = None,
    loan_payment_amount: float | None = None,
    linked_transaction_id: int | None = None,
    exchange_rate: float | None = None,
    fee_amount: float | None = None,
    tx_date: date = date(2026, 11, 20),
) -> Transaction:
    tx = Transaction(
        transaction_date=tx_date,
        account_id=account_id,
        amount=amount,
        currency=currency,
        merchant=merchant,
        category=category,
        raw_description=merchant,
        raw_hash=raw_hash,
        tags=tags,
        notes=notes,
        recurrence=recurrence,
        counterpart_account_id=counterpart_account_id,
        loan_id=loan_id,
        loan_payment_amount=loan_payment_amount,
        linked_transaction_id=linked_transaction_id,
        exchange_rate=exchange_rate,
        fee_amount=fee_amount,
    )
    db.add(tx)
    db.flush()
    return tx


def test_hydration_returns_detached_frozen_snapshot_without_orm_mutation(
    db_session: Session,
) -> None:
    checking = _create_account(db_session, name="Checking One")
    savings = _create_account(db_session, name="Savings One", account_type="savings")
    loan = _create_loan(db_session)
    partner = _create_tx(
        db_session,
        account_id=savings.id,
        raw_hash="hash-partner",
        amount=100.0,
        merchant="Transfer In",
        category="Transfers",
    )
    tx = _create_tx(
        db_session,
        account_id=checking.id,
        raw_hash="hash-main",
        amount=-100.0,
        merchant="Northside Market",
        category="Groceries",
        tags='["Weekly Shop", "groceries-run"]',
        counterpart_account_id=savings.id,
        loan_id=loan.id,
        loan_payment_amount=60.0,
        linked_transaction_id=partner.id,
    )
    receipt = Receipt(
        transaction_id=tx.id,
        receipt_date=date(2026, 11, 20),
        merchant="Northside Market",
        total_amount=100.0,
        currency="NZD",
        document_type="receipt",
        status="matched",
    )
    db_session.add(receipt)
    db_session.flush()
    db_session.add_all(
        [
            TransactionSplit(
                transaction_id=tx.id,
                amount=-60.0,
                category="Groceries",
                tags='["Food", "Pantry"]',
            ),
            TransactionSplit(
                transaction_id=tx.id,
                amount=-40.0,
                category="Debt & Loans",
                tags='["Repayment"]',
                loan_id=loan.id,
            ),
        ]
    )
    db_session.commit()
    tx_id = tx.id
    receipt_id = receipt.id

    loaded_orm = db_session.scalar(
        select(Transaction)
        .execution_options(populate_existing=True)
        .options(*transaction_load_options())
        .where(Transaction.id == tx_id)
    )
    assert loaded_orm is not None

    direct_snapshot = hydrate_transaction(loaded_orm)
    assert not hasattr(loaded_orm, "account_name")
    assert not hasattr(loaded_orm, "is_split")
    assert not hasattr(loaded_orm.splits[0], "loan_name")
    assert isinstance(loaded_orm.tags, str)

    loaded_snapshot = load_hydrated_transaction(db_session, tx_id, populate_existing=True)
    db_session.close()

    assert direct_snapshot == loaded_snapshot
    assert isinstance(loaded_snapshot, HydratedTransaction)
    assert loaded_snapshot.account_name == "Checking One"
    assert loaded_snapshot.counterpart_account_name == "Savings One"
    assert loaded_snapshot.counterpart_account_type == "savings"
    assert loaded_snapshot.loan_name == "Alex Tan Personal Loan"
    assert loaded_snapshot.loan_type == "lent"
    assert loaded_snapshot.loan_payment_amount == 60.0
    assert loaded_snapshot.linked_account_name == "Savings One"
    assert loaded_snapshot.linked_amount == 100.0
    assert loaded_snapshot.linked_currency == "NZD"
    assert loaded_snapshot.tags == ("weekly-shop", "groceries-run")
    assert loaded_snapshot.has_receipt is True
    assert loaded_snapshot.receipt_id == receipt_id
    assert loaded_snapshot.receipt_document_type == "receipt"
    assert loaded_snapshot.is_split is True
    assert len(loaded_snapshot.splits) == 2
    assert isinstance(loaded_snapshot.splits[1], HydratedManualSplit)
    assert loaded_snapshot.splits[0].tags == ("food", "pantry")
    assert loaded_snapshot.splits[1].loan_name == "Alex Tan Personal Loan"
    assert loaded_snapshot.splits[1].loan_type == "lent"

    schema_read = TransactionRead.model_validate(loaded_snapshot)
    assert schema_read.tags == ["weekly-shop", "groceries-run"]
    assert schema_read.splits[0].tags == ["food", "pantry"]

    with pytest.raises(FrozenInstanceError):
        loaded_snapshot.merchant = "Mutated"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        loaded_snapshot.splits[0].category = "Mutated"  # type: ignore[misc]


def test_load_hydrated_transaction_missing_raises(db_session: Session) -> None:
    with pytest.raises(LedgerEntryNotFoundError, match="Transaction not found"):
        load_hydrated_transaction(db_session, 999999)


def test_tag_utilities_normalize_strip_parse_and_collect(db_session: Session) -> None:
    assert normalize_tags([" #Weekly Shop ", "weekly  shop", "Coffee", ""]) == [
        "weekly-shop",
        "coffee",
    ]
    assert normalize_tags(["a" * 60], max_length=12) == ["a" * 12]
    assert strip_redundant_tags(
        ["general", "Personal Care", "personal-care", "skincare"],
        "Personal Care",
    ) == ["skincare"]
    assert parse_rule_tags('["#Bulk Run", "bulk-run"]') == ["bulk-run"]
    assert parse_rule_tags("coffee, #Morning Tea, coffee") == ["coffee", "morning-tea"]
    assert parse_rule_tags('{"not": "a-list"}') == []
    assert parse_rule_tags(None) == []

    acc = _create_account(db_session, name="Main Checking")
    tx = _create_tx(
        db_session,
        account_id=acc.id,
        raw_hash="hash-tags",
        tags='["groceries", "general", "weekly-shop"]',
    )
    rec = Receipt(
        transaction_id=tx.id,
        receipt_date=date(2026, 11, 20),
        merchant="Market",
        total_amount=50.0,
        currency="NZD",
        status="matched",
    )
    db_session.add(rec)
    db_session.flush()
    db_session.add(
        ReceiptItem(
            receipt_id=rec.id,
            item_name="Oats",
            quantity=1.0,
            unit_price=5.0,
            total_price=5.0,
            category="Groceries",
            tags='["groceries", "pantry"]',
        )
    )
    db_session.add(
        CategorizationRule(
            name="Market Rule",
            description_pattern="market",
            target_category="Groceries",
            target_tags='["groceries", "weekly-shop"]',
            priority=10,
            is_active=True,
        )
    )
    db_session.commit()

    vocab = collect_existing_tags(db_session, limit=2)
    assert vocab == ["groceries", "weekly-shop"]


def test_update_transaction_tristate_and_transfer_unlinking(db_session: Session) -> None:
    nzd_acc = _create_account(db_session, name="NZD Checking", currency="NZD")
    sgd_acc = _create_account(db_session, name="SGD Checking", currency="SGD")
    db_session.add(
        ExchangeRate(
            rate_date=date(2026, 11, 20),
            from_currency="SGD",
            to_currency="NZD",
            rate=1.28,
            source="test",
        )
    )
    out_leg = _create_tx(
        db_session,
        account_id=nzd_acc.id,
        raw_hash="hash-out-leg",
        amount=-128.0,
        currency="NZD",
        merchant="Wise Transfer",
        category="Transfers",
        notes="Keep note",
        recurrence="monthly",
        exchange_rate=1.0,
        fee_amount=2.5,
    )
    in_leg = _create_tx(
        db_session,
        account_id=sgd_acc.id,
        raw_hash="hash-in-leg",
        amount=100.0,
        currency="SGD",
        merchant="Wise Topup",
        category="Transfers",
        linked_transaction_id=out_leg.id,
        exchange_rate=1.25,
        fee_amount=2.5,
    )
    out_leg.linked_transaction_id = in_leg.id
    db_session.commit()

    # Whitespace merchant is a no-op; omitted fields stay unchanged; explicit None clears.
    patched = update_transaction(
        db_session,
        out_leg.id,
        TransactionPatch(
            merchant="   ",
            notes=None,
            tags=("Travel", "fx-fee"),
            category="Travel",
        ),
    )
    assert patched.merchant == "Wise Transfer"
    assert patched.notes is None
    assert patched.recurrence == "monthly"
    assert patched.tags == ("travel", "fx-fee")
    assert patched.category == "Travel"
    assert patched.linked_transaction_id is None
    assert patched.fee_amount is None
    assert patched.exchange_rate is None

    partner_after = load_hydrated_transaction(db_session, in_leg.id)
    assert partner_after.linked_transaction_id is None
    assert partner_after.fee_amount is None
    assert partner_after.exchange_rate == pytest.approx(1.28)


def test_update_transaction_validation_and_loan_state(db_session: Session) -> None:
    acc1 = _create_account(db_session, name="Checking A")
    acc2 = _create_account(db_session, name="Savings B", account_type="savings")
    loan1 = _create_loan(db_session, name="Loan One")
    loan2 = _create_loan(db_session, name="Loan Two")
    tx = _create_tx(db_session, account_id=acc1.id, raw_hash="hash-val", amount=-200.0)
    db_session.commit()

    with pytest.raises(LedgerEntryNotFoundError, match="Transaction not found"):
        update_transaction(db_session, 999999, TransactionPatch(merchant="Updated"))
    with pytest.raises(LedgerEntryBadRequestError, match="Category name cannot be empty"):
        update_transaction(db_session, tx.id, TransactionPatch(category="   "))
    with pytest.raises(LedgerEntryNotFoundError, match="Counterpart account not found"):
        update_transaction(db_session, tx.id, TransactionPatch(counterpart_account_id=999999))
    with pytest.raises(
        LedgerEntryBadRequestError,
        match="Counterpart account cannot match transaction account",
    ):
        update_transaction(db_session, tx.id, TransactionPatch(counterpart_account_id=acc1.id))
    with pytest.raises(LedgerEntryNotFoundError, match="Loan not found"):
        update_transaction(db_session, tx.id, TransactionPatch(loan_id=999999))
    with pytest.raises(
        LedgerEntryValidationError,
        match="loan_payment_amount requires an active loan_id.",
    ):
        update_transaction(db_session, tx.id, TransactionPatch(loan_payment_amount=50.0))
    with pytest.raises(
        LedgerEntryValidationError,
        match="loan_payment_amount must be positive.",
    ):
        update_transaction(
            db_session,
            tx.id,
            TransactionPatch(loan_id=loan1.id, loan_payment_amount=0.0),
        )
    with pytest.raises(
        LedgerEntryValidationError,
        match="loan_payment_amount cannot exceed the transaction amount.",
    ):
        update_transaction(
            db_session,
            tx.id,
            TransactionPatch(loan_id=loan1.id, loan_payment_amount=250.0),
        )

    linked = update_transaction(
        db_session,
        tx.id,
        TransactionPatch(
            counterpart_account_id=acc2.id,
            loan_id=loan1.id,
            loan_payment_amount=150.0,
        ),
    )
    assert linked.counterpart_account_id == acc2.id
    assert linked.loan_id == loan1.id
    assert linked.loan_payment_amount == 150.0

    # Switching loan_id without loan_payment_amount clears loan_payment_amount.
    switched = update_transaction(db_session, tx.id, TransactionPatch(loan_id=loan2.id))
    assert switched.loan_id == loan2.id
    assert switched.loan_payment_amount is None

    # Clearing loan_id and counterpart_account_id via explicit None.
    cleared = update_transaction(
        db_session,
        tx.id,
        TransactionPatch(counterpart_account_id=None, loan_id=None),
    )
    assert cleared.counterpart_account_id is None
    assert cleared.loan_id is None
    assert cleared.loan_payment_amount is None


def test_replace_manual_splits_validation_and_lifecycle(db_session: Session) -> None:
    acc = _create_account(db_session, name="Checking Main")
    loan = _create_loan(db_session)
    partner = _create_tx(
        db_session,
        account_id=acc.id,
        raw_hash="hash-linked-partner",
        amount=50.0,
        category="Transfers",
    )
    linked_tx = _create_tx(
        db_session,
        account_id=acc.id,
        raw_hash="hash-linked",
        amount=-50.0,
        category="Transfers",
        linked_transaction_id=partner.id,
    )
    tx = _create_tx(
        db_session,
        account_id=acc.id,
        raw_hash="hash-split-target",
        amount=-120.0,
        loan_id=loan.id,
        loan_payment_amount=120.0,
    )
    db_session.commit()

    with pytest.raises(LedgerEntryNotFoundError, match="Transaction not found"):
        replace_manual_splits(db_session, 999999, ())
    with pytest.raises(LedgerEntryBadRequestError, match="Linked transfers cannot be split."):
        replace_manual_splits(db_session, linked_tx.id, ())
    with pytest.raises(LedgerEntryValidationError, match="At least 2 split lines"):
        replace_manual_splits(
            db_session,
            tx.id,
            (ManualSplitLineInput(amount=-120.0, category="Groceries"),),
        )
    with pytest.raises(LedgerEntryValidationError, match="Split amount cannot be zero"):
        replace_manual_splits(
            db_session,
            tx.id,
            (
                ManualSplitLineInput(amount=-120.0, category="Groceries"),
                ManualSplitLineInput(amount=0.0, category="Shopping"),
            ),
        )
    with pytest.raises(LedgerEntryValidationError, match="must sum to transaction amount"):
        replace_manual_splits(
            db_session,
            tx.id,
            (
                ManualSplitLineInput(amount=-80.0, category="Groceries"),
                ManualSplitLineInput(amount=-30.0, category="Shopping"),
            ),
        )
    with pytest.raises(LedgerEntryValidationError, match="Split category cannot be empty"):
        replace_manual_splits(
            db_session,
            tx.id,
            (
                ManualSplitLineInput(amount=-80.0, category=" "),
                ManualSplitLineInput(amount=-40.0, category="Shopping"),
            ),
        )
    with pytest.raises(LedgerEntryValidationError, match="cannot exceed 50 characters"):
        replace_manual_splits(
            db_session,
            tx.id,
            (
                ManualSplitLineInput(amount=-80.0, category="x" * 51),
                ManualSplitLineInput(amount=-40.0, category="Shopping"),
            ),
        )
    with pytest.raises(LedgerEntryNotFoundError, match="Loan not found"):
        replace_manual_splits(
            db_session,
            tx.id,
            (
                ManualSplitLineInput(amount=-80.0, category="Debt & Loans", loan_id=999999),
                ManualSplitLineInput(amount=-40.0, category="Shopping"),
            ),
        )
    with pytest.raises(
        LedgerEntryValidationError,
        match="loan_id is only permitted when split category is 'Debt & Loans'",
    ):
        replace_manual_splits(
            db_session,
            tx.id,
            (
                ManualSplitLineInput(amount=-80.0, category="Groceries", loan_id=loan.id),
                ManualSplitLineInput(amount=-40.0, category="Shopping"),
            ),
        )

    # Legacy unsigned positive lines auto-negate on negative parent and clear parent loan.
    split_tx = replace_manual_splits(
        db_session,
        tx.id,
        (
            ManualSplitLineInput(
                amount=80.0,
                category="Groceries",
                tags=("Pantry", "bulk"),
                notes="  Weekly food  ",
            ),
            ManualSplitLineInput(
                amount=40.0,
                category="Debt & Loans",
                loan_id=loan.id,
                notes="   ",
            ),
        ),
    )
    assert split_tx.is_split is True
    assert split_tx.loan_id is None
    assert split_tx.loan_payment_amount is None
    assert [s.amount for s in split_tx.splits] == [-80.0, -40.0]
    assert split_tx.splits[0].tags == ("pantry", "bulk")
    assert split_tx.splits[0].notes == "Weekly food"
    assert split_tx.splits[1].loan_id == loan.id
    assert split_tx.splits[1].notes is None

    # Clearing splits with empty sequence keeps parent loan fields untouched.
    relabeled = update_transaction(
        db_session,
        tx.id,
        TransactionPatch(loan_id=loan.id, loan_payment_amount=50.0),
    )
    assert relabeled.loan_id == loan.id
    cleared = replace_manual_splits(db_session, tx.id, ())
    assert cleared.is_split is False
    assert cleared.splits == ()
    assert cleared.loan_id == loan.id
    assert cleared.loan_payment_amount == 50.0
