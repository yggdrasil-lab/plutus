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


def test_two_way_lent_loan_partial_repayment_and_budget_purity():
    client, TestingSessionLocal = _build_test_client()

    try:
        today = date.today()
        with TestingSessionLocal() as db:
            acc = Account(
                name="ANZ Checking",
                institution="ANZ",
                account_type="checking",
                tier="cash",
                currency="NZD",
            )
            db.add(acc)
            db.flush()
            db.add(
                AssetSnapshot(
                    account_id=acc.id,
                    snapshot_date=today,
                    balance=50000.0,
                    currency="NZD",
                    exchange_rate=1.0,
                    balance_nzd=50000.0,
                    source="manual",
                )
            )
            db.commit()
            db.refresh(acc)

        # Validation: borrowed loan requires term_months >= 1 and monthly_payment > 0
        bad_borrowed_term = client.post(
            "/loans",
            json={
                "name": "Bad Borrowed",
                "lender": "Bank",
                "loan_type": "borrowed",
                "principal_amount": 5000.0,
                "annual_interest_rate": 0.05,
                "term_months": 0,
                "monthly_payment": 200.0,
                "start_date": today.isoformat(),
            },
        )
        assert bad_borrowed_term.status_code == 422

        bad_borrowed_pmt = client.post(
            "/loans",
            json={
                "name": "Bad Borrowed Pmt",
                "lender": "Bank",
                "loan_type": "borrowed",
                "principal_amount": 5000.0,
                "annual_interest_rate": 0.05,
                "term_months": 12,
                "monthly_payment": 0.0,
                "start_date": today.isoformat(),
            },
        )
        assert bad_borrowed_pmt.status_code == 422

        # Create lent receivable ($10,000, flexible open term=0, payment=0, 0% interest)
        lent_res = client.post(
            "/loans",
            json={
                "name": "Family Bridge Loan",
                "lender": "Sibling",
                "loan_type": "LENT",
                "principal_amount": 10000.0,
                "annual_interest_rate": 0.0,
                "term_months": 0,
                "monthly_payment": 0.0,
                "start_date": today.isoformat(),
            },
        )
        assert lent_res.status_code == 201
        lent_loan = lent_res.json()
        lent_id = lent_loan["id"]
        assert lent_loan["loan_type"] == "lent"
        assert lent_loan["remaining_principal"] == 10000.0
        assert lent_loan["payments_remaining"] == 0
        assert lent_loan["next_payment_date"] is None
        assert lent_loan["is_payment_due"] is False
        assert lent_loan["payoff_date"] is None
        assert lent_loan["net_worth_impact"] == 0.0

        # Cannot switch open-term lent loan to borrowed without valid term & monthly_payment
        bad_switch = client.patch(f"/loans/{lent_id}", json={"loan_type": "borrowed"})
        assert bad_switch.status_code == 422

        # Create second borrowed loan for switch-clearing & partial repayment tests
        borrowed_res = client.post(
            "/loans",
            json={
                "name": "Car Finance",
                "lender": "Toyota",
                "loan_type": "borrowed",
                "principal_amount": 5000.0,
                "annual_interest_rate": 0.0,
                "term_months": 10,
                "monthly_payment": 500.0,
                "start_date": today.isoformat(),
            },
        )
        assert borrowed_res.status_code == 201
        borrowed_id = borrowed_res.json()["id"]

        # Create normal grocery spend ($100) so burn rate baseline is known
        grocery_tx = client.post(
            "/transactions",
            json={
                "transaction_date": today.isoformat(),
                "account_id": acc.id,
                "amount": -100.0,
                "currency": "NZD",
                "merchant": "Countdown",
                "category": "Groceries",
            },
        ).json()

        # 1. Link -$10,000 disbursement outflow to lent loan
        disb_tx = client.post(
            "/transactions",
            json={
                "transaction_date": today.isoformat(),
                "account_id": acc.id,
                "amount": -10000.0,
                "currency": "NZD",
                "merchant": "Transfer to Sibling",
                "category": "Debt & Loans",
            },
        ).json()

        # Validation: setting loan_payment_amount while unlinked (loan_id is None) returns 422
        unlinked_override = client.patch(
            f"/transactions/{disb_tx['id']}",
            json={"loan_payment_amount": 500.0},
        )
        assert unlinked_override.status_code == 422

        link_disb = client.patch(
            f"/transactions/{disb_tx['id']}",
            json={"loan_id": lent_id},
        )
        assert link_disb.status_code == 200
        assert link_disb.json()["loan_type"] == "lent"

        # 2. Mixed inflow +$5,020 with loan_payment_amount=5000 ($20 ride-along income)
        mixed_day = today + timedelta(days=1)
        mixed_tx = client.post(
            "/transactions",
            json={
                "transaction_date": mixed_day.isoformat(),
                "account_id": acc.id,
                "amount": 5020.0,
                "currency": "NZD",
                "merchant": "Sibling Repayment + Split",
                "category": "Debt & Loans",
            },
        ).json()

        # Validation: loan_payment_amount > abs(amount) -> 422; <= 0 -> 422
        too_high = client.patch(
            f"/transactions/{mixed_tx['id']}",
            json={"loan_id": lent_id, "loan_payment_amount": 6000.0},
        )
        assert too_high.status_code == 422
        non_positive = client.patch(
            f"/transactions/{mixed_tx['id']}",
            json={"loan_id": lent_id, "loan_payment_amount": 0.0},
        )
        assert non_positive.status_code == 422

        link_mixed = client.patch(
            f"/transactions/{mixed_tx['id']}",
            json={"loan_id": lent_id, "loan_payment_amount": 5000.0},
        )
        assert link_mixed.status_code == 200
        assert link_mixed.json()["loan_payment_amount"] == 5000.0
        assert link_mixed.json()["loan_type"] == "lent"

        # 3. Pure repayment inflow +$1,000 (no override)
        repay_day = today + timedelta(days=2)
        repay_tx = client.post(
            "/transactions",
            json={
                "transaction_date": repay_day.isoformat(),
                "account_id": acc.id,
                "amount": 1000.0,
                "currency": "NZD",
                "merchant": "Sibling Second Payment",
                "category": "Debt & Loans",
            },
        ).json()
        assert client.patch(
            f"/transactions/{repay_tx['id']}",
            json={"loan_id": lent_id},
        ).status_code == 200

        # Verify GET /loans portfolio summary and lent loan status
        summary = client.get("/loans").json()
        assert summary["total_liabilities"] == 5000.0
        assert summary["total_receivables"] == 4000.0

        lent_status = next(item for item in summary["loans"] if item["id"] == lent_id)
        assert lent_status["remaining_principal"] == 4000.0
        assert lent_status["principal_paid"] == 6000.0
        assert lent_status["total_paid_amount"] == 6000.0
        assert lent_status["payments_made"] == 2
        assert lent_status["payoff_date"] is None
        assert lent_status["net_worth_impact"] == 0.0
        assert len(lent_status["disbursements"]) == 1
        assert lent_status["disbursements"][0]["id"] == disb_tx["id"]
        assert lent_status["disbursements"][0]["entry_type"] == "disbursement"
        assert [p["id"] for p in lent_status["linked_payments"]] == [repay_tx["id"], mixed_tx["id"]]
        assert lent_status["linked_payments"][0]["applied_amount"] == 1000.0
        assert lent_status["linked_payments"][0]["entry_type"] == "repayment"
        assert lent_status["linked_payments"][1]["applied_amount"] == 5000.0
        assert lent_status["linked_payments"][1]["loan_payment_amount"] == 5000.0

        # Verify Budget Breakdown & Runway purity:
        # - -$10,000 disbursement is NOT in total_spend or weekly_burn_rate (only $100 Groceries counts)
        # - +$5,020 with loan_payment_amount=5000 adds only $20.00 to total_income; +$1,000 adds $0.00
        # - runway.breakdown.total_liabilities = 5000.0 (only borrowed), net_worth = 50000 - 5000 = 45000
        bd = client.get("/budgets/breakdown?period=all").json()
        assert bd["total_spend"] == 100.0
        assert bd["total_income"] == 20.0
        assert bd["net_savings"] == -80.0

        runway = client.get("/runway").json()
        assert runway["breakdown"]["total_liabilities"] == 5000.0
        assert runway["breakdown"]["net_worth"] == 45000.0
        # 4w variable burn = $100 / 4 = $25/wk (the -$10k disbursement did NOT inflate burn)
        assert runway["breakdown"]["weekly_burn_rate"] == 25.0

        # Verify rule loan_type hydration
        rule_res = client.post(
            "/rules",
            json={
                "name": "Sibling Loan Rule",
                "description_pattern": "SIBLING",
                "target_category": "Debt & Loans",
                "loan_id": lent_id,
                "apply_retroactively": False,
            },
        )
        assert rule_res.status_code == 201
        assert rule_res.json()["loan_type"] == "lent"

        # Switching loan_id to another loan without loan_payment_amount clears loan_payment_amount
        # First test on a borrowed outflow with loan_payment_amount
        part_out = client.post(
            "/transactions",
            json={
                "transaction_date": repay_day.isoformat(),
                "account_id": acc.id,
                "amount": -800.0,
                "currency": "NZD",
                "merchant": "Split Car + Groceries",
                "category": "Debt & Loans",
            },
        ).json()
        patched_borrowed = client.patch(
            f"/transactions/{part_out['id']}",
            json={"loan_id": borrowed_id, "loan_payment_amount": 500.0},
        )
        assert patched_borrowed.status_code == 200
        assert patched_borrowed.json()["loan_payment_amount"] == 500.0
        assert client.get("/loans").json()["total_liabilities"] == 4500.0

        # Switch to lent_id without specifying loan_payment_amount -> clears override to None
        switched = client.patch(
            f"/transactions/{part_out['id']}",
            json={"loan_id": lent_id},
        )
        assert switched.status_code == 200
        assert switched.json()["loan_payment_amount"] is None

        # Clear loan_id on mixed_tx -> clears loan_payment_amount to None
        unlinked_mixed = client.patch(
            f"/transactions/{mixed_tx['id']}",
            json={"loan_id": None},
        )
        assert unlinked_mixed.status_code == 200
        assert unlinked_mixed.json()["loan_id"] is None
        assert unlinked_mixed.json()["loan_type"] is None
        assert unlinked_mixed.json()["loan_payment_amount"] is None

        # Re-link mixed_tx and pay off remaining $4,000
        assert client.patch(
            f"/transactions/{mixed_tx['id']}",
            json={"loan_id": lent_id, "loan_payment_amount": 5000.0},
        ).status_code == 200
        final_day = today + timedelta(days=3)
        final_tx = client.post(
            "/transactions",
            json={
                "transaction_date": final_day.isoformat(),
                "account_id": acc.id,
                "amount": 4000.0,
                "currency": "NZD",
                "merchant": "Sibling Final Payoff",
                "category": "Debt & Loans",
            },
        ).json()
        assert client.patch(
            f"/transactions/{final_tx['id']}",
            json={"loan_id": lent_id},
        ).status_code == 200

        final_summary = client.get("/loans").json()
        assert final_summary["total_receivables"] == 0.0
        final_lent = next(item for item in final_summary["loans"] if item["id"] == lent_id)
        assert final_lent["remaining_principal"] == 0.0
        assert final_lent["payoff_date"] == final_day.isoformat()
        assert grocery_tx["id"] is not None
    finally:
        app.dependency_overrides.clear()


def test_cross_currency_partial_loan_payment_override():
    client, TestingSessionLocal = _build_test_client()

    try:
        today = date.today()
        with TestingSessionLocal() as db:
            sgd_acc = Account(
                name="Wise SGD",
                institution="Wise",
                account_type="checking",
                tier="cash",
                currency="SGD",
            )
            db.add(sgd_acc)
            db.commit()
            db.refresh(sgd_acc)

        loan_res = client.post(
            "/loans",
            json={
                "name": "SGD Repaid NZD Loan",
                "lender": "Friend",
                "loan_type": "lent",
                "principal_amount": 1000.0,
                "annual_interest_rate": 0.0,
                "term_months": 0,
                "monthly_payment": 0.0,
                "start_date": today.isoformat(),
                "currency": "NZD",
            },
        )
        assert loan_res.status_code == 201
        loan_id = loan_res.json()["id"]

        tx_res = client.post(
            "/transactions",
            json={
                "transaction_date": today.isoformat(),
                "account_id": sgd_acc.id,
                "amount": 150.0,
                "currency": "SGD",
                "exchange_rate": 1.30,
                "merchant": "Friend SGD Repayment + Bonus",
                "category": "Debt & Loans",
            },
        )
        assert tx_res.status_code == 201
        tx_id = tx_res.json()["id"]

        patch_res = client.patch(
            f"/transactions/{tx_id}",
            json={"loan_id": loan_id, "loan_payment_amount": 100.0},
        )
        assert patch_res.status_code == 200
        assert patch_res.json()["loan_payment_amount"] == 100.0

        summary = client.get("/loans").json()
        loan_status = next(item for item in summary["loans"] if item["id"] == loan_id)
        # 100 SGD * 1.30 = 130 NZD applied toward the 1000 NZD loan
        assert loan_status["linked_payments"][0]["applied_amount"] == 130.0
        assert loan_status["principal_paid"] == 130.0
        assert loan_status["remaining_principal"] == 870.0

        # Remaining 50 SGD * 1.30 = 65 NZD counts as non-loan income
        bd = client.get("/budgets/breakdown?period=all").json()
        assert bd["total_income"] == 65.0
    finally:
        app.dependency_overrides.clear()
