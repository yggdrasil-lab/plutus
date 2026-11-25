"""Integration tests for MCP bearer auth middleware and direct MCP tool handlers."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings
from app.db import Base, get_db
from app.main import app
from app.models import Account, AssetSnapshot, BudgetRule, Transaction
from app.routers.mcp import (
    _run_plutus_get_runway,
    _run_plutus_get_weekly_burn,
    _run_plutus_reconcile_transfers,
    _run_plutus_record_snapshot,
)


def _mcp_initialize_payload() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "plutus-test-client", "version": "1.0"},
        },
    }


class TestMcpAuthMiddleware(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        def override_get_db() -> Iterator[Session]:
            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=self.engine)

    def test_mcp_returns_503_when_token_empty_in_production(self) -> None:
        prod_no_token = Settings(environment="production", mcp_token="", mcp_token_file="")
        with patch("app.routers.mcp.settings", prod_no_token):
            with TestClient(app) as client:
                res = client.post("/mcp", json=_mcp_initialize_payload())
                self.assertEqual(res.status_code, 503)
                self.assertEqual(res.json(), {"error": "mcp token not configured"})

    def test_mcp_auth_with_secret_file_rejects_bad_and_accepts_valid_bearer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            token_path = Path(tmp_dir) / "mcp_token"
            token_path.write_text("plutus-secret-2002\n", encoding="utf-8")
            prod_with_file = Settings(
                environment="production",
                mcp_token="",
                mcp_token_file=str(token_path),
            )
            with patch("app.routers.mcp.settings", prod_with_file):
                with TestClient(app) as client:
                    missing = client.post("/mcp", json=_mcp_initialize_payload())
                    self.assertEqual(missing.status_code, 401)
                    self.assertEqual(missing.headers.get("www-authenticate"), "Bearer")

                    wrong = client.post(
                        "/mcp",
                        headers={"Authorization": "Bearer wrong-token"},
                        json=_mcp_initialize_payload(),
                    )
                    self.assertEqual(wrong.status_code, 401)

                    bad_scheme = client.post(
                        "/mcp",
                        headers={"Authorization": "Basic cGx1dHVz"},
                        json=_mcp_initialize_payload(),
                    )
                    self.assertEqual(bad_scheme.status_code, 401)

                    accepted = client.post(
                        "/mcp",
                        headers={
                            "Authorization": "Bearer plutus-secret-2002",
                            "Accept": "application/json, text/event-stream",
                        },
                        json=_mcp_initialize_payload(),
                    )
                    self.assertEqual(accepted.status_code, 200)

                    # Non-/mcp paths pass through without MCP bearer auth.
                    health = client.get("/health")
                    self.assertEqual(health.status_code, 200)


class TestMcpDirectToolHandlers(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = SessionLocal()

    def tearDown(self) -> None:
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_mcp_runway_weekly_burn_record_snapshot_and_reconcile(self) -> None:
        ref_date = date(2026, 11, 20)
        monday = ref_date - timedelta(days=ref_date.weekday())

        checking = Account(
            name="Alex Checking 1001",
            institution="North Bank",
            account_type="checking",
            tier="cash",
            currency="NZD",
            is_active=True,
        )
        savings = Account(
            name="Alex Savings 2002",
            institution="South Bank",
            account_type="savings",
            tier="cash",
            currency="NZD",
            is_active=True,
        )
        self.db.add_all([checking, savings])
        self.db.flush()

        self.db.add_all(
            [
                AssetSnapshot(
                    account_id=checking.id,
                    snapshot_date=ref_date,
                    balance=12000.0,
                    currency="NZD",
                    exchange_rate=1.0,
                    balance_nzd=12000.0,
                    source="manual",
                ),
                BudgetRule(category="Groceries", weekly_limit=500.0, monthly_limit=2166.67),
                Transaction(
                    transaction_date=monday,
                    account_id=checking.id,
                    amount=-150.0,
                    currency="NZD",
                    merchant="Corner Market",
                    category="Groceries",
                    raw_hash="mcp-burn-tx-1",
                ),
                Transaction(
                    transaction_date=ref_date,
                    account_id=checking.id,
                    amount=-1000.0,
                    currency="NZD",
                    merchant="Transfer to Alex Savings",
                    raw_description="Transfer to Alex Savings 2002",
                    category="Transfers",
                    raw_hash="mcp-xfer-out",
                ),
                Transaction(
                    transaction_date=ref_date,
                    account_id=savings.id,
                    amount=1000.0,
                    currency="NZD",
                    merchant="Transfer from Alex Checking",
                    raw_description="Transfer from Alex Checking 1001",
                    category="Transfers",
                    raw_hash="mcp-xfer-in",
                ),
            ]
        )
        self.db.commit()

        runway_payload = json.loads(_run_plutus_get_runway(db=self.db, reference_date=ref_date))
        self.assertEqual(runway_payload["as_of_date"], ref_date.isoformat())
        self.assertEqual(runway_payload["breakdown"]["cash_balance_nzd"], 12000.0)
        self.assertEqual(runway_payload["weekly_budget_target_nzd"], 500.0)

        burn_payload = json.loads(_run_plutus_get_weekly_burn(db=self.db, reference_date=ref_date))
        self.assertEqual(burn_payload["start_of_week"], monday.isoformat())
        self.assertEqual(burn_payload["this_week_spend_nzd"], 150.0)
        self.assertEqual(burn_payload["remaining_budget_nzd"], 350.0)
        self.assertEqual(burn_payload["categories"], {"Groceries": 150.0})

        created = json.loads(
            _run_plutus_record_snapshot(
                account_name="Alex Savings",
                balance=25000.0,
                snapshot_date=ref_date.isoformat(),
                db=self.db,
            )
        )
        self.assertEqual(created["status"], "created")
        self.assertEqual(created["account"], "Alex Savings 2002")
        self.assertEqual(created["balance"], 25000.0)

        updated = json.loads(
            _run_plutus_record_snapshot(
                account_name="Alex Savings",
                balance=26500.0,
                snapshot_date=ref_date.isoformat(),
                db=self.db,
            )
        )
        self.assertEqual(updated["status"], "updated")
        self.assertEqual(updated["balance"], 26500.0)

        with self.assertRaises(ToolError):
            _run_plutus_record_snapshot(
                account_name="Nonexistent Vault",
                balance=100.0,
                db=self.db,
            )

        reconciled = json.loads(_run_plutus_reconcile_transfers(db=self.db))
        self.assertEqual(reconciled, {"status": "success", "matched_transfers": 1})
        out_tx = self.db.scalar(select(Transaction).where(Transaction.raw_hash == "mcp-xfer-out"))
        in_tx = self.db.scalar(select(Transaction).where(Transaction.raw_hash == "mcp-xfer-in"))
        self.assertIsNotNone(out_tx)
        self.assertIsNotNone(in_tx)
        self.assertEqual(out_tx.linked_transaction_id, in_tx.id)
        self.assertEqual(in_tx.linked_transaction_id, out_tx.id)


if __name__ == "__main__":
    unittest.main()
