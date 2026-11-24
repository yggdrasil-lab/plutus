"""Direct SQLite :memory: interface tests for the deep Receipt Ingestion core package."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import FrozenInstanceError, is_dataclass
from datetime import date, datetime, timezone
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
from app.models import (
    Account,
    CategorizationRule,
    Category,
    ExchangeRate,
    Product,
    Receipt,
    ReceiptItem,
    Transaction,
)
from app.services.ai_gateway import (
    AiConfigError,
    AiInvalidResponseError,
    AiUpstreamError,
    InMemoryAiGateway,
)
from app.services.category_taxonomy import ensure_default_categories
from app.services.email_utils import PlainEmailHtmlParser, html_to_plain_text
from app.services.receipt_ingestion import (
    ItemClassification,
    LineItemHeuristic,
    ReceiptItemDraft,
    ReceiptItemSnapshot,
    ReceiptMatchOutcome,
    ReceiptNotFoundError,
    ReceiptParseError,
    ReceiptUpstreamError,
    SanitizedEmailContent,
    SemanticDuplicateMatch,
    TransactionCandidateSnapshot,
    apply_receipt_discounts_with_jev,
    build_multi_order_email_receipt_prompt,
    build_receipt_category_options,
    build_receipt_parsing_prompt,
    classify_line_item,
    classify_receipt_item_draft,
    compress_receipt_image,
    extract_tags_for_line_item,
    find_receipt_transaction_candidates,
    find_semantic_duplicate_receipt,
    infer_receipt_mime_type,
    match_receipt_to_transaction,
    normalize_merchant_key,
    normalize_order_number,
    normalize_receipt_currency,
    parse_original_fx_from_description,
    parse_payslip_text_with_llm,
    parse_payslip_with_llm,
    parse_receipt_orders_text_with_llm,
    parse_receipt_text_with_llm,
    parse_receipt_with_llm,
    persist_classified_receipt_items,
    prepare_image_for_llm,
    reconcile_unlinked_receipts,
    sanitize_receipt_email_content,
)


def _new_memory_session_factory() -> tuple[sessionmaker[Session], object]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine), engine


@pytest.fixture()
def db_session() -> Iterator[Session]:
    """Provide an isolated SQLite :memory: session."""
    session_factory, engine = _new_memory_session_factory()
    db = session_factory()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def _make_png_bytes(width: int = 800, height: int = 800) -> bytes:
    img = Image.new("RGB", (width, height), color=(230, 230, 230))
    rng = random.Random(42)
    px = img.load()
    for x in range(width):
        for y in range(0, height, 2):
            px[x, y] = (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _add_account(db: Session, *, name: str = "Harbour Checking", currency: str = "NZD") -> Account:
    acc = Account(
        name=name,
        account_type="checking",
        tier="cash",
        institution="Nordic Bank",
        currency=currency,
    )
    db.add(acc)
    db.flush()
    return acc


def test_html_parser_plain_vs_annotated_and_sanitization() -> None:
    html = (
        "<html><head><style>.x{display:none}</style></head><body>"
        "<table><tr><td><span aria-label='NZD 49.50'>$49.50</span>"
        "<span aria-label='Close'>x</span>"
        "<img alt='pixel' src='p.gif' />"
        "<img alt='Nordic Whey Protein Isolate 1kg' src='w.jpg' />"
        "</td></tr></table>"
        "<script>bad();</script></body></html>"
    )
    plain = html_to_plain_text(html)
    assert "[aria:" not in plain
    assert "[item:" not in plain
    assert "$49.50" in plain
    assert "bad()" not in plain

    parser = PlainEmailHtmlParser(preserve_annotations=True)
    parser.feed(html)
    parser.close()
    annotated = parser.get_text()
    assert "[aria: NZD 49.50]" in annotated
    assert "[aria: Close]" not in annotated
    assert "[item: Nordic Whey Protein Isolate 1kg]" in annotated
    assert "[item: pixel]" not in annotated

    fwd_html = (
        "<p>---------- Forwarded message ---------<br>"
        "From: Nordic Goods &lt;orders@nordicgoods.example&gt;<br>"
        "Date: Sat, Nov 7, 2026 at 10:15 AM<br>"
        "Subject: Order Confirmation</p>"
        f"{html}"
        "<p>Track https://tracker.example.com/click?token=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789</p>"
        "<p>Invisible\u200bMarker</p>"
    )
    sanitized = sanitize_receipt_email_content(
        subject="Fwd: Order Confirmation",
        from_address="alex.tan@example.com",
        received_at=datetime(2026, 11, 12, 1, 0, tzinfo=timezone.utc),
        text_body=None,
        html_excerpt=fwd_html,
        raw_excerpt=None,
        timezone_name="Pacific/Auckland",
    )
    assert is_dataclass(sanitized) and isinstance(sanitized, SanitizedEmailContent)
    assert sanitized.fallback_date == date(2026, 11, 7)

    tz_only = sanitize_receipt_email_content(
        subject="Order Confirmation",
        from_address="alex.tan@example.com",
        received_at=datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc),
        text_body="Total NZD 49.50",
        html_excerpt=None,
        raw_excerpt=None,
        timezone_name="Pacific/Auckland",
    )
    assert tz_only.fallback_date == date(2026, 11, 9)
    assert "[aria: NZD 49.50]" in sanitized.prompt_text
    assert "[item: Nordic Whey Protein Isolate 1kg]" in sanitized.prompt_text
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in sanitized.prompt_text
    assert "\u200b" not in sanitized.prompt_text
    assert "InvisibleMarker" in sanitized.prompt_text
    assert len(sanitized.prompt_text) <= 48000

    with pytest.raises(FrozenInstanceError):
        sanitized.prompt_text = "mutated"  # type: ignore[misc]

    assert normalize_receipt_currency("AU$") == "AUD"
    assert normalize_receipt_currency("€19.99") == "EUR"
    assert normalize_order_number("  n/a ") is None
    assert normalize_merchant_key("Nordic Goods Pty Ltd Online") == "nordic goods"


def test_dynamic_category_taxonomy_prompts(db_session: Session) -> None:
    default_opts = build_receipt_category_options(None)
    assert "Groceries" in default_opts
    assert "Transfers" not in default_opts
    assert "Income" not in default_opts

    unseeded_opts = build_receipt_category_options(db_session)
    assert unseeded_opts == default_opts

    ensure_default_categories(db_session)
    db_session.add(
        Category(name="Pet Care", is_system=False, is_active=True, sort_order=50)
    )
    db_session.add(
        Category(name="Retired Hobby", is_system=False, is_active=False, sort_order=51)
    )
    db_session.commit()

    live_opts = build_receipt_category_options(db_session)
    assert "Pet Care" in live_opts
    assert "Retired Hobby" not in live_opts
    assert "Transfers" not in live_opts
    assert "Income" not in live_opts

    single_prompt = build_receipt_parsing_prompt(db_session)
    multi_prompt = build_multi_order_email_receipt_prompt(db_session)
    assert "Pet Care" in single_prompt
    assert "Pet Care" in multi_prompt
    assert "Retired Hobby" not in single_prompt

    gateway = InMemoryAiGateway(
        text_responses=[
            {"merchant": "Pet Barn", "total_amount": 32.0},
            {"orders": [{"merchant": "Pet Barn", "total_amount": 32.0}]},
        ]
    )
    parse_receipt_text_with_llm("Pet food $32", db=db_session, gateway=gateway)
    parse_receipt_orders_text_with_llm("Pet food $32", db=db_session, gateway=gateway)
    assert len(gateway.text_requests) == 2
    assert "Pet Care" in gateway.text_requests[0].user_prompt
    assert "Pet Care" in gateway.text_requests[1].user_prompt


def test_media_and_injectable_pdf_extractor_and_errors() -> None:
    assert infer_receipt_mime_type("scan.heic", "application/octet-stream") == "image/heic"
    assert infer_receipt_mime_type("slip.pdf", None) == "application/pdf"

    png_bytes = _make_png_bytes(1800, 1800)
    compressed, comp_mime = compress_receipt_image(png_bytes, "image/png", max_dimension=1000)
    assert comp_mime == "image/webp"
    assert len(compressed) < len(png_bytes)

    llm_bytes, llm_mime = prepare_image_for_llm(png_bytes, "image/png", max_dimension=1000)
    assert llm_mime == "image/jpeg"
    assert len(llm_bytes) > 0

    calls: list[bytes] = []

    def fake_extractor(data: bytes) -> str:
        calls.append(data)
        return "Nordic Goods\nWhey Protein 1kg\nTotal 79.00 NZD"

    gateway = InMemoryAiGateway(
        text_responses=[
            {"merchant": "Nordic Goods", "total_amount": 79.0},
            {"employer": "Acme Payroll", "net_pay": 4200.0},
        ],
        vision_responses=[
            {"employer": "Acme Payroll Vision", "net_pay": 4300.0},
        ],
    )
    parsed_receipt = parse_receipt_with_llm(
        b"%PDF-fake",
        "receipt.pdf",
        "application/pdf",
        gateway=gateway,
        pdf_text_extractor=fake_extractor,
    )
    assert parsed_receipt == {"merchant": "Nordic Goods", "total_amount": 79.0}
    assert calls == [b"%PDF-fake"]

    parsed_payslip = parse_payslip_with_llm(
        b"%PDF-payslip",
        "payslip.pdf",
        "application/pdf",
        gateway=gateway,
        pdf_text_extractor=lambda _b: "Employer: Acme Payroll\nNet Pay: 4200.00",
    )
    assert parsed_payslip == {"employer": "Acme Payroll", "net_pay": 4200.0}

    vision_payslip = parse_payslip_with_llm(
        b"%PDF-scanned",
        "scan.pdf",
        "application/pdf",
        gateway=gateway,
        pdf_text_extractor=lambda _b: "",
    )
    assert vision_payslip == {"employer": "Acme Payroll Vision", "net_pay": 4300.0}
    assert len(gateway.vision_requests) == 1

    err_gateway = InMemoryAiGateway(
        text_responses=[
            AiInvalidResponseError("bad json"),
            AiConfigError("missing key"),
            AiUpstreamError("gateway down", status_code=503),
            AiUpstreamError("gateway down", status_code=503),
        ]
    )
    assert parse_receipt_text_with_llm("x", raise_on_error=False, gateway=err_gateway) is None
    with pytest.raises(ReceiptParseError):
        parse_receipt_text_with_llm("x", raise_on_error=True, gateway=err_gateway)
    with pytest.raises(ReceiptUpstreamError):
        parse_payslip_text_with_llm("x", raise_on_error=True, gateway=err_gateway)
    assert parse_payslip_text_with_llm("x", raise_on_error=False, gateway=err_gateway) is None


def test_classification_rules_jev_and_detached_snapshots() -> None:
    heuristic = classify_line_item("Pure Vitamin C Serum 30ml")
    assert isinstance(heuristic, LineItemHeuristic)
    assert heuristic == LineItemHeuristic("Personal Care", "02-skin", "vitamin_c_serum", "30ml")
    assert "vitamin-c" in extract_tags_for_line_item("Pure Vitamin C Serum 30ml", "02-skin", "vitamin_c_serum")

    session_factory, engine = _new_memory_session_factory()
    try:
        with session_factory() as db:
            receipt = Receipt(
                receipt_date=date(2026, 10, 12),
                merchant="Nordic Goods",
                total_amount=50.0,
                currency="NZD",
                status="unlinked",
            )
            rule = CategorizationRule(
                name="Coffee Rule",
                description_pattern="espresso",
                match_type="contains",
                target_category="Dining",
                target_tags='["Coffee", "Morning Brew"]',
                priority=1,
                is_active=True,
                match_count=0,
            )
            tx = Transaction(
                transaction_date=date(2026, 10, 10),
                account_id=_add_account(db).id,
                amount=-12.0,
                currency="NZD",
                merchant="Bean Co",
                category="Dining",
                raw_hash="tag-seed-tx",
                tags='["specialty-roast"]',
            )
            db.add_all([receipt, rule, tx])
            db.commit()

            gateway = InMemoryAiGateway(
                jev_responses=[
                    json.dumps(
                        {
                            "category": "Tech",
                            "primary_tag": "specialty-roast",
                            "is_anomaly": False,
                            "is_subscription": False,
                        }
                    ),
                    json.dumps(
                        {
                            "category": "Tech",
                            "primary_tag": "none",
                            "is_anomaly": False,
                            "is_subscription": False,
                        }
                    ),
                ]
            )

            single = classify_receipt_item_draft(
                db,
                draft=ReceiptItemDraft(
                    item_name="Wireless Ergonomic Stand",
                    total_price=35.0,
                    explicit_tags=("  Desk Setup ", "Tech"),
                ),
                merchant="Nordic Goods",
                gateway=gateway,
            )
            assert isinstance(single, ItemClassification)
            assert single.category == "Tech"
            assert "desk-setup" in single.tags
            assert "tech" not in single.tags

            snapshots = persist_classified_receipt_items(
                db,
                receipt_id=receipt.id,
                merchant="Nordic Goods",
                drafts=[
                    ReceiptItemDraft(
                        item_name="Single Origin Espresso Beans",
                        brand="Harbour Roast",
                        quantity=1.0,
                        unit_price=24.0,
                        total_price=24.0,
                    ),
                    ReceiptItemDraft(
                        item_name="USB-C Desk Dock",
                        quantity=1.0,
                        unit_price=40.0,
                        total_price=40.0,
                    ),
                    ReceiptItemDraft(
                        item_name="Basket Promo Credit",
                        quantity=1.0,
                        unit_price=-5.0,
                        total_price=-5.0,
                    ),
                ],
                gateway=gateway,
            )
            db.commit()
            rule_hits = db.scalar(
                select(CategorizationRule.match_count).where(CategorizationRule.id == rule.id)
            )
            product_count = len(db.scalars(select(Product)).all())
            neg_item = db.scalar(
                select(ReceiptItem).where(ReceiptItem.item_name == "Basket Promo Credit")
            )
            assert rule_hits == 1
            assert product_count == 2
            assert neg_item is not None and neg_item.product_id is None

            with pytest.raises(ReceiptNotFoundError):
                persist_classified_receipt_items(
                    db,
                    receipt_id=999999,
                    merchant="Missing",
                    drafts=[],
                )

        assert len(snapshots) == 3
        assert isinstance(snapshots[0], ReceiptItemSnapshot)
        assert snapshots[0].category == "Dining"
        assert snapshots[0].tags == ("coffee", "morning-brew")
        assert snapshots[1].category == "Tech"
        assert snapshots[1].product_id is not None
        assert snapshots[2].product_id is None

        with pytest.raises(FrozenInstanceError):
            snapshots[0].category = "Mutated"  # type: ignore[misc]
    finally:
        Base.metadata.drop_all(bind=engine)


def test_apply_receipt_discounts_with_jev_and_immutability() -> None:
    drafts = (
        ReceiptItemDraft(item_name="Nordic Whey Protein", unit_price=80.0, total_price=80.0),
        ReceiptItemDraft(item_name="Ceramic Mug", unit_price=10.0, total_price=10.0),
        ReceiptItemDraft(item_name="Member Savings Whey", unit_price=-12.0, total_price=-12.0),
        ReceiptItemDraft(item_name="Mug Discount", unit_price=-5.0, total_price=-5.0),
        ReceiptItemDraft(item_name="Excess Mug Rebate", unit_price=-8.0, total_price=-8.0),
        ReceiptItemDraft(item_name="Storewide Loyalty Credit", unit_price=-4.0, total_price=-4.0),
    )
    gateway = InMemoryAiGateway(
        jev_responses=[
            json.dumps({"target_index": "0"}),
            "invalid-json-triggers-heuristic",
            json.dumps({"target_index": "1"}),
            json.dumps({"target_index": "none"}),
        ]
    )
    result = apply_receipt_discounts_with_jev(drafts, gateway=gateway)

    assert drafts[0].total_price == 80.0
    assert drafts[0].discount_amount is None
    assert len(result) == 4
    assert result[0].total_price == 68.0
    assert result[0].discount_amount == 12.0
    assert "Member Savings Whey" in (result[0].notes or "")

    assert result[1].total_price == 5.0
    assert result[1].discount_amount == 5.0

    unmatched_names = [item.item_name for item in result[2:]]
    assert unmatched_names == ["Excess Mug Rebate", "Storewide Loyalty Credit"]


def test_three_tier_matching_payslips_and_reconcile() -> None:
    assert parse_original_fx_from_description("Card transaction of 179.74 AUD issued by Shop") == (
        179.74,
        "AUD",
    )
    assert parse_original_fx_from_description("USD 45.00 @ 0.6000") == (45.0, "USD")

    session_factory, engine = _new_memory_session_factory()
    try:
        with session_factory() as db:
            nzd_acc = _add_account(db, name="Wise NZD", currency="NZD")
            aud_acc = _add_account(db, name="Wise AUD", currency="AUD")

            tx_exact = Transaction(
                transaction_date=date(2026, 11, 2),
                account_id=nzd_acc.id,
                amount=-55.00,
                currency="NZD",
                merchant="Harbour Market",
                category="Groceries",
                raw_hash="tx-exact",
            )
            tx_fx_desc = Transaction(
                transaction_date=date(2026, 11, 16),
                account_id=nzd_acc.id,
                amount=-196.40,
                currency="NZD",
                merchant="Nordic Tech",
                category="Shopping",
                raw_description="Card transaction of 179.74 AUD issued by Nordic Tech AU",
                raw_hash="tx-fx-desc",
            )
            tx_fx_est = Transaction(
                transaction_date=date(2026, 11, 5),
                account_id=nzd_acc.id,
                amount=-100.00,
                currency="NZD",
                merchant="Euro Bistro",
                category="Dining",
                raw_hash="tx-fx-est",
            )
            tx_ambig_1 = Transaction(
                transaction_date=date(2026, 11, 9),
                account_id=nzd_acc.id,
                amount=-25.00,
                currency="NZD",
                merchant="Metro Cafe",
                category="Dining",
                raw_hash="tx-ambig-1",
            )
            tx_ambig_2 = Transaction(
                transaction_date=date(2026, 11, 9),
                account_id=aud_acc.id,
                amount=-25.00,
                currency="NZD",
                merchant="Metro Cafe",
                category="Dining",
                raw_hash="tx-ambig-2",
            )
            tx_pay_inflow = Transaction(
                transaction_date=date(2026, 11, 14),
                account_id=nzd_acc.id,
                amount=4100.00,
                currency="NZD",
                merchant="Acme Payroll",
                category="Income",
                raw_hash="tx-pay-inflow",
            )
            db.add(
                ExchangeRate(
                    rate_date=date(2026, 11, 5),
                    from_currency="NZD",
                    to_currency="EUR",
                    rate=0.55,
                    source="manual-test",
                )
            )
            db.add_all(
                [tx_exact, tx_fx_desc, tx_fx_est, tx_ambig_1, tx_ambig_2, tx_pay_inflow]
            )
            db.flush()

            rec_exact = Receipt(
                receipt_date=date(2026, 11, 2),
                merchant="Harbour Market",
                total_amount=55.00,
                currency="NZD",
                status="unlinked",
            )
            rec_online_fx = Receipt(
                receipt_date=date(2026, 11, 1),
                merchant="Nordic Tech",
                total_amount=179.74,
                currency="AUD",
                order_number="ORD-991",
                status="unlinked",
            )
            rec_fx_est = Receipt(
                receipt_date=date(2026, 11, 5),
                merchant="Euro Bistro",
                total_amount=55.00,
                currency="EUR",
                status="unlinked",
            )
            rec_ambig = Receipt(
                receipt_date=date(2026, 11, 9),
                merchant="Metro Cafe",
                total_amount=25.00,
                currency="NZD",
                status="unlinked",
            )
            rec_verified = Receipt(
                receipt_date=date(2026, 11, 9),
                merchant="Metro Cafe",
                total_amount=25.00,
                currency="NZD",
                status="verified",
            )
            rec_payslip = Receipt(
                document_type="payslip",
                receipt_date=date(2026, 11, 1),
                merchant="Acme Payroll",
                total_amount=4100.00,
                currency="NZD",
                status="unlinked",
            )
            rec_physical_late = Receipt(
                receipt_date=date(2026, 10, 20),
                merchant="Harbour Market",
                total_amount=55.00,
                currency="NZD",
                status="unlinked",
            )
            db.add_all(
                [
                    rec_exact,
                    rec_online_fx,
                    rec_fx_est,
                    rec_ambig,
                    rec_verified,
                    rec_payslip,
                    rec_physical_late,
                ]
            )
            db.commit()

            with pytest.raises(ReceiptNotFoundError):
                find_receipt_transaction_candidates(db, 999999)
            with pytest.raises(ReceiptNotFoundError):
                match_receipt_to_transaction(db, 999999)

            fx_candidates = find_receipt_transaction_candidates(db, rec_fx_est.id, limit=5)
            assert len(fx_candidates) == 1
            assert isinstance(fx_candidates[0], TransactionCandidateSnapshot)
            assert fx_candidates[0].match_reason == "fx_estimated"
            assert fx_candidates[0].account_name == "Wise NZD"

            fx_outcome = match_receipt_to_transaction(db, rec_fx_est.id)
            assert isinstance(fx_outcome, ReceiptMatchOutcome)
            assert fx_outcome.matched_transaction_id is None
            assert fx_outcome.status == "needs_review"
            assert fx_outcome.candidate_count == 1

            ambig_outcome = match_receipt_to_transaction(db, rec_ambig.id)
            assert ambig_outcome.status == "needs_review"
            assert ambig_outcome.matched_transaction_id is None

            verified_outcome = match_receipt_to_transaction(db, rec_verified.id)
            assert verified_outcome.status == "verified"

            late_outcome = match_receipt_to_transaction(db, rec_physical_late.id)
            assert late_outcome.status == "unlinked"

            matched_count = reconcile_unlinked_receipts(db)
            assert matched_count == 3

            db.refresh(rec_exact)
            db.refresh(rec_online_fx)
            db.refresh(rec_payslip)
            assert rec_exact.transaction_id == tx_exact.id
            assert rec_online_fx.transaction_id == tx_fx_desc.id
            assert rec_payslip.transaction_id == tx_pay_inflow.id

        with pytest.raises(FrozenInstanceError):
            fx_candidates[0].confidence = 0.0  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            fx_outcome.status = "matched"  # type: ignore[misc]
    finally:
        Base.metadata.drop_all(bind=engine)


def test_semantic_duplicate_detection_receipt_and_payslip() -> None:
    session_factory, engine = _new_memory_session_factory()
    try:
        with session_factory() as db:
            rec = Receipt(
                document_type="receipt",
                receipt_date=date(2026, 11, 10),
                merchant="Nordic Goods",
                total_amount=89.90,
                currency="NZD",
                order_number="ORD-500",
                status="unlinked",
            )
            payslip = Receipt(
                document_type="payslip",
                receipt_date=date(2026, 11, 15),
                merchant="Acme Payroll",
                total_amount=4200.00,
                currency="NZD",
                order_number="2026-11-01 to 2026-11-15",
                status="unlinked",
            )
            db.add_all([rec, payslip])
            db.commit()

            by_order = find_semantic_duplicate_receipt(
                db,
                merchant="Different Alias",
                receipt_date=date(2026, 11, 20),
                total_amount=89.90,
                currency="NZD",
                order_number="ord-500",
                document_type="receipt",
            )
            assert by_order is not None
            assert isinstance(by_order, SemanticDuplicateMatch)
            assert by_order.matched_by == "order_number"
            assert by_order.receipt_id == rec.id

            by_merchant = find_semantic_duplicate_receipt(
                db,
                merchant="Nordic Goods Pty Ltd",
                receipt_date=date(2026, 11, 11),
                total_amount=89.90,
                currency="NZD",
                order_number=None,
                document_type="receipt",
            )
            assert by_merchant is not None
            assert by_merchant.matched_by == "merchant_date_amount"

            conflicting_order = find_semantic_duplicate_receipt(
                db,
                merchant="Nordic Goods",
                receipt_date=date(2026, 11, 10),
                total_amount=89.90,
                currency="NZD",
                order_number="ORD-999",
                document_type="receipt",
            )
            assert conflicting_order is None

            payslip_far_date = find_semantic_duplicate_receipt(
                db,
                merchant="Acme Payroll",
                receipt_date=date(2026, 12, 1),
                total_amount=4200.00,
                currency="NZD",
                order_number="2026-11-01 to 2026-11-15",
                document_type="payslip",
            )
            assert payslip_far_date is None

            payslip_dup = find_semantic_duplicate_receipt(
                db,
                merchant="Acme Payroll Ltd",
                receipt_date=date(2026, 11, 16),
                total_amount=4200.00,
                currency="NZD",
                order_number="2026-11-01 to 2026-11-15",
                document_type="payslip",
            )
            assert payslip_dup is not None
            assert payslip_dup.matched_by == "merchant_date_amount"

        with pytest.raises(FrozenInstanceError):
            by_order.matched_by = "mutated"  # type: ignore[misc]
    finally:
        Base.metadata.drop_all(bind=engine)
