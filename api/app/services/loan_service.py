"""Amortization schedule, countdown, and net worth calculations for loans."""

import calendar
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.config import settings
from app.models import Loan, Transaction, TransactionSplit
from app.services.fx_service import (
    FxRateFetcher,
    HttpFxRateFetcher,
    get_exchange_rate,
    to_nzd_amount,
)


class LoanFxConversionError(ValueError):
    """Raised when a cross-currency loan payment or disbursement cannot be converted."""


class LoanNotFoundError(LookupError):
    """Raised when a requested loan does not exist."""


class LoanValidationError(ValueError):
    """Raised when loan attributes violate domain invariants."""


_VALID_LOAN_TYPES = frozenset({"borrowed", "lent"})
_NON_NULLABLE_UPDATE_FIELDS = frozenset(
    {
        "name",
        "lender",
        "loan_type",
        "principal_amount",
        "annual_interest_rate",
        "term_months",
        "monthly_payment",
        "start_date",
        "is_active",
    }
)
_NULLABLE_UPDATE_FIELDS = frozenset({"asset_value", "notes"})
_ALLOWED_UPDATE_FIELDS = _NON_NULLABLE_UPDATE_FIELDS | _NULLABLE_UPDATE_FIELDS


@dataclass(frozen=True)
class LoanCreateInput:
    """Validated domain payload for creating a new loan."""

    name: str
    lender: str
    principal_amount: float
    start_date: date
    loan_type: str = "borrowed"
    annual_interest_rate: float = 0.0
    term_months: int = 0
    monthly_payment: float = 0.0
    asset_value: Optional[float] = None
    currency: str = "NZD"
    notes: Optional[str] = None
    is_active: bool = True


@dataclass(frozen=True)
class LoanPaymentSnapshot:
    """Detached, immutable snapshot of one linked loan repayment or disbursement."""

    id: int
    split_id: Optional[int]
    transaction_date: date
    amount: float
    currency: str
    amount_nzd: float
    applied_amount: float
    loan_payment_amount: Optional[float]
    entry_type: str
    merchant: str
    account_id: int
    account_name: Optional[str]
    category: str
    notes: Optional[str]


@dataclass(frozen=True)
class LoanStatusSnapshot:
    """Detached, immutable snapshot of a loan's computed amortization and schedule."""

    id: int
    name: str
    lender: str
    loan_type: str
    principal_amount: float
    annual_interest_rate: float
    term_months: int
    monthly_payment: float
    start_date: date
    asset_value: Optional[float]
    currency: str
    notes: Optional[str]
    is_active: bool
    payments_made: int
    payments_remaining: int
    payoff_date: Optional[date]
    next_payment_date: Optional[date]
    remaining_principal: float
    principal_paid: float
    total_paid_amount: float
    interest_paid: float
    remaining_interest: float
    weekly_payment_equivalent: float
    net_worth_impact: float
    last_payment_date: Optional[date]
    is_payment_due: bool
    linked_payments: tuple[LoanPaymentSnapshot, ...]
    disbursements: tuple[LoanPaymentSnapshot, ...]


@dataclass(frozen=True)
class LoanPortfolioSummary:
    """Detached, immutable summary of active loans and portfolio rollups."""

    loans: tuple[LoanStatusSnapshot, ...]
    total_liabilities: float
    total_receivables: float
    total_asset_value: float
    net_loan_impact: float
    total_monthly_payment: float
    total_weekly_payment: float
    latest_payoff_date: Optional[date]


@dataclass(frozen=True)
class _LoanLedgerItem:
    """Detached normalized direct-transaction or split-line loan ledger entry."""

    tx_id: int
    transaction_date: date
    tx_amount: float
    tx_currency: str
    tx_exchange_rate: Optional[float]
    signed_amount: float
    category: str
    merchant: str
    account_id: int
    account_name: Optional[str]
    split_id: Optional[int] = None
    notes: Optional[str] = None
    loan_payment_amount: Optional[float] = None


@dataclass(frozen=True)
class _LoanParams:
    """Detached normalized loan parameters extracted from a Loan ORM row."""

    loan_id: int
    name: str
    lender: str
    loan_type: str
    principal: float
    annual_rate: float
    monthly_rate: float
    term: int
    monthly_payment: float
    start_date: date
    asset_value: Optional[float]
    currency: str
    notes: Optional[str]
    is_active: bool


@dataclass(frozen=True)
class _AmortizationResult:
    """Computed balance, payment totals, and chronological entries from ledger replay."""

    effective_principal: float
    balance: float
    payments_made: int
    interest_paid_total: float
    total_paid: float
    last_payment_date: Optional[date]
    linked_payments: tuple[LoanPaymentSnapshot, ...]
    disbursements: tuple[LoanPaymentSnapshot, ...]


@dataclass(frozen=True)
class _ScheduleProjection:
    """Derived payment countdown, next due date, and payoff schedule."""

    payments_remaining: int
    next_payment_date: Optional[date]
    is_payment_due: bool
    payoff_date: Optional[date]
    remaining_interest: float
    weekly_payment_equivalent: float


@dataclass
class _ReplayState:
    """Mutable loop accumulator for chronological loan ledger replay."""

    balance: float
    disbursement_total: float = 0.0
    payments_made: int = 0
    interest_paid_total: float = 0.0
    total_paid: float = 0.0
    repayment_entries: list[LoanPaymentSnapshot] = field(default_factory=list)
    disbursement_entries: list[LoanPaymentSnapshot] = field(default_factory=list)


def add_months(base_date: date, months: int) -> date:
    """Add calendar months while clamping day-of-month to the target month's length."""
    total_months = base_date.month - 1 + months
    year = base_date.year + total_months // 12
    month = total_months % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(base_date.day, last_day))


def _loan_eager_load_options() -> tuple[Any, ...]:
    """Return SQLAlchemy eager-load options for loan transactions, splits, and accounts."""
    return (
        selectinload(Loan.transactions).joinedload(Transaction.account),
        selectinload(Loan.transactions).selectinload(Transaction.splits),
        selectinload(Loan.split_entries)
        .joinedload(TransactionSplit.transaction)
        .joinedload(Transaction.account),
        selectinload(Loan.split_entries)
        .joinedload(TransactionSplit.transaction)
        .selectinload(Transaction.splits),
    )


def load_loans_with_transactions(db: Session, *, active_only: bool = True) -> list[Loan]:
    """Load loans with linked transactions, split entries, and accounts eagerly loaded."""
    stmt = (
        select(Loan)
        .options(*_loan_eager_load_options())
        .order_by(Loan.start_date.asc(), Loan.id.asc())
    )
    if active_only:
        stmt = stmt.where(Loan.is_active == True)
    return list(db.scalars(stmt).unique().all())


def load_loan_with_transactions(db: Session, loan_id: int) -> Optional[Loan]:
    """Load one loan with linked transactions, split entries, and accounts eagerly loaded."""
    stmt = (
        select(Loan)
        .options(*_loan_eager_load_options())
        .where(Loan.id == loan_id)
    )
    return db.scalars(stmt).unique().first()


def _extract_loan_params(loan: Loan) -> _LoanParams:
    """Detach scalar loan attributes into an immutable parameter record."""
    annual_rate = float(loan.annual_interest_rate)
    loan_type = (getattr(loan, "loan_type", None) or "borrowed").strip().lower()
    if loan_type not in ("borrowed", "lent"):
        loan_type = "borrowed"
    return _LoanParams(
        loan_id=int(loan.id or 0),
        name=loan.name,
        lender=loan.lender,
        loan_type=loan_type,
        principal=float(loan.principal_amount),
        annual_rate=annual_rate,
        monthly_rate=annual_rate / 12.0,
        term=int(loan.term_months),
        monthly_payment=float(loan.monthly_payment),
        start_date=loan.start_date,
        asset_value=float(loan.asset_value) if loan.asset_value is not None else None,
        currency=(loan.currency or "NZD").strip().upper(),
        notes=loan.notes,
        is_active=bool(loan.is_active),
    )


def _item_from_transaction(tx: Transaction) -> _LoanLedgerItem:
    """Detach one direct Transaction ORM row into an immutable ledger item."""
    return _LoanLedgerItem(
        tx_id=int(tx.id or 0),
        transaction_date=tx.transaction_date,
        tx_amount=float(tx.amount),
        tx_currency=(tx.currency or "NZD").strip().upper(),
        tx_exchange_rate=(float(tx.exchange_rate) if tx.exchange_rate is not None else None),
        signed_amount=float(tx.amount),
        category=tx.category,
        merchant=tx.merchant,
        account_id=int(tx.account_id),
        account_name=tx.account.name if tx.account else None,
        split_id=None,
        notes=tx.notes,
        loan_payment_amount=(
            float(tx.loan_payment_amount) if tx.loan_payment_amount is not None else None
        ),
    )


def _item_from_split(split: TransactionSplit, tx: Transaction) -> _LoanLedgerItem:
    """Detach one TransactionSplit ORM row into an immutable ledger item."""
    signed_split = round(
        split.signed_amount(parent_amount=float(tx.amount), siblings=list(tx.splits)),
        2,
    )
    return _LoanLedgerItem(
        tx_id=int(tx.id or 0),
        transaction_date=tx.transaction_date,
        tx_amount=float(tx.amount),
        tx_currency=(tx.currency or "NZD").strip().upper(),
        tx_exchange_rate=(float(tx.exchange_rate) if tx.exchange_rate is not None else None),
        signed_amount=signed_split,
        category=split.category,
        merchant=tx.merchant,
        account_id=int(tx.account_id),
        account_name=tx.account.name if tx.account else None,
        split_id=int(split.id) if split.id is not None else None,
        notes=split.notes,
        loan_payment_amount=None,
    )


def _extract_ledger_items(
    loan: Loan,
    linked_transactions: Optional[list[Transaction]] = None,
) -> list[_LoanLedgerItem]:
    """Detach direct unsplit transactions and split-line entries for a loan."""
    if linked_transactions is not None:
        return [_item_from_transaction(tx) for tx in linked_transactions]

    items: list[_LoanLedgerItem] = []
    for tx in loan.transactions or []:
        if getattr(tx, "splits", None):
            continue
        items.append(_item_from_transaction(tx))

    for split in getattr(loan, "split_entries", None) or []:
        tx = split.transaction
        if tx is None:
            continue
        items.append(_item_from_split(split, tx))
    return items


def _ledger_sort_key(item: _LoanLedgerItem) -> tuple[date, int, int]:
    """Chronological tie-breaking sort key for detached ledger items."""
    return (item.transaction_date, item.tx_id, item.split_id or 0)


def _partition_ledger_items(
    items: list[_LoanLedgerItem],
    loan_type: str,
) -> tuple[list[_LoanLedgerItem], list[_LoanLedgerItem]]:
    """Split detached ledger items into sorted (repayments, disbursements)."""
    outflows = sorted((i for i in items if i.signed_amount < 0), key=_ledger_sort_key)
    inflows = sorted((i for i in items if i.signed_amount > 0), key=_ledger_sort_key)
    if loan_type == "lent":
        return inflows, outflows
    return outflows, []


def _resolve_pair_rate(
    item: _LoanLedgerItem,
    from_currency: str,
    to_currency: str,
    *,
    db: Optional[Session],
    fetcher: FxRateFetcher,
) -> Optional[float]:
    """Resolve an exchange rate from cache/DB or direct fetcher without silent fallbacks."""
    resolved = (
        get_exchange_rate(
            db,
            rate_date=item.transaction_date,
            from_currency=from_currency,
            to_currency=to_currency,
            fetcher=fetcher,
        )
        if db is not None
        else fetcher.fetch_rate(
            rate_date=item.transaction_date,
            from_currency=from_currency,
            to_currency=to_currency,
        )
    )
    if resolved is not None and float(resolved[0]) > 0:
        return float(resolved[0])
    return None


def _convert_payment_to_loan_currency(
    item: _LoanLedgerItem,
    loan_currency: str,
    raw_amount: Optional[float] = None,
    *,
    db: Optional[Session],
    fetcher: FxRateFetcher,
) -> float:
    """Convert a transaction or partial override amount into the loan's currency."""
    amount = abs(float(item.tx_amount if raw_amount is None else raw_amount))
    tx_curr = (item.tx_currency or "NZD").strip().upper()
    loan_curr = (loan_currency or "NZD").strip().upper()
    if tx_curr == loan_curr:
        return amount

    if loan_curr == "NZD":
        if item.tx_exchange_rate is not None and item.tx_exchange_rate > 0:
            return abs(to_nzd_amount(amount, tx_curr, item.tx_exchange_rate))
        rate = _resolve_pair_rate(item, tx_curr, "NZD", db=db, fetcher=fetcher)
        if rate is not None:
            return amount * rate
        raise LoanFxConversionError(
            f"Unable to resolve FX rate for {tx_curr}->NZD on {item.transaction_date} "
            f"(tx_id={item.tx_id}, split_id={item.split_id})."
        )

    direct_rate = _resolve_pair_rate(item, tx_curr, loan_curr, db=db, fetcher=fetcher)
    if direct_rate is not None:
        return amount * direct_rate

    if db is not None and tx_curr != "NZD" and item.tx_exchange_rate is not None and item.tx_exchange_rate > 0:
        loan_to_nzd = _resolve_pair_rate(item, loan_curr, "NZD", db=db, fetcher=fetcher)
        if loan_to_nzd is not None:
            return amount * (float(item.tx_exchange_rate) / loan_to_nzd)

    raise LoanFxConversionError(
        f"Unable to resolve FX rate for {tx_curr}->{loan_curr} on {item.transaction_date} "
        f"(tx_id={item.tx_id}, split_id={item.split_id})."
    )


def _item_applied_amount(
    item: _LoanLedgerItem,
    loan_currency: str,
    *,
    db: Optional[Session],
    fetcher: FxRateFetcher,
) -> float:
    """Resolve the currency-normalized principal/payment magnitude for a ledger item."""
    raw_override: Optional[float] = None
    if item.split_id is not None:
        raw_override = abs(item.signed_amount)
    elif item.loan_payment_amount is not None:
        raw_override = abs(float(item.loan_payment_amount))
    return _convert_payment_to_loan_currency(
        item,
        loan_currency,
        raw_amount=raw_override,
        db=db,
        fetcher=fetcher,
    )


def _build_payment_snapshot(
    item: _LoanLedgerItem,
    *,
    applied_amount: float,
    entry_type: str,
    loan_currency: str,
) -> LoanPaymentSnapshot:
    """Build an immutable LoanPaymentSnapshot from a detached ledger item."""
    has_stored_rate = item.tx_exchange_rate is not None and item.tx_exchange_rate > 0
    if loan_currency == "NZD" and item.tx_currency != "NZD" and not has_stored_rate:
        if item.loan_payment_amount is not None and item.loan_payment_amount > 0:
            amount_nzd = round(
                abs(item.signed_amount) * (applied_amount / abs(float(item.loan_payment_amount))),
                2,
            )
        else:
            amount_nzd = round(applied_amount, 2)
    else:
        amount_nzd = round(
            abs(to_nzd_amount(item.signed_amount, item.tx_currency, item.tx_exchange_rate)),
            2,
        )

    return LoanPaymentSnapshot(
        id=item.tx_id,
        split_id=item.split_id,
        transaction_date=item.transaction_date,
        amount=float(item.signed_amount),
        currency=item.tx_currency,
        amount_nzd=amount_nzd,
        applied_amount=round(applied_amount, 2),
        loan_payment_amount=item.loan_payment_amount,
        entry_type=entry_type,
        merchant=item.merchant,
        account_id=item.account_id,
        account_name=item.account_name,
        category=item.category,
        notes=item.notes,
    )


def _apply_repayment_step(
    item: _LoanLedgerItem,
    current_balance: float,
    params: _LoanParams,
    *,
    db: Optional[Session],
    fetcher: FxRateFetcher,
) -> tuple[float, float, float, LoanPaymentSnapshot]:
    """Apply one repayment step to current balance and return (new_balance, interest, applied, snapshot)."""
    applied = _item_applied_amount(item, params.currency, db=db, fetcher=fetcher)
    interest = current_balance * params.monthly_rate if params.annual_rate > 0 else 0.0
    principal_part = max(0.0, applied - interest)
    new_balance = max(0.0, current_balance - principal_part)
    if new_balance <= 0.01:
        new_balance = 0.0
    snapshot = _build_payment_snapshot(
        item,
        applied_amount=applied,
        entry_type="repayment",
        loan_currency=params.currency,
    )
    return new_balance, interest, applied, snapshot


def _resolve_fetcher_context(fetcher: Optional[FxRateFetcher]):
    """Return an active FX fetcher context manager without masking missing config."""
    if fetcher is not None:
        return nullcontext(fetcher)
    return HttpFxRateFetcher(wise_api_token=settings.wise_api_token)


def _record_disbursement(
    state: _ReplayState,
    item: _LoanLedgerItem,
    params: _LoanParams,
    *,
    db: Optional[Session],
    fetcher: FxRateFetcher,
) -> None:
    """Accumulate one disbursement advance into the replay state."""
    applied = _item_applied_amount(item, params.currency, db=db, fetcher=fetcher)
    state.balance += applied
    state.disbursement_total += applied
    state.disbursement_entries.append(
        _build_payment_snapshot(
            item,
            applied_amount=applied,
            entry_type="disbursement",
            loan_currency=params.currency,
        )
    )


def _record_repayment(
    state: _ReplayState,
    item: _LoanLedgerItem,
    params: _LoanParams,
    *,
    db: Optional[Session],
    fetcher: FxRateFetcher,
) -> None:
    """Accumulate one repayment step into the replay state."""
    state.balance, interest, applied, snapshot = _apply_repayment_step(
        item, state.balance, params, db=db, fetcher=fetcher
    )
    state.interest_paid_total += interest
    state.total_paid += applied
    state.payments_made += 1
    state.repayment_entries.append(snapshot)


def _finalize_amortization(
    state: _ReplayState,
    effective_principal: float,
    repayment_items: list[_LoanLedgerItem],
) -> _AmortizationResult:
    """Freeze replay state into an immutable _AmortizationResult."""
    return _AmortizationResult(
        effective_principal=effective_principal,
        balance=state.balance,
        payments_made=state.payments_made,
        interest_paid_total=state.interest_paid_total,
        total_paid=state.total_paid,
        last_payment_date=repayment_items[-1].transaction_date if repayment_items else None,
        linked_payments=tuple(reversed(state.repayment_entries)),
        disbursements=tuple(reversed(state.disbursement_entries)),
    )


def _replay_lent_with_disbursements(
    params: _LoanParams,
    repayment_items: list[_LoanLedgerItem],
    disbursement_items: list[_LoanLedgerItem],
    *,
    db: Optional[Session],
    fetcher: FxRateFetcher,
) -> _AmortizationResult:
    """Replay chronological disbursements and repayments for a lent loan with advances."""
    events: list[tuple[bool, _LoanLedgerItem]] = [
        *((True, item) for item in disbursement_items),
        *((False, item) for item in repayment_items),
    ]
    events.sort(key=lambda pair: (*_ledger_sort_key(pair[1]), 0 if pair[0] else 1))
    state = _ReplayState(balance=0.0)
    for is_disbursement, item in events:
        if is_disbursement:
            _record_disbursement(state, item, params, db=db, fetcher=fetcher)
        else:
            _record_repayment(state, item, params, db=db, fetcher=fetcher)
    return _finalize_amortization(state, round(state.disbursement_total, 2), repayment_items)


def _replay_standard_repayments(
    params: _LoanParams,
    repayment_items: list[_LoanLedgerItem],
    *,
    db: Optional[Session],
    fetcher: FxRateFetcher,
) -> _AmortizationResult:
    """Replay chronological repayments starting from declared principal."""
    state = _ReplayState(balance=params.principal)
    for item in repayment_items:
        _record_repayment(state, item, params, db=db, fetcher=fetcher)
    return _finalize_amortization(state, params.principal, repayment_items)


def _open_term_schedule_projection(amortization: _AmortizationResult) -> _ScheduleProjection:
    """Return schedule projection for an open-term lent loan (term == 0)."""
    payoff_date = amortization.last_payment_date if amortization.balance <= 0.0 else None
    return _ScheduleProjection(
        payments_remaining=0,
        next_payment_date=None,
        is_payment_due=False,
        payoff_date=payoff_date,
        remaining_interest=0.0,
        weekly_payment_equivalent=0.0,
    )


def _compute_schedule_projection(
    params: _LoanParams,
    amortization: _AmortizationResult,
    as_of: date,
) -> _ScheduleProjection:
    """Compute remaining payments, next due date, payoff date, and weekly equivalent."""
    if params.loan_type == "lent" and params.term == 0:
        return _open_term_schedule_projection(amortization)

    remaining_principal = round(amortization.balance, 2)
    payments_remaining = (
        0 if amortization.balance <= 0.0 else max(0, params.term - amortization.payments_made)
    )
    next_payment_date = (
        add_months(params.start_date, amortization.payments_made) if payments_remaining > 0 else None
    )
    is_payment_due = bool(
        payments_remaining > 0 and next_payment_date is not None and next_payment_date <= as_of
    )
    payoff_date = (
        amortization.last_payment_date
        if (payments_remaining == 0 and amortization.last_payment_date is not None)
        else add_months(params.start_date, max(0, params.term - 1))
    )
    remaining_interest = round(
        max(0.0, (params.monthly_payment * payments_remaining) - remaining_principal), 2
    )
    weekly_equiv = round(params.monthly_payment * 12.0 / 52.0, 2) if payments_remaining > 0 else 0.0
    return _ScheduleProjection(
        payments_remaining=payments_remaining,
        next_payment_date=next_payment_date,
        is_payment_due=is_payment_due,
        payoff_date=payoff_date,
        remaining_interest=remaining_interest,
        weekly_payment_equivalent=weekly_equiv,
    )


def _build_loan_status_snapshot(
    params: _LoanParams,
    amortization: _AmortizationResult,
    schedule: _ScheduleProjection,
) -> LoanStatusSnapshot:
    """Assemble an immutable LoanStatusSnapshot from replay and schedule outputs."""
    remaining_principal = round(amortization.balance, 2)
    principal_paid = round(max(0.0, amortization.effective_principal - remaining_principal), 2)
    net_worth_impact = (
        0.0
        if params.loan_type == "lent"
        else round((params.asset_value or 0.0) - remaining_principal, 2)
    )
    return LoanStatusSnapshot(
        id=params.loan_id, name=params.name, lender=params.lender,
        loan_type=params.loan_type, principal_amount=amortization.effective_principal,
        annual_interest_rate=params.annual_rate, term_months=params.term,
        monthly_payment=params.monthly_payment, start_date=params.start_date,
        asset_value=params.asset_value, currency=params.currency,
        notes=params.notes, is_active=params.is_active,
        payments_made=amortization.payments_made,
        payments_remaining=schedule.payments_remaining,
        payoff_date=schedule.payoff_date,
        next_payment_date=schedule.next_payment_date,
        remaining_principal=remaining_principal, principal_paid=principal_paid,
        total_paid_amount=round(amortization.total_paid, 2),
        interest_paid=round(amortization.interest_paid_total, 2),
        remaining_interest=schedule.remaining_interest,
        weekly_payment_equivalent=schedule.weekly_payment_equivalent,
        net_worth_impact=net_worth_impact,
        last_payment_date=amortization.last_payment_date,
        is_payment_due=schedule.is_payment_due,
        linked_payments=amortization.linked_payments,
        disbursements=amortization.disbursements,
    )


def compute_loan_status(
    loan: Loan,
    as_of: date,
    linked_transactions: Optional[list[Transaction]] = None,
    *,
    db: Optional[Session] = None,
    fetcher: Optional[FxRateFetcher] = None,
) -> LoanStatusSnapshot:
    """Compute detached loan amortization, payment countdown, and net worth impact."""
    params = _extract_loan_params(loan)
    repayment_items, disbursement_items = _partition_ledger_items(
        _extract_ledger_items(loan, linked_transactions), params.loan_type
    )
    with _resolve_fetcher_context(fetcher) as active_fetcher:
        if params.loan_type == "lent" and disbursement_items:
            amortization = _replay_lent_with_disbursements(
                params, repayment_items, disbursement_items, db=db, fetcher=active_fetcher
            )
        else:
            amortization = _replay_standard_repayments(
                params, repayment_items, db=db, fetcher=active_fetcher
            )
    schedule = _compute_schedule_projection(params, amortization, as_of)
    return _build_loan_status_snapshot(params, amortization, schedule)


def _build_portfolio_summary(items: tuple[LoanStatusSnapshot, ...]) -> LoanPortfolioSummary:
    """Aggregate detached active loan snapshots into a LoanPortfolioSummary."""
    borrowed = tuple(item for item in items if item.loan_type == "borrowed")
    total_liabilities = round(sum(item.remaining_principal for item in borrowed), 2)
    total_receivables = round(
        sum(item.remaining_principal for item in items if item.loan_type == "lent"), 2
    )
    total_asset_value = round(sum(item.asset_value or 0.0 for item in borrowed), 2)
    total_monthly = round(
        sum(item.monthly_payment for item in borrowed if item.payments_remaining > 0), 2
    )
    total_weekly = round(sum(item.weekly_payment_equivalent for item in borrowed), 2)
    payoff_dates = [
        item.payoff_date
        for item in borrowed
        if item.payments_remaining > 0 and item.payoff_date is not None
    ]
    return LoanPortfolioSummary(
        loans=items,
        total_liabilities=total_liabilities,
        total_receivables=total_receivables,
        total_asset_value=total_asset_value,
        net_loan_impact=round(total_asset_value - total_liabilities, 2),
        total_monthly_payment=total_monthly,
        total_weekly_payment=total_weekly,
        latest_payoff_date=max(payoff_dates) if payoff_dates else None,
    )


def summarize_loans(
    loans: list[Loan],
    as_of: date,
    *,
    db: Optional[Session] = None,
    fetcher: Optional[FxRateFetcher] = None,
) -> LoanPortfolioSummary:
    """Compute detached individual loan snapshots and active portfolio rollups."""
    with _resolve_fetcher_context(fetcher) as active_fetcher:
        items = tuple(
            compute_loan_status(loan, as_of, db=db, fetcher=active_fetcher)
            for loan in loans
            if loan.is_active
        )
    return _build_portfolio_summary(items)


def _normalize_loan_type(value: Any) -> str:
    """Normalize and validate a loan_type string against allowed values."""
    if not isinstance(value, str):
        raise LoanValidationError("loan_type must be 'borrowed' or 'lent'.")
    normalized = value.strip().lower()
    if normalized not in _VALID_LOAN_TYPES:
        raise LoanValidationError("loan_type must be 'borrowed' or 'lent'.")
    return normalized


def _validate_loan_invariants(
    *,
    name: str,
    lender: str,
    loan_type: str,
    principal_amount: float,
    annual_interest_rate: float,
    term_months: int,
    monthly_payment: float,
    asset_value: Optional[float],
) -> None:
    """Enforce core loan domain invariants before persisting or mutating ORM state."""
    if not isinstance(name, str) or not name.strip():
        raise LoanValidationError("name must not be blank.")
    if not isinstance(lender, str) or not lender.strip():
        raise LoanValidationError("lender must not be blank.")
    if loan_type not in _VALID_LOAN_TYPES:
        raise LoanValidationError("loan_type must be 'borrowed' or 'lent'.")
    if principal_amount <= 0.0:
        raise LoanValidationError("principal_amount must be > 0.")
    if annual_interest_rate < 0.0 or annual_interest_rate > 1.0:
        raise LoanValidationError("annual_interest_rate must be between 0 and 1.")
    if term_months < 0 or term_months > 600:
        raise LoanValidationError("term_months must be between 0 and 600.")
    if monthly_payment < 0.0:
        raise LoanValidationError("monthly_payment must be >= 0.")
    if asset_value is not None and asset_value < 0.0:
        raise LoanValidationError("asset_value must be >= 0.")
    if loan_type == "borrowed" and (int(term_months) < 1 or float(monthly_payment) <= 0.0):
        raise LoanValidationError(
            "Borrowed loans require term_months >= 1 and monthly_payment > 0."
        )


def _prepare_loan_updates(loan: Loan, updates: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and stage partial loan updates without mutating the ORM instance."""
    unknown_keys = sorted(set(updates.keys()) - _ALLOWED_UPDATE_FIELDS)
    if unknown_keys:
        raise LoanValidationError(
            f"Unsupported loan update field(s): {', '.join(unknown_keys)}."
        )
    staged: dict[str, Any] = {}
    for field_name, raw_value in updates.items():
        if field_name in _NON_NULLABLE_UPDATE_FIELDS and raw_value is None:
            raise LoanValidationError(f"{field_name} cannot be null.")
        if field_name == "loan_type":
            staged[field_name] = _normalize_loan_type(raw_value)
        elif isinstance(raw_value, str):
            staged[field_name] = raw_value.strip()
        else:
            staged[field_name] = raw_value

    merged_asset = (
        staged["asset_value"]
        if "asset_value" in staged
        else (float(loan.asset_value) if loan.asset_value is not None else None)
    )
    _validate_loan_invariants(
        name=staged.get("name", loan.name),
        lender=staged.get("lender", loan.lender),
        loan_type=staged.get("loan_type", _normalize_loan_type(loan.loan_type)),
        principal_amount=float(staged.get("principal_amount", loan.principal_amount)),
        annual_interest_rate=float(staged.get("annual_interest_rate", loan.annual_interest_rate)),
        term_months=int(staged.get("term_months", loan.term_months)),
        monthly_payment=float(staged.get("monthly_payment", loan.monthly_payment)),
        asset_value=float(merged_asset) if merged_asset is not None else None,
    )
    return staged


def create_loan(
    db: Session,
    data: LoanCreateInput,
    *,
    as_of: Optional[date] = None,
    fetcher: Optional[FxRateFetcher] = None,
) -> LoanStatusSnapshot:
    """Validate, persist, and return a detached status snapshot for a new loan."""
    name = data.name.strip() if isinstance(data.name, str) else ""
    lender = data.lender.strip() if isinstance(data.lender, str) else ""
    notes = data.notes.strip() if isinstance(data.notes, str) else None
    currency = (data.currency or "").strip().upper()[:3] or "NZD"
    loan_type = _normalize_loan_type(data.loan_type)
    _validate_loan_invariants(
        name=name,
        lender=lender,
        loan_type=loan_type,
        principal_amount=float(data.principal_amount),
        annual_interest_rate=float(data.annual_interest_rate),
        term_months=int(data.term_months),
        monthly_payment=float(data.monthly_payment),
        asset_value=float(data.asset_value) if data.asset_value is not None else None,
    )
    loan = Loan(
        name=name, lender=lender, loan_type=loan_type,
        principal_amount=data.principal_amount,
        annual_interest_rate=data.annual_interest_rate,
        term_months=data.term_months, monthly_payment=data.monthly_payment,
        start_date=data.start_date, asset_value=data.asset_value,
        currency=currency, notes=notes, is_active=data.is_active,
    )
    db.add(loan)
    db.commit()
    snapshot = get_loan_status(db, loan.id, as_of=as_of, fetcher=fetcher)
    if snapshot is None:
        raise LoanNotFoundError("Loan not found")
    return snapshot


def update_loan(
    db: Session,
    loan_id: int,
    updates: Mapping[str, Any],
    *,
    as_of: Optional[date] = None,
    fetcher: Optional[FxRateFetcher] = None,
) -> LoanStatusSnapshot:
    """Validate and apply partial updates to an existing loan, returning its snapshot."""
    loan = db.get(Loan, loan_id)
    if loan is None:
        raise LoanNotFoundError("Loan not found")
    staged = _prepare_loan_updates(loan, updates)
    for field_name, value in staged.items():
        setattr(loan, field_name, value)
    db.commit()
    snapshot = get_loan_status(db, loan.id, as_of=as_of, fetcher=fetcher)
    if snapshot is None:
        raise LoanNotFoundError("Loan not found")
    return snapshot


def delete_loan(db: Session, loan_id: int) -> None:
    """Delete an existing loan record or raise LoanNotFoundError."""
    loan = db.get(Loan, loan_id)
    if loan is None:
        raise LoanNotFoundError("Loan not found")
    db.delete(loan)
    db.commit()


def get_loan_status(
    db: Session,
    loan_id: int,
    *,
    as_of: Optional[date] = None,
    fetcher: Optional[FxRateFetcher] = None,
) -> Optional[LoanStatusSnapshot]:
    """Load one loan with transactions/splits and compute its detached status snapshot."""
    loan = load_loan_with_transactions(db, loan_id)
    if loan is None:
        return None
    return compute_loan_status(loan, as_of or date.today(), db=db, fetcher=fetcher)


def get_loan_portfolio_summary(
    db: Session,
    *,
    as_of: Optional[date] = None,
    active_only: bool = True,
    fetcher: Optional[FxRateFetcher] = None,
) -> LoanPortfolioSummary:
    """Load loans with transactions/splits and compute the detached portfolio summary."""
    loans = load_loans_with_transactions(db, active_only=active_only)
    return summarize_loans(loans, as_of or date.today(), db=db, fetcher=fetcher)
