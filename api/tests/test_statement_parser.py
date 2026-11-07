"""Unit tests for Plutus statement parser and deduplication hashing."""

import sys
import unittest
from datetime import date
from pathlib import Path

# Add api directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.statement_parser import (
    auto_categorize,
    generate_transaction_hash,
    parse_csv_statement,
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
        result_sh = parse_csv_statement(csv_sharesies, institution="Unknown")
        self.assertEqual(result_sh.institution, "Sharesies")


if __name__ == "__main__":
    unittest.main()
