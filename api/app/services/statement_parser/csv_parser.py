"""Pure CSV parser for bank and brokerage statement exports."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
import io
import re
from typing import Optional

from app.services.statement_parser._types import ParsedTransaction, StatementParseResult
from app.services.statement_parser.categorization import (
    INSTITUTION_DEFAULT_CURRENCIES,
    auto_categorize,
    clean_merchant_name,
    detect_institution,
)

_CSV_DATE_FORMATS: tuple[str, ...] = ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y")


@dataclass(frozen=True)
class _CsvColumns:
    """Resolved column indexes for a CSV statement header."""

    date_idx: int
    amount_idx: Optional[int]
    payee_idx: int
    balance_idx: Optional[int]
    currency_idx: Optional[int]
    rate_idx: Optional[int]
    fee_idx: Optional[int]


def _find_column(col_map: dict[str, int], *candidates: str) -> Optional[int]:
    """Return the first matching column index from candidate lowercase headers."""
    for candidate in candidates:
        if candidate in col_map:
            return col_map[candidate]
    return None


def _resolve_csv_columns(col_map: dict[str, int]) -> _CsvColumns:
    """Map normalized CSV header names to concrete column indices."""
    date_cand = _find_column(col_map, "date", "transaction date", "booking date", "value date")
    payee_cand = _find_column(
        col_map,
        "payee",
        "description",
        "merchant",
        "details",
        "transaction details",
        "particulars",
    )
    return _CsvColumns(
        date_idx=date_cand if date_cand is not None else 0,
        amount_idx=_find_column(col_map, "amount", "value", "net amount", "debit/credit"),
        payee_idx=payee_cand if payee_cand is not None else 1,
        balance_idx=_find_column(col_map, "balance", "running balance", "available balance"),
        currency_idx=_find_column(
            col_map,
            "currency",
            "source currency",
            "txn currency",
            "account currency",
            "ccy",
        ),
        rate_idx=_find_column(col_map, "exchange rate", "rate", "fx rate", "conversion rate"),
        fee_idx=_find_column(
            col_map,
            "total fees",
            "fee",
            "fees",
            "total fee",
            "charge",
            "charges",
            "bank charges",
            "remittance fee",
        ),
    )


def _detect_csv_institution(
    csv_content: str,
    col_map: dict[str, int],
    institution: str,
) -> str:
    """Resolve explicit or auto-detected institution from CSV headers and preamble."""
    if institution and institution.lower() not in ("auto", "unknown"):
        return institution
    found = detect_institution(csv_content[:2000])
    if found:
        return found
    if "unique id" in col_map and "tran type" in col_map:
        return "ASB"
    if "transferwise id" in col_map or "source currency" in col_map:
        return "Wise"
    preamble_lower = csv_content[:1000].lower()
    for token, name in (("dbs", "DBS Bank"), ("bdo", "BDO Unibank"), ("bpi", "BPI")):
        if token in preamble_lower:
            return name
    return "Unknown"


def _parse_csv_date(raw_date: str) -> Optional[date]:
    """Parse supported CSV date formats or return None when unparseable."""
    cleaned = raw_date.strip()
    for fmt in _CSV_DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _parse_optional_float(row: list[str], idx: Optional[int]) -> Optional[float]:
    """Extract an optional currency-formatted float cell from a CSV row."""
    if idx is None or idx >= len(row):
        return None
    raw = row[idx].replace("$", "").replace(",", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_csv_currency(row: list[str], idx: Optional[int]) -> Optional[str]:
    """Extract an optional 3-letter ISO currency code from a CSV cell."""
    if idx is None or idx >= len(row):
        return None
    code = row[idx].strip().upper()
    return code if len(code) == 3 else None


def _parse_csv_row(
    row: list[str],
    cols: _CsvColumns,
) -> Optional[tuple[ParsedTransaction, Optional[float]]]:
    """Parse a single CSV data row into (ParsedTransaction, optional balance)."""
    if not row or len(row) <= max(cols.date_idx, cols.payee_idx):
        return None
    parsed_dt = _parse_csv_date(row[cols.date_idx])
    if parsed_dt is None:
        return None
    amt = _parse_optional_float(row, cols.amount_idx)
    if amt is None:
        return None

    payee_raw = row[cols.payee_idx].strip()
    tx = ParsedTransaction(
        transaction_date=parsed_dt,
        amount=amt,
        merchant=clean_merchant_name(payee_raw),
        raw_description=payee_raw,
        category=auto_categorize(payee_raw),
        currency=_parse_csv_currency(row, cols.currency_idx),
        exchange_rate=_parse_optional_float(row, cols.rate_idx),
        fee_amount=_parse_optional_float(row, cols.fee_idx),
    )
    return tx, _parse_optional_float(row, cols.balance_idx)


def _resolve_csv_balances(
    balance_rows: list[tuple[date, float]],
) -> tuple[Optional[float], Optional[float]]:
    """Derive (opening_balance, closing_balance) accounting for row ordering."""
    if not balance_rows:
        return None, None
    if len(balance_rows) == 1:
        bal = balance_rows[0][1]
        return bal, bal
    if balance_rows[0][0] >= balance_rows[-1][0]:
        return balance_rows[-1][1], balance_rows[0][1]
    return balance_rows[0][1], balance_rows[-1][1]


def _resolve_csv_currency(
    csv_content: str,
    detected_inst: str,
    parsed_list: list[ParsedTransaction],
) -> str:
    """Resolve statement-level currency from rows, institution defaults, or header scan."""
    if parsed_list and parsed_list[0].currency:
        return parsed_list[0].currency
    if detected_inst in INSTITUTION_DEFAULT_CURRENCIES:
        return INSTITUTION_DEFAULT_CURRENCIES[detected_inst]
    cur_match = re.search(
        r"\b(SGD|USD|AUD|NZD|PHP|EUR|GBP|CAD|JPY|HKD)\b",
        csv_content[:1500],
        re.I,
    )
    return cur_match.group(1).upper() if cur_match else "NZD"


def parse_csv_statement(csv_content: str, institution: str = "auto") -> StatementParseResult:
    """Parse standard New Zealand bank or brokerage CSV exports."""
    reader = csv.reader(io.StringIO(csv_content))
    header = next(reader, None)
    if not header:
        return StatementParseResult(institution=institution, transaction_count=0, transactions=[])

    col_map = {col.strip().lower(): idx for idx, col in enumerate(header)}
    detected_inst = _detect_csv_institution(csv_content, col_map, institution)
    cols = _resolve_csv_columns(col_map)

    parsed_list: list[ParsedTransaction] = []
    balance_rows: list[tuple[date, float]] = []
    for row in reader:
        parsed_row = _parse_csv_row(row, cols)
        if parsed_row is None:
            continue
        tx, bal = parsed_row
        parsed_list.append(tx)
        if bal is not None:
            balance_rows.append((tx.transaction_date, bal))

    opening_balance, closing_balance = _resolve_csv_balances(balance_rows)
    earliest_date = min((tx.transaction_date for tx in parsed_list), default=None)
    latest_date = max((tx.transaction_date for tx in parsed_list), default=None)
    stmt_currency = _resolve_csv_currency(csv_content, detected_inst, parsed_list)

    return StatementParseResult(
        institution=detected_inst,
        start_date=earliest_date,
        end_date=latest_date,
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        transaction_count=len(parsed_list),
        transactions=parsed_list,
        currency=stmt_currency,
    )
