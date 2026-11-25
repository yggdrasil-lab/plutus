"""Typed domain models, immutable PDF extraction contracts, and error hierarchy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


class StatementParseError(RuntimeError):
    """Base error raised when a bank or brokerage statement cannot be parsed."""


class PdfExtractionError(StatementParseError):
    """Raised when raw PDF bytes cannot be decoded into text and tables."""


@dataclass
class ParsedTransaction:
    """Normalized transaction row extracted from a CSV or PDF statement."""

    transaction_date: date
    amount: float
    merchant: str
    raw_description: str
    category: str = "General"
    currency: Optional[str] = None
    exchange_rate: Optional[float] = None
    fee_amount: Optional[float] = None


@dataclass
class StatementParseResult:
    """Normalized statement payload returned by CSV and PDF parsers."""

    institution: str
    transaction_count: int
    transactions: list[ParsedTransaction] = field(default_factory=list)
    account_number_masked: Optional[str] = None
    account_name: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    opening_balance: Optional[float] = None
    closing_balance: Optional[float] = None
    currency: str = "NZD"


@dataclass(frozen=True)
class PdfPageContent:
    """Immutable extracted text and bordered tables for a single PDF page."""

    text: str
    tables: tuple[tuple[tuple[Optional[str], ...], ...], ...] = ()


@dataclass(frozen=True)
class PdfDocumentContent:
    """Immutable extracted content for an entire PDF document."""

    pages: tuple[PdfPageContent, ...]

    @property
    def full_text(self) -> str:
        """Join extracted page texts with newlines."""
        return "\n".join(page.text for page in self.pages)

    @property
    def all_tables(self) -> tuple[tuple[tuple[Optional[str], ...], ...], ...]:
        """Flatten bordered tables across all pages in page order."""
        return tuple(table for page in self.pages for table in page.tables)


PdfDocumentExtractor = Callable[[bytes], PdfDocumentContent]
