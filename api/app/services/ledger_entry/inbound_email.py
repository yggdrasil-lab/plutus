"""Idempotent inbound email dispatch workflow for DBS alerts, receipts, payslips, and statements."""

from __future__ import annotations

from collections.abc import Callable
import json
from typing import TYPE_CHECKING, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import IngestedEmail
from app.schemas import DbsAlertPayload, InboundEmailDispatchPayload
from app.services.ai_gateway import AiGateway
from app.services.connectors import ConnectorUpstreamError
from app.services.ledger_entry._types import (
    InboundEmailDispatchOutcome,
    LedgerEntryUpstreamError,
    LedgerEntryValidationError,
)

if TYPE_CHECKING:
    from app.services.receipt_ingestion import PdfTextExtractor


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


def _dispatch_dbs_alert_email(
    db: Session,
    payload: InboundEmailDispatchPayload,
    gateway: AiGateway | None = None,
    _pdf_text_extractor: Optional[PdfTextExtractor] = None,
) -> InboundEmailDispatchOutcome:
    from app.services.connectors.dbs_email import html_to_plain_text, ingest_dbs_alert

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
    return InboundEmailDispatchOutcome(
        status="processed",
        route_target=payload.route_target,
        message_id=payload.message_id,
        duplicate=bool(dbs_res.duplicate),
        consumer_ref=consumer_ref,
        transaction_id=tx_id,
    )


def _dispatch_purchase_receipt_email(
    db: Session,
    payload: InboundEmailDispatchPayload,
    gateway: AiGateway | None = None,
    pdf_text_extractor: Optional[PdfTextExtractor] = None,
) -> InboundEmailDispatchOutcome:
    from app.services.receipt_ingestion.email_ingestion import ingest_purchase_receipt_email

    result = ingest_purchase_receipt_email(
        db,
        payload,
        ai_gateway=gateway,
        pdf_text_extractor=pdf_text_extractor,
    )
    ids = tuple(result.receipt_ids)
    consumer_ref = (
        f"receipt:{ids[0]}"
        if len(ids) == 1
        else f"receipts:{','.join(map(str, ids))}"
    )
    return InboundEmailDispatchOutcome(
        status="processed",
        route_target=payload.route_target,
        message_id=payload.message_id,
        consumer_ref=consumer_ref,
        receipt_id=ids[0] if ids else None,
        receipt_ids=ids,
    )


def _dispatch_payslip_email(
    db: Session,
    payload: InboundEmailDispatchPayload,
    gateway: AiGateway | None = None,
    pdf_text_extractor: Optional[PdfTextExtractor] = None,
) -> InboundEmailDispatchOutcome:
    from app.services.receipt_ingestion.payslip_ingestion import ingest_payslip_email

    result = ingest_payslip_email(
        db,
        payload,
        ai_gateway=gateway,
        pdf_text_extractor=pdf_text_extractor,
    )
    rid = result.receipt.id
    return InboundEmailDispatchOutcome(
        status="processed",
        route_target=payload.route_target,
        message_id=payload.message_id,
        consumer_ref=f"receipt:{rid}",
        receipt_id=rid,
        receipt_ids=(rid,),
    )


def _dispatch_bank_statement_email(
    _db: Session,
    payload: InboundEmailDispatchPayload,
    _gateway: AiGateway | None = None,
    _pdf_text_extractor: Optional[PdfTextExtractor] = None,
) -> InboundEmailDispatchOutcome:
    return InboundEmailDispatchOutcome(
        status="needs_review",
        route_target=payload.route_target,
        message_id=payload.message_id,
        consumer_ref="staged:statement",
    )


_EMAIL_ROUTE_DISPATCHERS: dict[
    str,
    Callable[
        [Session, InboundEmailDispatchPayload, AiGateway | None, Optional[PdfTextExtractor]],
        InboundEmailDispatchOutcome,
    ],
] = {
    "plutus_dbs_alert": _dispatch_dbs_alert_email,
    "plutus_purchase_receipt": _dispatch_purchase_receipt_email,
    "plutus_payslip": _dispatch_payslip_email,
    "plutus_bank_statement": _dispatch_bank_statement_email,
}


def _build_duplicate_email_result(existing: IngestedEmail) -> InboundEmailDispatchOutcome:
    """Build detached idempotent outcome from an already-processed IngestedEmail row."""
    receipt_ids: tuple[int, ...] = ()
    if existing.receipt_ids_json:
        try:
            parsed_ids = json.loads(existing.receipt_ids_json)
            if isinstance(parsed_ids, list):
                receipt_ids = tuple(int(i) for i in parsed_ids)
        except Exception:
            receipt_ids = ()
    return InboundEmailDispatchOutcome(
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
    outcome: InboundEmailDispatchOutcome,
) -> None:
    """Persist or update the IngestedEmail audit row for a processed or staged outcome."""
    receipt_ids = list(outcome.receipt_ids)
    email_record = db.scalar(
        select(IngestedEmail).where(IngestedEmail.message_id == payload.message_id)
    )
    receipts_json = json.dumps(receipt_ids) if receipt_ids else None
    consumer_ref = (outcome.consumer_ref or "")[:256]
    if email_record is None:
        db.add(
            IngestedEmail(
                message_id=payload.message_id,
                iris_email_id=payload.email_id,
                route_target=payload.route_target,
                status=outcome.status,
                consumer_ref=consumer_ref,
                transaction_id=outcome.transaction_id,
                receipt_ids_json=receipts_json,
                error_detail=None,
            )
        )
        return
    email_record.iris_email_id = payload.email_id
    email_record.route_target = payload.route_target
    email_record.status = outcome.status
    email_record.consumer_ref = consumer_ref
    email_record.transaction_id = outcome.transaction_id
    email_record.receipt_ids_json = receipts_json
    email_record.error_detail = None


def dispatch_inbound_email(
    db: Session,
    payload: InboundEmailDispatchPayload,
    *,
    ai_gateway: AiGateway | None = None,
    pdf_text_extractor: Optional[PdfTextExtractor] = None,
) -> InboundEmailDispatchOutcome:
    """Dispatch a routed inbound financial email with idempotency and failure audit state."""
    from app.services.receipt_ingestion import (
        ReceiptParseError,
        ReceiptUpstreamError,
        ReceiptValidationError,
    )

    existing = db.scalar(
        select(IngestedEmail).where(IngestedEmail.message_id == payload.message_id)
    )
    if existing is not None and existing.status != "failed":
        return _build_duplicate_email_result(existing)

    dispatcher = _EMAIL_ROUTE_DISPATCHERS.get(payload.route_target)
    if dispatcher is None:
        raise LedgerEntryValidationError(
            f"Unsupported route_target '{payload.route_target}'."
        )

    try:
        outcome = dispatcher(db, payload, ai_gateway, pdf_text_extractor)
        _upsert_processed_ingested_email(db, payload, outcome)
        db.commit()
        return outcome
    except (ValueError, ReceiptValidationError, ReceiptParseError) as exc:
        _upsert_failed_ingested_email(db, payload, str(exc))
        raise LedgerEntryValidationError(str(exc)) from exc
    except (ConnectorUpstreamError, ReceiptUpstreamError) as exc:
        _upsert_failed_ingested_email(db, payload, str(exc))
        raise LedgerEntryUpstreamError(str(exc)) from exc
