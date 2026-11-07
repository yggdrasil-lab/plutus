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
    currency: Optional[str] = None
    exchange_rate: Optional[float] = None
    fee_amount: Optional[float] = None


@dataclass
class StatementParseResult:
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


# Common New Zealand expense categorizer patterns
CATEGORIZATION_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"pak\s*n\s*save|countdown|woolworths|new\s*world|four\s*square|supermarket", re.I), "Groceries"),
    (re.compile(r"bp\b|z\s*energy|caltex|mobil\b|at\s*hop|auckland\s*transport|uber\b|parking|fuel", re.I), "Transport"),
    (re.compile(r"cafe|coffee|flat\s*white|mcdonald|burger|kfc|pizza|bakery|restaurant|sushi|subway|domino", re.I), "Food"),
    (re.compile(r"spark\b|one\s*nz|vodafone|2degrees|skinny\b|mercury|contact\s*energy|electric\s*kiwi|watercare|google\s*one|icloud|apple\s*bill|aws|digitalocean", re.I), "Utilities"),
    (re.compile(r"gym\b|fitness|physio|pharmacy|chemist|doctor|health|cityfitness|southern\s*cross", re.I), "Health"),
    (re.compile(r"steam\b|playstation|nintendo|netflix|spotify|cinema|event\s*cinemas|hoyts|disney|movie", re.I), "Leisure"),
    (re.compile(r"salary|wages|payroll|nzx\b|dividend|interest|ird\b|tax\s*refund|invoice|freelance|consulting|client\s*payment", re.I), "Income"),
    (re.compile(r"insurance|aa\s*insurance|iag\b|ami\b|state\s*insurance", re.I), "Insurance"),
    (re.compile(r"air\s*nz|air\s*new\s*zealand|singaporeair|jetstar|flight|airline|qantas|emirates", re.I), "Travel"),
    (
        re.compile(
            r"\b(?:wise|transferwise|investnow|sharesies|kernel|simplicity|hatch|stake|"
            r"transfer\s+to|transfer\s+from|tfr\s+to|tfr\s+from|internal\s+transfer|"
            r"internal\s+tfr|funds\s+transfer|trsf\s+to|trsf\s+from|internet\s+tfr|"
            r"acc\s+tfr|account\s+transfer|to\s+account|from\s+account|"
            r"dbs|posb|bdo|bpi|unionbank|metrobank|paynow|instapay|pesonet|"
            r"remit|remittance|wire\s+transfer|swift|crypto|bitcoin|btc|sats|"
            r"binance|easycrypto|coinbase|kraken)\b",
            re.I,
        ),
        "Transfers",
    ),
]

MONTH_NAMES: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12
}


def auto_categorize(merchant_text: str) -> str:
    """Classify transaction into a budget category based on keywords."""
    for pattern, category in CATEGORIZATION_RULES:
        if pattern.search(merchant_text):
            return category
    return "General"


def clean_merchant_name(desc: str) -> str:
    """Strip banking transaction prefixes and filler terms to expose clear merchant names."""
    cleaned = re.sub(r"^(?:AP|BP|DC|DD|VT|EP|IA|AT|ED|FX|IP|IF|CQ)\s+", "", desc.strip())
    cleaned = re.sub(
        r"\b(?:AUTOMATIC PAYMENT|BILL PAYMENT|DIRECT DEBIT|DIRECT CREDIT|DEBIT|CREDIT)\b",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or desc.strip()


def generate_transaction_hash(tx_date: date, amount: float, merchant: str, account_id: int) -> str:
    """Generate deterministic SHA-256 hash for deduplicating identical statement uploads."""
    payload = f"{tx_date.isoformat()}:{amount:.2f}:{merchant.strip().lower()}:{account_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


INSTITUTION_DETECTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bASB\s+Bank\b|\bASB\b|asb\.co\.nz", re.I), "ASB"),
    (re.compile(r"\bANZ\s+Bank\b|\bANZ\b|anz\.co\.nz", re.I), "ANZ"),
    (re.compile(r"\bBank\s+of\s+New\s+Zealand\b|\bBNZ\b|bnz\.co\.nz", re.I), "BNZ"),
    (re.compile(r"\bWestpac\b|westpac\.co\.nz", re.I), "Westpac"),
    (re.compile(r"\bKiwibank\b|kiwibank\.co\.nz", re.I), "Kiwibank"),
    (re.compile(r"\bRabobank\b", re.I), "Rabobank"),
    (re.compile(r"\bTSB\s+Bank\b|\bTSB\b", re.I), "TSB"),
    (re.compile(r"\bSharesies\b", re.I), "Sharesies"),
    (re.compile(r"\bSimplicity\b", re.I), "Simplicity"),
    (re.compile(r"\bKernel\s+Wealth\b|\bKernel\b", re.I), "Kernel"),
    (re.compile(r"\bMilford\s+Asset\s+Management\b|\bMilford\b", re.I), "Milford"),
    (re.compile(r"\bInvestNow\b|investnow\.co\.nz", re.I), "InvestNow"),
    (re.compile(r"\bInteractive\s+Brokers\b|\bIBKR\b", re.I), "Interactive Brokers"),
    (re.compile(r"\bWise\b|\bTransferWise\b|wise\.com", re.I), "Wise"),
    (re.compile(r"\bDBS\s+Bank\b|\bDBS\b|\bPOSB\b|dbs\.com|posb\.com", re.I), "DBS Bank"),
    (re.compile(r"\bBDO\s+Unibank\b|\bBDO\b|bdo\.com\.ph", re.I), "BDO Unibank"),
    (re.compile(r"\bBank\s+of\s+the\s+Philippine\s+Islands\b|\bBPI\b|bpi\.com\.ph|bpiexpressonline", re.I), "BPI"),
    (re.compile(r"\bUnionBank\b|unionbankph\.com", re.I), "UnionBank"),
    (re.compile(r"\bMetrobank\b|metrobank\.com\.ph", re.I), "Metrobank"),
    (re.compile(r"\bBitcoin\b|\bBTC\b|\bBinance\b|\bEasyCrypto\b|\bCoinbase\b|\bKraken\b", re.I), "Crypto"),
]


def detect_institution(text: str) -> Optional[str]:
    """Scan statement text for known banking and investment institution keywords."""
    for pattern, name in INSTITUTION_DETECTION_PATTERNS:
        if pattern.search(text):
            return name
    return None


def parse_csv_statement(csv_content: str, institution: str = "auto") -> StatementParseResult:
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

    detected_inst = institution
    if not detected_inst or detected_inst.lower() in ("auto", "unknown"):
        found = detect_institution(csv_content[:2000])
        if found:
            detected_inst = found
        elif "unique id" in col_map and "tran type" in col_map:
            detected_inst = "ASB"
        elif "transferwise id" in col_map or "source currency" in col_map:
            detected_inst = "Wise"
        elif "dbs" in csv_content[:1000].lower():
            detected_inst = "DBS Bank"
        elif "bdo" in csv_content[:1000].lower():
            detected_inst = "BDO Unibank"
        elif "bpi" in csv_content[:1000].lower():
            detected_inst = "BPI"
        else:
            detected_inst = "Unknown"
    def _get_col(*candidates: str) -> Optional[int]:
        for c in candidates:
            if c in col_map:
                return col_map[c]
        return None

    date_cand = _get_col("date", "transaction date", "booking date", "value date")
    date_idx = date_cand if date_cand is not None else 0
    amount_idx = _get_col("amount", "value", "net amount", "debit/credit")
    payee_cand = _get_col("payee", "description", "merchant", "details", "transaction details", "particulars")
    payee_idx = payee_cand if payee_cand is not None else 1
    balance_idx = _get_col("balance", "running balance", "available balance")
    currency_idx = _get_col("currency", "source currency", "txn currency", "account currency", "ccy")
    rate_idx = _get_col("exchange rate", "rate", "fx rate", "conversion rate")
    fee_idx = _get_col("total fees", "fee", "fees", "total fee", "charge", "charges", "bank charges", "remittance fee")

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

        row_currency = None
        if currency_idx is not None and currency_idx < len(row):
            c_cand = row[currency_idx].strip().upper()
            if len(c_cand) == 3:
                row_currency = c_cand

        row_rate = None
        if rate_idx is not None and rate_idx < len(row):
            r_str = row[rate_idx].replace("$", "").replace(",", "").strip()
            try:
                row_rate = float(r_str) if r_str else None
            except ValueError:
                pass

        row_fee = None
        if fee_idx is not None and fee_idx < len(row):
            f_str = row[fee_idx].replace("$", "").replace(",", "").strip()
            try:
                row_fee = float(f_str) if f_str else None
            except ValueError:
                pass

        category = auto_categorize(payee_raw)
        merchant = clean_merchant_name(payee_raw)
        parsed_list.append(
            ParsedTransaction(
                transaction_date=parsed_dt,
                amount=amt,
                merchant=merchant,
                raw_description=payee_raw,
                category=category,
                currency=row_currency,
                exchange_rate=row_rate,
                fee_amount=row_fee,
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

    stmt_currency = "NZD"
    if parsed_list and parsed_list[0].currency:
        stmt_currency = parsed_list[0].currency

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


def parse_pdf_statement(file_bytes: bytes, institution: str = "auto") -> StatementParseResult:
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
    account_number_masked: Optional[str] = None
    account_name: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            full_text += "\n" + page_text

            # 1. Attempt structured table extraction if bordered tables exist
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    if not row or len(row) < 3:
                        continue
                    col0 = (row[0] or "").strip()
                    for fmt in ("%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
                        try:
                            parsed_dt = datetime.strptime(col0, fmt).date()
                            desc = (row[1] or "").strip()
                            amt_col = (row[2] or "").replace("$", "").replace(",", "").strip()
                            amt = float(amt_col)
                            merchant = clean_merchant_name(desc)
                            parsed_transactions.append(
                                ParsedTransaction(
                                    transaction_date=parsed_dt,
                                    amount=amt,
                                    merchant=merchant,
                                    raw_description=desc,
                                    category=auto_categorize(desc),
                                )
                            )
                            break
                        except (ValueError, TypeError):
                            continue

    # Institution detection from full document text
    detected_inst = institution
    if not detected_inst or detected_inst.lower() in ("auto", "unknown"):
        found = detect_institution(full_text)
        detected_inst = found if found else "Unknown"

    # 2. Extract account metadata
    acc_num_match = re.search(r"\b([0-9]{2}-[0-9]{4}-[0-9]{7}-[0-9]{2,3})\b", full_text)
    if not acc_num_match:
        acc_num_match = re.search(r"\bAccount\s*number[:\s]+([0-9][0-9A-Za-z-]*[0-9])\b", full_text, re.I)
    if acc_num_match:
        account_number_masked = acc_num_match.group(1).strip()

    acc_name_match = re.search(r"^([A-Za-z0-9_-]+)\nAccount\s*name", full_text, re.M)
    if not acc_name_match:
        acc_name_match = re.search(r"Account\s*type\s+Account\s*number\s+Balance\s*\n\s*([A-Za-z0-9_-]+)", full_text, re.I)
    if acc_name_match:
        account_name = acc_name_match.group(1).strip()

    # Statement period detection
    period_match = re.search(
        r"Statement\s*period\s+(\d{1,2}\s+[A-Za-z]{3,9}\s+(\d{4}))\s*[-\u2013\u2014to]+\s*(\d{1,2}\s+[A-Za-z]{3,9}\s+(\d{4}))",
        full_text,
        re.I,
    )
    if period_match:
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                start_date = datetime.strptime(period_match.group(1), fmt).date()
                break
            except ValueError:
                pass
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                end_date = datetime.strptime(period_match.group(3), fmt).date()
                break
            except ValueError:
                pass

    default_year = end_date.year if end_date else datetime.now().year

    # Opening balance
    open_match = re.search(
        r"(?:Opening\s*balance|Balance\s*brought\s*forward)[\s:]*\$?([0-9,]+\.[0-9]{2})",
        full_text,
        re.I,
    )
    if open_match:
        try:
            opening_balance = float(open_match.group(1).replace(",", ""))
        except ValueError:
            pass

    # Closing balance detection from summary totals or available credit
    totals_match = re.search(
        r"Totals\s*at\s*end\s*of\s*period\s+\$?[0-9,]+\.[0-9]{2}\s+\$?[0-9,]+\.[0-9]{2}\s+\$?([0-9,]+\.[0-9]{2})",
        full_text,
        re.I,
    )
    if totals_match:
        try:
            closing_balance = float(totals_match.group(1).replace(",", ""))
        except ValueError:
            pass

    if closing_balance is None:
        avail_match = re.search(
            r"available\s+credit\s+is\s+\$?([0-9,]+\.[0-9]{2})\s+as\s+at\s+the\s+closing",
            full_text,
            re.I,
        )
        if avail_match:
            try:
                closing_balance = float(avail_match.group(1).replace(",", ""))
            except ValueError:
                pass

    if closing_balance is None:
        closing_match = re.search(
            r"(?:closing|ending)\s*balance[\s:]*\$?([0-9,]+\.[0-9]{2})",
            full_text,
            re.I,
        )
        if closing_match:
            try:
                closing_balance = float(closing_match.group(1).replace(",", ""))
            except ValueError:
                pass

    # 3. Fallback to line-by-line regex parsing for unbordered statements (e.g. ANZ, BNZ)
    if not parsed_transactions:
        tx_line_regex = re.compile(
            r"^(\d{1,2}\s+[A-Za-z]{3,9}(?:\s+\d{2,4})?|\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s+(.+?)\s+([0-9,]+\.[0-9]{2})(?:\s+([0-9,]+\.[0-9]{2}))?$"
        )

        ignore_patterns = [
            re.compile(r"^(?:Totals\s*at\s*end|Balance\s*brought\s*forward|Opening\s*balance)", re.I),
            re.compile(r"^(?:Statement\s*of\s*Accounts|Your\s*accounts\s*at\s*a\s*glance|Today's\s*statements)", re.I),
            re.compile(r"^(?:Upcoming\s*automatic\s*payments|Other\s*account\s*balances|Freedom\s*-\s*continued)", re.I),
            re.compile(r"^(?:Account\s*(?:type|name|number)|Statement\s*number|Statement\s*period)", re.I),
            re.compile(r"^(?:Date\s+Transaction|Totalsatendof|Text|text|Page\s+\d+|PPaaggee|anz\.co\.nz|asb\.co\.nz|IPKey=)", re.I),
            re.compile(r"^(?:Payment\s*dates\s*displayed|Low\s*Rate\s*Visa)", re.I),
            re.compile(r"^(?:APAutomaticPayment|ATAutomaticTellerMachine|ANZ\s*Bank|ASB\s*Bank|Bank\s*of\s*New\s*Zealand)", re.I),
        ]

        def should_ignore_line(line: str) -> bool:
            return any(p.search(line) for p in ignore_patterns)

        current_bal = opening_balance

        for raw_line in full_text.splitlines():
            line_str = raw_line.strip()
            if not line_str:
                continue

            m = tx_line_regex.match(line_str)
            if m:
                date_raw, raw_desc, col1_str, col2_str = m.groups()
                # Skip opening balance or brought forward summary lines
                if re.search(r"opening\s*balance|balance\s*brought\s*forward", raw_desc, re.I):
                    continue

                # Parse date
                tx_date = None
                for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
                    try:
                        tx_date = datetime.strptime(date_raw, fmt).date()
                        break
                    except ValueError:
                        continue

                # If no year in date string (e.g. '18 Aug' or '2 Sep')
                if not tx_date:
                    d_parts = date_raw.split()
                    if len(d_parts) >= 2:
                        try:
                            day = int(d_parts[0])
                            mon_idx = MONTH_NAMES.get(d_parts[1].lower()[:3], 1)
                            yr = default_year
                            if start_date and end_date and start_date.year != end_date.year:
                                yr = start_date.year if mon_idx >= start_date.month else end_date.year
                            tx_date = date(yr, mon_idx, day)
                        except (ValueError, TypeError):
                            pass

                if not tx_date:
                    continue

                col1 = float(col1_str.replace(",", ""))
                if col2_str:
                    col2 = float(col2_str.replace(",", ""))
                    # col1 is amount, col2 is running balance
                    if current_bal is not None:
                        delta = round(col2 - current_bal, 2)
                        if abs(round(abs(delta) - col1, 2)) < 0.05:
                            amt = delta
                        else:
                            amt = -col1
                    else:
                        amt = -col1
                    current_bal = col2
                else:
                    amt = -col1

                merchant = clean_merchant_name(raw_desc)
                parsed_transactions.append(
                    ParsedTransaction(
                        transaction_date=tx_date,
                        amount=amt,
                        merchant=merchant,
                        raw_description=raw_desc,
                        category=auto_categorize(raw_desc),
                    )
                )
            elif parsed_transactions and not should_ignore_line(line_str):
                # Continuation text for multiline descriptions
                parsed_transactions[-1].raw_description += " " + line_str
                # Re-categorize with enriched description
                parsed_transactions[-1].category = auto_categorize(parsed_transactions[-1].raw_description)

        if closing_balance is None and current_bal is not None:
            closing_balance = current_bal

    # Calculate dates from transactions if header lacked explicit period
    if parsed_transactions:
        if not start_date:
            start_date = min(t.transaction_date for t in parsed_transactions)
        if not end_date:
            end_date = max(t.transaction_date for t in parsed_transactions)

    return StatementParseResult(
        institution=detected_inst,
        account_number_masked=account_number_masked,
        account_name=account_name,
        start_date=start_date,
        end_date=end_date,
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        transaction_count=len(parsed_transactions),
        transactions=parsed_transactions,
    )
