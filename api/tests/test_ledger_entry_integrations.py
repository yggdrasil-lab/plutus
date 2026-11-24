"""Direct interface and HTTP parity tests for Slice 03 integration workflows."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path
import sys
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base, get_db
from app.main import app
from app.models import Account, AssetSnapshot, IngestedEmail, Transaction
from app.routers import integrations as integrations_router
from app.schemas import InboundEmailDispatchPayload, SyncAccountSummary, SyncResult
from app.services.ai_gateway import AiUpstreamError, InMemoryAiGateway, get_ai_gateway
from app.services.connectors import registry as connector_registry
from app.services.connectors.registry import (
    InvalidSyncDateRangeError,
    UnsupportedSyncProviderError,
    sync_provider,
)
from app.services.ledger_entry import (
    InboundEmailDispatchOutcome,
    LedgerEntryBadRequestError,
    LedgerEntryUpstreamError,
    LedgerEntryValidationError,
    StatementUploadInput,
    commit_parsed_statement,
    dispatch_inbound_email,
    process_statement_upload,
)
from app.services.statement_parser import ParsedTransaction, StatementParseResult
from app.services.sync_scheduler import run_scheduled_sync_once


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


@pytest.fixture()
def session_and_client() -> Iterator[tuple[sessionmaker[Session], Session, TestClient]]:
    """Provide an isolated session factory, DB session, and FastAPI TestClient."""
    session_factory, engine = _new_memory_session_factory()
    db = session_factory()

    def _override_get_db() -> Iterator[Session]:
        local_db = session_factory()
        try:
            yield local_db
        finally:
            local_db.close()

    app.dependency_overrides[get_db] = _override_get_db
    client = TestClient(app)
    try:
        yield session_factory, db, client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_ai_gateway, None)
        db.close()
        Base.metadata.drop_all(bind=engine)


def _sample_csv_bytes() -> bytes:
    csv_text = (
        "Date,Payee,Amount,Balance\n"
        "2026-11-10,North Roast Cafe,-14.50,985.50\n"
        "2026-11-12,Metro Grocery,-62.00,923.50\n"
    )
    return csv_text.encode("utf-8")


def _sample_parse_result(
    *,
    institution: str = "Unknown",
    account_name: str | None = None,
    closing_balance: float | None = 500.0,
    end_date: date | None = date(2026, 11, 15),
    currency: str = "NZD",
) -> StatementParseResult:
    return StatementParseResult(
        institution=institution,
        account_name=account_name,
        transaction_count=1,
        transactions=[
            ParsedTransaction(
                transaction_date=date(2026, 11, 15),
                amount=-25.00,
                merchant="Harbour Roasters 1001",
                raw_description="HARBOUR ROASTERS 1001",
                category="Dining",
                currency=currency,
            )
        ],
        start_date=date(2026, 11, 1),
        end_date=end_date,
        closing_balance=closing_balance,
        currency=currency,
    )


def test_process_statement_upload_rejects_unsupported_extension(db_session: Session) -> None:
    with pytest.raises(LedgerEntryBadRequestError, match="Unsupported file format"):
        process_statement_upload(
            db_session,
            StatementUploadInput(filename="notes.txt", content=b"hello"),
        )


def test_process_statement_upload_dry_run_applies_overrides_without_writes(
    db_session: Session,
) -> None:
    res = process_statement_upload(
        db_session,
        StatementUploadInput(
            filename="export.csv",
            content=_sample_csv_bytes(),
            commit=False,
            closing_balance=1234.56,
            currency=" sgd ",
        ),
    )

    assert res.committed is False
    assert res.account_id is None
    assert res.account_created is False
    assert res.snapshot_upserted is False
    assert res.parse_result.closing_balance == 1234.56
    assert res.parse_result.currency == "SGD"
    assert res.parse_result.end_date is not None
    assert db_session.scalars(select(Account)).all() == []
    assert db_session.scalars(select(Transaction)).all() == []


def test_commit_parsed_statement_matches_explicit_account_id_and_upserts_snapshot(
    db_session: Session,
) -> None:
    account = Account(
        name="Pacific Checking 1001",
        account_type="checking",
        tier="cash",
        institution="Pacific Bank",
        currency="NZD",
        is_active=True,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)

    res = commit_parsed_statement(
        db_session,
        parse_result=_sample_parse_result(institution="Other Bank", closing_balance=450.25),
        filename="statement.pdf",
        account_id=account.id,
    )

    assert res.committed is True
    assert res.account_id == account.id
    assert res.account_created is False
    assert res.snapshot_upserted is True

    tx = db_session.scalar(select(Transaction))
    assert tx is not None
    assert tx.account_id == account.id
    snap = db_session.scalar(select(AssetSnapshot))
    assert snap is not None
    assert snap.source == "statement_pdf"
    assert snap.notes == "Auto-anchored from statement.pdf"
    assert float(snap.balance) == pytest.approx(450.25)


def test_commit_parsed_statement_matches_existing_account_by_institution_and_name(
    db_session: Session,
) -> None:
    acc_main = Account(
        name="Harbour Everyday",
        account_type="checking",
        tier="cash",
        institution="Harbour Bank",
        currency="NZD",
        is_active=True,
    )
    acc_savings = Account(
        name="Harbour Savings",
        account_type="savings",
        tier="cash",
        institution="Harbour Bank",
        currency="NZD",
        is_active=True,
    )
    db_session.add_all([acc_main, acc_savings])
    db_session.commit()
    db_session.refresh(acc_savings)

    res = commit_parsed_statement(
        db_session,
        parse_result=_sample_parse_result(
            institution="Harbour Bank",
            closing_balance=None,
            end_date=None,
        ),
        filename="harbour.csv",
        institution="Harbour Bank",
        account_name="Savings",
    )

    assert res.account_created is False
    assert res.account_id == acc_savings.id
    assert res.snapshot_upserted is False


@pytest.mark.parametrize(
    ("institution", "expected_tier", "expected_type", "expected_return"),
    [
        ("Simplicity KiwiSaver", "locked", "kiwisaver", 0.065),
        ("Sharesies Portfolio", "growth", "brokerage", 0.07),
        ("Unknown", "cash", "checking", 0.0),
    ],
)
def test_commit_parsed_statement_autocreates_tier_accounts(
    db_session: Session,
    institution: str,
    expected_tier: str,
    expected_type: str,
    expected_return: float,
) -> None:
    res = commit_parsed_statement(
        db_session,
        parse_result=_sample_parse_result(institution=institution, closing_balance=900.0),
        filename="export.csv",
        institution="auto",
    )

    assert res.committed is True
    assert res.account_created is True
    assert res.snapshot_upserted is True

    created = db_session.get(Account, res.account_id)
    assert created is not None
    assert created.tier == expected_tier
    assert created.account_type == expected_type
    assert float(created.expected_annual_return) == pytest.approx(expected_return)
    snap = db_session.scalar(
        select(AssetSnapshot).where(AssetSnapshot.account_id == created.id)
    )
    assert snap is not None
    assert snap.source == "statement_csv"


def test_http_statement_upload_parity_400_and_200(
    session_and_client: tuple[sessionmaker[Session], Session, TestClient],
) -> None:
    _factory, db, client = session_and_client

    bad_resp = client.post(
        "/integrations/statement",
        files={"file": ("bad.txt", b"invalid", "text/plain")},
    )
    assert bad_resp.status_code == 400

    ok_resp = client.post(
        "/integrations/statement?institution=Pacific%20Bank&commit=true&closing_balance=923.50",
        files={"file": ("pacific.csv", _sample_csv_bytes(), "text/csv")},
    )
    assert ok_resp.status_code == 200
    body = ok_resp.json()
    assert body["institution"] == "Pacific Bank"
    assert body["transaction_count"] == 2
    assert db.scalar(select(Account)) is not None
    assert len(db.scalars(select(Transaction)).all()) == 2


def test_dispatch_inbound_email_staging_and_detached_duplicate(
    session_and_client: tuple[sessionmaker[Session], Session, TestClient],
) -> None:
    session_factory, _db, _client = session_and_client
    payload = InboundEmailDispatchPayload(
        email_id=401,
        message_id="<stmt-sync-1@example.test>",
        route_target="plutus_bank_statement",
        subject="Monthly Statement",
    )

    db1 = session_factory()
    first = dispatch_inbound_email(db1, payload)
    db1.close()

    assert isinstance(first, InboundEmailDispatchOutcome)
    assert first.status == "needs_review"
    assert first.consumer_ref == "staged:statement"
    assert first.duplicate is False

    db2 = session_factory()
    dup = dispatch_inbound_email(db2, payload)
    db2.close()

    assert dup.status == "needs_review"
    assert dup.duplicate is True
    assert dup.consumer_ref == "staged:statement"
    assert dup.message_id == "<stmt-sync-1@example.test>"


def test_dispatch_inbound_email_unknown_target_does_not_persist_failed_row(
    db_session: Session,
) -> None:
    payload = InboundEmailDispatchPayload.model_construct(
        email_id=402,
        message_id="<unknown-route@example.test>",
        route_target="plutus_unknown_target",
        subject="Unknown",
    )

    with pytest.raises(LedgerEntryValidationError, match="Unsupported route_target"):
        dispatch_inbound_email(db_session, payload)

    assert db_session.scalars(select(IngestedEmail)).all() == []


def test_dispatch_inbound_email_validation_failure_and_retry_recovery(
    db_session: Session,
) -> None:
    bad_payload = InboundEmailDispatchPayload(
        email_id=403,
        message_id="<retry-flow@example.test>",
        route_target="plutus_dbs_alert",
        subject="Alert",
        text_body="",
        html_excerpt="",
        raw_excerpt="",
    )

    with pytest.raises(LedgerEntryValidationError, match="no readable body"):
        dispatch_inbound_email(db_session, bad_payload)

    failed_row = db_session.scalar(
        select(IngestedEmail).where(IngestedEmail.message_id == "<retry-flow@example.test>")
    )
    assert failed_row is not None
    assert failed_row.status == "failed"
    assert failed_row.error_detail is not None

    retry_payload = InboundEmailDispatchPayload(
        email_id=403,
        message_id="<retry-flow@example.test>",
        route_target="plutus_purchase_receipt",
        subject="Demo Store Receipt",
        text_body="Demo Store Total NZD 24.50",
    )
    gateway = InMemoryAiGateway(
        text_responses=[
            {
                "orders": [
                    {
                        "order_number": "ORD-2002",
                        "merchant": "Demo Store",
                        "receipt_date": "2026-11-20",
                        "currency": "NZD",
                        "total_amount": 24.50,
                        "items": [],
                    }
                ]
            }
        ]
    )
    outcome = dispatch_inbound_email(db_session, retry_payload, ai_gateway=gateway)
    assert outcome.status == "processed"
    assert outcome.duplicate is False
    assert outcome.receipt_id is not None

    db_session.refresh(failed_row)
    assert failed_row.status == "processed"
    assert failed_row.error_detail is None
    assert failed_row.consumer_ref == f"receipt:{outcome.receipt_id}"


def test_dispatch_inbound_email_upstream_error_persists_failed_and_http_503(
    session_and_client: tuple[sessionmaker[Session], Session, TestClient],
) -> None:
    _factory, db, client = session_and_client
    gateway = InMemoryAiGateway(
        text_responses=[
            AiUpstreamError("upstream timeout"),
            AiUpstreamError("upstream timeout http"),
        ]
    )

    direct_payload = InboundEmailDispatchPayload(
        email_id=404,
        message_id="<upstream-direct@example.test>",
        route_target="plutus_purchase_receipt",
        subject="Order Receipt",
        text_body="Demo Store NZD 19.99",
    )
    with pytest.raises(LedgerEntryUpstreamError):
        dispatch_inbound_email(db, direct_payload, ai_gateway=gateway)

    audit = db.scalar(
        select(IngestedEmail).where(IngestedEmail.message_id == "<upstream-direct@example.test>")
    )
    assert audit is not None
    assert audit.status == "failed"

    app.dependency_overrides[get_ai_gateway] = lambda: gateway
    http_resp = client.post(
        "/integrations/email",
        json={
            "email_id": 405,
            "message_id": "<upstream-http@example.test>",
            "route_target": "plutus_purchase_receipt",
            "subject": "Order Receipt",
            "text_body": "Demo Store NZD 19.99",
        },
    )
    assert http_resp.status_code == 503


def test_shared_provider_registry_and_scheduler_integration(db_session: Session) -> None:
    assert integrations_router.SYNC_HANDLERS is connector_registry.SYNC_HANDLERS

    with pytest.raises(InvalidSyncDateRangeError):
        sync_provider(
            db_session,
            "akahu",
            start_date=date(2026, 11, 20),
            end_date=date(2026, 11, 19),
        )

    with pytest.raises(UnsupportedSyncProviderError):
        sync_provider(db_session, "unknown_provider")

    calls: list[tuple[str, int, date | None, date | None, bool]] = []

    def _fake_akahu(_db: Session, days: int, sd: date | None, ed: date | None, skip: bool) -> SyncResult:
        calls.append(("akahu", days, sd, ed, skip))
        return SyncResult(
            provider="akahu",
            accounts_synced=1,
            transactions_created=2,
            transactions_skipped=1,
            snapshots_updated=1,
            accounts=[
                SyncAccountSummary(
                    account_id=10,
                    account_name="Checking 1001",
                    institution="Pacific Bank",
                    currency="NZD",
                )
            ],
        )

    def _fake_wise(_db: Session, days: int, sd: date | None, ed: date | None, skip: bool) -> SyncResult:
        calls.append(("wise", days, sd, ed, skip))
        return SyncResult(
            provider="wise",
            accounts_synced=1,
            transactions_created=3,
            transactions_skipped=0,
            snapshots_updated=1,
        )

    single = sync_provider(
        db_session,
        "akahu",
        days=7,
        skip_llm=True,
        handlers={"akahu": _fake_akahu, "wise": _fake_wise},
    )
    assert single.provider == "akahu"
    assert single.transactions_created == 2

    combined = sync_provider(
        db_session,
        "all",
        days=14,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 15),
        handlers={"akahu": _fake_akahu, "wise": _fake_wise},
    )
    assert combined.provider == "all"
    assert combined.accounts_synced == 2
    assert combined.transactions_created == 5
    assert combined.transactions_skipped == 1
    assert combined.snapshots_updated == 2
    assert len(combined.accounts) == 1

    with patch.dict(
        connector_registry.SYNC_HANDLERS,
        {"akahu": _fake_akahu, "wise": _fake_wise},
        clear=True,
    ):
        registry_outcomes = run_scheduled_sync_once(
            db_factory=lambda: db_session,
            providers=["akahu"],
            days=10,
        )
        assert isinstance(registry_outcomes["akahu"], SyncResult)
        assert calls[-1] == ("akahu", 10, None, None, False)

    legacy_outcomes = run_scheduled_sync_once(
        db_factory=lambda: db_session,
        providers=["wise"],
        handlers={"wise": lambda _db, days: SyncResult(provider="wise", transactions_created=days)},
        days=12,
    )
    assert isinstance(legacy_outcomes["wise"], SyncResult)
    assert legacy_outcomes["wise"].transactions_created == 12
