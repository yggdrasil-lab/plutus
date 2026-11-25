"""Deep Ledger Entry package for tag utilities, hydration, writes, mutations, statements, and inbound email."""

from app.services.ledger_entry._types import (
    MANUAL_WRITE_POLICY,
    MCP_EXPENSE_WRITE_POLICY,
    QUICK_LOG_WRITE_POLICY,
    UNSET,
    HydratedManualSplit,
    HydratedTransaction,
    InboundEmailDispatchOutcome,
    LedgerEntryBadRequestError,
    LedgerEntryError,
    LedgerEntryNotFoundError,
    LedgerEntryUpstreamError,
    LedgerEntryValidationError,
    ManualSplitLineInput,
    StatementUploadInput,
    StatementUploadResult,
    TransactionPatch,
    TransactionWriteInput,
    TransactionWritePolicy,
    TransactionWriteResult,
)
from app.services.ledger_entry.hashing import generate_transaction_hash
from app.services.ledger_entry.hydration import (
    hydrate_transaction,
    load_hydrated_transaction,
    transaction_load_options,
)
from app.services.ledger_entry.inbound_email import dispatch_inbound_email
from app.services.ledger_entry.mutations import (
    replace_manual_splits,
    update_transaction,
)
from app.services.ledger_entry.statements import (
    commit_parsed_statement,
    process_statement_upload,
)
from app.services.ledger_entry.tags import (
    collect_existing_tags,
    normalize_tags,
    parse_rule_tags,
    strip_redundant_tags,
)
from app.services.ledger_entry.writes import (
    record_transaction,
    resolve_default_account,
)

__all__ = [
    "HydratedManualSplit",
    "HydratedTransaction",
    "InboundEmailDispatchOutcome",
    "LedgerEntryBadRequestError",
    "LedgerEntryError",
    "LedgerEntryNotFoundError",
    "LedgerEntryUpstreamError",
    "LedgerEntryValidationError",
    "MANUAL_WRITE_POLICY",
    "MCP_EXPENSE_WRITE_POLICY",
    "ManualSplitLineInput",
    "QUICK_LOG_WRITE_POLICY",
    "StatementUploadInput",
    "StatementUploadResult",
    "TransactionPatch",
    "TransactionWriteInput",
    "TransactionWritePolicy",
    "TransactionWriteResult",
    "UNSET",
    "collect_existing_tags",
    "commit_parsed_statement",
    "dispatch_inbound_email",
    "generate_transaction_hash",
    "hydrate_transaction",
    "load_hydrated_transaction",
    "normalize_tags",
    "parse_rule_tags",
    "process_statement_upload",
    "record_transaction",
    "replace_manual_splits",
    "resolve_default_account",
    "strip_redundant_tags",
    "transaction_load_options",
    "update_transaction",
]
