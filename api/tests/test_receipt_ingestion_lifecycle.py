"""Direct unit tests for all 4 receipt lifecycles, payslip ingestion, and frozen DTOs."""

from __future__ import annotations

import base64
from dataclasses import FrozenInstanceError
from datetime import date
import io
import json
from pathlib import Path
import random
import sys

from PIL import Image
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base
from app.models import Account, Product, Receipt, ReceiptItem, Transaction
from app.schemas import EmailAttachmentPayload, InboundEmailDispatchPayload
from app.services.ai_gateway import (
    AiInvalidResponseError,
    AiUpstreamError,
    InMemoryAiGateway,
    JevGateRequest,
)
from app.services.receipt_ingestion import (
    ManualReceiptInput,
    PayslipEmailIngestionResult,
    PurchaseEmailIngestionResult,
    ReceiptItemDraft,
    ReceiptNotFoundError,
    ReceiptParseError,
    ReceiptSnapshot,
    ReceiptUpstreamError,
    ReceiptValidationError,
    apply_parsed_payslip_fields,
    create_manual_receipt,
    ingest_payslip_email,
    ingest_purchase_receipt_email,
    reprocess_receipt_media,
    upload_receipt_media,
)


def _new_memory_session_factory() -> tuple[sessionmaker[Session], object]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine), engine


def _make_png_bytes(width: int = 300, height: int = 300) -> bytes:
    img = Image.new("RGB", (width, height), color=(235, 235, 235))
    rng = random.Random(11)
    px = img.load()
    for x in range(width):
        for y in range(0, height, 2):
            px[x, y] = (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_pdf_bytes(size: int = 16000) -> bytes:
    header = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    return header + b"%" + (b"0" * max(0, size - len(header) - 1))


def _seed_account(db: Session) -> Account:
    acc = Account(
        name="Harbour Checking",
        account_type="checking",
        tier="cash",
        institution="Nordic Bank",
        currency="NZD",
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return acc


def test_manual_receipt_lifecycle_detached_no_dedup_no_jev_discount() -> None:
    session_factory, engine = _new_memory_session_factory()
    try:
        with session_factory() as db:
            acc = _seed_account(db)
            tx = Transaction(
                transaction_date=date(2026, 11, 20),
                account_id=acc.id,
                amount=-27.99,
                currency="NZD",
                merchant="Harbour Market",
                category="Groceries",
                tags='["weekly-shop"]',
                raw_hash="manual-tx-1",
            )
            db.add(tx)
            db.commit()
            db.refresh(tx)
            tx_id = tx.id

            gateway = InMemoryAiGateway(
                jev_responses=[
                    '{"category": "Groceries", "is_anomaly": false, "primary_tag": "none"}',
                    '{"category": "Groceries", "is_anomaly": false, "primary_tag": "none"}',
                ]
            )
            payload = ManualReceiptInput(
                receipt_date=date(2026, 11, 20),
                merchant="Harbour Market",
                total_amount=27.99,
                currency="NZD",
                items=(
                    ReceiptItemDraft(
                        item_name="Sparkling Water 24P",
                        unit_price=34.99,
                        total_price=34.99,
                        explicit_category="Groceries",
                    ),
                    ReceiptItemDraft(
                        item_name="TPD Sparkling Water",
                        unit_price=-7.00,
                        total_price=-7.00,
                        explicit_category="Groceries",
                    ),
                ),
            )
            first = create_manual_receipt(db, payload, ai_gateway=gateway)
            second = create_manual_receipt(db, payload, ai_gateway=gateway)

            receipt_count = len(db.scalars(select(Receipt)).all())
            product_count = len(db.scalars(select(Product)).all())

        assert receipt_count == 2
        assert product_count == 1
        assert isinstance(first, ReceiptSnapshot)
        assert first.id != second.id
        assert first.status == "matched"
        assert first.transaction_id == tx_id
        assert len(first.items) == 2
        assert first.items[0].product_id is not None
        assert first.items[0].inherited_tags == ("weekly-shop",)
        assert first.items[1].total_price == -7.00
        assert first.items[1].product_id is None
        assert all("target_index" not in req.system_prompt for req in gateway.jev_requests)

        with pytest.raises(FrozenInstanceError):
            first.merchant = "Mutated"  # type: ignore[misc]
    finally:
        Base.metadata.drop_all(bind=engine)


def test_upload_and_reprocess_lifecycles_with_pdf_injector_and_jev_discounts() -> None:
    session_factory, engine = _new_memory_session_factory()
    try:
        pdf_bytes = _make_pdf_bytes(2048)
        png_bytes = _make_png_bytes()
        extracted_calls: list[bytes] = []

        def fake_pdf_extractor(raw: bytes) -> str:
            extracted_calls.append(raw)
            return "Harbour Mart\nOrganic Oat Milk 4.50\nTPD Oat Milk -1.00\nTotal 3.50"

        parsed_receipt = {
            "merchant": "Harbour Mart",
            "receipt_date": "2026-11-21",
            "currency": "NZD",
            "total_amount": 3.50,
            "items": [
                {
                    "item_name": "Organic Oat Milk",
                    "quantity": 1.0,
                    "unit_price": 4.50,
                    "total_price": 4.50,
                    "category": "Groceries",
                },
                {
                    "item_name": "TPD Oat Milk",
                    "quantity": 1.0,
                    "unit_price": -1.00,
                    "total_price": -1.00,
                },
            ],
        }

        def jev_handler(req: JevGateRequest) -> str:
            if "target_index" in req.system_prompt:
                return json.dumps({"target_index": "0"})
            return json.dumps({"category": "Groceries", "is_anomaly": False, "primary_tag": "none"})

        gateway = InMemoryAiGateway(
            text_responses=[parsed_receipt, parsed_receipt],
            vision_responses=[AiUpstreamError("Vision offline", status_code=503), parsed_receipt],
            jev_handler=jev_handler,
        )

        with session_factory() as db:
            pdf_one = upload_receipt_media(
                db,
                content=pdf_bytes,
                filename="order.pdf",
                content_type="application/pdf",
                ai_gateway=gateway,
                pdf_text_extractor=fake_pdf_extractor,
            )
            pdf_two = upload_receipt_media(
                db,
                content=pdf_bytes,
                filename="order.pdf",
                content_type="application/pdf",
                ai_gateway=gateway,
                pdf_text_extractor=fake_pdf_extractor,
            )
            unprocessed = upload_receipt_media(
                db,
                content=png_bytes,
                filename="photo.png",
                content_type="image/png",
                ai_gateway=gateway,
            )
            reprocessed = reprocess_receipt_media(
                db,
                unprocessed.id,
                ai_gateway=gateway,
            )

        assert len(extracted_calls) == 2
        assert pdf_one.id != pdf_two.id
        assert pdf_one.is_processed is True
        assert len(pdf_one.items) == 1
        assert pdf_one.items[0].discount_amount == 1.00
        assert pdf_one.items[0].total_price == 3.50

        assert unprocessed.is_processed is False
        assert unprocessed.image_mime_type == "image/png"
        assert reprocessed.is_processed is True
        assert reprocessed.image_mime_type == "image/webp"
        assert len(reprocessed.items) == 1
        assert reprocessed.items[0].discount_amount == 1.00
    finally:
        Base.metadata.drop_all(bind=engine)


def test_email_and_payslip_lifecycles_with_dedup_and_injected_pdf_extractor() -> None:
    session_factory, engine = _new_memory_session_factory()
    try:
        pdf_bytes = _make_pdf_bytes(16000)
        extractor_calls: list[bytes] = []

        def fake_pdf(raw: bytes) -> str:
            extractor_calls.append(raw)
            return "Order #ORD-55\nHarbour Gear\nUSB Hub 50.00\nPromo Hub -10.00\nTotal 40.00 NZD"

        multi_orders = {
            "orders": [
                {
                    "order_number": "ORD-55",
                    "merchant": "Harbour Gear",
                    "receipt_date": "2026-11-22",
                    "currency": "NZD",
                    "total_amount": 40.00,
                    "items": [
                        {
                            "item_name": "USB Hub",
                            "quantity": 1.0,
                            "unit_price": 50.00,
                            "total_price": 50.00,
                            "category": "Tech",
                        },
                        {
                            "item_name": "Promo Hub",
                            "quantity": 1.0,
                            "unit_price": -10.00,
                            "total_price": -10.00,
                        },
                    ],
                }
            ]
        }
        payslip_json = {
            "employer": "Northwind Corp",
            "pay_date": "2026-11-15",
            "pay_period_start": "2026-11-01",
            "pay_period_end": "2026-11-15",
            "currency": "NZD",
            "net_pay": 3200.00,
            "tax_amount": 750.00,
            "pay_period_label": "Mid Nov",
        }

        def jev_handler(req: JevGateRequest) -> str:
            if "target_index" in req.system_prompt:
                return json.dumps({"target_index": "0"})
            return json.dumps({"category": "Tech", "is_anomaly": False, "primary_tag": "none"})

        gateway = InMemoryAiGateway(
            text_responses=[multi_orders, multi_orders, payslip_json, payslip_json],
            jev_handler=jev_handler,
        )

        with session_factory() as db:
            purchase_1 = ingest_purchase_receipt_email(
                db,
                InboundEmailDispatchPayload(
                    message_id="<buy-1@example.com>",
                    route_target="plutus_purchase_receipt",
                    subject="Your order",
                    attachments=[
                        EmailAttachmentPayload(
                            filename="order.pdf",
                            content_type="application/pdf",
                            size_bytes=len(pdf_bytes),
                            content_base64=base64.b64encode(pdf_bytes).decode("ascii"),
                        )
                    ],
                ),
                ai_gateway=gateway,
                pdf_text_extractor=fake_pdf,
            )
            purchase_2 = ingest_purchase_receipt_email(
                db,
                InboundEmailDispatchPayload(
                    message_id="<buy-2@example.com>",
                    route_target="plutus_purchase_receipt",
                    subject="Fwd: Your order",
                    text_body="Order ORD-55 Harbour Gear 40.00",
                ),
                ai_gateway=gateway,
                pdf_text_extractor=fake_pdf,
            )
            slip_1 = ingest_payslip_email(
                db,
                InboundEmailDispatchPayload(
                    message_id="<slip-1@example.com>",
                    route_target="plutus_payslip",
                    subject="Payslip",
                    attachments=[
                        EmailAttachmentPayload(
                            filename="slip.pdf",
                            content_type="application/pdf",
                            size_bytes=len(pdf_bytes),
                            content_base64=base64.b64encode(pdf_bytes).decode("ascii"),
                        )
                    ],
                ),
                ai_gateway=gateway,
                pdf_text_extractor=fake_pdf,
            )
            slip_2 = ingest_payslip_email(
                db,
                InboundEmailDispatchPayload(
                    message_id="<slip-2@example.com>",
                    route_target="plutus_payslip",
                    subject="Fwd: Payslip",
                    text_body="Northwind Corp 3200.00",
                ),
                ai_gateway=gateway,
                pdf_text_extractor=fake_pdf,
            )

            row_for_apply = Receipt(
                receipt_date=date(2026, 11, 15),
                merchant="Pending",
                total_amount=0.0,
            )
            db.add(row_for_apply)
            db.flush()
            db.add(ReceiptItem(receipt_id=row_for_apply.id, item_name="Stale", unit_price=1.0, total_price=1.0))
            db.flush()
            applied_ok = apply_parsed_payslip_fields(row_for_apply, payslip_json, db)
            applied_bad = apply_parsed_payslip_fields(row_for_apply, {"employer": "", "net_pay": 0}, db)
            db.commit()
            total_receipts = len(db.scalars(select(Receipt)).all())

        assert len(extractor_calls) == 2
        assert isinstance(purchase_1, PurchaseEmailIngestionResult)
        assert purchase_1.deduplicated_count == 0
        assert purchase_1.receipts[0].items[0].discount_amount == 10.00
        assert purchase_2.deduplicated_count == 1
        assert purchase_2.receipt_ids == purchase_1.receipt_ids

        assert isinstance(slip_1, PayslipEmailIngestionResult)
        assert slip_1.deduplicated is False
        assert slip_2.deduplicated is True
        assert slip_2.receipt.id == slip_1.receipt.id
        assert applied_ok is True
        assert applied_bad is False
        assert total_receipts == 3
    finally:
        Base.metadata.drop_all(bind=engine)


def test_lifecycle_domain_exceptions() -> None:
    session_factory, engine = _new_memory_session_factory()
    try:
        with session_factory() as db:
            with pytest.raises(ReceiptValidationError):
                create_manual_receipt(
                    db,
                    ManualReceiptInput(receipt_date=date(2026, 11, 20), merchant="   ", total_amount=10.0),
                )
            with pytest.raises(ReceiptValidationError):
                upload_receipt_media(db, content=b"", filename="empty.png", content_type="image/png")
            with pytest.raises(ReceiptNotFoundError):
                reprocess_receipt_media(db, 999999)

            no_media = Receipt(receipt_date=date(2026, 11, 20), merchant="No Media", total_amount=5.0)
            has_media = Receipt(
                receipt_date=date(2026, 11, 20),
                merchant="Has Media",
                total_amount=5.0,
                image_data=_make_png_bytes(64, 64),
                image_mime_type="image/png",
                image_filename="test.png",
            )
            db.add_all([no_media, has_media])
            db.commit()
            db.refresh(no_media)
            db.refresh(has_media)

            with pytest.raises(ReceiptValidationError):
                reprocess_receipt_media(db, no_media.id)

            bad_gateway = InMemoryAiGateway(
                vision_responses=[
                    AiInvalidResponseError("Invalid JSON"),
                    AiUpstreamError("Gateway down", status_code=503),
                ]
            )
            with pytest.raises(ReceiptParseError):
                reprocess_receipt_media(db, has_media.id, ai_gateway=bad_gateway)
            with pytest.raises(ReceiptUpstreamError):
                reprocess_receipt_media(db, has_media.id, ai_gateway=bad_gateway)

            with pytest.raises(ReceiptValidationError):
                ingest_purchase_receipt_email(
                    db,
                    InboundEmailDispatchPayload(
                        message_id="<empty-body@example.com>",
                        route_target="plutus_purchase_receipt",
                        subject="",
                        text_body="",
                    ),
                )
    finally:
        Base.metadata.drop_all(bind=engine)
