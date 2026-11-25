"""Pure statement parsing package with injectable PDF extraction seam."""

from app.services.statement_parser._types import (
    ParsedTransaction,
    PdfDocumentContent,
    PdfDocumentExtractor,
    PdfExtractionError,
    PdfPageContent,
    StatementParseError,
    StatementParseResult,
)
from app.services.statement_parser.categorization import (
    CATEGORIZATION_RULES,
    INSTITUTION_DEFAULT_CURRENCIES,
    INSTITUTION_DETECTION_PATTERNS,
    MONTH_NAMES,
    auto_categorize,
    clean_merchant_name,
    detect_institution,
)
from app.services.statement_parser.csv_parser import parse_csv_statement
from app.services.statement_parser.dbs_parser import parse_dbs_statement
from app.services.statement_parser.nz_parser import parse_nz_statement
from app.services.statement_parser.orchestrator import (
    parse_pdf_statement,
    parse_statement_text,
)
from app.services.statement_parser.pdf_extractor import (
    extract_pdf_document,
    get_default_pdf_extractor,
)
from app.services.ledger_entry.hashing import generate_transaction_hash

__all__ = [
    "CATEGORIZATION_RULES",
    "INSTITUTION_DEFAULT_CURRENCIES",
    "INSTITUTION_DETECTION_PATTERNS",
    "MONTH_NAMES",
    "ParsedTransaction",
    "PdfDocumentContent",
    "PdfDocumentExtractor",
    "PdfExtractionError",
    "PdfPageContent",
    "StatementParseError",
    "StatementParseResult",
    "auto_categorize",
    "clean_merchant_name",
    "detect_institution",
    "extract_pdf_document",
    "generate_transaction_hash",
    "get_default_pdf_extractor",
    "parse_csv_statement",
    "parse_dbs_statement",
    "parse_nz_statement",
    "parse_pdf_statement",
    "parse_statement_text",
]
