"""External connector, inbound email dispatch, and statement ingestion schemas."""

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel, Field, field_validator

from .transactions import TransactionRead

if TYPE_CHECKING:
    from app.services.statement_parser import ParsedTransaction, StatementParseResult


class SyncAccountSummary(BaseModel):
    account_id: int
    account_name: str
    institution: str
    currency: str
    balance: Optional[float] = None
    transactions_created: int = 0
    transactions_skipped: int = 0


class SyncResult(BaseModel):
    provider: str
    accounts_synced: int = 0
    transactions_created: int = 0
    transactions_skipped: int = 0
    snapshots_updated: int = 0
    accounts: list[SyncAccountSummary] = Field(default_factory=list)


class DbsAlertPayload(BaseModel):
    """Inbound email alert payload forwarded from Cloudflare Email Worker."""

    subject: str = Field(..., max_length=300)
    text: str = Field(..., min_length=1)
    from_address: Optional[str] = Field(default=None, max_length=200)
    account_name: Optional[str] = Field(default=None, max_length=100)


class DbsAlertResult(BaseModel):
    status: str
    transaction: Optional[TransactionRead] = None
    duplicate: bool = False


VALID_EMAIL_ROUTE_TARGETS = {
    "plutus_dbs_alert",
    "plutus_purchase_receipt",
    "plutus_payslip",
    "plutus_bank_statement",
}


class EmailAttachmentPayload(BaseModel):
    filename: Optional[str] = Field(default=None, max_length=255)
    content_type: Optional[str] = Field(default=None, max_length=128)
    size_bytes: Optional[int] = Field(default=None, ge=0)
    sha256: Optional[str] = Field(default=None, max_length=128)
    content_base64: Optional[str] = None


class InboundEmailDispatchPayload(BaseModel):
    """Unified inbound financial email payload routed from Iris."""

    email_id: Optional[int] = None
    message_id: str = Field(..., min_length=1, max_length=512)
    route_target: str = Field(..., min_length=1, max_length=64)
    from_address: Optional[str] = Field(default=None, max_length=255)
    to_address: Optional[str] = Field(default=None, max_length=255)
    subject: Optional[str] = Field(default="", max_length=500)
    received_at: Optional[datetime] = None
    text_body: Optional[str] = None
    html_excerpt: Optional[str] = None
    raw_excerpt: Optional[str] = None
    jev_verdict: Optional[Any] = None
    attachments: list[EmailAttachmentPayload] = Field(default_factory=list)

    @field_validator("message_id", mode="before")
    @classmethod
    def validate_message_id(cls, v: Any) -> str:
        cleaned = str(v or "").strip()
        if not cleaned:
            raise ValueError("message_id is required.")
        return cleaned

    @field_validator("route_target", mode="before")
    @classmethod
    def validate_route_target(cls, v: Any) -> str:
        cleaned = str(v or "").strip().lower()
        if cleaned not in VALID_EMAIL_ROUTE_TARGETS:
            raise ValueError(
                f"Unsupported route_target '{v}'. Must be one of: {', '.join(sorted(VALID_EMAIL_ROUTE_TARGETS))}."
            )
        return cleaned


class InboundEmailDispatchResult(BaseModel):
    status: str
    route_target: str
    message_id: str
    duplicate: bool = False
    consumer_ref: Optional[str] = None
    transaction_id: Optional[int] = None
    receipt_id: Optional[int] = None
    receipt_ids: list[int] = Field(default_factory=list)
    error_detail: Optional[str] = None


__all__ = [
    "DbsAlertPayload",
    "DbsAlertResult",
    "EmailAttachmentPayload",
    "InboundEmailDispatchPayload",
    "InboundEmailDispatchResult",
    "ParsedTransaction",
    "StatementParseResult",
    "SyncAccountSummary",
    "SyncResult",
    "VALID_EMAIL_ROUTE_TARGETS",
]


def __getattr__(name: str) -> Any:
    """Lazily resolve statement parser re-exports to prevent circular imports at package init."""
    if name in ("ParsedTransaction", "StatementParseResult"):
        from app.services.statement_parser import ParsedTransaction, StatementParseResult

        return ParsedTransaction if name == "ParsedTransaction" else StatementParseResult
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
