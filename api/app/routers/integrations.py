"""Machine-to-machine integrations router for Shortcuts, Siri, and document ingestion."""

from dataclasses import dataclass
from datetime import date, timedelta
import json
import logging
from typing import Callable, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Account, AssetSnapshot, CategorizationRule, IngestedEmail, Transaction
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
from app.services.connectors.akahu import sync_akahu
from app.services.connectors.bitcoin import sync_bitcoin
from app.services.connectors.dbs_email import html_to_plain_text, ingest_dbs_alert
from app.services.connectors.wise import sync_wise
from app.services.ingestion_service import (
    NormalizedTx,
    ingest_normalized_transactions,
    upsert_account_snapshot,
)
from app.services.jev_client import classify_transaction_jev_with_tags, get_extra_jev_categories
from app.services.ledger_entry import (
    collect_existing_tags,
    load_hydrated_transaction,
    normalize_tags,
    strip_redundant_tags,
)
from app.services.receipt_ingestion import (
    PdfTextExtractor,
    ReceiptParseError,
    ReceiptUpstreamError,
    ReceiptValidationError,
    get_pdf_text_extractor,
    ingest_payslip_email,
    ingest_purchase_receipt_email,
    reconcile_unlinked_receipts,
)
from app.services.transfer_service import reconcile_transfers
from app.services.rules_engine import evaluate_rules
from app.services.statement_parser import (
    auto_categorize,
    generate_transaction_hash,
    parse_csv_statement,
    parse_pdf_statement,
)

router = APIRouter(prefix="/integrations", tags=["Integrations"])
logger = logging.getLogger(__name__)


def verify_integration_token(
    authorization: Optional[str] = Header(None),
    secret: Optional[str] = Query(None),
    x_authentik_username: Optional[str] = Header(None, alias="X-authentik-username"),
    x_authentik_email: Optional[str] = Header(None, alias="X-authentik-email"),
) -> bool:
    """Validate bearer token from Authorization header, secret query parameter, or Authentik SSO forwarded session."""
    if x_authentik_username or x_authentik_email:
        # Authenticated through Traefik Authentik forward-auth proxy
        return True

    valid_tokens = [
        t for t in (settings.resolved_mcp_token, settings.resolved_webhook_secret) if t
    ]
    if not valid_tokens:
        if settings.environment == "development":
            return True
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Integration token not configured",
        )

    candidate = None
    if authorization and authorization.lower().startswith("bearer "):
        candidate = authorization[7:].strip()
    elif secret:
        candidate = secret.strip()

    if candidate and candidate in valid_tokens:
        return True

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing integration token",
    )


def _resolve_quick_log_account(db: Session, account_name: Optional[str]) -> Account:
    target_account = None
    if account_name:
        target_account = db.scalar(
            select(Account).where(Account.name.ilike(f"%{account_name}%"), Account.is_active == True)
        )
    if not target_account:
        target_account = db.scalar(
            select(Account).where(Account.account_type == "checking", Account.is_active == True)
        )
    if target_account:
        return target_account
    target_account = Account(
        name="Primary Checking",
        account_type="checking",
        tier="cash",
        institution="Bank",
        currency="NZD",
        expected_annual_return=0.0,
        is_active=True,
    )
    db.add(target_account)
    db.commit()
    db.refresh(target_account)
    return target_account


def _resolve_quick_log_category_and_notes(
    db: Session,
    payload: QuickLogRequest,
    gateway: AiGateway,
) -> tuple[str, list[str], Optional[str]]:
    category = payload.category
    notes = payload.notes
    if category and category != "General":
        return category, [], notes
    jev_cat, is_anomaly, jev_tags = classify_transaction_jev_with_tags(
        payload.merchant,
        payload.amount,
        payload.notes,
        existing_tags=collect_existing_tags(db),
        extra_categories=get_extra_jev_categories(db),
        gateway=gateway,
    )
    resolved_cat = jev_cat if jev_cat and jev_cat != "General" else auto_categorize(payload.merchant)
    cleaned_tags = strip_redundant_tags(jev_tags, resolved_cat)
    if is_anomaly:
        notes = f"{notes} [Anomaly Detected]".strip() if notes else "[Anomaly Detected]"
    return resolved_cat, cleaned_tags, notes


@router.post("/quick-log", response_model=TransactionRead, status_code=status.HTTP_201_CREATED)
def quick_log_expense(
    payload: QuickLogRequest,
    db: Session = Depends(get_db),
    ai_gateway: AiGateway = Depends(get_ai_gateway),
    _authorized: bool = Depends(verify_integration_token),
):
    """Frictionless quick expense entry from iOS Shortcuts, Siri, or Apple Pay notifications."""
    target_account = _resolve_quick_log_account(db, payload.account_name)
    tx_date = payload.transaction_date or date.today()
    spend_amount = -abs(payload.amount)
    raw_hash = generate_transaction_hash(
        tx_date=tx_date, amount=spend_amount, merchant=payload.merchant, account_id=target_account.id
    )
    existing_id = db.scalar(select(Transaction.id).where(Transaction.raw_hash == raw_hash))
    if existing_id is not None:
        return load_hydrated_transaction(db, existing_id)

    category, jev_tags, notes = _resolve_quick_log_category_and_notes(db, payload, ai_gateway)
    tx = Transaction(
        transaction_date=tx_date,
        account_id=target_account.id,
        amount=spend_amount,
        currency="NZD",
        merchant=payload.merchant,
        category=category,
        raw_description=f"QuickLog: {payload.merchant}",
        raw_hash=raw_hash,
        tags=json.dumps(normalize_tags(jev_tags)),
        notes=notes,
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    reconcile_unlinked_receipts(db)
    reconcile_transfers(db)
    return load_hydrated_transaction(db, tx.id, populate_existing=True)


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
    filename = (file.filename or "").lower()

    if filename.endswith(".pdf"):
        parse_result = parse_pdf_statement(content, institution=institution)
    elif filename.endswith(".csv"):
        text_content = content.decode("utf-8", errors="replace")
        parse_result = parse_csv_statement(text_content, institution=institution)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported file format. Please upload a .pdf or .csv statement.",
        )

    if closing_balance is not None:
        parse_result.closing_balance = closing_balance
        if parse_result.end_date is None:
            parse_result.end_date = date.today()

    if currency and currency.strip():
        parse_result.currency = currency.strip().upper()

    if commit:
        # Effective institution: use caller override if specific, else use auto-detected from statement
        target_institution = (
            institution if institution and institution.lower() not in ("auto", "unknown")
            else parse_result.institution if parse_result.institution != "Unknown"
            else "Bank"
        )

        account = None
        if account_id:
            account = db.get(Account, account_id)
        if not account and target_institution:
            stmt = select(Account).where(
                Account.institution.ilike(f"%{target_institution}%"),
                Account.is_active == True,
            )
            if account_name:
                stmt = stmt.where(Account.name.ilike(f"%{account_name}%"))
            account = db.scalar(stmt)

        if not account:
            inst_lower = target_institution.lower()
            resolved_tier = tier or (
                "locked" if "simplicity" in inst_lower or "kiwi" in inst_lower
                else "growth" if any(k in inst_lower for k in ("sharesies", "kernel", "milford", "investnow", "hatch", "broker"))
                else "cash"
            )
            resolved_type = account_type or (
                "checking" if resolved_tier == "cash"
                else "brokerage" if resolved_tier == "growth"
                else "kiwisaver"
            )
            resolved_name = account_name or (
                f"{target_institution} {parse_result.account_name}" if parse_result.account_name
                else f"{target_institution} {resolved_type.capitalize()}"
            )
            expected_return = (
                0.07 if resolved_tier == "growth"
                else 0.065 if resolved_tier == "locked"
                else 0.0
            )

            account = Account(
                name=resolved_name,
                account_type=resolved_type,
                tier=resolved_tier,
                institution=target_institution,
                currency=parse_result.currency,
                expected_annual_return=expected_return,
                is_active=True,
            )
            db.add(account)
            db.commit()
            db.refresh(account)

        normalized_items = [
            NormalizedTx(
                transaction_date=ptx.transaction_date,
                amount=ptx.amount,
                currency=ptx.currency or parse_result.currency,
                merchant=ptx.merchant,
                category=ptx.category,
                raw_description=ptx.raw_description,
                exchange_rate=ptx.exchange_rate,
                fee_amount=ptx.fee_amount,
            )
            for ptx in parse_result.transactions
        ]
        ingest_normalized_transactions(db, account, normalized_items)

        if parse_result.closing_balance is not None and parse_result.end_date is not None:
            snap_source = "statement_pdf" if filename.endswith(".pdf") else "statement_csv"
            upsert_account_snapshot(
                db,
                account=account,
                snapshot_date=parse_result.end_date,
                balance=parse_result.closing_balance,
                currency=parse_result.currency,
                source=snap_source,
                notes=f"Auto-anchored from {filename}",
            )

    return parse_result


SYNC_HANDLERS: dict[
    str, Callable[[Session, int, Optional[date], Optional[date], bool], SyncResult]
] = {
    "akahu": lambda db, days, start_date, end_date, skip_llm: sync_akahu(
        db, days=days, start_date=start_date, end_date=end_date, skip_llm=skip_llm
    ),
    "wise": lambda db, days, start_date, end_date, skip_llm: sync_wise(
        db, days=days, start_date=start_date, end_date=end_date, skip_llm=skip_llm
    ),
    "bitcoin": lambda db, _days, start_date, end_date, skip_llm: sync_bitcoin(
        db, start_date=start_date, end_date=end_date, skip_llm=skip_llm
    ),
}


@router.post("/sync/{provider}", response_model=SyncResult)
def sync_external_provider(
    provider: str,
    days: int = Query(default=30, ge=1, le=90, description="Lookback window in days"),
    start_date: Optional[date] = Query(
        default=None,
        description="Optional inclusive backfill start date (YYYY-MM-DD)",
    ),
    end_date: Optional[date] = Query(
        default=None,
        description="Optional inclusive backfill end date (YYYY-MM-DD)",
    ),
    skip_llm: bool = Query(
        default=False,
        description="Skip Jev LLM categorization/tagging and use deterministic rules and heuristics",
    ),
    db: Session = Depends(get_db),
    _authorized: bool = Depends(verify_integration_token),
):
    """Trigger live sync for Akahu or Wise accounts and transactions."""
    if start_date is not None and end_date is not None and start_date > end_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start_date must be on or before end_date.",
        )

    normalized = provider.strip().lower()
    if normalized != "all" and normalized not in SYNC_HANDLERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported sync provider '{provider}'. Use 'akahu', 'wise', 'bitcoin', or 'all'.",
        )

    targets = list(SYNC_HANDLERS.values()) if normalized == "all" else [SYNC_HANDLERS[normalized]]
    try:
        results = [handler(db, days, start_date, end_date, skip_llm) for handler in targets]
    except ConnectorConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except ConnectorUpstreamError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("External sync failed for provider '%s'", normalized)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Sync failed ({type(exc).__name__}): {exc}",
        ) from exc

    if len(results) == 1:
        return results[0]

    return SyncResult(
        provider="all",
        accounts_synced=sum(r.accounts_synced for r in results),
        transactions_created=sum(r.transactions_created for r in results),
        transactions_skipped=sum(r.transactions_skipped for r in results),
        snapshots_updated=sum(r.snapshots_updated for r in results),
        accounts=[acc for r in results for acc in r.accounts],
    )


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
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except ConnectorUpstreamError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


def _upsert_failed_ingested_email(
    db: Session,
    payload: InboundEmailDispatchPayload,
    error_detail: str,
) -> None:
    """Persist failed email state cleanly after rolling back any aborted work."""
    db.rollback()
    record = db.scalar(
        select(IngestedEmail).where(IngestedEmail.message_id == payload.message_id)
    )
    if record is None:
        record = IngestedEmail(
            message_id=payload.message_id,
            iris_email_id=payload.email_id,
            route_target=payload.route_target,
            status="failed",
            error_detail=error_detail[:2000],
        )
        db.add(record)
    else:
        record.iris_email_id = payload.email_id
        record.route_target = payload.route_target
        record.status = "failed"
        record.error_detail = error_detail[:2000]
    db.commit()


@dataclass(frozen=True)
class _EmailDispatchOutcome:
    """Normalized dispatch outcome produced by one inbound email route handler."""

    status: str
    consumer_ref: str
    transaction_id: Optional[int] = None
    receipt_ids: tuple[int, ...] = ()
    duplicate: bool = False


def _dispatch_dbs_alert_email(
    db: Session,
    payload: InboundEmailDispatchPayload,
    gateway: AiGateway | None = None,
    _pdf_text_extractor: Optional[PdfTextExtractor] = None,
) -> _EmailDispatchOutcome:
    effective_text = (
        (payload.text_body or "").strip()
        or html_to_plain_text(payload.html_excerpt)
        or (payload.raw_excerpt or "").strip()
    )
    if not effective_text:
        raise ValueError("DBS alert email contains no readable body.")
    dbs_res = ingest_dbs_alert(
        db,
        DbsAlertPayload(
            subject=(payload.subject or "")[:300],
            text=effective_text,
            from_address=(payload.from_address or "")[:200] or None,
        ),
        received_at=payload.received_at,
        gateway=gateway,
    )
    tx_id = dbs_res.transaction.id if dbs_res.transaction else None
    consumer_ref = f"tx:{tx_id}" if tx_id is not None else "tx:duplicate"
    return _EmailDispatchOutcome(
        status="processed",
        consumer_ref=consumer_ref,
        transaction_id=tx_id,
        duplicate=bool(dbs_res.duplicate),
    )


def _dispatch_purchase_receipt_email(
    db: Session,
    payload: InboundEmailDispatchPayload,
    gateway: AiGateway | None = None,
    pdf_text_extractor: Optional[PdfTextExtractor] = None,
) -> _EmailDispatchOutcome:
    result = ingest_purchase_receipt_email(
        db,
        payload,
        ai_gateway=gateway,
        pdf_text_extractor=pdf_text_extractor,
    )
    ids = result.receipt_ids
    consumer_ref = (
        f"receipt:{ids[0]}"
        if len(ids) == 1
        else f"receipts:{','.join(map(str, ids))}"
    )
    return _EmailDispatchOutcome(
        status="processed",
        consumer_ref=consumer_ref,
        receipt_ids=ids,
    )


def _dispatch_payslip_email(
    db: Session,
    payload: InboundEmailDispatchPayload,
    gateway: AiGateway | None = None,
    pdf_text_extractor: Optional[PdfTextExtractor] = None,
) -> _EmailDispatchOutcome:
    result = ingest_payslip_email(
        db,
        payload,
        ai_gateway=gateway,
        pdf_text_extractor=pdf_text_extractor,
    )
    return _EmailDispatchOutcome(
        status="processed",
        consumer_ref=f"receipt:{result.receipt.id}",
        receipt_ids=(result.receipt.id,),
    )


def _dispatch_bank_statement_email(
    _db: Session,
    _payload: InboundEmailDispatchPayload,
    _gateway: AiGateway | None = None,
    _pdf_text_extractor: Optional[PdfTextExtractor] = None,
) -> _EmailDispatchOutcome:
    return _EmailDispatchOutcome(
        status="needs_review",
        consumer_ref="staged:statement",
    )


_EMAIL_ROUTE_DISPATCHERS: dict[
    str,
    Callable[
        [Session, InboundEmailDispatchPayload, AiGateway | None, Optional[PdfTextExtractor]],
        _EmailDispatchOutcome,
    ],
] = {
    "plutus_dbs_alert": _dispatch_dbs_alert_email,
    "plutus_purchase_receipt": _dispatch_purchase_receipt_email,
    "plutus_payslip": _dispatch_payslip_email,
    "plutus_bank_statement": _dispatch_bank_statement_email,
}


def _build_duplicate_email_result(existing: IngestedEmail) -> InboundEmailDispatchResult:
    """Build idempotent response from an already-processed IngestedEmail row."""
    receipt_ids: list[int] = []
    if existing.receipt_ids_json:
        try:
            parsed_ids = json.loads(existing.receipt_ids_json)
            if isinstance(parsed_ids, list):
                receipt_ids = [int(i) for i in parsed_ids]
        except Exception:
            receipt_ids = []
    return InboundEmailDispatchResult(
        status=existing.status,
        route_target=existing.route_target,
        message_id=existing.message_id,
        duplicate=True,
        consumer_ref=existing.consumer_ref,
        transaction_id=existing.transaction_id,
        receipt_id=receipt_ids[0] if receipt_ids else None,
        receipt_ids=receipt_ids,
        error_detail=existing.error_detail,
    )


def _upsert_processed_ingested_email(
    db: Session,
    payload: InboundEmailDispatchPayload,
    outcome: _EmailDispatchOutcome,
) -> list[int]:
    """Persist or update the IngestedEmail audit row and return normalized receipt IDs."""
    receipt_ids = list(outcome.receipt_ids)
    email_record = db.scalar(
        select(IngestedEmail).where(IngestedEmail.message_id == payload.message_id)
    )
    receipts_json = json.dumps(receipt_ids) if receipt_ids else None
    if email_record is None:
        db.add(
            IngestedEmail(
                message_id=payload.message_id,
                iris_email_id=payload.email_id,
                route_target=payload.route_target,
                status=outcome.status,
                consumer_ref=outcome.consumer_ref[:256],
                transaction_id=outcome.transaction_id,
                receipt_ids_json=receipts_json,
                error_detail=None,
            )
        )
    else:
        email_record.iris_email_id = payload.email_id
        email_record.route_target = payload.route_target
        email_record.status = outcome.status
        email_record.consumer_ref = outcome.consumer_ref[:256]
        email_record.transaction_id = outcome.transaction_id
        email_record.receipt_ids_json = receipts_json
        email_record.error_detail = None
    return receipt_ids


@router.post("/email", response_model=InboundEmailDispatchResult)
def receive_routed_email(
    payload: InboundEmailDispatchPayload,
    db: Session = Depends(get_db),
    ai_gateway: AiGateway = Depends(get_ai_gateway),
    pdf_text_extractor: PdfTextExtractor = Depends(get_pdf_text_extractor),
    _authorized: bool = Depends(verify_integration_token),
):
    """Unified idempotent email ingestion endpoint routed from Iris Notification Gateway."""
    existing = db.scalar(select(IngestedEmail).where(IngestedEmail.message_id == payload.message_id))
    if existing is not None and existing.status != "failed":
        return _build_duplicate_email_result(existing)

    dispatcher = _EMAIL_ROUTE_DISPATCHERS.get(payload.route_target)
    if dispatcher is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported route_target '{payload.route_target}'.",
        )
    try:
        outcome = dispatcher(db, payload, ai_gateway, pdf_text_extractor)
        receipt_ids = _upsert_processed_ingested_email(db, payload, outcome)
        db.commit()
        return InboundEmailDispatchResult(
            status=outcome.status,
            route_target=payload.route_target,
            message_id=payload.message_id,
            duplicate=outcome.duplicate,
            consumer_ref=outcome.consumer_ref,
            transaction_id=outcome.transaction_id,
            receipt_id=receipt_ids[0] if receipt_ids else None,
            receipt_ids=receipt_ids,
        )
    except (ValueError, ReceiptValidationError, ReceiptParseError) as exc:
        _upsert_failed_ingested_email(db, payload, str(exc))
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except (ConnectorUpstreamError, ReceiptUpstreamError) as exc:
        _upsert_failed_ingested_email(db, payload, str(exc))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
