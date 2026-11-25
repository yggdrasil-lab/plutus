"""Direct seam tests for CrossSourceReconciler, bounded note dedup, and AiGateway threading."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sys
from typing import Any, Callable, Optional, Sequence
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base, get_db
from app.main import app
from app.models import Account, Transaction
from app.services.ai_gateway import AiGateway, InMemoryAiGateway, get_ai_gateway
from app.services.connectors.akahu import sync_akahu
from app.services.connectors.bitcoin import sync_bitcoin
import app.services.connectors.registry as connector_registry
from app.services.connectors.registry import sync_provider
from app.services.connectors.wise import sync_wise
from app.services.cross_source_reconciler import (
    CrossSourceMatchResult,
    DuplicateNoteResult,
)
from app.services.ingestion_service import (
    ExternalAccountSpec,
    NormalizedTx,
    ProviderAccountFeed,
    ingest_normalized_transactions_detailed,
)
from app.services.sync_scheduler import run_scheduled_sync_once

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class _FakeGateway:
    """In-memory BankFeedGateway yielding prebuilt ProviderAccountFeed payloads."""

    def __init__(self, feeds: list[ProviderAccountFeed]) -> None:
        self._feeds = feeds

    def fetch_feeds(
        self,
        *,
        days: int = 30,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> list[ProviderAccountFeed]:
        del days, start_date, end_date
        return self._feeds


class _RecordingReconciler:
    """Custom CrossSourceReconciler stub recording duplicate and cross-source hook calls."""

    def __init__(self, *, use_custom_clauses: bool = True) -> None:
        self.use_custom_clauses = use_custom_clauses
        self.duplicate_calls: list[tuple[int, str]] = []
        self.cross_source_calls: list[str] = []

    def extract_note_keys(self, notes: str) -> tuple[str, ...]:
        stripped = (notes or "").strip()
        if stripped.startswith("custom:"):
            return (stripped,)
        return ()

    def build_note_lookup_clauses(self, notes_column: Any, incoming_keys: Sequence[str]) -> list[Any]:
        if not self.use_custom_clauses:
            return []
        matched = [k for k in incoming_keys if k.startswith("custom:")]
        return [notes_column.in_(matched)] if matched else []

    def on_duplicate_external_note(
        self,
        db: Session,
        account: Account,
        existing_tx: Transaction,
        item: NormalizedTx,
        *,
        exchange_rate_fn: Callable[..., Any],
    ) -> DuplicateNoteResult:
        del db, account, exchange_rate_fn
        self.duplicate_calls.append((existing_tx.id, item.merchant))
        existing_tx.merchant = f"{existing_tx.merchant} [refreshed]"
        return DuplicateNoteResult(handled=True, refreshed=True)

    def reconcile_cross_source(
        self,
        db: Session,
        account: Account,
        item: NormalizedTx,
        window_existing_txs: list[Transaction],
        existing_by_note: dict[str, Transaction],
        *,
        category_resolver: Callable[..., Any],
        gateway: AiGateway | None = None,
    ) -> CrossSourceMatchResult:
        del db, account, existing_by_note, category_resolver, gateway
        self.cross_source_calls.append(item.merchant)
        for tx in window_existing_txs:
            if tx.amount == item.amount and tx.notes == "match-target":
                tx.merchant = item.merchant
                return CrossSourceMatchResult(matched=True, enriched=True, matched_tx=tx)
        return CrossSourceMatchResult(matched=False, enriched=False)


class TestIngestionSeams(unittest.TestCase):
    def setUp(self) -> None:
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()

        def override_get_db():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        self.acc = Account(
            name="Alex Tan Checking",
            account_type="checking",
            tier="cash",
            institution="Pacific Bank",
            currency="SGD",
        )
        self.db.add(self.acc)
        self.db.commit()
        self.db.refresh(self.acc)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_custom_reconciler_hooks_execute_for_duplicate_and_cross_source(self) -> None:
        existing_dup = Transaction(
            transaction_date=date(2026, 11, 10),
            account_id=self.acc.id,
            amount=-24.00,
            currency="SGD",
            merchant="Custom Cafe",
            category="Dining",
            raw_hash="custom-dup-1",
            notes="custom:tx-001",
        )
        existing_target = Transaction(
            transaction_date=date(2026, 11, 11),
            account_id=self.acc.id,
            amount=-58.50,
            currency="SGD",
            merchant="Advice Generic",
            category="General",
            raw_hash="custom-target-1",
            notes="match-target",
        )
        self.db.add_all([existing_dup, existing_target])
        self.db.commit()
        self.db.refresh(existing_dup)
        self.db.refresh(existing_target)

        reconciler = _RecordingReconciler(use_custom_clauses=False)
        created, skipped, created_txs, matched_existing = ingest_normalized_transactions_detailed(
            self.db,
            self.acc,
            [
                NormalizedTx(
                    transaction_date=date(2026, 11, 10),
                    amount=-24.00,
                    currency="SGD",
                    merchant="Custom Cafe",
                    notes="custom:tx-001",
                ),
                NormalizedTx(
                    transaction_date=date(2026, 11, 12),
                    amount=-58.50,
                    currency="SGD",
                    merchant="Alex Tan Pharmacy",
                    notes="custom:tx-002",
                ),
            ],
            skip_llm=True,
            reconcilers=(reconciler,),
        )
        self.assertEqual(created, 0)
        self.assertEqual(skipped, 2)
        self.assertEqual(created_txs, [])
        self.assertEqual([tx.id for tx in matched_existing], [existing_dup.id, existing_target.id])
        self.assertEqual(reconciler.duplicate_calls, [(existing_dup.id, "Custom Cafe")])
        self.assertEqual(reconciler.cross_source_calls, ["Alex Tan Pharmacy"])
        self.db.refresh(existing_dup)
        self.db.refresh(existing_target)
        self.assertEqual(existing_dup.merchant, "Custom Cafe [refreshed]")
        self.assertEqual(existing_target.merchant, "Alex Tan Pharmacy")

    def test_bounded_note_dedup_queries_and_prefix_collision_safety(self) -> None:
        self.db.add_all(
            [
                Transaction(
                    transaction_date=date(2026, 10, 1),
                    account_id=self.acc.id,
                    amount=-12.00,
                    currency="SGD",
                    merchant="Old Akahu",
                    category="Food",
                    raw_hash="seed-akahu-1",
                    notes="akahu:tx-old-1",
                ),
                Transaction(
                    transaction_date=date(2026, 10, 2),
                    account_id=self.acc.id,
                    amount=-30.00,
                    currency="SGD",
                    merchant="DBS Alert 100",
                    category="Shopping",
                    raw_hash="seed-dbs-100",
                    notes="dbs:DBS-REF-100 | dbs_email_alert",
                ),
                Transaction(
                    transaction_date=date(2026, 10, 3),
                    account_id=self.acc.id,
                    amount=-15.00,
                    currency="SGD",
                    merchant="DBS Alert 1001",
                    category="Dining",
                    raw_hash="seed-dbs-1001",
                    notes="dbs:DBS-REF-1001 | dbs_email_alert",
                ),
            ]
        )
        self.db.commit()

        executed_sql: list[str] = []

        def _capture_sql(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
            executed_sql.append(statement)

        event.listen(engine, "before_cursor_execute", _capture_sql)
        try:
            # 1. Zero-key batch must emit zero external-note lookup queries
            created_zero, skipped_zero, _, _ = ingest_normalized_transactions_detailed(
                self.db,
                self.acc,
                [
                    NormalizedTx(
                        transaction_date=date(2026, 11, 20),
                        amount=-19.95,
                        currency="SGD",
                        merchant="Bookshop",
                        notes=None,
                    )
                ],
                skip_llm=True,
            )
            self.assertEqual((created_zero, skipped_zero), (1, 0))
            note_queries_zero = [s for s in executed_sql if "transactions.notes IS NOT NULL" in s]
            self.assertEqual(note_queries_zero, [])

            # 2. Incoming keys emit bounded lookup, dedup compound DBS note, and avoid REF-10 vs REF-100 collision
            executed_sql.clear()
            created_mix, skipped_mix, created_txs, _ = ingest_normalized_transactions_detailed(
                self.db,
                self.acc,
                [
                    NormalizedTx(
                        transaction_date=date(2026, 10, 1),
                        amount=-12.00,
                        currency="SGD",
                        merchant="Old Akahu",
                        notes="akahu:tx-old-1",
                    ),
                    NormalizedTx(
                        transaction_date=date(2026, 10, 3),
                        amount=-15.00,
                        currency="SGD",
                        merchant="DBS Alert 1001",
                        notes="dbs:DBS-REF-1001",
                    ),
                    NormalizedTx(
                        transaction_date=date(2026, 10, 6),
                        amount=-31.00,
                        currency="SGD",
                        merchant="DBS Alert 10",
                        notes="dbs:DBS-REF-10",
                    ),
                ],
                skip_llm=True,
            )
            self.assertEqual(created_mix, 1)
            self.assertEqual(skipped_mix, 2)
            self.assertEqual(created_txs[0].notes, "dbs:DBS-REF-10")
            note_queries_mix = [s for s in executed_sql if "transactions.notes IS NOT NULL" in s]
            self.assertEqual(len(note_queries_mix), 1)
            self.assertIn("transactions.notes IN", note_queries_mix[0])
            self.assertIn("LIKE", note_queries_mix[0])
        finally:
            event.remove(engine, "before_cursor_execute", _capture_sql)

    def test_large_batch_note_lookup_chunks_safely_over_200_keys(self) -> None:
        items = [
            NormalizedTx(
                transaction_date=date(2026, 11, 1),
                amount=round(-1.0 - idx * 0.01, 2),
                currency="SGD",
                merchant=f"Merchant {idx}",
                notes=f"akahu:batch-{idx}",
            )
            for idx in range(210)
        ]
        created, skipped, _, _ = ingest_normalized_transactions_detailed(
            self.db, self.acc, items, skip_llm=True
        )
        self.assertEqual((created, skipped), (210, 0))
        created2, skipped2, _, _ = ingest_normalized_transactions_detailed(
            self.db, self.acc, items, skip_llm=True
        )
        self.assertEqual((created2, skipped2), (0, 210))

    @staticmethod
    def _build_feed_gateway(provider: str, tx_id: str, currency: str) -> _FakeGateway:
        return _FakeGateway(
            [
                ProviderAccountFeed(
                    account=ExternalAccountSpec(
                        provider=provider,
                        external_id=f"{provider}-acc-1",
                        name=f"Alex Tan {provider.title()}",
                        institution=provider.title(),
                        currency=currency,
                    ),
                    transactions=(
                        NormalizedTx(
                            transaction_date=date(2026, 11, 18),
                            amount=-18.50,
                            currency=currency,
                            merchant=f"{provider.title()} Coffee",
                            category="General",
                            notes=f"{provider}:{tx_id}",
                        ),
                    ),
                )
            ]
        )

    def test_ai_gateway_threaded_across_connectors(self) -> None:
        jev_payload = json.dumps({"category": "Dining", "is_anomaly": False, "tags": ["coffee"]})

        gw_akahu = InMemoryAiGateway(jev_responses=[jev_payload])
        sync_akahu(
            self.db,
            gateway=self._build_feed_gateway("akahu", "a-1", "NZD"),
            ai_gateway=gw_akahu,
        )
        self.assertEqual(len(gw_akahu.jev_requests), 1)

        gw_wise = InMemoryAiGateway(jev_responses=[jev_payload])
        sync_wise(
            self.db,
            gateway=self._build_feed_gateway("wise", "w-1", "NZD"),
            ai_gateway=gw_wise,
        )
        self.assertEqual(len(gw_wise.jev_requests), 1)

        gw_btc = InMemoryAiGateway(jev_responses=[jev_payload])
        sync_bitcoin(
            self.db,
            skip_llm=False,
            gateway=self._build_feed_gateway("bitcoin", "b-1", "NZD"),
            ai_gateway=gw_btc,
        )
        self.assertEqual(len(gw_btc.jev_requests), 1)

    def test_ai_gateway_threaded_across_registry_scheduler_and_router(self) -> None:
        jev_payload = json.dumps({"category": "Dining", "is_anomaly": False, "tags": ["coffee"]})
        gw_registry = InMemoryAiGateway(jev_responses=[jev_payload])
        with patch.dict(
            connector_registry.SYNC_HANDLERS,
            {
                "akahu": lambda db, days, sd, ed, skip, ai_gateway=None: sync_akahu(
                    db,
                    days=days,
                    start_date=sd,
                    end_date=ed,
                    skip_llm=skip,
                    gateway=self._build_feed_gateway("akahu", "reg-1", "NZD"),
                    ai_gateway=ai_gateway,
                )
            },
            clear=True,
        ):
            res = sync_provider(self.db, "akahu", ai_gateway=gw_registry)
        self.assertEqual(res.transactions_created, 1)
        self.assertEqual(len(gw_registry.jev_requests), 1)

        gw_http = InMemoryAiGateway(jev_responses=[jev_payload])
        app.dependency_overrides[get_ai_gateway] = lambda: gw_http
        with patch.dict(
            connector_registry.SYNC_HANDLERS,
            {
                "wise": lambda db, days, sd, ed, skip, ai_gateway=None: sync_wise(
                    db,
                    days=days,
                    start_date=sd,
                    end_date=ed,
                    skip_llm=skip,
                    gateway=self._build_feed_gateway("wise", "http-1", "NZD"),
                    ai_gateway=ai_gateway,
                )
            },
            clear=True,
        ):
            resp = self.client.post("/integrations/sync/wise")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(gw_http.jev_requests), 1)

        received_gateways: list[AiGateway | None] = []
        gw_sched = InMemoryAiGateway()

        def _sched_handler(db, days, sd, ed, skip, ai_gateway=None):
            del days, sd, ed
            received_gateways.append(ai_gateway)
            return sync_akahu(
                db,
                skip_llm=True if skip else True,
                gateway=self._build_feed_gateway("akahu", "sched-1", "NZD"),
                ai_gateway=ai_gateway,
            )

        with patch.dict(
            connector_registry.SYNC_HANDLERS,
            {"akahu": _sched_handler},
            clear=True,
        ):
            run_scheduled_sync_once(
                db_factory=TestingSessionLocal,
                providers=["akahu"],
                ai_gateway=gw_sched,
            )
        self.assertEqual(received_gateways, [gw_sched])


if __name__ == "__main__":
    unittest.main()
