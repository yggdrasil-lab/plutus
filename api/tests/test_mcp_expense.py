"""End-to-end tests for MCP _run_plutus_log_expense through the unified ledger_entry write seam."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
import json
from pathlib import Path
import sys

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base
from app.models import Account, CategorizationRule, Receipt, Transaction
from app.routers.mcp import _run_plutus_log_expense
from app.services.ai_gateway import InMemoryAiGateway
from app.services.fx_service import cache_exchange_rate
from app.services.ledger_entry import parse_rule_tags


@pytest.fixture()
def db_session() -> Iterator[Session]:
    """Provide an isolated SQLite :memory: session for MCP expense tests."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = session_local()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_mcp_log_expense_creates_primary_checking_and_short_circuits_duplicate_before_jev(
    db_session: Session,
) -> None:
    gateway = InMemoryAiGateway()
    tx_date = date(2026, 11, 22)

    raw_res = _run_plutus_log_expense(
        amount=18.50,
        merchant="Cafe Meridian",
        category="Dining",
        notes="Lunch coffee",
        db=db_session,
        reference_date=tx_date,
        ai_gateway=gateway,
    )
    payload = json.loads(raw_res)
    assert payload["status"] == "success"
    assert payload["merchant"] == "Cafe Meridian"
    assert payload["amount"] == pytest.approx(-18.50)
    assert payload["category"] == "Dining"
    assert len(gateway.jev_requests) == 0

    account = db_session.scalar(select(Account))
    assert account is not None
    assert account.name == "Primary Checking"
    assert account.account_type == "checking"

    tx = db_session.get(Transaction, payload["id"])
    assert tx is not None
    assert tx.raw_description == "AgentLog: Cafe Meridian"
    assert float(tx.amount) == pytest.approx(-18.50)

    # Identical call on same day short-circuits BEFORE Jev even when category is General.
    dup_res = _run_plutus_log_expense(
        amount=18.50,
        merchant="Cafe Meridian",
        category="General",
        db=db_session,
        reference_date=tx_date,
        ai_gateway=gateway,
    )
    dup_payload = json.loads(dup_res)
    assert dup_payload == {
        "status": "duplicate",
        "message": f"Transaction already logged on {tx_date}",
    }
    assert len(gateway.jev_requests) == 0


def test_mcp_log_expense_applies_categorization_rule_and_counterpart_attribution(
    db_session: Session,
) -> None:
    checking = Account(
        name="North Checking",
        institution="North Bank",
        account_type="checking",
        tier="cash",
        currency="NZD",
        is_active=True,
    )
    savings = Account(
        name="Reserve Savings",
        institution="North Bank",
        account_type="savings",
        tier="cash",
        currency="NZD",
        is_active=True,
    )
    db_session.add_all([checking, savings])
    db_session.flush()
    db_session.add(
        CategorizationRule(
            name="Meridian Rule",
            description_pattern="Cafe Meridian",
            match_type="exact",
            target_category="Dining",
            target_tags='["#Coffee", "Work Lunch"]',
            rename_merchant="Cafe Meridian Flagship",
            counterpart_account_id=savings.id,
            priority=1,
            is_active=True,
        )
    )
    db_session.commit()

    gateway = InMemoryAiGateway()
    res = _run_plutus_log_expense(
        amount=12.40,
        merchant="Cafe Meridian",
        category="General",
        db=db_session,
        reference_date=date(2026, 11, 22),
        ai_gateway=gateway,
    )
    payload = json.loads(res)
    assert payload["status"] == "success"
    assert payload["merchant"] == "Cafe Meridian Flagship"
    assert payload["category"] == "Dining"
    assert len(gateway.jev_requests) == 0

    stored = db_session.get(Transaction, payload["id"])
    assert stored is not None
    assert stored.merchant == "Cafe Meridian Flagship"
    assert stored.category == "Dining"
    assert parse_rule_tags(stored.tags) == ["coffee", "work-lunch"]
    assert stored.counterpart_account_id == savings.id


def test_mcp_log_expense_uses_jev_fallback_tags_and_anomaly_note(db_session: Session) -> None:
    gateway = InMemoryAiGateway(
        jev_responses=[
            json.dumps(
                {
                    "category": "Medical",
                    "is_anomaly": True,
                    "tags": ["General", "#Medical", "Urgent Visit"],
                }
            )
        ]
    )
    res = _run_plutus_log_expense(
        amount=320.00,
        merchant="Harbour Clinic 1001",
        category="General",
        notes="Clinic consult",
        db=db_session,
        reference_date=date(2026, 11, 22),
        ai_gateway=gateway,
    )
    payload = json.loads(res)
    assert payload["status"] == "success"
    assert payload["category"] == "Medical"
    assert len(gateway.jev_requests) == 1

    stored = db_session.get(Transaction, payload["id"])
    assert stored is not None
    assert parse_rule_tags(stored.tags) == ["urgent-visit"]
    assert stored.notes == "Clinic consult [Anomaly Detected]"


def test_mcp_log_expense_resolves_non_nzd_fx_and_reconciles_receipt(db_session: Session) -> None:
    tx_date = date(2026, 11, 22)
    sgd_checking = Account(
        name="North SGD Checking",
        institution="North Bank",
        account_type="checking",
        tier="cash",
        currency="SGD",
        is_active=True,
    )
    receipt = Receipt(
        receipt_date=tx_date,
        merchant="Harbour Market",
        total_amount=45.00,
        currency="SGD",
        status="unlinked",
    )
    db_session.add_all([sgd_checking, receipt])
    cache_exchange_rate(
        db_session,
        rate_date=tx_date,
        from_currency="SGD",
        to_currency="NZD",
        rate=1.28,
        source="stub",
    )
    db_session.commit()

    res = _run_plutus_log_expense(
        amount=45.00,
        merchant="Harbour Market",
        category="Groceries",
        db=db_session,
        reference_date=tx_date,
    )
    payload = json.loads(res)
    assert payload["status"] == "success"

    stored = db_session.get(Transaction, payload["id"])
    assert stored is not None
    assert stored.currency == "SGD"
    assert float(stored.exchange_rate) == pytest.approx(1.28)

    db_session.refresh(receipt)
    assert receipt.status == "matched"
    assert receipt.transaction_id == stored.id
