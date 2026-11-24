"""Machine-to-machine integrations router for Shortcuts, Siri, and document ingestion."""

from datetime import date
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.schemas import (
    DbsAlertPayload,
    DbsAlertResult,
    InboundEmailDispatchPayload,
    InboundEmailDispatchResult,
    QuickLogRequest,
    StatementParseResult,
    SyncResult,
    TransactionRead,
)
from app.services.ai_gateway import AiGateway, get_ai_gateway
from app.services.connectors import ConnectorConfigError, ConnectorUpstreamError
from app.services.connectors.dbs_email import ingest_dbs_alert
from app.services.connectors.registry import (
    SYNC_HANDLERS,
    InvalidSyncDateRangeError,
    UnsupportedSyncProviderError,
    sync_provider,
)
from app.services.ledger_entry import (
    QUICK_LOG_WRITE_POLICY,
    LedgerEntryBadRequestError,
    LedgerEntryUpstreamError,
    LedgerEntryValidationError,
    StatementUploadInput,
    TransactionWriteInput,
    dispatch_inbound_email,
    load_hydrated_transaction,
    process_statement_upload,
    record_transaction,
)
from app.services.receipt_ingestion import PdfTextExtractor, get_pdf_text_extractor

router = APIRouter(prefix="/integrations", tags=["Integrations"])
logger = logging.getLogger(__name__)

__all__ = ["SYNC_HANDLERS", "SyncResult", "router", "verify_integration_token"]

_SYNC_ERROR_STATUS: tuple[tuple[type[Exception], int], ...] = (
    (InvalidSyncDateRangeError, status.HTTP_422_UNPROCESSABLE_ENTITY),
    (UnsupportedSyncProviderError, status.HTTP_400_BAD_REQUEST),
    (ConnectorConfigError, status.HTTP_503_SERVICE_UNAVAILABLE),
    (ConnectorUpstreamError, status.HTTP_502_BAD_GATEWAY),
)


def verify_integration_token(
    authorization: Optional[str] = Header(None),
    secret: Optional[str] = Query(None),
    x_authentik_username: Optional[str] = Header(None, alias="X-authentik-username"),
    x_authentik_email: Optional[str] = Header(None, alias="X-authentik-email"),
) -> bool:
    """Validate bearer token from Authorization header, secret query parameter, or Authentik SSO forwarded session."""
    if x_authentik_username or x_authentik_email:
        return True

    valid_tokens = [t for t in (settings.resolved_mcp_token, settings.resolved_webhook_secret) if t]
    if not valid_tokens:
        if settings.environment == "development":
            return True
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Integration token not configured")

    candidate = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else (secret.strip() if secret else None)
    if candidate and candidate in valid_tokens:
        return True
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing integration token")


@router.post("/quick-log", response_model=TransactionRead, status_code=status.HTTP_201_CREATED)
def quick_log_expense(
    payload: QuickLogRequest,
    db: Session = Depends(get_db),
    ai_gateway: AiGateway = Depends(get_ai_gateway),
    _authorized: bool = Depends(verify_integration_token),
):
    """Frictionless quick expense entry from iOS Shortcuts, Siri, or Apple Pay notifications."""
    result = record_transaction(
        db,
        TransactionWriteInput(
            transaction_date=payload.transaction_date or date.today(),
            account_name=payload.account_name,
            amount=-abs(payload.amount),
            currency=None,
            merchant=payload.merchant,
            category=payload.category or "General",
            raw_description=f"QuickLog: {payload.merchant}",
            notes=payload.notes,
        ),
        policy=QUICK_LOG_WRITE_POLICY,
        ai_gateway=ai_gateway,
    )
    return result.transaction


@router.post("/statement", response_model=StatementParseResult)
async def upload_statement(
    file: UploadFile = File(...),
    institution: str = Query(default="auto", description="Institution name, or 'auto' for automatic content detection"),
    commit: bool = Query(default=False, description="Whether to commit extracted transactions directly to database"),
    account_id: Optional[int] = Query(default=None),
    closing_balance: Optional[float] = Query(default=None, description="Optional manual closing balance override"),
    tier: Optional[str] = Query(default=None, description="Asset tier: cash, growth, or locked"),
    account_type: Optional[str] = Query(default=None, description="Account type: checking, savings, brokerage, kiwisaver"),
    account_name: Optional[str] = Query(default=None, description="Custom account name"),
    currency: Optional[str] = Query(default=None, description="Custom currency override (e.g. SGD, USD, NZD)"),
    db: Session = Depends(get_db),
    _authorized: bool = Depends(verify_integration_token),
):
    """Upload and parse an official bank statement PDF or CSV file."""
    content = await file.read()
    try:
        result = process_statement_upload(
            db,
            StatementUploadInput(
                filename=file.filename or "",
                content=content,
                institution=institution,
                commit=commit,
                account_id=account_id,
                closing_balance=closing_balance,
                tier=tier,
                account_type=account_type,
                account_name=account_name,
                currency=currency,
            ),
        )
    except LedgerEntryBadRequestError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return result.parse_result


@router.post("/sync/{provider}", response_model=SyncResult)
def sync_external_provider(
    provider: str,
    days: int = Query(default=30, ge=1, le=90, description="Lookback window in days"),
    start_date: Optional[date] = Query(default=None, description="Optional inclusive backfill start date (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(default=None, description="Optional inclusive backfill end date (YYYY-MM-DD)"),
    skip_llm: bool = Query(default=False, description="Skip Jev LLM categorization/tagging and use deterministic rules and heuristics"),
    db: Session = Depends(get_db),
    _authorized: bool = Depends(verify_integration_token),
):
    """Trigger live sync for Akahu, Wise, Bitcoin, or all configured providers."""
    try:
        return sync_provider(db, provider, days=days, start_date=start_date, end_date=end_date, skip_llm=skip_llm)
    except (InvalidSyncDateRangeError, UnsupportedSyncProviderError, ConnectorConfigError, ConnectorUpstreamError) as exc:
        status_code = next(code for exc_type, code in _SYNC_ERROR_STATUS if isinstance(exc, exc_type))
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("External sync failed for provider '%s'", provider.strip().lower())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Sync failed ({type(exc).__name__}): {exc}",
        ) from exc


@router.post("/dbs-alert", response_model=DbsAlertResult)
def receive_dbs_alert(
    payload: DbsAlertPayload,
    db: Session = Depends(get_db),
    ai_gateway: AiGateway = Depends(get_ai_gateway),
    _authorized: bool = Depends(verify_integration_token),
):
    """Ingest a forwarded DBS transaction alert email from Cloudflare Email Worker."""
    try:
        result = ingest_dbs_alert(db, payload, gateway=ai_gateway)
        if result.transaction is not None:
            hydrated = load_hydrated_transaction(db, result.transaction.id)
            result.transaction = TransactionRead.model_validate(hydrated)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except ConnectorUpstreamError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post("/email", response_model=InboundEmailDispatchResult)
def receive_routed_email(
    payload: InboundEmailDispatchPayload,
    db: Session = Depends(get_db),
    ai_gateway: AiGateway = Depends(get_ai_gateway),
    pdf_text_extractor: PdfTextExtractor = Depends(get_pdf_text_extractor),
    _authorized: bool = Depends(verify_integration_token),
):
    """Unified idempotent email ingestion endpoint routed from Iris Notification Gateway."""
    try:
        outcome = dispatch_inbound_email(db, payload, ai_gateway=ai_gateway, pdf_text_extractor=pdf_text_extractor)
    except LedgerEntryValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except LedgerEntryUpstreamError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    return InboundEmailDispatchResult(
        status=outcome.status,
        route_target=outcome.route_target,
        message_id=outcome.message_id,
        duplicate=outcome.duplicate,
        consumer_ref=outcome.consumer_ref,
        transaction_id=outcome.transaction_id,
        receipt_id=outcome.receipt_id,
        receipt_ids=list(outcome.receipt_ids),
        error_detail=outcome.error_detail,
    )
