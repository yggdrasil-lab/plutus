"""Fail-loud pdfplumber adapter implementing the PdfDocumentExtractor seam."""

from __future__ import annotations

from collections.abc import Sequence
import io
from typing import Any, Optional

from app.services.statement_parser._types import (
    PdfDocumentContent,
    PdfDocumentExtractor,
    PdfExtractionError,
    PdfPageContent,
)


def _normalize_tables(
    raw_tables: Optional[Sequence[Sequence[Sequence[Optional[str]]]]],
) -> tuple[tuple[tuple[Optional[str], ...], ...], ...]:
    """Convert nested mutable table lists into immutable nested tuples."""
    if not raw_tables:
        return ()
    return tuple(
        tuple(tuple(cell for cell in row) for row in table)
        for table in raw_tables
    )


def _extract_page(page: Any, page_num: int) -> PdfPageContent:
    """Extract text and bordered tables from a single pdfplumber page loudly."""
    try:
        text = page.extract_text() or ""
    except Exception as exc:
        raise PdfExtractionError(
            f"Failed to extract text from PDF page {page_num}: {exc}"
        ) from exc

    try:
        raw_tables = page.extract_tables()
    except Exception as exc:
        raise PdfExtractionError(
            f"Failed to extract tables from PDF page {page_num}: {exc}"
        ) from exc

    return PdfPageContent(text=text, tables=_normalize_tables(raw_tables))


def extract_pdf_document(file_bytes: bytes) -> PdfDocumentContent:
    """Extract immutable text and tables from raw PDF bytes using pdfplumber."""
    if not file_bytes:
        raise PdfExtractionError("Uploaded PDF file is empty.")

    try:
        import pdfplumber
    except ImportError as exc:
        raise PdfExtractionError(
            "pdfplumber is required for PDF statement extraction."
        ) from exc

    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            pages = tuple(
                _extract_page(page, idx)
                for idx, page in enumerate(pdf.pages, start=1)
            )
    except PdfExtractionError:
        raise
    except Exception as exc:
        raise PdfExtractionError(f"Failed to parse PDF document: {exc}") from exc

    return PdfDocumentContent(pages=pages)


def get_default_pdf_extractor() -> PdfDocumentExtractor:
    """Return the default concrete PDF document extractor adapter."""
    return extract_pdf_document
