"""Detached frozen domain contracts and typed exceptions for Receipt Ingestion."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

PdfTextExtractor = Callable[[bytes], str]


class ReceiptIngestionError(Exception):
    """Base domain exception for receipt ingestion, parsing, and matching."""


class ReceiptNotFoundError(ReceiptIngestionError):
    """Raised when a target receipt cannot be found."""


class ReceiptValidationError(ReceiptIngestionError):
    """Raised when receipt inputs or media violate ingestion constraints."""


class ReceiptParseError(ReceiptIngestionError):
    """Raised when receipt or payslip text/vision LLM output is invalid or unconfigured."""


class ReceiptUpstreamError(ReceiptIngestionError):
    """Raised when an upstream AI gateway call fails during receipt or payslip parsing."""


@dataclass(frozen=True)
class StoredReceiptMedia:
    """Compressed or raw media bytes retained for receipt persistence."""

    data: bytes
    mime_type: str
    filename: Optional[str]


@dataclass(frozen=True)
class SanitizedEmailContent:
    """Detached sanitized email prompt text and resolved local fallback date."""

    prompt_text: str
    fallback_date: date


@dataclass(frozen=True)
class LineItemHeuristic:
    """Heuristic category, domain, product type, and volume inference for a line item."""

    category: str
    domain: Optional[str]
    product_type: Optional[str]
    volume_ml: Optional[str]


@dataclass(frozen=True)
class ReceiptItemDraft:
    """Immutable input draft representing one candidate or manual receipt line item."""

    item_name: str
    brand: Optional[str] = None
    quantity: float = 1.0
    unit_price: float = 0.0
    discount_amount: Optional[float] = None
    total_price: float = 0.0
    explicit_category: Optional[str] = None
    explicit_domain: Optional[str] = None
    explicit_product_type: Optional[str] = None
    explicit_volume_ml: Optional[str] = None
    explicit_tags: tuple[str, ...] = ()
    notes: Optional[str] = None


@dataclass(frozen=True)
class ItemClassification:
    """Resolved category, domain, product type, volume, and normalized tags for a line item."""

    category: str
    domain: Optional[str]
    product_type: Optional[str]
    volume_ml: Optional[str]
    tags: tuple[str, ...]


@dataclass(frozen=True)
class ReceiptItemSnapshot:
    """Detached immutable view of a persisted receipt line item."""

    id: int
    receipt_id: int
    product_id: Optional[int]
    item_name: str
    brand: Optional[str]
    quantity: float
    unit_price: float
    discount_amount: Optional[float]
    total_price: float
    category: str
    domain: Optional[str]
    product_type: Optional[str]
    tags: tuple[str, ...]
    volume_ml: Optional[str]
    notes: Optional[str]
    inherited_tags: tuple[str, ...] = ()
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class ManualReceiptInput:
    """Immutable command contract for creating a manual receipt or payslip."""

    receipt_date: date
    merchant: str
    total_amount: float
    document_type: str = "receipt"
    currency: str = "NZD"
    tax_amount: Optional[float] = None
    order_number: Optional[str] = None
    status: str = "unlinked"
    notes: Optional[str] = None
    items: tuple[ReceiptItemDraft, ...] = ()


@dataclass(frozen=True)
class ReceiptSnapshot:
    """Detached immutable view of a persisted receipt or payslip and its items."""

    id: int
    transaction_id: Optional[int]
    document_type: str
    receipt_date: date
    merchant: str
    total_amount: float
    currency: str
    tax_amount: Optional[float]
    order_number: Optional[str]
    source_email_message_id: Optional[str]
    status: str
    image_path: Optional[str]
    image_mime_type: Optional[str]
    image_filename: Optional[str]
    has_image: bool
    is_processed: bool
    raw_text: Optional[str]
    notes: Optional[str]
    items: tuple[ReceiptItemSnapshot, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class PurchaseEmailIngestionResult:
    """Detached outcome of ingesting a single- or multi-order purchase receipt email."""

    receipts: tuple[ReceiptSnapshot, ...]
    receipt_ids: tuple[int, ...]
    deduplicated_count: int = 0


@dataclass(frozen=True)
class PayslipEmailIngestionResult:
    """Detached outcome of ingesting an automated payslip email."""

    receipt: ReceiptSnapshot
    deduplicated: bool = False


@dataclass(frozen=True)
class TransactionCandidateSnapshot:
    """Detached immutable view of a candidate transaction scored against a receipt."""

    transaction_id: int
    transaction_date: date
    merchant: str
    raw_description: Optional[str]
    amount: float
    currency: str
    account_id: int
    account_name: Optional[str]
    original_amount: Optional[float]
    original_currency: Optional[str]
    estimated_receipt_amount: Optional[float]
    confidence: float
    match_reason: str


@dataclass(frozen=True)
class ReceiptMatchOutcome:
    """Detached outcome of running 3-tier receipt-to-transaction matching."""

    receipt_id: int
    matched_transaction_id: Optional[int]
    status: str
    match_reason: Optional[str]
    confidence: Optional[float]
    candidate_count: int


@dataclass(frozen=True)
class SemanticDuplicateMatch:
    """Detached summary of an existing receipt matched during semantic deduplication."""

    receipt_id: int
    merchant: str
    receipt_date: date
    total_amount: float
    currency: str
    order_number: Optional[str]
    document_type: str
    matched_by: str
