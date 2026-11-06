"""Statement parser service for PDF and CSV bank and brokerage exports."""

import csv
from dataclasses import dataclass, field
import hashlib
import io
import re
from datetime import date, datetime
from typing import Optional


@dataclass
class ParsedTransaction:
    transaction_date: date
    amount: float
    merchant: str
    raw_description: str
    category: str = "General"


@dataclass
class StatementParseResult:
    institution: str
    transaction_count: int
    transactions: list[ParsedTransaction] = field(default_factory=list)
    account_number_masked: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    opening_balance: Optional[float] = None
    closing_balance: Optional[float] = None
    currency: str = "NZD"


# Common New Zealand expense categorizer patterns
CATEGORIZATION_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"pak\s*n\s*save|countdown|woolworths|new\s*world|four\s*square", re.I), "Groceries"),
    (re.compile(r"bp|z\s*energy|caltex|mobil|at\s*hop|auckland\s*transport|uber", re.I), "Transport"),
    (re.compile(r"cafe|coffee|flat\s*white|mcdonald|burger|kfc|pizza|bakery|restaurant|sushi", re.I), "Food"),
    (re.compile(r"spark|one\s*nz|vodafone|2degrees|mercury|contact\s*energy|electric\s*kiwi|watercare", re.I), "Utilities"),
    (re.compile(r"gym|fitness|physio|pharmacy|chemist|doctor|health", re.I), "Health"),
    (re.compile(r"steam|playstation|nintendo|netflix|spotify|cinema|event\s*cinemas", re.I), "Leisure"),
    (re.compile(r"salary|wages|payroll|nzx|dividend|interest", re.I), "Income"),
]


def auto_categorize(merchant_text: str) -> str:
    """Classify transaction into a budget category based on keywords."""
    for pattern, category in CATEGORIZATION_RULES:
        if pattern.search(merchant_text):
            return category
    return "General"


def generate_transaction_hash(tx_date: date, amount: float, merchant: str, account_id: int) -> str:
    """Generate deterministic SHA-256 hash for deduplicating identical statement uploads."""
    payload = f"{tx_date.isoformat()}:{amount:.2f}:{merchant.strip().lower()}:{account_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_csv_statement(csv_content: str, institution: str = "Unknown") -> StatementParseResult:
    """Parse standard New Zealand bank or brokerage CSV exports."""
    reader = csv.reader(io.StringIO(csv_content))
    header = next(reader, None)
    if not header:
        return StatementParseResult(
            institution=institution,
            transaction_count=0,
            transactions=[],
        )

    # Normalize header column lookups
    col_map = {col.strip().lower(): idx for idx, col in enumerate(header)}
    date_idx = col_map.get("date") or col_map.get("transaction date") or 0
    amount_idx = col_map.get("amount") or col_map.get("value")
    payee_idx = col_map.get("payee") or col_map.get("description") or col_map.get("merchant") or 1
    balance_idx = col_map.get("balance") or col_map.get("running balance")

    parsed_list: list[ParsedTransaction] = []
    balance_rows: list[tuple[date, float]] = []
    earliest_date: Optional[date] = None
    latest_date: Optional[date] = None

    for row in reader:
        if not row or len(row) <= max(date_idx, payee_idx):
            continue

        date_raw = row[date_idx].strip()
        payee_raw = row[payee_idx].strip()

        # Parse date formats: YYYY-MM-DD or DD/MM/YYYY
        parsed_dt: Optional[date] = None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
            try:
                parsed_dt = datetime.strptime(date_raw, fmt).date()
                break
            except ValueError:
                continue

        if not parsed_dt:
            continue

        if amount_idx is not None and amount_idx < len(row):
            amt_str = row[amount_idx].replace("$", "").replace(",", "").strip()
            try:
                amt = float(amt_str)
            except ValueError:
                continue
        else:
            continue

        if balance_idx is not None and balance_idx < len(row):
            bal_str = row[balance_idx].replace("$", "").replace(",", "").strip()
            try:
                bal = float(bal_str)
                balance_rows.append((parsed_dt, bal))
            except ValueError:
                pass

        if earliest_date is None or parsed_dt < earliest_date:
            earliest_date = parsed_dt
        if latest_date is None or parsed_dt > latest_date:
            latest_date = parsed_dt

        category = auto_categorize(payee_raw)
        parsed_list.append(
            ParsedTransaction(
                transaction_date=parsed_dt,
                amount=amt,
                merchant=payee_raw,
                raw_description=payee_raw,
                category=category,
            )
        )

    closing_balance: Optional[float] = None
    opening_balance: Optional[float] = None
    if balance_rows:
        if len(balance_rows) == 1:
            closing_balance = balance_rows[0][1]
            opening_balance = balance_rows[0][1]
        elif balance_rows[0][0] >= balance_rows[-1][0]:
            # Reverse-chronological (top row is newest)
            closing_balance = balance_rows[0][1]
            opening_balance = balance_rows[-1][1]
        else:
            # Chronological (bottom row is newest)
            closing_balance = balance_rows[-1][1]
            opening_balance = balance_rows[0][1]

    return StatementParseResult(
        institution=institution,
        start_date=earliest_date,
        end_date=latest_date,
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        transaction_count=len(parsed_list),
        transactions=parsed_list,
    )


def parse_pdf_statement(file_bytes: bytes, institution: str = "Unknown") -> StatementParseResult:
    """Parse text and tables from an official bank statement PDF."""
    try:
        import pdfplumber
    except ImportError:
        # Fallback if pdfplumber is not installed
        return StatementParseResult(
            institution=institution,
            transaction_count=0,
            transactions=[],
        )

    full_text = ""
    parsed_transactions: list[ParsedTransaction] = []
    closing_balance: Optional[float] = None
    opening_balance: Optional[float] = None

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            full_text += "\n" + text

            # Extract tables if available
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    if not row or len(row) < 3:
                        continue
                    # Match row with date in first column and amount in subsequent columns
                    col0 = (row[0] or "").strip()
                    for fmt in ("%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
                        try:
                            parsed_dt = datetime.strptime(col0, fmt).date()
                            desc = (row[1] or "").strip()
                            amt_col = (row[2] or "").replace("$", "").replace(",", "").strip()
                            amt = float(amt_col)
                            parsed_transactions.append(
                                ParsedTransaction(
                                    transaction_date=parsed_dt,
                                    amount=amt,
                                    merchant=desc,
                                    raw_description=desc,
                                    category=auto_categorize(desc),
                                )
                            )
                            break
                        except (ValueError, TypeError):
                            continue

    # Regex search for Closing Balance in full statement text
    closing_match = re.search(r"closing\s*balance[\s:]*\$?([0-9,]+\.[0-9]{2})", full_text, re.I)
    if closing_match:
        try:
            closing_balance = float(closing_match.group(1).replace(",", ""))
        except ValueError:
            pass

    opening_match = re.search(r"opening\s*balance[\s:]*\$?([0-9,]+\.[0-9]{2})", full_text, re.I)
    if opening_match:
        try:
            opening_balance = float(opening_match.group(1).replace(",", ""))
        except ValueError:
            pass

    return StatementParseResult(
        institution=institution,
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        transaction_count=len(parsed_transactions),
        transactions=parsed_transactions,
    )
