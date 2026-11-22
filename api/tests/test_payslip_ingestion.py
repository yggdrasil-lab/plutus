"""Tests for payslip email ingestion, PDF archival, inflow-only matching, and projection isolation."""

import base64
from collections.abc import Iterator
from datetime import date, timedelta
import io
import json
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.models import Account, IngestedEmail, Product, Receipt, ReceiptItem, Transaction
from app.services.category_taxonomy import ensure_default_categories
from app.services.receipt_service import (
    MIN_PAYSLIP_PDF_BYTES,
    PAYSLIP_PARSING_PROMPT,
    parse_payslip_text_with_llm,
    parse_payslip_with_llm,
    reconcile_unlinked_receipts,
)
from app.services.spending_projection import (
    ReportingWindow,
    expand_expense_lines,
    project_window,
)


class _FakeLlmResponse:
    """Minimal context-manager mock for urllib.request.urlopen LLM calls."""

    def __init__(self, content_obj: dict):
        self._body = json.dumps(
            {"choices": [{"message": {"content": json.dumps(content_obj)}}]}
        ).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeLlmResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def _make_synthetic_pdf_bytes(size: int = 1024) -> bytes:
    header = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    padding = b"%" + (b"A" * max(0, size - len(header) - 1))
    return header + padding


def _make_large_png_bytes(width: int = 256, height: int = 256) -> bytes:
    import random

    img = Image.new("RGB", (width, height), color=(240, 240, 240))
    rng = random.Random(7)
    px = img.load()
    for x in range(width):
        for y in range(0, height, 2):
            px[x, y] = (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def memory_db() -> Iterator[Session]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = testing_session_local()
    ensure_default_categories(db)
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client(memory_db: Session) -> Iterator[TestClient]:
    def _override_get_db() -> Iterator[Session]:
        yield memory_db

    orig_webhook = settings.webhook_secret
    orig_env = settings.environment
    orig_llm_key = settings.llm_api_key
    settings.environment = "development"
    settings.llm_api_key = "test-llm-key"
    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as tc:
        yield tc
    app.dependency_overrides.clear()
    settings.webhook_secret = orig_webhook
    settings.environment = orig_env
    settings.llm_api_key = orig_llm_key


def _seed_checking_account(db: Session) -> Account:
    acct = Account(
        name="ANZ Checking",
        account_type="checking",
        tier="cash",
        institution="ANZ",
        currency="NZD",
    )
    db.add(acct)
    db.commit()
    db.refresh(acct)
    return acct


def test_payslip_email_pdf_ingests_skips_items_matches_inflow_and_hydrates_tx(
    memory_db: Session,
    client: TestClient,
) -> None:
    """POST /integrations/email plutus_payslip archives PDF, creates 0 items/products, matches +inflow only."""
    acct = _seed_checking_account(memory_db)
    pay_day = date(2026, 11, 15)

    tx_inflow = Transaction(
        transaction_date=pay_day,
        account_id=acct.id,
        amount=4250.50,
        currency="NZD",
        merchant="ACME CORP PAYROLL",
        category="Income",
        raw_description="DIRECT CREDIT ACME CORP SALARY",
        raw_hash="inflow-acme-4250",
    )
    tx_outflow_same_amt = Transaction(
        transaction_date=pay_day,
        account_id=acct.id,
        amount=-4250.50,
        currency="NZD",
        merchant="Acme Corp",
        category="Shopping",
        raw_description="OUTFLOW SAME AMOUNT MUST BE IGNORED",
        raw_hash="outflow-acme-4250",
    )
    memory_db.add_all([tx_inflow, tx_outflow_same_amt])
    memory_db.commit()
    memory_db.refresh(tx_inflow)
    memory_db.refresh(tx_outflow_same_amt)

    pdf_bytes = _make_synthetic_pdf_bytes(1024)
    assert len(pdf_bytes) >= MIN_PAYSLIP_PDF_BYTES
    assert len(pdf_bytes) < 15 * 1024

    extracted_pdf_text = (
        "Acme Corp Payslip\n"
        "Employee: Alex Tan\n"
        "Pay Date: 15/11/2026\n"
        "Pay Period: 01/11/2026 - 30/11/2026\n"
        "Gross Pay: 5430.75\n"
        "PAYE Tax: 1180.25\n"
        "KiwiSaver Employee: 162.92\n"
        "Net Pay / Total Banked: 4250.50"
    )
    llm_payslip_json = {
        "employer": "Acme Corp",
        "pay_date": "2026-11-15",
        "pay_period_start": "2026-11-01",
        "pay_period_end": "2026-11-30",
        "currency": "NZD",
        "net_pay": 4250.50,
        "tax_amount": 1180.25,
        "pay_period_label": "Nov 2026",
    }

    recorded_llm_calls: list[tuple[dict, int]] = []

    def fake_urlopen(req, timeout=None):
        recorded_llm_calls.append((json.loads(req.data.decode("utf-8")), timeout))
        return _FakeLlmResponse(llm_payslip_json)

    with (
        patch("app.services.receipt_service.extract_pdf_text", return_value=extracted_pdf_text) as mock_pdf,
        patch("app.services.receipt_service.urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        resp = client.post(
            "/integrations/email",
            json={
                "email_id": 11,
                "message_id": "<payslip-acme-1@acme.example>",
                "route_target": "plutus_payslip",
                "from_address": "payroll@acme.example",
                "subject": "Fwd: Your November Payslip",
                "received_at": "2026-11-15T04:00:00Z",
                "text_body": "Hi Alex Tan, your payslip is attached.",
                "attachments": [
                    {
                        "filename": "payslip.pdf",
                        "content_type": "application/pdf",
                        "size_bytes": len(pdf_bytes),
                        "content_base64": base64.b64encode(pdf_bytes).decode("ascii"),
                    }
                ],
            },
        )
    mock_pdf.assert_called_once()

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "processed"
    assert body["route_target"] == "plutus_payslip"
    rid = body["receipt_id"]
    assert rid is not None
    assert body["receipt_ids"] == [rid]
    assert body["consumer_ref"] == f"receipt:{rid}"

    assert len(recorded_llm_calls) == 1
    llm_payload, llm_timeout = recorded_llm_calls[0]
    assert llm_payload["model"] == settings.resolved_llm_model
    assert llm_payload["response_format"] == {"type": "json_object"}
    assert llm_timeout == 60
    assert "Net Pay" in llm_payload["messages"][0]["content"]

    rec = memory_db.get(Receipt, rid)
    assert rec is not None
    assert rec.document_type == "payslip"
    assert rec.merchant == "Acme Corp"
    assert rec.receipt_date == pay_day
    assert float(rec.total_amount) == 4250.50
    assert float(rec.tax_amount) == 1180.25
    assert rec.currency == "NZD"
    assert rec.order_number == "2026-11-01 to 2026-11-30"
    assert rec.notes is not None and "Nov 2026" in rec.notes
    assert rec.is_processed is True
    assert rec.status == "matched"
    assert rec.transaction_id == tx_inflow.id
    assert rec.image_mime_type == "application/pdf"
    assert rec.image_data == pdf_bytes
    assert memory_db.scalars(select(ReceiptItem)).all() == []
    assert memory_db.scalars(select(Product)).all() == []

    # Stored PDF is served via GET /receipts/{id}/image
    img_res = client.get(f"/receipts/{rid}/image")
    assert img_res.status_code == 200
    assert img_res.headers["content-type"] == "application/pdf"
    assert img_res.content == pdf_bytes

    # GET /receipts/{id} returns document_type="payslip" and items=[]
    rec_res = client.get(f"/receipts/{rid}")
    assert rec_res.status_code == 200
    assert rec_res.json()["document_type"] == "payslip"
    assert rec_res.json()["items"] == []

    # GET /transactions hydrates receipt_document_type="payslip" on inflow only
    txs_res = client.get("/transactions")
    assert txs_res.status_code == 200
    by_id = {row["id"]: row for row in txs_res.json()}
    assert by_id[tx_inflow.id]["has_receipt"] is True
    assert by_id[tx_inflow.id]["receipt_id"] == rid
    assert by_id[tx_inflow.id]["receipt_document_type"] == "payslip"
    assert by_id[tx_outflow_same_amt.id]["has_receipt"] is False
    assert by_id[tx_outflow_same_amt.id]["receipt_id"] is None
    assert by_id[tx_outflow_same_amt.id]["receipt_document_type"] is None


def test_purchase_receipt_isolates_from_positive_inflows(
    memory_db: Session,
    client: TestClient,
) -> None:
    """Purchase receipts strictly match negative outflows and exclude positive salary deposits."""
    acct = _seed_checking_account(memory_db)
    tx_date = date(2026, 11, 15)
    tx_inflow = Transaction(
        transaction_date=tx_date,
        account_id=acct.id,
        amount=4250.50,
        currency="NZD",
        merchant="Acme Corp",
        category="Income",
        raw_hash="inflow-iso-4250",
    )
    memory_db.add(tx_inflow)
    memory_db.commit()

    res = client.post(
        "/receipts",
        json={
            "receipt_date": tx_date.isoformat(),
            "merchant": "Acme Corp",
            "total_amount": 4250.50,
            "currency": "NZD",
            "items": [],
        },
    )
    assert res.status_code == 201
    created = res.json()
    assert created["document_type"] == "receipt"
    assert created["status"] == "unlinked"
    assert created["transaction_id"] is None

    cands_res = client.get(f"/receipts/{created['id']}/candidates")
    assert cands_res.status_code == 200
    assert cands_res.json() == []


def test_payslip_candidates_inflow_window_and_reconcile_after_bank_sync(
    memory_db: Session,
    client: TestClient,
) -> None:
    """Payslip candidates return positive inflows within [-7d, +14d] and reconcile picks up synced deposits."""
    acct = _seed_checking_account(memory_db)
    pay_date = date(2026, 11, 15)

    # Tied positive inflows at -7d and +7d -> needs_review, plus +14d, out-of-window (-8d/+15d), and negative rows
    tx_a = Transaction(
        transaction_date=pay_date - timedelta(days=7),
        account_id=acct.id,
        amount=4250.50,
        currency="NZD",
        merchant="Payroll Deposit A",
        category="Income",
        raw_hash="cand-inflow-a",
    )
    tx_b = Transaction(
        transaction_date=pay_date + timedelta(days=7),
        account_id=acct.id,
        amount=4250.50,
        currency="NZD",
        merchant="Payroll Deposit B",
        category="Income",
        raw_hash="cand-inflow-b",
    )
    tx_plus_14 = Transaction(
        transaction_date=pay_date + timedelta(days=14),
        account_id=acct.id,
        amount=4250.50,
        currency="NZD",
        merchant="Payroll Deposit C",
        category="Income",
        raw_hash="cand-inflow-14",
    )
    tx_too_early = Transaction(
        transaction_date=pay_date - timedelta(days=8),
        account_id=acct.id,
        amount=4250.50,
        currency="NZD",
        merchant="Too Early Inflow",
        category="Income",
        raw_hash="cand-inflow-early",
    )
    tx_too_late = Transaction(
        transaction_date=pay_date + timedelta(days=15),
        account_id=acct.id,
        amount=4250.50,
        currency="NZD",
        merchant="Too Late Inflow",
        category="Income",
        raw_hash="cand-inflow-late",
    )
    tx_outflow = Transaction(
        transaction_date=pay_date,
        account_id=acct.id,
        amount=-4250.50,
        currency="NZD",
        merchant="Acme Corp",
        category="Shopping",
        raw_hash="cand-outflow-neg",
    )
    memory_db.add_all([tx_a, tx_b, tx_plus_14, tx_too_early, tx_too_late, tx_outflow])
    memory_db.commit()

    # Ambiguous payslip (created via POST /receipts with document_type="payslip")
    res = client.post(
        "/receipts",
        json={
            "document_type": "payslip",
            "receipt_date": pay_date.isoformat(),
            "merchant": "Acme Corp",
            "total_amount": 4250.50,
            "tax_amount": 1180.25,
            "currency": "NZD",
            "items": [
                {
                    "item_name": "Should Not Be Created",
                    "quantity": 1.0,
                    "unit_price": 4250.50,
                    "total_price": 4250.50,
                }
            ],
        },
    )
    assert res.status_code == 201
    slip = res.json()
    assert slip["document_type"] == "payslip"
    assert slip["items"] == []
    assert slip["status"] == "needs_review"

    cands_res = client.get(f"/receipts/{slip['id']}/candidates")
    assert cands_res.status_code == 200
    cands = cands_res.json()
    cand_ids = {c["transaction_id"] for c in cands}
    assert cand_ids == {tx_a.id, tx_b.id, tx_plus_14.id}
    assert all(c["amount"] > 0 for c in cands)

    # Also test payslip ingested BEFORE bank deposit arrives, then reconciled later
    unlinked_slip = Receipt(
        document_type="payslip",
        receipt_date=date(2026, 10, 15),
        merchant="Northwind Payroll",
        total_amount=3100.00,
        currency="NZD",
        status="unlinked",
        is_processed=True,
    )
    memory_db.add(unlinked_slip)
    memory_db.commit()
    memory_db.refresh(unlinked_slip)

    # Negative outflow of 3100.00 must be ignored by reconcile
    memory_db.add(
        Transaction(
            transaction_date=date(2026, 10, 15),
            account_id=acct.id,
            amount=-3100.00,
            currency="NZD",
            merchant="Northwind Payroll",
            category="Shopping",
            raw_hash="recon-neg-3100",
        )
    )
    memory_db.commit()
    assert reconcile_unlinked_receipts(memory_db) == 0

    synced_salary = Transaction(
        transaction_date=date(2026, 10, 16),
        account_id=acct.id,
        amount=3100.00,
        currency="NZD",
        merchant="NORTHWIND LTD SALARY",
        category="Income",
        raw_hash="recon-pos-3100",
    )
    memory_db.add(synced_salary)
    memory_db.commit()
    memory_db.refresh(synced_salary)

    assert reconcile_unlinked_receipts(memory_db) == 1
    memory_db.refresh(unlinked_slip)
    assert unlinked_slip.status == "matched"
    assert unlinked_slip.transaction_id == synced_salary.id


def test_payslip_semantic_dedup_and_cross_document_type_isolation(
    memory_db: Session,
    client: TestClient,
) -> None:
    """Re-forwarded payslips dedup semantically without colliding with purchase receipts."""
    pdf_bytes = _make_synthetic_pdf_bytes(1024)
    parsed_slip = {
        "employer": "Acme Corp",
        "pay_date": "2026-11-15",
        "pay_period_start": "2026-11-01",
        "pay_period_end": "2026-11-30",
        "currency": "NZD",
        "net_pay": 4250.50,
        "tax_amount": 1180.25,
        "pay_period_label": "Nov 2026",
    }

    with patch("app.services.receipt_service.parse_payslip_with_llm", return_value=parsed_slip):
        first = client.post(
            "/integrations/email",
            json={
                "email_id": 501,
                "message_id": "<slip-dedup-1@acme.example>",
                "route_target": "plutus_payslip",
                "subject": "Payslip Nov",
                "attachments": [
                    {
                        "filename": "payslip.pdf",
                        "content_type": "application/pdf",
                        "size_bytes": len(pdf_bytes),
                        "content_base64": base64.b64encode(pdf_bytes).decode("ascii"),
                    }
                ],
            },
        )
        second = client.post(
            "/integrations/email",
            json={
                "email_id": 502,
                "message_id": "<slip-dedup-2-reforward@acme.example>",
                "route_target": "plutus_payslip",
                "subject": "Fwd: Payslip Nov",
                "attachments": [
                    {
                        "filename": "payslip.pdf",
                        "content_type": "application/pdf",
                        "size_bytes": len(pdf_bytes),
                        "content_base64": base64.b64encode(pdf_bytes).decode("ascii"),
                    }
                ],
            },
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["receipt_id"] == first.json()["receipt_id"]
    assert len(memory_db.scalars(select(Receipt)).all()) == 1

    # Purchase receipt with matching merchant/date/amount/order_number must NOT collide with payslip
    purchase_orders = {
        "orders": [
            {
                "order_number": "2026-11-01 to 2026-11-30",
                "merchant": "Acme Corp",
                "receipt_date": "2026-11-15",
                "currency": "NZD",
                "total_amount": 4250.50,
                "items": [],
            }
        ]
    }
    with patch("app.services.receipt_service._invoke_receipt_llm", return_value=purchase_orders):
        purchase = client.post(
            "/integrations/email",
            json={
                "email_id": 503,
                "message_id": "<purchase-same-amt@acme.example>",
                "route_target": "plutus_purchase_receipt",
                "subject": "Acme Invoice",
                "text_body": "Acme Corp Invoice 4250.50 NZD",
            },
        )
    assert purchase.status_code == 200
    assert purchase.json()["receipt_id"] != first.json()["receipt_id"]
    assert len(memory_db.scalars(select(Receipt)).all()) == 2


def test_payslip_vision_fallback_for_scanned_pdf_and_image_and_422_on_unparseable(
    memory_db: Session,
    client: TestClient,
) -> None:
    """Scanned/empty-text PDFs and images route to vision LLM; unparseable payloads return 422."""
    raw_pdf = _make_synthetic_pdf_bytes(900)
    large_png = _make_large_png_bytes()
    recorded: list[tuple[dict, int]] = []

    def fake_urlopen(req, timeout=None):
        recorded.append((json.loads(req.data.decode("utf-8")), timeout))
        return _FakeLlmResponse(
            {
                "employer": "Acme Corp",
                "pay_date": "2026-11-15",
                "currency": "NZD",
                "net_pay": 4250.50,
                "tax_amount": 1180.25,
                "pay_period_label": "Nov 2026",
            }
        )

    # 1. Scanned PDF (extract_pdf_text == "") -> vision LLM with application/pdf data URI
    with (
        patch("app.services.receipt_service.extract_pdf_text", return_value=""),
        patch("app.services.receipt_service.urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        scanned = parse_payslip_with_llm(raw_pdf, "scanned.pdf", "application/pdf")
    assert scanned is not None
    assert scanned["employer"] == "Acme Corp"
    assert len(recorded) == 1
    payload_pdf, timeout_pdf = recorded[0]
    assert payload_pdf["model"] == settings.resolved_receipt_llm_model
    assert timeout_pdf == 105
    content_parts = payload_pdf["messages"][0]["content"]
    assert content_parts[0]["text"] == PAYSLIP_PARSING_PROMPT
    assert content_parts[1]["image_url"]["url"].startswith("data:application/pdf;base64,")

    # 2. Image attachment -> vision LLM + WebP stored in DB
    recorded.clear()
    with patch("app.services.receipt_service.urllib.request.urlopen", side_effect=fake_urlopen):
        img_resp = client.post(
            "/integrations/email",
            json={
                "email_id": 601,
                "message_id": "<payslip-img-1@acme.example>",
                "route_target": "plutus_payslip",
                "subject": "Payslip Image",
                "attachments": [
                    {
                        "filename": "payslip.png",
                        "content_type": "image/png",
                        "size_bytes": len(large_png),
                        "content_base64": base64.b64encode(large_png).decode("ascii"),
                    }
                ],
            },
        )
    assert img_resp.status_code == 200
    img_rec = memory_db.get(Receipt, img_resp.json()["receipt_id"])
    assert img_rec.document_type == "payslip"
    assert img_rec.image_mime_type == "image/webp"

    # 3. Unparseable / missing fields -> 422 and failed audit record
    fail_empty = client.post(
        "/integrations/email",
        json={
            "email_id": 602,
            "message_id": "<payslip-fail-1@acme.example>",
            "route_target": "plutus_payslip",
            "subject": "Empty payslip",
            "text_body": "",
            "html_excerpt": "",
        },
    )
    assert fail_empty.status_code == 422
    audit = memory_db.scalar(
        select(IngestedEmail).where(IngestedEmail.message_id == "<payslip-fail-1@acme.example>")
    )
    assert audit is not None
    assert audit.status == "failed"

    with patch(
        "app.services.receipt_service.parse_payslip_text_with_llm",
        return_value={"employer": "", "net_pay": 0},
    ):
        fail_invalid = client.post(
            "/integrations/email",
            json={
                "email_id": 603,
                "message_id": "<payslip-fail-2@acme.example>",
                "route_target": "plutus_payslip",
                "subject": "Invalid values",
                "text_body": "Payslip notification without valid numbers",
            },
        )
    assert fail_invalid.status_code == 422

    # 4. Invalid document_type in ReceiptCreate -> 422
    bad_schema = client.post(
        "/receipts",
        json={
            "document_type": "invoice",
            "receipt_date": "2026-11-15",
            "merchant": "Acme Corp",
            "total_amount": 100.0,
            "items": [],
        },
    )
    assert bad_schema.status_code == 422


def test_payslip_reprocess_and_spending_projection_isolation(
    memory_db: Session,
    client: TestClient,
) -> None:
    """Reprocessing a stored payslip preserves document_type and zero items; projection spend stays unchanged."""
    acct = _seed_checking_account(memory_db)
    pay_day = date(2026, 11, 15)

    tx_income = Transaction(
        transaction_date=pay_day,
        account_id=acct.id,
        amount=4250.50,
        currency="NZD",
        merchant="ACME CORP PAYROLL",
        category="Income",
        raw_hash="proj-income-4250",
    )
    tx_expense = Transaction(
        transaction_date=pay_day,
        account_id=acct.id,
        amount=-120.00,
        currency="NZD",
        merchant="Countdown",
        category="Groceries",
        raw_hash="proj-expense-120",
    )
    memory_db.add_all([tx_income, tx_expense])
    memory_db.commit()
    memory_db.refresh(tx_income)

    before_proj = project_window(
        memory_db,
        ReportingWindow(period="month"),
        reference_date=date(2026, 11, 20),
    )

    # Create unprocessed stored PDF payslip and reprocess it
    raw_pdf = _make_synthetic_pdf_bytes(1024)
    stored_slip = Receipt(
        document_type="payslip",
        receipt_date=pay_day,
        merchant="Pending Payslip",
        total_amount=0.0,
        currency="NZD",
        image_data=raw_pdf,
        image_mime_type="application/pdf",
        image_filename="payslip.pdf",
        is_processed=False,
        status="unlinked",
    )
    memory_db.add(stored_slip)
    memory_db.commit()
    memory_db.refresh(stored_slip)

    reprocessed_json = {
        "employer": "Acme Corp",
        "pay_date": "2026-11-15",
        "pay_period_start": "2026-11-01",
        "pay_period_end": "2026-11-30",
        "currency": "NZD",
        "net_pay": 4250.50,
        "tax_amount": 1180.25,
        "pay_period_label": "Nov 2026",
    }
    with patch("app.routers.receipts.parse_payslip_with_llm", return_value=reprocessed_json):
        re_res = client.post(f"/receipts/{stored_slip.id}/reprocess")

    assert re_res.status_code == 200
    re_body = re_res.json()
    assert re_body["document_type"] == "payslip"
    assert re_body["merchant"] == "Acme Corp"
    assert re_body["total_amount"] == 4250.50
    assert re_body["tax_amount"] == 1180.25
    assert re_body["is_processed"] is True
    assert re_body["status"] == "matched"
    assert re_body["transaction_id"] == tx_income.id
    assert re_body["items"] == []
    assert memory_db.scalars(select(ReceiptItem)).all() == []
    assert memory_db.scalars(select(Product)).all() == []

    # Spending projection & /budgets/breakdown must remain completely unaltered
    memory_db.expire_all()
    refreshed_income = memory_db.get(Transaction, tx_income.id)
    assert expand_expense_lines(refreshed_income) == []

    after_proj = project_window(
        memory_db,
        ReportingWindow(period="month"),
        reference_date=date(2026, 11, 20),
    )
    assert after_proj.total_spend == before_proj.total_spend == 120.00
    assert after_proj.total_income == before_proj.total_income == 4250.50
    assert after_proj.categories == before_proj.categories

    # Also verify parse_payslip_text_with_llm contract directly
    recorded_direct: list[tuple[dict, int]] = []

    def fake_urlopen_direct(req, timeout=None):
        recorded_direct.append((json.loads(req.data.decode("utf-8")), timeout))
        return _FakeLlmResponse(reprocessed_json)

    with patch("app.services.receipt_service.urllib.request.urlopen", side_effect=fake_urlopen_direct):
        direct_out = parse_payslip_text_with_llm("Acme Corp Net Pay 4250.50 NZD", raise_on_error=True)
    assert direct_out["employer"] == "Acme Corp"
    assert recorded_direct[0][1] == 60
    assert recorded_direct[0][0]["response_format"] == {"type": "json_object"}


def test_payslip_body_fallback_retains_pdf_and_forwarded_date_when_attachment_parse_fails(
    memory_db: Session,
    client: TestClient,
) -> None:
    """When attachment parsing returns None, email body fallback keeps the PDF attachment and forwarded Date."""
    raw_pdf = _make_synthetic_pdf_bytes(900)
    body_parsed = {
        "employer": "Northwind Payroll",
        "pay_date": None,
        "currency": "NZD",
        "net_pay": 2850.00,
        "tax_amount": 610.00,
        "pay_period_label": "Fortnight 22",
    }
    with (
        patch("app.services.receipt_service.parse_payslip_with_llm", return_value=None),
        patch("app.services.receipt_service.parse_payslip_text_with_llm", return_value=body_parsed),
    ):
        resp = client.post(
            "/integrations/email",
            json={
                "email_id": 701,
                "message_id": "<payslip-body-fallback-1@acme.example>",
                "route_target": "plutus_payslip",
                "subject": "Fwd: Payslip Advice",
                "text_body": (
                    "---------- Forwarded message ---------\n"
                    "From: Payroll <payroll@northwind.example>\n"
                    "Date: Fri, Nov 13, 2026 at 9:00 AM\n"
                    "Subject: Payslip Advice\n\n"
                    "Northwind Payroll Net Pay NZD 2850.00"
                ),
                "attachments": [
                    {
                        "filename": "advice.pdf",
                        "content_type": "application/pdf",
                        "size_bytes": len(raw_pdf),
                        "content_base64": base64.b64encode(raw_pdf).decode("ascii"),
                    }
                ],
            },
        )

    assert resp.status_code == 200
    rid = resp.json()["receipt_id"]
    rec = memory_db.get(Receipt, rid)
    assert rec is not None
    assert rec.document_type == "payslip"
    assert rec.merchant == "Northwind Payroll"
    assert rec.receipt_date == date(2026, 11, 13)
    assert rec.image_data == raw_pdf
    assert rec.image_mime_type == "application/pdf"
    assert rec.image_filename == "advice.pdf"


def test_payslip_semantic_dedup_does_not_collide_across_months_with_generic_period_label(
    memory_db: Session,
    client: TestClient,
) -> None:
    """Two payslips 30+ days apart with identical employer, net_pay, currency, and generic label create 2 Receipts."""
    pdf_bytes = _make_synthetic_pdf_bytes(1024)
    oct_slip = {
        "employer": "Acme Corp",
        "pay_date": "2026-10-15",
        "pay_period_start": None,
        "pay_period_end": None,
        "currency": "NZD",
        "net_pay": 4250.50,
        "tax_amount": 1180.25,
        "pay_period_label": "Fortnight Pay",
    }
    nov_slip = {
        **oct_slip,
        "pay_date": "2026-11-15",
    }

    with patch("app.services.receipt_service.parse_payslip_with_llm", return_value=oct_slip):
        first = client.post(
            "/integrations/email",
            json={
                "email_id": 801,
                "message_id": "<slip-oct-generic@acme.example>",
                "route_target": "plutus_payslip",
                "subject": "Payslip Oct",
                "attachments": [
                    {
                        "filename": "payslip.pdf",
                        "content_type": "application/pdf",
                        "size_bytes": len(pdf_bytes),
                        "content_base64": base64.b64encode(pdf_bytes).decode("ascii"),
                    }
                ],
            },
        )

    with patch("app.services.receipt_service.parse_payslip_with_llm", return_value=nov_slip):
        second = client.post(
            "/integrations/email",
            json={
                "email_id": 802,
                "message_id": "<slip-nov-generic@acme.example>",
                "route_target": "plutus_payslip",
                "subject": "Payslip Nov",
                "attachments": [
                    {
                        "filename": "payslip.pdf",
                        "content_type": "application/pdf",
                        "size_bytes": len(pdf_bytes),
                        "content_base64": base64.b64encode(pdf_bytes).decode("ascii"),
                    }
                ],
            },
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["receipt_id"] != second.json()["receipt_id"]
    rows = memory_db.scalars(select(Receipt).order_by(Receipt.receipt_date.asc())).all()
    assert len(rows) == 2
    assert [r.receipt_date for r in rows] == [date(2026, 10, 15), date(2026, 11, 15)]
    assert all(r.order_number == "Fortnight Pay" for r in rows)
