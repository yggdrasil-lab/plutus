"""Import parity, cross-domain schema behavior, and dead Discord removal tests."""

from datetime import date
import importlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.schemas as schemas
from app.config import Settings
from app.schemas import (
    AccountBase,
    AccountCreate,
    AccountRead,
    AccountUpdate,
    AssetSnapshotBase,
    AssetSnapshotCreate,
    AssetSnapshotRead,
    AverageBurnReportResponse,
    BudgetRuleBase,
    BudgetRuleCreate,
    BudgetRuleRead,
    CategorizationRuleBase,
    CategorizationRuleCreate,
    CategorizationRuleRead,
    CategorizationRuleUpdate,
    CategoryBurnEstimateRead,
    CategoryCreate,
    CategoryDeleteResult,
    CategoryRead,
    CategorySpendingReport,
    CategoryUpdate,
    ClassifyUncategorizedItem,
    ClassifyUncategorizedRequest,
    ClassifyUncategorizedResponse,
    CreditBackstopSummary,
    DbsAlertPayload,
    DbsAlertResult,
    EmailAttachmentPayload,
    FxRateEntryRead,
    FxRatesRead,
    InboundEmailDispatchPayload,
    InboundEmailDispatchResult,
    InstitutionInfo,
    LoanBase,
    LoanCreate,
    LoanPaymentRead,
    LoanRead,
    LoansSummaryResponse,
    LoanUpdate,
    ParsedTransaction,
    PricePoint,
    ProductBase,
    ProductCreate,
    ProductListResponse,
    ProductRead,
    ProductStatisticItem,
    ProductStatisticsResponse,
    ProductUpdate,
    QuickLogRequest,
    RECURRENCE_CHOICES,
    REVOLVING_CREDIT_ACCOUNT_TYPES,
    ReceiptBase,
    ReceiptCreate,
    ReceiptItemBase,
    ReceiptItemCreate,
    ReceiptItemRead,
    ReceiptItemUpdate,
    ReceiptRead,
    RecurringCommitmentRead,
    RunwayAiSynthesisRead,
    RunwayResponse,
    RunwayScenarioRequest,
    RunwayScenarioResponse,
    RunwaySynthesizeRequest,
    RunwayTierBreakdown,
    SpendingBreakdownResponse,
    StatementParseResult,
    SyncAccountSummary,
    SyncResult,
    TagCategoryBreakdownItem,
    TagSpendingSummaryItem,
    TagSpendingSummaryResponse,
    TransactionBase,
    TransactionCandidateRead,
    TransactionCreate,
    TransactionRead,
    TransactionSplitInput,
    TransactionSplitRead,
    TransactionSplitsUpdate,
    TransactionUpdate,
    VALID_ACCOUNT_TIERS,
    VALID_ACCOUNT_TYPES,
    VALID_AMORTIZE_PERIODS,
    VALID_BUDGET_PERIODS,
    VALID_EMAIL_ROUTE_TARGETS,
    VALID_LOAN_TYPES,
    VALID_RECEIPT_DOCUMENT_TYPES,
    VALID_RULE_DIRECTIONS,
    normalize_account_tier,
    normalize_account_type,
    normalize_amortize_period,
    normalize_budget_period,
    normalize_loan_type,
    normalize_receipt_document_type,
    normalize_recurrence,
    normalize_rule_direction,
    parse_tags_if_string,
)
from app.schemas import (
    accounts as accounts_mod,
    budgets as budgets_mod,
    categories as categories_mod,
    fx as fx_mod,
    integrations as integrations_mod,
    loans as loans_mod,
    products as products_mod,
    receipts as receipts_mod,
    rules as rules_mod,
    runway as runway_mod,
    transactions as transactions_mod,
)


EXPECTED_PUBLIC_SYMBOLS = {
    "AccountBase",
    "AccountCreate",
    "AccountRead",
    "AccountUpdate",
    "AssetSnapshotBase",
    "AssetSnapshotCreate",
    "AssetSnapshotRead",
    "AverageBurnReportResponse",
    "BudgetRuleBase",
    "BudgetRuleCreate",
    "BudgetRuleRead",
    "CategorizationRuleBase",
    "CategorizationRuleCreate",
    "CategorizationRuleRead",
    "CategorizationRuleUpdate",
    "CategoryBurnEstimateRead",
    "CategoryCreate",
    "CategoryDeleteResult",
    "CategoryRead",
    "CategorySpendingReport",
    "CategoryUpdate",
    "ClassifyUncategorizedItem",
    "ClassifyUncategorizedRequest",
    "ClassifyUncategorizedResponse",
    "CreditBackstopSummary",
    "DbsAlertPayload",
    "DbsAlertResult",
    "EmailAttachmentPayload",
    "FxRateEntryRead",
    "FxRatesRead",
    "InboundEmailDispatchPayload",
    "InboundEmailDispatchResult",
    "InstitutionInfo",
    "LoanBase",
    "LoanCreate",
    "LoanPaymentRead",
    "LoanRead",
    "LoansSummaryResponse",
    "LoanUpdate",
    "ParsedTransaction",
    "PricePoint",
    "ProductBase",
    "ProductCreate",
    "ProductListResponse",
    "ProductRead",
    "ProductStatisticItem",
    "ProductStatisticsResponse",
    "ProductUpdate",
    "QuickLogRequest",
    "RECURRENCE_CHOICES",
    "REVOLVING_CREDIT_ACCOUNT_TYPES",
    "ReceiptBase",
    "ReceiptCreate",
    "ReceiptItemBase",
    "ReceiptItemCreate",
    "ReceiptItemRead",
    "ReceiptItemUpdate",
    "ReceiptRead",
    "RecurringCommitmentRead",
    "RunwayAiSynthesisRead",
    "RunwayResponse",
    "RunwayScenarioRequest",
    "RunwayScenarioResponse",
    "RunwaySynthesizeRequest",
    "RunwayTierBreakdown",
    "SpendingBreakdownResponse",
    "StatementParseResult",
    "SyncAccountSummary",
    "SyncResult",
    "TagCategoryBreakdownItem",
    "TagSpendingSummaryItem",
    "TagSpendingSummaryResponse",
    "TransactionBase",
    "TransactionCandidateRead",
    "TransactionCreate",
    "TransactionRead",
    "TransactionSplitInput",
    "TransactionSplitRead",
    "TransactionSplitsUpdate",
    "TransactionUpdate",
    "VALID_ACCOUNT_TIERS",
    "VALID_ACCOUNT_TYPES",
    "VALID_AMORTIZE_PERIODS",
    "VALID_BUDGET_PERIODS",
    "VALID_EMAIL_ROUTE_TARGETS",
    "VALID_LOAN_TYPES",
    "VALID_RECEIPT_DOCUMENT_TYPES",
    "VALID_RULE_DIRECTIONS",
    "normalize_account_tier",
    "normalize_account_type",
    "normalize_amortize_period",
    "normalize_budget_period",
    "normalize_loan_type",
    "normalize_receipt_document_type",
    "normalize_recurrence",
    "normalize_rule_direction",
    "parse_tags_if_string",
}

SUBMODULE_SYMBOL_MAP = {
    accounts_mod: [
        "VALID_ACCOUNT_TYPES",
        "REVOLVING_CREDIT_ACCOUNT_TYPES",
        "VALID_ACCOUNT_TIERS",
        "normalize_account_type",
        "normalize_account_tier",
        "AccountBase",
        "AccountCreate",
        "AccountUpdate",
        "AccountRead",
        "InstitutionInfo",
        "AssetSnapshotBase",
        "AssetSnapshotCreate",
        "AssetSnapshotRead",
    ],
    categories_mod: [
        "VALID_AMORTIZE_PERIODS",
        "normalize_amortize_period",
        "CategoryCreate",
        "CategoryUpdate",
        "CategoryRead",
        "CategoryDeleteResult",
    ],
    fx_mod: [
        "FxRateEntryRead",
        "FxRatesRead",
    ],
    transactions_mod: [
        "TransactionBase",
        "TransactionCreate",
        "TransactionRead",
        "TransactionUpdate",
        "QuickLogRequest",
        "TransactionSplitInput",
        "TransactionSplitsUpdate",
        "TransactionSplitRead",
        "TagCategoryBreakdownItem",
        "TagSpendingSummaryItem",
        "TagSpendingSummaryResponse",
    ],
    budgets_mod: [
        "VALID_BUDGET_PERIODS",
        "normalize_budget_period",
        "BudgetRuleBase",
        "BudgetRuleCreate",
        "BudgetRuleRead",
        "CategorySpendingReport",
        "SpendingBreakdownResponse",
        "CategoryBurnEstimateRead",
        "RecurringCommitmentRead",
        "AverageBurnReportResponse",
    ],
    rules_mod: [
        "VALID_RULE_DIRECTIONS",
        "normalize_rule_direction",
        "CategorizationRuleBase",
        "CategorizationRuleCreate",
        "CategorizationRuleUpdate",
        "CategorizationRuleRead",
        "ClassifyUncategorizedRequest",
        "ClassifyUncategorizedItem",
        "ClassifyUncategorizedResponse",
    ],
    products_mod: [
        "PricePoint",
        "ProductBase",
        "ProductCreate",
        "ProductUpdate",
        "ProductRead",
        "ProductListResponse",
    ],
    receipts_mod: [
        "VALID_RECEIPT_DOCUMENT_TYPES",
        "normalize_receipt_document_type",
        "ReceiptItemBase",
        "ReceiptItemCreate",
        "ReceiptItemUpdate",
        "ReceiptItemRead",
        "ReceiptBase",
        "ReceiptCreate",
        "ReceiptRead",
        "TransactionCandidateRead",
        "ProductStatisticItem",
        "ProductStatisticsResponse",
    ],
    runway_mod: [
        "CreditBackstopSummary",
        "RunwayTierBreakdown",
        "RunwayResponse",
        "RunwayScenarioRequest",
        "RunwayScenarioResponse",
        "RunwaySynthesizeRequest",
        "RunwayAiSynthesisRead",
    ],
    integrations_mod: [
        "SyncAccountSummary",
        "SyncResult",
        "DbsAlertPayload",
        "DbsAlertResult",
        "VALID_EMAIL_ROUTE_TARGETS",
        "EmailAttachmentPayload",
        "InboundEmailDispatchPayload",
        "InboundEmailDispatchResult",
        "ParsedTransaction",
        "StatementParseResult",
    ],
    loans_mod: [
        "VALID_LOAN_TYPES",
        "normalize_loan_type",
        "LoanBase",
        "LoanCreate",
        "LoanUpdate",
        "LoanRead",
        "LoanPaymentRead",
        "LoansSummaryResponse",
    ],
}


class TestSchemasPackageParity(unittest.TestCase):
    def test_all_public_symbols_exported_and_importable(self) -> None:
        self.assertEqual(set(schemas.__all__), EXPECTED_PUBLIC_SYMBOLS)
        for symbol in EXPECTED_PUBLIC_SYMBOLS:
            self.assertTrue(hasattr(schemas, symbol), f"Missing symbol on app.schemas: {symbol}")

    def test_submodule_exports_share_identity_with_package_root(self) -> None:
        for submodule, symbol_names in SUBMODULE_SYMBOL_MAP.items():
            for symbol_name in symbol_names:
                self.assertIs(
                    getattr(submodule, symbol_name),
                    getattr(schemas, symbol_name),
                    f"{submodule.__name__}.{symbol_name} does not match app.schemas.{symbol_name}",
                )

    def test_cross_domain_behavior_parity(self) -> None:
        tx = TransactionCreate(
            transaction_date=date(2026, 11, 1),
            account_id=1,
            amount=-24.5,
            merchant="Local Grocer",
            category="Groceries",
            tags='["food", " ", "weekly"]',
            recurrence="one_off",
        )
        self.assertEqual(tx.tags, ["food", "weekly"])
        self.assertEqual(tx.recurrence, "one-off")

        split = TransactionSplitRead(
            id=10,
            transaction_id=1,
            amount=-12.0,
            category="Groceries",
            tags="produce, pantry, ",
        )
        self.assertEqual(split.tags, ["produce", "pantry"])

        rule = CategorizationRuleCreate(
            name="Groceries rule",
            description_pattern="GROCER",
            target_category="Groceries",
            target_tags='["essential", ""]',
            target_recurrence="MONTHLY",
            direction="OUTFLOW",
        )
        self.assertEqual(rule.target_tags, ["essential"])
        self.assertEqual(rule.target_recurrence, "monthly")
        self.assertEqual(rule.direction, "outflow")

        tx_read = TransactionRead(
            id=1,
            transaction_date=date(2026, 11, 1),
            account_id=1,
            amount=-24.5,
            merchant="Local Grocer",
            category="Groceries",
            raw_hash="abc123",
            created_at="2026-11-01T00:00:00",
        )
        alert_result = DbsAlertResult(status="created", transaction=tx_read, duplicate=False)
        self.assertIsNotNone(alert_result.transaction)
        self.assertEqual(alert_result.transaction.id, 1)

        parse_result = StatementParseResult(
            institution="Test Bank",
            transaction_count=1,
            transactions=[
                ParsedTransaction(
                    transaction_date=date(2026, 11, 1),
                    amount=-15.0,
                    merchant="Coffee Shop",
                    raw_description="COFFEE SHOP POS",
                )
            ],
        )
        self.assertEqual(len(parse_result.transactions), 1)
        self.assertEqual(parse_tags_if_string("alpha, beta"), ["alpha", "beta"])
        self.assertIn("monthly", RECURRENCE_CHOICES)
        self.assertIsNone(normalize_recurrence("variable"))

    def test_dead_discord_module_and_settings_removed(self) -> None:
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("app.services.discord")

        for removed_field in ("discord_bot_token", "discord_wealth_channel_id", "discord_user_id"):
            self.assertNotIn(removed_field, Settings.model_fields)


if __name__ == "__main__":
    unittest.main()
