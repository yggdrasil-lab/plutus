"""Pure DBS Bank statement parser for Cashline, Credit Card, and Consolidated exports."""

from __future__ import annotations

from datetime import date, datetime
import re
from typing import Optional

from app.services.statement_parser._types import ParsedTransaction, StatementParseResult
from app.services.statement_parser.categorization import (
    MONTH_NAMES,
    auto_categorize,
    clean_merchant_name,
)

_CASHLINE_DATED_RE = re.compile(
    r"^(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s+(.+?)\s+([0-9,]+\.[0-9]{2})(\s*CR)?$"
)
_CASHLINE_UNDATED_RE = re.compile(r"^([0-9]{3}[A-Z].+?)\s+([0-9,]+\.[0-9]{2})$")
_CASHLINE_SKIP_WORDS: tuple[str, ...] = ("PREVIOUS BALANCE", "SUB-TOTAL", "CLOSING BALANCE", "TOTAL")

_CARD_TX_RE = re.compile(r"^(\d{1,2}\s+[A-Za-z]{3})\s+(.+?)\s+([0-9,]+\.[0-9]{2})(\s*CR)?$", re.M)
_CARD_MULTILINE_RE = re.compile(r"^(\d{1,2}\s+[A-Za-z]{3})\s+(.+)$", re.M)
_CARD_SKIP_HEADERS: tuple[str, ...] = (
    "STATEMENT DATE",
    "PAYMENT DUE DATE",
    "CARD NO",
    "TAX INVOICE",
    "HOTLINE",
)
_CARD_SKIP_TX_WORDS: tuple[str, ...] = (
    "PREVIOUS BALANCE",
    "SUB-TOTAL",
    "TOTAL",
    "INSTALMENT PLANS",
    "MINIMUM PAYMENT",
)

_CONSOLIDATED_SINGLE_RE = re.compile(
    r"^(\d{2}/\d{2}/\d{4})\s+(.+?)\s+([0-9,]+\.[0-9]{2})\s+([0-9,]+\.[0-9]{2})$"
)
_CONSOLIDATED_STOP_WORDS: tuple[str, ...] = (
    "Total Balance",
    "Page ",
    "Transaction Details",
    "DBS Multiplier",
    "DBS Savings",
)


def _build_dbs_tx(
    tx_date: date,
    amount: float,
    desc: str,
    *,
    currency: str,
    raw_description: Optional[str] = None,
) -> ParsedTransaction:
    """Construct a normalized ParsedTransaction from DBS statement fields."""
    cleaned_desc = desc.strip()
    return ParsedTransaction(
        transaction_date=tx_date,
        amount=amount,
        merchant=clean_merchant_name(cleaned_desc),
        raw_description=(raw_description if raw_description is not None else cleaned_desc).strip(),
        category=auto_categorize(cleaned_desc),
        currency=currency,
    )


def _parse_d_mon_year(text: str) -> Optional[date]:
    """Parse DD Mon YYYY strings safely."""
    try:
        return datetime.strptime(text.strip(), "%d %b %Y").date()
    except ValueError:
        return None


def _extract_first_float(patterns: tuple[re.Pattern[str], ...], full_text: str) -> Optional[float]:
    """Search regex patterns sequentially and parse the first captured float amount."""
    for pattern in patterns:
        match = pattern.search(full_text)
        if match:
            return float(match.group(1).replace(",", ""))
    return None


def _extract_credit_closing_balance(
    patterns: tuple[re.Pattern[str], ...],
    full_text: str,
) -> tuple[Optional[float], bool]:
    """Extract signed closing balance for DBS credit products (negative when owed, positive when CR)."""
    for pattern in patterns:
        match = pattern.search(full_text)
        if match:
            amount = float(match.group(1).replace(",", ""))
            is_credit = bool(match.lastindex and match.lastindex >= 2 and match.group(2))
            if is_credit:
                return amount, True
            return (-amount if amount > 0.0 else 0.0), False
    return None, False


def _extract_dbs_cashline_metadata(
    full_text: str,
) -> tuple[Optional[str], Optional[date], Optional[float], Optional[float], bool]:
    """Extract account number, statement date, opening, and closing balance for Cashline."""
    acc_m = re.search(r"CASHLINE\s+ACCOUNT\s+NO[.:\s]*\n?\s*([0-9-]+)", full_text, re.I)
    if not acc_m:
        acc_m = re.search(r"\b([0-9]{3}-[0-9]{6}-[0-9])\b", full_text)
    account_number_masked = acc_m.group(1).strip() if acc_m else None

    date_m = re.search(
        r"STATEMENT\s+PRINTED\s+ON\s*\n?\s*(\d{1,2}\s+[A-Za-z]{3,9}\s+(\d{4}))",
        full_text,
        re.I,
    )
    stmt_date = _parse_d_mon_year(date_m.group(1)) if date_m else None
    opening_balance = _extract_first_float(
        (re.compile(r"PREVIOUS\s+BALANCE\s+([0-9,]+\.[0-9]{2})", re.I),),
        full_text,
    )
    closing_balance, closing_balance_is_credit = _extract_credit_closing_balance(
        (
            re.compile(
                r"Total\s+Outstanding\s+Balance\s*\n?\s*S?\$?\s*([0-9,]+\.[0-9]{2})(\s*CR\b)?",
                re.I,
            ),
            re.compile(r"CLOSING\s+BALANCE\s+([0-9,]+\.[0-9]{2})(\s*CR\b)?", re.I),
        ),
        full_text,
    )
    return account_number_masked, stmt_date, opening_balance, closing_balance, closing_balance_is_credit


def _parse_cashline_line(
    line: str,
    last_date: Optional[date],
    stmt_date: Optional[date],
    currency: str,
) -> tuple[Optional[ParsedTransaction], Optional[date]]:
    """Parse one Cashline line (dated or undated) and return updated last_date."""
    dated_m = _CASHLINE_DATED_RE.match(line)
    if dated_m:
        d_str, desc, amt_str, cr = dated_m.groups()
        if any(word in desc.upper() for word in _CASHLINE_SKIP_WORDS):
            return None, last_date
        tx_date = _parse_d_mon_year(d_str)
        if tx_date is None:
            return None, last_date
        amt = float(amt_str.replace(",", ""))
        return _build_dbs_tx(tx_date, amt if cr else -amt, desc, currency=currency), tx_date

    undated_m = _CASHLINE_UNDATED_RE.match(line)
    if undated_m:
        desc, amt_str = undated_m.groups()
        amt = -float(amt_str.replace(",", ""))
        tx_date = last_date or stmt_date or date.today()
        return _build_dbs_tx(tx_date, amt, desc, currency=currency), last_date

    return None, last_date


def _parse_dbs_cashline(full_text: str, detected_inst: str = "DBS Bank") -> StatementParseResult:
    """Parse DBS Cashline account statements."""
    currency = "SGD"
    account_number_masked, stmt_date, opening_balance, closing_balance, closing_balance_is_credit = (
        _extract_dbs_cashline_metadata(full_text)
    )
    lines = [line.strip() for line in full_text.splitlines() if line.strip()]
    parsed_transactions: list[ParsedTransaction] = []
    last_date = stmt_date

    for line in lines:
        tx, last_date = _parse_cashline_line(line, last_date, stmt_date, currency)
        if tx is not None:
            parsed_transactions.append(tx)

    return StatementParseResult(
        institution=detected_inst,
        account_number_masked=account_number_masked,
        account_name="DBS Cashline",
        account_type="credit_line",
        start_date=min((tx.transaction_date for tx in parsed_transactions), default=stmt_date),
        end_date=stmt_date or max((tx.transaction_date for tx in parsed_transactions), default=None),
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        closing_balance_is_credit=closing_balance_is_credit,
        transaction_count=len(parsed_transactions),
        transactions=parsed_transactions,
        currency=currency,
    )


def _extract_dbs_card_metadata(
    full_text: str,
) -> tuple[str, Optional[str], Optional[date], Optional[float], Optional[float], bool]:
    """Extract card name, masked card number, statement date, and balances."""
    card_name_m = re.search(r"([A-Z0-9 ]+CARD)\s+NO\.?:", full_text, re.I)
    account_name = card_name_m.group(1).strip().title() if card_name_m else "DBS Credit Card"

    card_num_m = re.search(
        r"CARD\s+NO\.?:?\s*([0-9Xx]{4}[\s-][0-9Xx]{4}[\s-][0-9Xx]{4}[\s-][0-9Xx]{4})",
        full_text,
        re.I,
    )
    if not card_num_m:
        card_num_m = re.search(r"\b([0-9]{4}[\s-][0-9]{4}[\s-][0-9]{4}[\s-][0-9]{4})\b", full_text)
    account_number_masked = card_num_m.group(1).strip() if card_num_m else None

    date_m = re.search(
        r"STATEMENT\s+DATE\s+CREDIT\s+LIMIT.*?(\d{1,2}\s+[A-Za-z]{3,9}\s+(\d{4}))",
        full_text,
        re.I | re.S,
    )
    if not date_m:
        date_m = re.search(r"STATEMENT\s+DATE.*?(\d{1,2}\s+[A-Za-z]{3,9}\s+(\d{4}))", full_text, re.I)
    if not date_m:
        date_m = re.search(r"STATEMENT\s+PRINTED\s+ON\s+(\d{1,2}\s+[A-Za-z]{3,9}\s+(\d{4}))", full_text, re.I)
    stmt_date = _parse_d_mon_year(date_m.group(1)) if date_m else None

    opening_balance = _extract_first_float(
        (re.compile(r"PREVIOUS\s+BALANCE\s+([0-9,]+\.[0-9]{2})", re.I),),
        full_text,
    )
    closing_balance, closing_balance_is_credit = _extract_credit_closing_balance(
        (
            re.compile(r"TOTAL:\s*([0-9,]+\.[0-9]{2})(\s*CR\b)?", re.I),
            re.compile(r"GRAND\s+TOTAL\s+FOR\s+ALL\s+CARD\s+ACCOUNTS:\s*([0-9,]+\.[0-9]{2})(\s*CR\b)?", re.I),
            re.compile(r"TOTAL\s+OUTSTANDING\s+BALANCE\s*\n?\s*\$?\s*([0-9,]+\.[0-9]{2})(\s*CR\b)?", re.I),
        ),
        full_text,
    )
    return account_name, account_number_masked, stmt_date, opening_balance, closing_balance, closing_balance_is_credit


def _resolve_card_tx_date(
    day_str: str,
    mon_str: str,
    year: int,
    stmt_date: Optional[date],
) -> date:
    """Resolve DD MON transaction date against statement year boundary."""
    tx_mon = MONTH_NAMES.get(mon_str.lower()[:3], 1)
    tx_year = year - 1 if (stmt_date and tx_mon > stmt_date.month) else year
    return date(tx_year, tx_mon, int(day_str))


def _consume_dbs_card_multiline(
    lines: list[str],
    start_idx: int,
    year: int,
    stmt_date: Optional[date],
    currency: str,
) -> tuple[Optional[ParsedTransaction], int, bool]:
    """Consume continuation lines for a multiline DBS card transaction."""
    match = _CARD_MULTILINE_RE.match(lines[start_idx])
    if not match or any(word in lines[start_idx].upper() for word in _CARD_SKIP_HEADERS):
        return None, start_idx + 1, False

    parts = match.group(1).split()
    if len(parts) != 2 or parts[1].lower()[:3] not in MONTH_NAMES or not parts[0].isdigit():
        return None, start_idx + 1, False

    day_str, mon_str = parts
    desc_parts = [match.group(2)]
    idx = start_idx + 1
    while idx < len(lines):
        cur = lines[idx]
        amt_m = re.match(r"^([0-9,]+\.[0-9]{2})(\s*CR)?$", cur)
        if amt_m:
            amt_str, cr = amt_m.groups()
            tx_date = _resolve_card_tx_date(day_str, mon_str, year, stmt_date)
            amt = float(amt_str.replace(",", ""))
            full_desc = " ".join(desc_parts).strip()
            tx = None
            if not any(word in full_desc.upper() for word in ("PREVIOUS BALANCE", "SUB-TOTAL", "TOTAL")):
                tx = _build_dbs_tx(tx_date, amt if cr else -amt, full_desc, currency=currency)
            return tx, idx + 1, True
        if _CARD_MULTILINE_RE.match(cur) or any(
            word in cur.upper() for word in ("NEW TRANSACTIONS", "SUB-TOTAL", "TOTAL", "INSTALMENT")
        ):
            return None, idx + 1, False
        desc_parts.append(cur)
        idx += 1
    return None, idx, False


def _parse_dbs_card_lines(
    lines: list[str],
    year: int,
    stmt_date: Optional[date],
    currency: str,
) -> list[ParsedTransaction]:
    """Parse single-line and multiline transactions from DBS credit card text."""
    parsed: list[ParsedTransaction] = []
    idx = 0
    while idx < len(lines):
        single_m = _CARD_TX_RE.match(lines[idx])
        if single_m:
            d_str, desc, amt_str, cr = single_m.groups()
            if not any(word in desc.upper() for word in _CARD_SKIP_TX_WORDS):
                day_str, mon_str = d_str.split()
                tx_date = _resolve_card_tx_date(day_str, mon_str, year, stmt_date)
                amt = float(amt_str.replace(",", ""))
                parsed.append(_build_dbs_tx(tx_date, amt if cr else -amt, desc, currency=currency))
            idx += 1
            continue

        tx, next_idx, matched = _consume_dbs_card_multiline(lines, idx, year, stmt_date, currency)
        if matched and tx is not None:
            parsed.append(tx)
        idx = next_idx
    return parsed


def _parse_dbs_card(full_text: str, detected_inst: str = "DBS Bank") -> StatementParseResult:
    """Parse DBS credit card statements."""
    currency = "SGD"
    (
        account_name,
        account_number_masked,
        stmt_date,
        opening_balance,
        closing_balance,
        closing_balance_is_credit,
    ) = _extract_dbs_card_metadata(full_text)
    year = stmt_date.year if stmt_date else datetime.now().year
    lines = [line.strip() for line in full_text.splitlines() if line.strip()]
    parsed_transactions = _parse_dbs_card_lines(lines, year, stmt_date, currency)

    return StatementParseResult(
        institution=detected_inst,
        account_number_masked=account_number_masked,
        account_name=account_name,
        account_type="credit_card",
        start_date=min((tx.transaction_date for tx in parsed_transactions), default=stmt_date),
        end_date=stmt_date or max((tx.transaction_date for tx in parsed_transactions), default=None),
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        closing_balance_is_credit=closing_balance_is_credit,
        transaction_count=len(parsed_transactions),
        transactions=parsed_transactions,
        currency=currency,
    )


def _extract_dbs_consolidated_metadata(
    full_text: str,
) -> tuple[str, Optional[str], Optional[date], Optional[float], Optional[float]]:
    """Extract currency, sub-account numbers, statement date, and balances."""
    currency = "SGD"
    cur_match = (
        re.search(r"Total\s+Balance\s+Carried\s+Forward\s+in\s+([A-Z]{3}):", full_text, re.I)
        or re.search(r"Current\s+and\s+Savings\s+Account\s+Total:\s*([A-Z]{3})\s+Equivalent", full_text, re.I)
        or re.search(r"DBS\s+(?:Savings|Multiplier|Current)\s+Account\s+[0-9-]+\s+([A-Z]{3})\b", full_text, re.I)
    )
    if cur_match:
        currency = cur_match.group(1).upper()

    acc_nums = re.findall(r"DBS\s+(?:Savings|Multiplier|Current)\s+Account\s+([0-9-]+)", full_text)
    account_number_masked = " / ".join(acc_nums) if acc_nums else None
    if account_number_masked is None:
        m_acc = re.search(r"\b([0-9]{3}-[0-9]-[0-9]{6}|[0-9]{3}-[0-9]{6}-[0-9])\b", full_text)
        account_number_masked = m_acc.group(1).strip() if m_acc else None

    date_m = (
        re.search(r"Account\s+Summary\s+as\s+(?:at|of)\s+(\d{1,2}\s+[A-Za-z]{3,9}\s+(\d{4}))", full_text, re.I)
        or re.search(r"Transaction\s+Details\s+as\s+(?:at|of)\s+(\d{1,2}\s+[A-Za-z]{3,9}\s+(\d{4}))", full_text, re.I)
    )
    stmt_date = _parse_d_mon_year(date_m.group(1)) if date_m else None
    closing_balance = _extract_first_float(
        (
            re.compile(r"Current\s+and\s+Savings\s+Account\s+Total:\s*SGD\s+Equivalent\s+([0-9,]+\.[0-9]{2})", re.I),
            re.compile(r"Total\s+Balance\s+Carried\s+Forward\s+in\s+SGD:\s*[0-9,.]+\s+[0-9,.]+\s+([0-9,]+\.[0-9]{2})", re.I),
        ),
        full_text,
    )
    opening_balance = _extract_first_float(
        (re.compile(r"Balance\s+Brought\s+Forward\s*(?:SGD)?\s*([0-9,]+\.[0-9]{2})", re.I),),
        full_text,
    )
    return currency, account_number_masked, stmt_date, opening_balance, closing_balance


def _compute_running_delta(val1: float, val2: float, current_balance: float) -> float:
    """Infer signed transaction amount from value and new running balance."""
    delta = round(val2 - current_balance, 2)
    if abs(round(abs(delta) - val1, 2)) < 0.05:
        return delta
    return val1 if val2 > current_balance else -val1


def _parse_dd_mm_yyyy(date_str: str) -> Optional[date]:
    """Parse DD/MM/YYYY date strings safely."""
    try:
        return datetime.strptime(date_str, "%d/%m/%Y").date()
    except ValueError:
        return None


def _consume_dbs_consolidated_multiline(
    lines: list[str],
    start_idx: int,
    *,
    current_subacc: str,
    current_balance: float,
    currency: str,
) -> tuple[Optional[ParsedTransaction], float, int]:
    """Parse a multiline consolidated statement entry starting with DD/MM/YYYY."""
    date_m = re.match(r"^(\d{2}/\d{2}/\d{4})\s*(.*)$", lines[start_idx])
    if not date_m:
        return None, current_balance, start_idx + 1

    tx_date = _parse_dd_mm_yyyy(date_m.group(1))
    if tx_date is None:
        return None, current_balance, start_idx + 1

    desc_parts = [date_m.group(2)] if date_m.group(2) else []
    idx = start_idx + 1
    while idx < len(lines):
        cur = lines[idx]
        amt_m = re.match(r"^([0-9,]+\.[0-9]{2})\s+([0-9,]+\.[0-9]{2})$", cur)
        if amt_m:
            val1 = float(amt_m.group(1).replace(",", ""))
            val2 = float(amt_m.group(2).replace(",", ""))
            amt = _compute_running_delta(val1, val2, current_balance)
            desc = " ".join(filter(None, desc_parts)).strip()
            raw_desc = f"{desc} [{current_subacc}]" if current_subacc else desc
            tx = _build_dbs_tx(tx_date, amt, desc, currency=currency, raw_description=raw_desc)
            return tx, val2, idx + 1
        if re.match(r"^\d{2}/\d{2}/\d{4}", cur) or any(word in cur for word in _CONSOLIDATED_STOP_WORDS):
            return None, current_balance, idx + 1
        desc_parts.append(cur)
        idx += 1
    return None, current_balance, idx


def _parse_consolidated_single_line(
    line: str,
    *,
    current_subacc: str,
    current_balance: float,
    currency: str,
) -> tuple[Optional[ParsedTransaction], float, bool]:
    """Parse a single-line consolidated entry and return (tx, new_balance, matched)."""
    single_m = _CONSOLIDATED_SINGLE_RE.match(line)
    if not single_m:
        return None, current_balance, False
    d_str, desc, val1_str, val2_str = single_m.groups()
    tx_date = _parse_dd_mm_yyyy(d_str)
    if tx_date is None:
        return None, current_balance, True
    val1, val2 = float(val1_str.replace(",", "")), float(val2_str.replace(",", ""))
    amt = _compute_running_delta(val1, val2, current_balance)
    raw_desc = f"{desc} [{current_subacc}]" if current_subacc else desc
    tx = _build_dbs_tx(tx_date, amt, desc, currency=currency, raw_description=raw_desc)
    return tx, val2, True


def _parse_dbs_consolidated_lines(lines: list[str], currency: str) -> list[ParsedTransaction]:
    """Walk consolidated DBS statement lines tracking sub-account and running balance."""
    parsed: list[ParsedTransaction] = []
    current_subacc = ""
    current_balance = 0.0
    idx = 0

    while idx < len(lines):
        line = lines[idx]
        subacc_m = re.search(r"(DBS\s+(?:Savings|Multiplier|Current)\s+Account)\s+Account\s+No\.\s+([0-9-]+)", line)
        if subacc_m:
            current_subacc = f"{subacc_m.group(1)} ({subacc_m.group(2)})"
            idx += 1
            continue

        bbf_m = re.match(r"Balance\s+Brought\s+Forward\s*(?:SGD)?\s*([0-9,]+\.[0-9]{2})", line, re.I)
        if bbf_m:
            current_balance = float(bbf_m.group(1).replace(",", ""))
            idx += 1
            continue

        tx, current_balance, matched = _parse_consolidated_single_line(
            line,
            current_subacc=current_subacc,
            current_balance=current_balance,
            currency=currency,
        )
        if matched:
            if tx is not None:
                parsed.append(tx)
            idx += 1
            continue

        tx, current_balance, idx = _consume_dbs_consolidated_multiline(
            lines,
            idx,
            current_subacc=current_subacc,
            current_balance=current_balance,
            currency=currency,
        )
        if tx is not None:
            parsed.append(tx)
    return parsed


def _parse_dbs_consolidated(full_text: str, detected_inst: str = "DBS Bank") -> StatementParseResult:
    """Parse DBS Consolidated Savings / Multiplier statements."""
    currency, account_number_masked, stmt_date, opening_balance, closing_balance = (
        _extract_dbs_consolidated_metadata(full_text)
    )
    lines = [line.strip() for line in full_text.splitlines() if line.strip()]
    parsed_transactions = _parse_dbs_consolidated_lines(lines, currency)

    return StatementParseResult(
        institution=detected_inst,
        account_number_masked=account_number_masked,
        account_name="DBS Savings / Multiplier",
        start_date=min((tx.transaction_date for tx in parsed_transactions), default=stmt_date),
        end_date=stmt_date or max((tx.transaction_date for tx in parsed_transactions), default=None),
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        transaction_count=len(parsed_transactions),
        transactions=parsed_transactions,
        currency=currency,
    )


def parse_dbs_statement(full_text: str, detected_inst: str = "DBS Bank") -> StatementParseResult:
    """Dispatch DBS Bank statement text to Cashline, Credit Card, or Consolidated parsers."""
    is_cashline = bool(re.search(r"CASHLINE\s+ACCOUNT\s+NO|\bCASHLINE\b", full_text, re.I))
    is_card = bool(
        re.search(
            r"CARD\s+NO\.?:|CREDIT\s+CARD|TOTAL\s+OUTSTANDING\s+BALANCE|GRAND\s+TOTAL\s+FOR\s+ALL\s+CARD|LIVE\s+FRESH",
            full_text,
            re.I,
        )
    ) and not is_cashline

    if is_cashline:
        return _parse_dbs_cashline(full_text, detected_inst)
    if is_card:
        return _parse_dbs_card(full_text, detected_inst)
    return _parse_dbs_consolidated(full_text, detected_inst)
