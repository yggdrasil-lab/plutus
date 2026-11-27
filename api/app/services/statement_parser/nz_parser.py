"""Pure New Zealand / generic statement text and bordered-table parser."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
import re
from typing import Optional

from app.services.statement_parser._types import ParsedTransaction, StatementParseResult
from app.services.statement_parser.categorization import (
    INSTITUTION_DEFAULT_CURRENCIES,
    MONTH_NAMES,
    auto_categorize,
    clean_merchant_name,
)

_TABLE_DATE_FORMATS: tuple[str, ...] = ("%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d")
_UNBORDERED_DATE_FORMATS: tuple[str, ...] = (
    "%d %b %Y",
    "%d %B %Y",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
)
_PERIOD_FORMATS: tuple[str, ...] = ("%d %b %Y", "%d %B %Y")
_TX_LINE_RE = re.compile(
    r"^(\d{1,2}\s+[A-Za-z]{3,9}(?:\s+\d{2,4})?|\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s+(.+?)\s+([0-9,]+\.[0-9]{2})(?:\s+([0-9,]+\.[0-9]{2}))?$"
)
_IGNORE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?:Totals\s*at\s*end|Balance\s*brought\s*forward|Opening\s*balance)", re.I),
    re.compile(r"^(?:Statement\s*of\s*Accounts|Your\s*accounts\s*at\s*a\s*glance|Today's\s*statements)", re.I),
    re.compile(r"^(?:Upcoming\s*automatic\s*payments|Other\s*account\s*balances|Freedom\s*-\s*continued)", re.I),
    re.compile(r"^(?:Account\s*(?:type|name|number)|Statement\s*number|Statement\s*period)", re.I),
    re.compile(r"^(?:Date\s+Transaction|Totalsatendof|Text|text|Page\s+\d+|PPaaggee|anz\.co\.nz|asb\.co\.nz|IPKey=)", re.I),
    re.compile(r"^(?:Payment\s*dates\s*displayed|Low\s*Rate\s*Visa)", re.I),
    re.compile(r"^(?:APAutomaticPayment|ATAutomaticTellerMachine|ANZ\s*Bank|ASB\s*Bank|Bank\s*of\s*New\s*Zealand)", re.I),
)
_NZ_CREDIT_CARD_RE = re.compile(
    r"\b(?:credit\s+card|credit\s+limit|minimum\s+payment|low\s+rate\s+visa|visa\s+platinum|mastercard|card\s+number|card\s+no\.?)\b",
    re.I,
)


def _parse_date_formats(raw: str, formats: tuple[str, ...]) -> Optional[date]:
    """Try parsing a date string across an ordered tuple of strptime formats."""
    cleaned = raw.strip()
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _parse_table_row(row: Sequence[Optional[str]]) -> Optional[ParsedTransaction]:
    """Parse a single bordered PDF table row if it starts with a valid date and amount."""
    if not row or len(row) < 3:
        return None
    tx_date = _parse_date_formats((row[0] or "").strip(), _TABLE_DATE_FORMATS)
    if tx_date is None:
        return None
    amt_raw = (row[2] or "").replace("$", "").replace(",", "").strip()
    try:
        amt = float(amt_raw)
    except (ValueError, TypeError):
        return None
    desc = (row[1] or "").strip()
    return ParsedTransaction(
        transaction_date=tx_date,
        amount=amt,
        merchant=clean_merchant_name(desc),
        raw_description=desc,
        category=auto_categorize(desc),
    )


def _parse_table_transactions(
    tables: Sequence[Sequence[Sequence[Optional[str]]]],
) -> list[ParsedTransaction]:
    """Extract normalized transactions from pre-extracted bordered PDF tables."""
    parsed: list[ParsedTransaction] = []
    for table in tables:
        for row in table:
            tx = _parse_table_row(row)
            if tx is not None:
                parsed.append(tx)
    return parsed


def _extract_nz_account_metadata(full_text: str) -> tuple[Optional[str], Optional[str]]:
    """Extract masked account number and account name from NZ statement text."""
    acc_num_match = (
        re.search(r"\b([0-9]{2}-[0-9]{4}-[0-9]{7}-[0-9]{2,3})\b", full_text)
        or re.search(r"\bAccount\s*number[:\s]+([0-9][0-9A-Za-z-]*[0-9])\b", full_text, re.I)
    )
    account_number_masked = acc_num_match.group(1).strip() if acc_num_match else None

    acc_name_match = (
        re.search(r"^([A-Za-z0-9_-]+)\nAccount\s*name", full_text, re.M)
        or re.search(r"Account\s*type\s+Account\s*number\s+Balance\s*\n\s*([A-Za-z0-9_-]+)", full_text, re.I)
    )
    account_name = acc_name_match.group(1).strip() if acc_name_match else None
    return account_number_masked, account_name


def _extract_statement_period(full_text: str) -> tuple[Optional[date], Optional[date]]:
    """Extract statement start_date and end_date from period header."""
    period_match = re.search(
        r"Statement\s*period\s+(\d{1,2}\s+[A-Za-z]{3,9}\s+(\d{4}))\s*[-\u2013\u2014to]+\s*(\d{1,2}\s+[A-Za-z]{3,9}\s+(\d{4}))",
        full_text,
        re.I,
    )
    if not period_match:
        return None, None
    start_date = _parse_date_formats(period_match.group(1), _PERIOD_FORMATS)
    end_date = _parse_date_formats(period_match.group(3), _PERIOD_FORMATS)
    return start_date, end_date


def _extract_nz_balances(full_text: str) -> tuple[Optional[float], Optional[float], bool]:
    """Extract opening balance, closing balance, and CR marker from NZ statement summary lines."""
    opening_balance: Optional[float] = None
    open_match = re.search(
        r"(?:Opening\s*balance|Balance\s*brought\s*forward)[\s:]*\$?([0-9,]+\.[0-9]{2})",
        full_text,
        re.I,
    )
    if open_match:
        opening_balance = float(open_match.group(1).replace(",", ""))

    closing_patterns = (
        re.compile(
            r"Totals\s*at\s*end\s*of\s*period\s+\$?[0-9,]+\.[0-9]{2}\s+\$?[0-9,]+\.[0-9]{2}\s+\$?([0-9,]+\.[0-9]{2})(\s*CR\b)?",
            re.I,
        ),
        re.compile(r"(?:closing|ending|new)\s*balance[\s:]*\$?([0-9,]+\.[0-9]{2})(\s*CR\b)?", re.I),
        re.compile(r"(?:total\s+amount\s+due|total\s+owing)[\s:]*\$?([0-9,]+\.[0-9]{2})(\s*CR\b)?", re.I),
    )
    closing_balance: Optional[float] = None
    closing_balance_is_credit = False
    for pattern in closing_patterns:
        match = pattern.search(full_text)
        if match:
            closing_balance = float(match.group(1).replace(",", ""))
            closing_balance_is_credit = bool(match.lastindex and match.lastindex >= 2 and match.group(2))
            break
    return opening_balance, closing_balance, closing_balance_is_credit


def _parse_unbordered_tx_date(
    date_raw: str,
    default_year: int,
    start_date: Optional[date],
    end_date: Optional[date],
) -> Optional[date]:
    """Parse explicit or yearless DD Mon dates using statement period bounds."""
    parsed = _parse_date_formats(date_raw, _UNBORDERED_DATE_FORMATS)
    if parsed is not None:
        return parsed

    parts = date_raw.split()
    if len(parts) < 2:
        return None
    try:
        day = int(parts[0])
        mon_idx = MONTH_NAMES.get(parts[1].lower()[:3], 1)
        year = default_year
        if start_date and end_date and start_date.year != end_date.year:
            year = start_date.year if mon_idx >= start_date.month else end_date.year
        return date(year, mon_idx, day)
    except (ValueError, TypeError):
        return None


def _compute_unbordered_amount(
    col1: float,
    col2_str: Optional[str],
    current_bal: Optional[float],
) -> tuple[float, Optional[float]]:
    """Infer signed amount and updated running balance for unbordered NZ lines."""
    if not col2_str:
        return -col1, current_bal
    col2 = float(col2_str.replace(",", ""))
    if current_bal is not None:
        delta = round(col2 - current_bal, 2)
        amt = delta if abs(round(abs(delta) - col1, 2)) < 0.05 else -col1
    else:
        amt = -col1
    return amt, col2


def _should_ignore_line(line: str) -> bool:
    """Return True if a statement line is a header/footer rather than continuation text."""
    return any(pattern.search(line) for pattern in _IGNORE_PATTERNS)


def _parse_unbordered_transactions(
    full_text: str,
    *,
    opening_balance: Optional[float],
    start_date: Optional[date],
    end_date: Optional[date],
) -> tuple[list[ParsedTransaction], Optional[float]]:
    """Parse line-by-line unbordered transactions and track ending balance."""
    default_year = end_date.year if end_date else datetime.now().year
    current_bal = opening_balance
    parsed: list[ParsedTransaction] = []

    for raw_line in full_text.splitlines():
        line_str = raw_line.strip()
        if not line_str:
            continue
        match = _TX_LINE_RE.match(line_str)
        if match:
            date_raw, raw_desc, col1_str, col2_str = match.groups()
            if re.search(r"opening\s*balance|balance\s*brought\s*forward", raw_desc, re.I):
                continue
            tx_date = _parse_unbordered_tx_date(date_raw, default_year, start_date, end_date)
            if tx_date is None:
                continue
            col1 = float(col1_str.replace(",", ""))
            amt, current_bal = _compute_unbordered_amount(col1, col2_str, current_bal)
            parsed.append(
                ParsedTransaction(
                    transaction_date=tx_date,
                    amount=amt,
                    merchant=clean_merchant_name(raw_desc),
                    raw_description=raw_desc,
                    category=auto_categorize(raw_desc),
                )
            )
        elif parsed and not _should_ignore_line(line_str):
            parsed[-1].raw_description += " " + line_str
            parsed[-1].category = auto_categorize(parsed[-1].raw_description)

    return parsed, current_bal


def _detect_nz_currency(full_text: str, institution: str) -> str:
    """Resolve statement currency from institution defaults or document header."""
    if institution in INSTITUTION_DEFAULT_CURRENCIES:
        return INSTITUTION_DEFAULT_CURRENCIES[institution]
    cur_match = re.search(
        r"\b(SGD|USD|AUD|NZD|PHP|EUR|GBP|CAD|JPY|HKD)\b",
        full_text[:2000],
        re.I,
    )
    return cur_match.group(1).upper() if cur_match else "NZD"


def parse_nz_statement(
    full_text: str,
    *,
    tables: Sequence[Sequence[Sequence[Optional[str]]]] = (),
    institution: str = "Unknown",
) -> StatementParseResult:
    """Parse New Zealand and generic bank statement text plus optional bordered tables."""
    account_number_masked, account_name = _extract_nz_account_metadata(full_text)
    start_date, end_date = _extract_statement_period(full_text)
    opening_balance, closing_balance, closing_balance_is_credit = _extract_nz_balances(full_text)
    is_credit_card = bool(_NZ_CREDIT_CARD_RE.search(full_text))

    parsed_transactions = _parse_table_transactions(tables)
    if not parsed_transactions:
        parsed_transactions, tracked_bal = _parse_unbordered_transactions(
            full_text,
            opening_balance=opening_balance,
            start_date=start_date,
            end_date=end_date,
        )
        if closing_balance is None and tracked_bal is not None:
            closing_balance = tracked_bal

    if is_credit_card and closing_balance is not None and closing_balance > 0.0 and not closing_balance_is_credit:
        closing_balance = -closing_balance

    if parsed_transactions:
        start_date = start_date or min(tx.transaction_date for tx in parsed_transactions)
        end_date = end_date or max(tx.transaction_date for tx in parsed_transactions)

    return StatementParseResult(
        institution=institution,
        account_number_masked=account_number_masked,
        account_name=account_name,
        account_type="credit_card" if is_credit_card else None,
        start_date=start_date,
        end_date=end_date,
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        closing_balance_is_credit=closing_balance_is_credit if is_credit_card else False,
        transaction_count=len(parsed_transactions),
        transactions=parsed_transactions,
        currency=_detect_nz_currency(full_text, institution),
    )
