"""Unit tests for Plutus statement parser package, PDF extraction seam, hashing, and ISO dates."""

from datetime import date, datetime, timedelta
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Add api directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.db import Base
from app.services.connectors import parse_iso_date
from app.services.ledger_entry import (
    LedgerEntryBadRequestError,
    StatementUploadInput,
    generate_transaction_hash,
    process_statement_upload,
)
from app.services.statement_parser import (
    PdfDocumentContent,
    PdfExtractionError,
    PdfPageContent,
    auto_categorize,
    clean_merchant_name,
    extract_pdf_document,
    generate_transaction_hash as legacy_generate_transaction_hash,
    parse_csv_statement,
    parse_dbs_statement,
    parse_nz_statement,
    parse_pdf_statement,
    parse_statement_text,
)


class TestStatementParser(unittest.TestCase):
    """Test statement parsing, categorization heuristics, PDF seam, and shared primitives."""

    def test_auto_categorize(self):
        """Verify common New Zealand merchants map to expected categories."""
        self.assertEqual(auto_categorize("PAK N SAVE ALBANY"), "Groceries")
        self.assertEqual(auto_categorize("Countdown Auckland"), "Groceries")
        self.assertEqual(auto_categorize("BP Connect Dairy Flat"), "Fuel")
        self.assertEqual(auto_categorize("AT HOP AUTOLOAD"), "Transit")
        self.assertEqual(auto_categorize("Columbus Coffee"), "Dining")
        self.assertEqual(auto_categorize("McDonald's Britomart"), "Dining")
        self.assertEqual(auto_categorize("Spark New Zealand"), "Bills & Telco")
        self.assertEqual(auto_categorize("Random Corner Dairy"), "General")

    def test_generate_transaction_hash_and_reexport(self):
        """Hash from ledger_entry must be deterministic and match transitional re-export."""
        d = date(2026, 11, 4)
        hash1 = generate_transaction_hash(d, -24.50, "Flat White & Bagel", 1)
        hash2 = generate_transaction_hash(d, -24.50, "Flat White & Bagel", 1)
        hash_diff_amt = generate_transaction_hash(d, -25.00, "Flat White & Bagel", 1)

        self.assertEqual(hash1, hash2)
        self.assertNotEqual(hash1, hash_diff_amt)
        self.assertEqual(len(hash1), 64)
        self.assertEqual(hash1, legacy_generate_transaction_hash(d, -24.50, "Flat White & Bagel", 1))

    def test_parse_csv_statement(self):
        """Parse standard CSV statement lines."""
        csv_data = """Date,Payee,Amount
2026-11-01,PAK N SAVE,-150.25
2026-11-02,BP CONNECT,-85.00
2026-11-03,COLUMBUS COFFEE,-14.50
"""
        result = parse_csv_statement(csv_data, institution="ASB")
        self.assertEqual(result.transaction_count, 3)
        self.assertEqual(result.start_date, date(2026, 11, 1))
        self.assertEqual(result.end_date, date(2026, 11, 3))
        self.assertEqual(result.transactions[0].category, "Groceries")
        self.assertEqual(result.transactions[1].category, "Fuel")
        self.assertEqual(result.transactions[2].category, "Dining")

    def test_parse_csv_statement_with_balance(self):
        """Parse standard CSV statement lines with running balance column."""
        csv_data = """Date,Payee,Amount,Balance
2026-11-03,COLUMBUS COFFEE,-14.50,4900.50
2026-11-02,BP CONNECT,-85.00,4915.00
2026-11-01,PAK N SAVE,-150.25,5000.00
"""
        result = parse_csv_statement(csv_data, institution="ASB")
        self.assertEqual(result.transaction_count, 3)
        self.assertEqual(result.start_date, date(2026, 11, 1))
        self.assertEqual(result.end_date, date(2026, 11, 3))
        self.assertEqual(result.closing_balance, 4900.50)
        self.assertEqual(result.opening_balance, 5000.00)

    def test_parse_sample_statement_format(self):
        """Verify standard 8-column ASB CSV format parses correctly."""
        today = date.today()
        d1 = (today - timedelta(days=1)).strftime("%Y/%m/%d")
        d2 = (today - timedelta(days=2)).strftime("%Y/%m/%d")
        d3 = (today - timedelta(days=3)).strftime("%Y/%m/%d")
        d4 = (today - timedelta(days=4)).strftime("%Y/%m/%d")
        d5 = (today - timedelta(days=5)).strftime("%Y/%m/%d")
        sample_csv = "\n".join(
            [
                "Date,Unique Id,Tran Type,Cheque Number,Payee,Memo,Amount,Balance",
                f"{d1},1001,DEBIT,,PAK N SAVE ALBANY,Weekly Groceries,-142.50,4250.00",
                f"{d2},1002,DEBIT,,SPOTIFY AUCKLAND,Family Plan,-18.99,4392.50",
                f"{d3},1003,DEBIT,,BP CONNECT TAKAPUNA,Fuel,-85.00,4411.49",
                f"{d4},1004,DEBIT,,CAFE MIMOSA TAKAPUNA,Morning Coffee,-6.50,4496.49",
                f"{d5},1005,CREDIT,,EMPLOYER SALARY,Fortnightly Pay,2850.00,4502.99",
            ]
        )
        result = parse_csv_statement(sample_csv, institution="ASB")
        self.assertEqual(result.transaction_count, 5)
        self.assertGreater(len(result.transactions), 0)
        self.assertEqual(result.transactions[0].merchant, "PAK N SAVE ALBANY")
        self.assertEqual(result.transactions[0].category, "Groceries")
        self.assertEqual(result.transactions[0].amount, -142.50)
        self.assertEqual(result.closing_balance, 4250.00)
        self.assertEqual(result.opening_balance, 4502.99)

    def test_auto_detect_institution(self):
        """Statement parser should auto-detect institution from content if Unknown."""
        csv_asb = """Date,Unique Id,Tran Type,Cheque Number,Payee,Memo,Amount
2026-11-01,101,DEBIT,,PAK N SAVE,-150.25
"""
        result_asb = parse_csv_statement(csv_asb, institution="Unknown")
        self.assertEqual(result_asb.institution, "ASB")

        csv_sharesies = """Date,Payee,Amount
# Sharesies Portfolio Statement
2026-11-01,US500 ETF,250.00
"""
        result_sh = parse_csv_statement(csv_sharesies, institution="auto")
        self.assertEqual(result_sh.institution, "Sharesies")

        csv_anz = """Date,Details,Amount,Balance
ANZ Bank New Zealand
2026-11-01,Salary,2500.00,5000.00
"""
        result_anz = parse_csv_statement(csv_anz, institution="auto")
        self.assertEqual(result_anz.institution, "ANZ")
        self.assertEqual(result_anz.currency, "NZD")

        csv_dbs = """Date,Transaction Details,Amount,Balance
DBS Bank Singapore
2026-11-01,Salary,5000.00,10000.00
"""
        result_dbs = parse_csv_statement(csv_dbs, institution="auto")
        self.assertEqual(result_dbs.institution, "DBS Bank")
        self.assertEqual(result_dbs.currency, "SGD")

    def test_clean_merchant_name(self):
        """Banking transaction prefixes like AP, BP, DC, DD, VT should be stripped."""
        self.assertEqual(clean_merchant_name("AP ALEX TAN & JANE DOE AUTOMATIC PAYMENT"), "ALEX TAN & JANE DOE")
        self.assertEqual(clean_merchant_name("DD Auckland Transport Autoload"), "Auckland Transport Autoload")
        self.assertEqual(clean_merchant_name("VT Google One"), "Google One")
        self.assertEqual(clean_merchant_name("DC NZX LIMITED"), "NZX LIMITED")

    def test_parse_pdf_anz_statement_unbordered_via_fake_extractor(self):
        """Verify unbordered NZ bank statement PDFs parse transactions, balances, and period."""
        page1_text = """Statement of Accounts
Your accounts at a glance as at 16 October 2026
Accounttype Accountnumber Balance
Freedom 06-0100-0001001-00 7,574.21
Statementperiod 17 Aug 2026 - 16 Oct 2026
Date Transactiontypeanddetails Withdrawals Deposits Balance
17 Aug Openingbalance 453.61
18 Aug DC TRANSFERWISE ALEX TAN 2000100100TW 300.00 753.61
19 Aug DC NZX LIMITED PAY A/C B NZX LIMITED 10,097.95 10,851.56
19 Aug AP ALEX TAN & JANE DOE AUTOMATIC PAYMENT 400.00 10,451.56
26 Aug DD Auckland Transport Autoload 40001001000W 20.00 10,431.56
Totalsatendofperiod $420.00 $10,397.95 $10,431.56
Your available credit is $10,431.56 as at the closing date of this statement
ANZ Bank New Zealand Limited
"""

        def fake_extractor(_file_bytes: bytes) -> PdfDocumentContent:
            return PdfDocumentContent(pages=(PdfPageContent(text=page1_text),))

        result = parse_pdf_statement(
            b"%PDF-1.4 fake",
            institution="Unknown",
            pdf_extractor=fake_extractor,
        )
        self.assertEqual(result.institution, "ANZ")
        self.assertEqual(result.account_number_masked, "06-0100-0001001-00")
        self.assertEqual(result.account_name, "Freedom")
        self.assertEqual(result.opening_balance, 453.61)
        self.assertEqual(result.closing_balance, 10431.56)
        self.assertEqual(result.transaction_count, 4)
        self.assertEqual(result.transactions[0].amount, 300.00)
        self.assertEqual(result.transactions[1].amount, 10097.95)
        self.assertEqual(result.transactions[2].amount, -400.00)
        self.assertEqual(result.transactions[3].amount, -20.00)
        self.assertEqual(result.transactions[0].merchant, "TRANSFERWISE ALEX TAN 2000100100TW")

    def test_parse_pdf_bordered_tables_via_fake_extractor(self):
        """Verify bordered table extraction through the PdfPageContent tables seam."""
        page_text = """ASB Bank
Account number: 12-3000-0001001-00
Statement period 01 Nov 2026 - 15 Nov 2026
Opening balance: $1,000.00
Closing balance: $835.50
"""
        bordered_table = (
            ("Date", "Description", "Amount"),
            ("02 Nov 2026", "PAK N SAVE ALBANY", "-150.00"),
            ("03/11/2026", "COLUMBUS COFFEE", "-14.50"),
        )

        def fake_extractor(_file_bytes: bytes) -> PdfDocumentContent:
            return PdfDocumentContent(
                pages=(PdfPageContent(text=page_text, tables=(bordered_table,)),)
            )

        result = parse_pdf_statement(b"%PDF-1.4 table", institution="auto", pdf_extractor=fake_extractor)
        self.assertEqual(result.institution, "ASB")
        self.assertEqual(result.account_number_masked, "12-3000-0001001-00")
        self.assertEqual(result.transaction_count, 2)
        self.assertEqual(result.transactions[0].transaction_date, date(2026, 11, 2))
        self.assertEqual(result.transactions[0].amount, -150.00)
        self.assertEqual(result.transactions[0].category, "Groceries")
        self.assertEqual(result.transactions[1].transaction_date, date(2026, 11, 3))
        self.assertEqual(result.transactions[1].amount, -14.50)
        self.assertEqual(result.transactions[1].category, "Dining")

    def test_parse_nz_statement_direct_pure_input(self):
        """Verify parse_nz_statement runs directly on pure text and tables without PDF I/O."""
        res = parse_nz_statement(
            "Kiwibank\nOpening balance: $500.00\nClosing balance: $450.00\n",
            tables=((("2026-11-10", "BP CONNECT", "-50.00"),),),
            institution="Kiwibank",
        )
        self.assertEqual(res.institution, "Kiwibank")
        self.assertEqual(res.transaction_count, 1)
        self.assertEqual(res.transactions[0].category, "Fuel")
        self.assertEqual(res.currency, "NZD")

    def test_parse_dbs_consolidated_statement_pure(self):
        """Verify DBS Consolidated statement parses accounts, balances, and transactions purely."""
        page_text = """DBS Bank Ltd
Account Summary as at 31 Oct 2026
Consolidated Statement
DBS Savings Account 019-9-100101 SGD
DBS Multiplier Account 120-200202-2 SGD
Current and Savings Account Total: SGD Equivalent 300.00
DBS Savings Account Account No. 019-9-100101
Balance Brought Forward 0.00
30/09/2026 Funds Transfer 85.00 85.00
30/09/2026 Funds Transfer 85.00 0.00
DBS Multiplier Account Account No. 120-200202-2
Balance Brought Forward 0.00
19/10/2026 IB:JANE DOE 305.00 305.00
31/10/2026 Service Charge 5.00 300.00
Total Balance Carried Forward in SGD: 300.00
"""
        result = parse_statement_text(page_text, institution="auto")
        self.assertEqual(result.institution, "DBS Bank")
        self.assertEqual(result.account_name, "DBS Savings / Multiplier")
        self.assertEqual(result.account_number_masked, "019-9-100101 / 120-200202-2")
        self.assertEqual(result.currency, "SGD")
        self.assertEqual(result.closing_balance, 300.00)
        self.assertEqual(result.transaction_count, 4)
        self.assertEqual(result.transactions[0].amount, 85.00)
        self.assertEqual(result.transactions[1].amount, -85.00)
        self.assertEqual(result.transactions[2].amount, 305.00)
        self.assertEqual(result.transactions[3].amount, -5.00)

    def test_parse_dbs_cashline_statement_pure(self):
        """Verify DBS Cashline statement parses credit line, transactions, and balances purely."""
        page_text = """DBS Bank Ltd
CASHLINE ACCOUNT NO: 082-300303-8
STATEMENT PRINTED ON 20 Oct 2026
PREVIOUS BALANCE 228.42
Total Outstanding Balance S$117.78
DATE TRANSACTION ACTIVITY AMOUNT
28 Sep 2026 PAYMENT - DBS INTERNET/WIRELESS 228.42 CR
20 Oct 2026 CASHCARE PROTECTOR 0.68
002IL (24M) 5.88%PA+2%PF (20/24MTH) 107.10
20 Oct 2026 INTEREST CHARGED THIS STATEMENT 10.00
CLOSING BALANCE 117.78
"""
        result = parse_dbs_statement(page_text, detected_inst="DBS Bank")
        self.assertEqual(result.institution, "DBS Bank")
        self.assertEqual(result.account_name, "DBS Cashline")
        self.assertEqual(result.account_number_masked, "082-300303-8")
        self.assertEqual(result.currency, "SGD")
        self.assertEqual(result.opening_balance, 228.42)
        self.assertEqual(result.closing_balance, 117.78)
        self.assertEqual(result.transaction_count, 4)
        self.assertEqual(result.transactions[0].amount, 228.42)
        self.assertEqual(result.transactions[1].amount, -0.68)
        self.assertEqual(result.transactions[2].amount, -107.10)
        self.assertEqual(result.transactions[3].amount, -10.00)

    def test_parse_dbs_credit_card_statement_pure(self):
        """Verify DBS Credit Card statement parses card details and multiline transactions purely."""
        page_text = """DBS Bank Ltd
LIVE FRESH DBS VISA PAYWAVE PLATINUM CARD NO: 4111 1111 1111 1001
STATEMENT DATE 12 Oct 2026
PREVIOUS BALANCE 347.28
TOTAL: 377.26
DATE DESCRIPTION AMOUNT
28 SEP BILL PAYMENT - DBS INTERNET/WIRELESS
REF NO 12345
347.15 CR
18 SEP BASE REWARDS 0.13 CR
18 SEP ALL OTHER SPEND 0.65 CR
10 OCT SPOTIFY 17.99
11 OCT NETFLIX 23.98
"""
        result = parse_statement_text(page_text, institution="auto")
        self.assertEqual(result.institution, "DBS Bank")
        self.assertEqual(result.account_name, "Live Fresh Dbs Visa Paywave Platinum Card")
        self.assertEqual(result.account_number_masked, "4111 1111 1111 1001")
        self.assertEqual(result.currency, "SGD")
        self.assertEqual(result.opening_balance, 347.28)
        self.assertEqual(result.closing_balance, 377.26)
        self.assertEqual(result.transaction_count, 5)
        self.assertEqual(result.transactions[0].amount, 347.15)
        self.assertEqual(result.transactions[1].amount, 0.13)
        self.assertEqual(result.transactions[2].amount, 0.65)
        self.assertEqual(result.transactions[3].amount, -17.99)
        self.assertEqual(result.transactions[4].amount, -23.98)

    def test_pdf_extraction_fails_loudly(self):
        """Injected and default PDF extractors must raise PdfExtractionError on failure."""
        def failing_extractor(_file_bytes: bytes) -> PdfDocumentContent:
            raise PdfExtractionError("corrupt xref table")

        with self.assertRaises(PdfExtractionError):
            parse_pdf_statement(b"%PDF-1.4 broken", pdf_extractor=failing_extractor)

        with self.assertRaises(PdfExtractionError):
            extract_pdf_document(b"")

        with self.assertRaises(PdfExtractionError):
            extract_pdf_document(b"not-a-valid-pdf")

    def test_process_statement_upload_translates_pdf_error_to_bad_request(self):
        """Statement upload boundary must translate PdfExtractionError into LedgerEntryBadRequestError."""
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def failing_extractor(_file_bytes: bytes) -> PdfDocumentContent:
            raise PdfExtractionError("Unreadable PDF stream")

        with SessionLocal() as db:
            with self.assertRaises(LedgerEntryBadRequestError) as ctx:
                process_statement_upload(
                    db,
                    StatementUploadInput(filename="statement.pdf", content=b"bad"),
                    pdf_extractor=failing_extractor,
                )
            self.assertIn("Unreadable PDF stream", str(ctx.exception))
        Base.metadata.drop_all(bind=engine)

    def test_parse_iso_date_edge_cases(self):
        """Shared parse_iso_date handles UTC Z, offsets, naive ISO, empty, and invalid values."""
        self.assertEqual(parse_iso_date("2026-11-01T12:30:00Z"), date(2026, 11, 2))
        self.assertEqual(parse_iso_date("2026-11-01T23:30:00+00:00"), date(2026, 11, 2))
        self.assertEqual(parse_iso_date("2026-11-01T10:15:00"), date(2026, 11, 1))
        expected_today = datetime.now(ZoneInfo(settings.timezone)).date()
        self.assertEqual(parse_iso_date(None), expected_today)
        self.assertEqual(parse_iso_date(""), expected_today)
        with self.assertRaises(ValueError):
            parse_iso_date("not-an-iso-date")


if __name__ == "__main__":
    unittest.main()
