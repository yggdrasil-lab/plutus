from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Account, AssetSnapshot, Transaction
from app.services.ingestion_service import NormalizedTx, ingest_normalized_transactions
from app.services.loan_service import add_months


def _build_test_client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app), TestingSessionLocal


def test_loans_transaction_driven_linking_rules_payoff_and_account_patch():
    client, TestingSessionLocal = _build_test_client()

    try:
        today = date.today()
        with TestingSessionLocal() as db:
            acc = Account(
                name="ANZ Freedom",
                institution="ANZ",
                account_type="checking",
                tier="cash",
                currency="NZD",
            )
            locked_acc = Account(
                name="Sharesies Portfolio",
                institution="Sharesies",
                account_type="brokerage",
                tier="locked",
                currency="NZD",
            )
            db.add_all([acc, locked_acc])
            db.flush()
            db.add(
                AssetSnapshot(
                    account_id=acc.id,
                    snapshot_date=today,
                    balance=100000.0,
                    currency="NZD",
                    exchange_rate=1.0,
                    balance_nzd=100000.0,
                    source="manual",
                )
            )
            db.commit()
            db.refresh(acc)
            db.refresh(locked_acc)

        # 1. PATCH /accounts/{id} updates tier from locked to growth and validates inputs
        bad_tier = client.patch(f"/accounts/{locked_acc.id}", json={"tier": "invalid_tier"})
        assert bad_tier.status_code == 422
        bad_type = client.patch(f"/accounts/{locked_acc.id}", json={"account_type": "invalid_type"})
        assert bad_type.status_code == 422
        missing_acc = client.patch("/accounts/99999", json={"tier": "growth"})
        assert missing_acc.status_code == 404

        patch_acc = client.patch(
            f"/accounts/{locked_acc.id}",
            json={"tier": "growth", "expected_annual_return": 0.08},
        )
        assert patch_acc.status_code == 200
        assert patch_acc.json()["tier"] == "growth"
        assert patch_acc.json()["expected_annual_return"] == 0.08

        # 2. Create loan starting today with 0 linked transactions: strictly stays 0/36
        res = client.post(
            "/loans",
            json={
                "name": "Toyota Corolla Cross (TFS)",
                "lender": "Toyota Financial Services",
                "principal_amount": 45399.42,
                "annual_interest_rate": 0.0962,
                "term_months": 36,
                "monthly_payment": 1452.42,
                "start_date": today.isoformat(),
                "asset_value": 55000.0,
            },
        )
        assert res.status_code == 201
        loan = res.json()
        loan_id = loan["id"]
        assert loan["payments_made"] == 0
        assert loan["payments_remaining"] == 36
        assert loan["remaining_principal"] == 45399.42
        assert loan["principal_paid"] == 0.0
        assert loan["interest_paid"] == 0.0
        assert loan["total_paid_amount"] == 0.0
        assert loan["last_payment_date"] is None
        assert loan["is_payment_due"] is True
        assert loan["linked_payments"] == []
        assert loan["payoff_date"] == add_months(today, 35).isoformat()
        assert loan["weekly_payment_equivalent"] == 335.17

        summary = client.get("/loans").json()
        assert summary["total_liabilities"] == 45399.42
        assert summary["total_monthly_payment"] == 1452.42

        runway = client.get("/runway").json()
        assert runway["breakdown"]["total_liabilities"] == 45399.42
        assert runway["breakdown"]["net_worth"] == round(100000.0 - 45399.42, 2)

        sim = client.post("/runway/simulate", json={"pay_off_loans": True}).json()
        assert sim["total_liabilities"] == 0.0
        assert sim["loan_weekly_relief"] == 335.17
        assert sim["loan_free_date"] == add_months(today, 35).isoformat()

        # 3. Manual PATCH /transactions/{id} linking & unlinking updates loan and runway
        tx_res = client.post(
            "/transactions",
            json={
                "transaction_date": today.isoformat(),
                "account_id": acc.id,
                "amount": -1452.42,
                "currency": "NZD",
                "merchant": "Toyota Financial Services",
                "category": "Debt & Loans",
            },
        )
        assert tx_res.status_code == 201
        tx1_id = tx_res.json()["id"]

        bad_link = client.patch(f"/transactions/{tx1_id}", json={"loan_id": 99999})
        assert bad_link.status_code == 404

        link_res = client.patch(f"/transactions/{tx1_id}", json={"loan_id": loan_id})
        assert link_res.status_code == 200
        assert link_res.json()["loan_id"] == loan_id
        assert link_res.json()["loan_name"] == "Toyota Corolla Cross (TFS)"

        expected_interest_1 = round(45399.42 * (0.0962 / 12.0), 2)
        expected_principal_part_1 = 1452.42 - (45399.42 * (0.0962 / 12.0))
        expected_remaining_1 = round(45399.42 - expected_principal_part_1, 2)
        expected_principal_paid_1 = round(45399.42 - expected_remaining_1, 2)

        loans_after_link = client.get("/loans").json()
        linked_loan = loans_after_link["loans"][0]
        assert linked_loan["payments_made"] == 1
        assert linked_loan["payments_remaining"] == 35
        assert linked_loan["remaining_principal"] == expected_remaining_1
        assert linked_loan["principal_paid"] == expected_principal_paid_1
        assert linked_loan["interest_paid"] == expected_interest_1
        assert linked_loan["total_paid_amount"] == 1452.42
        assert linked_loan["last_payment_date"] == today.isoformat()
        assert linked_loan["next_payment_date"] == add_months(today, 1).isoformat()
        assert len(linked_loan["linked_payments"]) == 1
        assert linked_loan["linked_payments"][0]["id"] == tx1_id
        assert linked_loan["linked_payments"][0]["account_name"] == "ANZ Freedom"

        runway_after_link = client.get("/runway").json()
        assert runway_after_link["breakdown"]["total_liabilities"] == expected_remaining_1
        assert runway_after_link["breakdown"]["net_worth"] == round(
            100000.0 - expected_remaining_1, 2
        )

        # GET /transactions?loan_id=<id> returns linked transaction
        filtered_txs = client.get(f"/transactions?loan_id={loan_id}").json()
        assert [t["id"] for t in filtered_txs] == [tx1_id]
        assert filtered_txs[0]["loan_name"] == "Toyota Corolla Cross (TFS)"

        # Unlink via PATCH loan_id=None restores 0/36
        unlink_res = client.patch(f"/transactions/{tx1_id}", json={"loan_id": None})
        assert unlink_res.status_code == 200
        assert unlink_res.json()["loan_id"] is None
        assert unlink_res.json()["loan_name"] is None

        loans_after_unlink = client.get("/loans").json()
        assert loans_after_unlink["loans"][0]["payments_made"] == 0
        assert loans_after_unlink["loans"][0]["remaining_principal"] == 45399.42
        assert loans_after_unlink["loans"][0]["linked_payments"] == []
        assert client.get(f"/transactions?loan_id={loan_id}").json() == []

        # 4. Rule-driven loan_id via POST /rules + idempotent apply-all + ingestion
        bad_rule = client.post(
            "/rules",
            json={
                "name": "Bad Loan Rule",
                "description_pattern": "TOYOTA FINANCE",
                "target_category": "Debt & Loans",
                "loan_id": 99999,
            },
        )
        assert bad_rule.status_code == 404

        rule_res = client.post(
            "/rules",
            json={
                "name": "TFS Car Loan",
                "description_pattern": "TOYOTA FINANCIAL",
                "direction": "outflow",
                "target_category": "Debt & Loans",
                "loan_id": loan_id,
                "apply_retroactively": True,
            },
        )
        assert rule_res.status_code == 201
        rule_data = rule_res.json()
        assert rule_data["loan_id"] == loan_id
        assert rule_data["loan_name"] == "Toyota Corolla Cross (TFS)"

        # Retroactive apply matched tx1_id without creating mirror transactions
        with TestingSessionLocal() as db:
            all_txs = db.scalars(select(Transaction)).all()
            assert len(all_txs) == 1
            assert all_txs[0].loan_id == loan_id

        # Idempotent apply-all produces 0 updates and 0 extra transactions
        reapply = client.post("/rules/apply-all")
        assert reapply.status_code == 200
        assert reapply.json()["updated"] == 0

        # Ingestion assigns loan_id automatically
        next_day = today + timedelta(days=1)
        with TestingSessionLocal() as db:
            acc_db = db.get(Account, acc.id)
            created_cnt, skipped_cnt, created_txs = ingest_normalized_transactions(
                db,
                acc_db,
                [
                    NormalizedTx(
                        transaction_date=next_day,
                        amount=-1452.42,
                        currency="NZD",
                        merchant="TOYOTA FINANCIAL SERVICES",
                        raw_description="DD TOYOTA FINANCIAL SERVICES",
                    )
                ],
                skip_llm=True,
            )
            assert created_cnt == 1
            assert skipped_cnt == 0
            assert created_txs[0].loan_id == loan_id

        loans_after_ingest = client.get("/loans").json()["loans"][0]
        assert loans_after_ingest["payments_made"] == 2
        assert loans_after_ingest["payments_remaining"] == 34
        assert [p["transaction_date"] for p in loans_after_ingest["linked_payments"]] == [
            next_day.isoformat(),
            today.isoformat(),
        ]

        # 5. Lump-sum payoff reaches payments_remaining=0 and remaining_principal=0.0
        payoff_day = today + timedelta(days=5)
        lump_res = client.post(
            "/transactions",
            json={
                "transaction_date": payoff_day.isoformat(),
                "account_id": acc.id,
                "amount": -50000.0,
                "currency": "NZD",
                "merchant": "TOYOTA FINANCIAL PAYOFF",
                "category": "Debt & Loans",
            },
        )
        assert lump_res.status_code == 201
        lump_id = lump_res.json()["id"]
        assert client.patch(f"/transactions/{lump_id}", json={"loan_id": loan_id}).status_code == 200

        paid_off_loan = client.get("/loans").json()["loans"][0]
        assert paid_off_loan["payments_made"] == 3
        assert paid_off_loan["payments_remaining"] == 0
        assert paid_off_loan["remaining_principal"] == 0.0
        assert paid_off_loan["is_payment_due"] is False
        assert paid_off_loan["payoff_date"] == payoff_day.isoformat()
        assert paid_off_loan["last_payment_date"] == payoff_day.isoformat()
        assert paid_off_loan["weekly_payment_equivalent"] == 0.0
    finally:
        app.dependency_overrides.clear()
