"""Statement parsing orchestration bridging PDF extraction and pure text/table dispatch."""

from __future__ import annotations

from collections.abc import Sequence
import re
from typing import Optional

from app.services.statement_parser._types import PdfDocumentExtractor, StatementParseResult
from app.services.statement_parser.categorization import detect_institution
from app.services.statement_parser.dbs_parser import parse_dbs_statement
from app.services.statement_parser.nz_parser import parse_nz_statement
from app.services.statement_parser.pdf_extractor import extract_pdf_document


def parse_statement_text(
    full_text: str,
    *,
    institution: str = "auto",
    tables: Sequence[Sequence[Sequence[Optional[str]]]] = (),
) -> StatementParseResult:
    """Detect institution and dispatch extracted statement text and tables to pure parsers."""
    detected_inst = institution
    if not detected_inst or detected_inst.lower() in ("auto", "unknown"):
        detected_inst = detect_institution(full_text) or "Unknown"

    if detected_inst == "DBS Bank" or re.search(
        r"\b(?:DBS|POSB)\b|dbs\.com|posb\.com",
        full_text,
        re.I,
    ):
        return parse_dbs_statement(full_text, detected_inst="DBS Bank")

    return parse_nz_statement(full_text, tables=tables, institution=detected_inst)


def parse_pdf_statement(
    file_bytes: bytes,
    institution: str = "auto",
    *,
    pdf_extractor: Optional[PdfDocumentExtractor] = None,
) -> StatementParseResult:
    """Extract PDF text/tables via injected or default extractor and parse pure content."""
    extractor = pdf_extractor or extract_pdf_document
    document = extractor(file_bytes)
    return parse_statement_text(
        document.full_text,
        institution=institution,
        tables=document.all_tables,
    )
