"""Unit tests for Plutus statement parser and deduplication hashing."""

import sys
import unittest
from datetime import date
from pathlib import Path

# Add api directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.statement_parser import (
    auto_categorize,
    clean_merchant_name,
    generate_transaction_hash,
    parse_csv_statement,
    parse_pdf_statement,
)


class TestStatementParser(unittest.TestCase):
    """Test statement parsing, categorization heuristics, and hash generation."""

    def test_auto_categorize(self):
        """Verify common New Zealand merchants map to expected categories."""
        self.assertEqual(auto_categorize("PAK N SAVE ALBANY"), "Groceries")
        self.assertEqual(auto_categorize("Countdown Auckland"), "Groceries")
        self.assertEqual(auto_categorize("BP Connect Dairy Flat"), "Transport")
        self.assertEqual(auto_categorize("AT HOP AUTOLOAD"), "Transport")
        self.assertEqual(auto_categorize("Columbus Coffee"), "Food")
        self.assertEqual(auto_categorize("McDonald's Britomart"), "Food")
        self.assertEqual(auto_categorize("Spark New Zealand"), "Utilities")
        self.assertEqual(auto_categorize("Random Corner Dairy"), "General")

    def test_generate_transaction_hash(self):
        """Hash must be deterministic and identical for same date, amount, merchant, and account."""
        d = date(2026, 11, 4)
        hash1 = generate_transaction_hash(d, -24.50, "Flat White & Bagel", 1)
        hash2 = generate_transaction_hash(d, -24.50, "Flat White & Bagel", 1)
        hash_diff_amt = generate_transaction_hash(d, -25.00, "Flat White & Bagel", 1)

        self.assertEqual(hash1, hash2)
        self.assertNotEqual(hash1, hash_diff_amt)
        self.assertEqual(len(hash1), 64)  # SHA-256 length

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
        self.assertEqual(result.transactions[1].category, "Transport")
        self.assertEqual(result.transactions[2].category, "Food")

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
        """Verify sample statement format parses correctly."""
        from app.routers.integrations import get_sample_statement

        sample_csv = get_sample_statement(institution="ASB")
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

    def test_clean_merchant_name(self):
        """Banking transaction prefixes like AP, BP, DC, DD, VT should be stripped."""
        self.assertEqual(clean_merchant_name("AP MR & MRS E M TIENZO AUTOMATIC PAYMENT"), "MR & MRS E M TIENZO")
        self.assertEqual(clean_merchant_name("DD Auckland Transport Autoload"), "Auckland Transport Autoload")
        self.assertEqual(clean_merchant_name("VT Google One"), "Google One")
        self.assertEqual(clean_merchant_name("DC NZX LIMITED"), "NZX LIMITED")

    @unittest.mock.patch("pdfplumber.open")
    def test_parse_pdf_anz_statement_unbordered(self, mock_pdf_open):
        """Verify unbordered bank statement PDFs (like ANZ) parse transactions, balances, and period."""
        page1_text = """Statement of Accounts
Your accounts at a glance as at 16 October 2026
Accounttype Accountnumber Balance
Freedom 06-0323-0895056-00 7,574.21
Statementperiod 17 Aug 2026 - 16 Oct 2026
Date Transactiontypeanddetails Withdrawals Deposits Balance
17 Aug Openingbalance 453.61
18 Aug DC TRANSFERWISE RAYMUND TIEN 2051669430TW 300.00 753.61
19 Aug DC NZX LIMITED PAY A/C B NZX LIMITED 10,097.95 10,851.56
19 Aug AP MR & MRS E M TIENZO AUTOMATIC PAYMENT 400.00 10,451.56
26 Aug DD Auckland Transport Autoload 48386545000w 20.00 10,431.56
Totalsatendofperiod $420.00 $10,397.95 $10,431.56
Your available credit is $10,431.56 as at the closing date of this statement
ANZ Bank New Zealand Limited
"""
        mock_page = unittest.mock.MagicMock()
        mock_page.extract_text.return_value = page1_text
        mock_page.extract_tables.return_value = []
        mock_pdf = unittest.mock.MagicMock()
        mock_pdf.pages = [mock_page]
        mock_pdf_open.return_value.__enter__.return_value = mock_pdf

        result = parse_pdf_statement(b"%PDF-1.4 mock", institution="Unknown")
        self.assertEqual(result.institution, "ANZ")
        self.assertEqual(result.account_number_masked, "06-0323-0895056-00")
        self.assertEqual(result.account_name, "Freedom")
        self.assertEqual(result.opening_balance, 453.61)
        self.assertEqual(result.closing_balance, 10431.56)
        self.assertEqual(result.transaction_count, 4)
        self.assertEqual(result.transactions[0].amount, 300.00)
        self.assertEqual(result.transactions[1].amount, 10097.95)
        self.assertEqual(result.transactions[2].amount, -400.00)
        self.assertEqual(result.transactions[3].amount, -20.00)
        self.assertEqual(result.transactions[0].merchant, "TRANSFERWISE RAYMUND TIEN 2051669430TW")


if __name__ == "__main__":
    unittest.main()
