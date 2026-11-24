"""Detached frozen domain contracts, sentinels, and typed exceptions for Ledger Entry."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from app.services.statement_parser import StatementParseResult

TRANSFER_CATEGORIES: tuple[str, ...] = ("transfers", "transfer")
MAX_SPLIT_CATEGORY_LENGTH = 50


class _UnsetType:
    """Sentinel distinguishing omitted patch fields from explicit None values."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _UnsetType()


class LedgerEntryError(ValueError):
    """Base domain exception for ledger entry hydration and mutation operations."""


class LedgerEntryNotFoundError(LedgerEntryError):
    """Raised when a requested transaction, counterpart account, or loan does not exist."""


class LedgerEntryBadRequestError(LedgerEntryError):
    """Raised when a ledger mutation violates state or relationship preconditions."""


class LedgerEntryValidationError(LedgerEntryError):
    """Raised when patch or manual split line inputs violate validation rules."""


class LedgerEntryUpstreamError(LedgerEntryError):
    """Raised when an upstream AI or connector dependency fails during ingestion."""


@dataclass(frozen=True)
class HydratedManualSplit:
    """Detached-session-safe immutable view of one manual TransactionSplit row."""

    id: int
    transaction_id: int
    amount: float
    category: str
    tags: tuple[str, ...] = ()
    loan_id: Optional[int] = None
    loan_name: Optional[str] = None
    loan_type: Optional[str] = None
    notes: Optional[str] = None


@dataclass(frozen=True)
class HydratedTransaction:
    """Detached-session-safe immutable TransactionRead projection without ORM references."""

    id: int
    transaction_date: date
    account_id: int
    amount: float
    currency: str
    merchant: str
    category: str
    raw_hash: str
    created_at: datetime
    account_name: Optional[str] = None
    raw_description: Optional[str] = None
    notes: Optional[str] = None
    linked_transaction_id: Optional[int] = None
    linked_account_name: Optional[str] = None
    linked_amount: Optional[float] = None
    linked_currency: Optional[str] = None
    counterpart_account_id: Optional[int] = None
    counterpart_account_name: Optional[str] = None
    counterpart_account_type: Optional[str] = None
    loan_id: Optional[int] = None
    loan_name: Optional[str] = None
    loan_type: Optional[str] = None
    loan_payment_amount: Optional[float] = None
    exchange_rate: Optional[float] = None
    fee_amount: Optional[float] = None
    tags: tuple[str, ...] = ()
    recurrence: Optional[str] = None
    has_receipt: bool = False
    receipt_id: Optional[int] = None
    receipt_document_type: Optional[str] = None
    is_receipt_split: bool = False
    splits: tuple[HydratedManualSplit, ...] = ()
    is_split: bool = False
    is_amortized_commitment: bool = False
    amortized_period_amount_nzd: Optional[float] = None
    amortized_cadence: Optional[str] = None
    excluded_from_period_budget: bool = False


@dataclass(frozen=True)
class ManualSplitLineInput:
    """Immutable command input representing one line of a manual split replacement."""

    amount: float
    category: str
    tags: Sequence[str] = ()
    loan_id: Optional[int] = None
    notes: Optional[str] = None


@dataclass(frozen=True)
class TransactionPatch:
    """Immutable tri-state patch command for updating an existing transaction."""

    category: str | None | _UnsetType = UNSET
    merchant: str | None | _UnsetType = UNSET
    notes: str | None | _UnsetType = UNSET
    tags: Sequence[str] | None | _UnsetType = UNSET
    recurrence: str | None | _UnsetType = UNSET
    counterpart_account_id: int | None | _UnsetType = UNSET
    loan_id: int | None | _UnsetType = UNSET
    loan_payment_amount: float | None | _UnsetType = UNSET


@dataclass(frozen=True)
class TransactionWritePolicy:
    """Policy switches governing rules, Jev fallback, FX, tags, dedup, and reconciliation."""

    apply_rules: bool = True
    use_jev_fallback: bool = False
    resolve_reporting_fx: bool = True
    normalize_tags: bool = True
    deduplicate: bool = True
    reconcile_post_write: bool = True
    apply_counterpart_rules: bool = True


MANUAL_WRITE_POLICY = TransactionWritePolicy(use_jev_fallback=False)
QUICK_LOG_WRITE_POLICY = TransactionWritePolicy(use_jev_fallback=True)
MCP_EXPENSE_WRITE_POLICY = TransactionWritePolicy(use_jev_fallback=True)


@dataclass(frozen=True)
class TransactionWriteInput:
    """Immutable command input for recording a transaction via the unified write seam."""

    transaction_date: date
    amount: float
    merchant: str
    account_id: Optional[int] = None
    account_name: Optional[str] = None
    currency: Optional[str] = None
    category: Optional[str] = "General"
    raw_description: Optional[str] = None
    notes: Optional[str] = None
    linked_transaction_id: Optional[int] = None
    exchange_rate: Optional[float] = None
    fee_amount: Optional[float] = None
    tags: Sequence[str] = ()
    recurrence: Optional[str] = None


@dataclass(frozen=True)
class TransactionWriteResult:
    """Result returned by record_transaction containing the detached snapshot and created flag."""

    transaction: HydratedTransaction
    created: bool


@dataclass(frozen=True)
class StatementUploadInput:
    """Immutable command input for parsing and optionally committing a bank statement."""

    filename: str
    content: bytes
    institution: str = "auto"
    commit: bool = False
    account_id: Optional[int] = None
    closing_balance: Optional[float] = None
    tier: Optional[str] = None
    account_type: Optional[str] = None
    account_name: Optional[str] = None
    currency: Optional[str] = None


@dataclass(frozen=True)
class StatementUploadResult:
    """Result returned by statement upload or commit workflows."""

    parse_result: StatementParseResult
    committed: bool = False
    account_id: Optional[int] = None
    account_created: bool = False
    snapshot_upserted: bool = False


@dataclass(frozen=True)
class InboundEmailDispatchOutcome:
    """Detached immutable outcome of dispatching one inbound email through Plutus."""

    status: str
    route_target: str
    message_id: str
    duplicate: bool = False
    consumer_ref: Optional[str] = None
    transaction_id: Optional[int] = None
    receipt_id: Optional[int] = None
    receipt_ids: tuple[int, ...] = ()
    error_detail: Optional[str] = None
