"""Regression tests for manual transaction splits, loan integration, budgets, and rules."""

from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Account, Loan, Receipt, ReceiptItem, Transaction


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


def test_outflow_split_breakdown_transactions_and_burn():
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
            db.commit()
            db.refresh(acc)

        tx_res = client.post(
            "/transactions",
            json={
                "transaction_date": today.isoformat(),
                "account_id": acc.id,
                "amount": -120.0,
                "currency": "NZD",
                "merchant": "Pak n Save",
                "category": "General",
                "tags": ["weekly-shop"],
            },
        )
        assert tx_res.status_code == 201
        tx_id = tx_res.json()["id"]
        assert tx_res.json()["is_split"] is False
        assert tx_res.json()["splits"] == []

        # Attach a receipt with a single item to verify manual splits take precedence over receipt items
        with TestingSessionLocal() as db:
            rec = Receipt(
                transaction_id=tx_id,
                receipt_date=today,
                merchant="Pak n Save",
                total_amount=120.0,
                currency="NZD",
                status="matched",
            )
            db.add(rec)
            db.flush()
            db.add(
                ReceiptItem(
                    receipt_id=rec.id,
                    item_name="Misc Receipt Item",
                    quantity=1.0,
                    unit_price=120.0,
                    total_price=120.0,
                    category="Shopping",
                )
            )
            db.commit()

        split_res = client.put(
            f"/transactions/{tx_id}/splits",
            json={
                "splits": [
                    {
                        "amount": 80.0,
                        "category": "Groceries",
                        "tags": ["pantry"],
                        "notes": "Food items",
                    },
                    {
                        "amount": 40.0,
                        "category": "Household",
                        "tags": ["cleaning"],
                        "notes": "Detergent",
                    },
                ]
            },
        )
        assert split_res.status_code == 200
        split_data = split_res.json()
        assert split_data["is_split"] is True
        assert len(split_data["splits"]) == 2
        assert split_data["splits"][0]["amount"] == -80.0
        assert split_data["splits"][0]["category"] == "Groceries"
        assert split_data["splits"][0]["tags"] == ["pantry"]
        assert split_data["splits"][1]["amount"] == -40.0
        assert split_data["splits"][1]["category"] == "Household"

        # Breakdown reflects split categories (manual splits override receipt items)
        bd = client.get("/budgets/breakdown?period=month")
        assert bd.status_code == 200
        bd_data = bd.json()
        assert bd_data["total_spend"] == 120.0
        cats = {c["category"]: c["total_spend"] for c in bd_data["categories"]}
        assert cats == {"Groceries": 80.0, "Household": 40.0}

        # Category filter returns sliced amounts with inherited + split tags
        g_res = client.get("/transactions?category=Groceries&period=month")
        assert g_res.status_code == 200
        g_rows = g_res.json()
        assert len(g_rows) == 1
        assert g_rows[0]["amount"] == -80.0
        assert g_rows[0]["category"] == "Groceries"
        assert g_rows[0]["is_split"] is True
        assert g_rows[0]["tags"] == ["weekly-shop", "pantry"]

        h_res = client.get("/transactions?category=Household&period=month")
        assert h_res.status_code == 200
        h_rows = h_res.json()
        assert len(h_rows) == 1
        assert h_rows[0]["amount"] == -40.0
        assert h_rows[0]["category"] == "Household"
        assert h_rows[0]["is_split"] is True
        assert h_rows[0]["tags"] == ["weekly-shop", "cleaning"]

        # Average burn uses split categories
        burn = client.get("/budgets/average-burn?lookback_weeks=4")
        assert burn.status_code == 200
        burn_cats = {c["category"]: c["variable_weekly_burn"] for c in burn.json()["categories"]}
        assert burn_cats == {"Groceries": 20.0, "Household": 10.0}
    finally:
        app.dependency_overrides.clear()


def test_mixed_inflow_split_lent_loan_and_budget_purity():
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
            db.commit()
            db.refresh(acc)

        loan_res = client.post(
            "/loans",
            json={
                "name": "Loan to Brother",
                "lender": "Brother",
                "loan_type": "lent",
                "principal_amount": 10000.0,
                "annual_interest_rate": 0.0,
                "term_months": 0,
                "monthly_payment": 0.0,
                "start_date": today.isoformat(),
            },
        )
        assert loan_res.status_code == 201
        loan_id = loan_res.json()["id"]

        inflow_res = client.post(
            "/transactions",
            json={
                "transaction_date": today.isoformat(),
                "account_id": acc.id,
                "amount": 5020.0,
                "currency": "NZD",
                "merchant": "Brother Bank Transfer",
                "category": "Income",
            },
        )
        assert inflow_res.status_code == 201
        tx_id = inflow_res.json()["id"]

        # Link directly first to verify PUT /splits clears parent loan_id & loan_payment_amount
        assert (
            client.patch(
                f"/transactions/{tx_id}",
                json={"loan_id": loan_id, "loan_payment_amount": 1000.0},
            ).status_code
            == 200
        )

        split_res = client.put(
            f"/transactions/{tx_id}/splits",
            json={
                "splits": [
                    {
                        "amount": 5000.0,
                        "category": "Debt & Loans",
                        "loan_id": loan_id,
                        "notes": "Principal return",
                    },
                    {
                        "amount": 20.0,
                        "category": "Family",
                        "notes": "Food split",
                    },
                ]
            },
        )
        assert split_res.status_code == 200
        body = split_res.json()
        assert body["is_split"] is True
        assert body["loan_id"] is None
        assert body["loan_payment_amount"] is None
        assert body["splits"][0]["loan_id"] == loan_id
        assert body["splits"][0]["loan_name"] == "Loan to Brother"
        assert body["splits"][0]["loan_type"] == "lent"
        loan_split_id = body["splits"][0]["id"]

        # GET /loans credits 5,000.00 (remaining_principal = 5,000.0, split_id present)
        loans_res = client.get("/loans")
        assert loans_res.status_code == 200
        loans_data = loans_res.json()
        assert loans_data["total_receivables"] == 5000.0
        brother_loan = loans_data["loans"][0]
        assert brother_loan["remaining_principal"] == 5000.0
        assert brother_loan["principal_paid"] == 5000.0
        assert brother_loan["payments_made"] == 1
        assert len(brother_loan["linked_payments"]) == 1
        pmt = brother_loan["linked_payments"][0]
        assert pmt["id"] == tx_id
        assert pmt["split_id"] == loan_split_id
        assert pmt["amount"] == 5000.0
        assert pmt["applied_amount"] == 5000.0
        assert pmt["notes"] == "Principal return"

        # GET /budgets/breakdown counts ONLY 20.00 in total_income
        bd_res = client.get("/budgets/breakdown?period=all")
        assert bd_res.status_code == 200
        bd_data = bd_res.json()
        assert bd_data["total_income"] == 20.0
        assert bd_data["total_spend"] == 0.0
        assert bd_data["net_savings"] == 20.0

        # GET /transactions?loan_id=... matches split loan links
        loan_txs = client.get(f"/transactions?loan_id={loan_id}")
        assert loan_txs.status_code == 200
        assert len(loan_txs.json()) == 1
        assert loan_txs.json()[0]["id"] == tx_id
        assert loan_txs.json()[0]["is_split"] is True

        # GET /transactions?category=Family returns sliced +20.00 transaction
        fam_txs = client.get("/transactions?category=Family")
        assert fam_txs.status_code == 200
        fam_rows = fam_txs.json()
        assert len(fam_rows) == 1
        assert fam_rows[0]["amount"] == 20.0
        assert fam_rows[0]["category"] == "Family"
        assert fam_rows[0]["is_split"] is True
        assert fam_rows[0]["notes"] == "Food split"
    finally:
        app.dependency_overrides.clear()


def test_split_validation_clearing_rules_and_lent_outflow_exclusion():
    client, TestingSessionLocal = _build_test_client()

    try:
        today = date.today()
        with TestingSessionLocal() as db:
            acc1 = Account(
                name="ANZ Everyday",
                institution="ANZ",
                account_type="checking",
                tier="cash",
                currency="NZD",
            )
            acc2 = Account(
                name="ANZ Savings",
                institution="ANZ",
                account_type="savings",
                tier="cash",
                currency="NZD",
            )
            db.add_all([acc1, acc2])
            db.flush()

            tx_out = Transaction(
                transaction_date=today,
                account_id=acc1.id,
                amount=-100.0,
                currency="NZD",
                merchant="COUNTDOWN ALBANY",
                category="General",
                raw_hash="split-val-out",
            )
            tx_tr1 = Transaction(
                transaction_date=today,
                account_id=acc1.id,
                amount=-200.0,
                currency="NZD",
                merchant="Transfer Out",
                category="Transfers",
                raw_hash="split-val-tr1",
            )
            tx_tr2 = Transaction(
                transaction_date=today,
                account_id=acc2.id,
                amount=200.0,
                currency="NZD",
                merchant="Transfer In",
                category="Transfers",
                raw_hash="split-val-tr2",
            )
            db.add_all([tx_out, tx_tr1, tx_tr2])
            db.flush()
            tx_tr1.linked_transaction_id = tx_tr2.id
            tx_tr2.linked_transaction_id = tx_tr1.id
            db.commit()
            tx_out_id = tx_out.id
            tx_tr1_id = tx_tr1.id
            acc1_id = acc1.id

        lent_loan = client.post(
            "/loans",
            json={
                "name": "Friend Advance",
                "lender": "Friend",
                "loan_type": "lent",
                "principal_amount": 500.0,
                "annual_interest_rate": 0.0,
                "term_months": 0,
                "monthly_payment": 0.0,
                "start_date": today.isoformat(),
            },
        ).json()
        lent_id = lent_loan["id"]

        # 1. Validation errors
        # Missing transaction -> 404
        assert (
            client.put("/transactions/99999/splits", json={"splits": []}).status_code
            == 404
        )
        # Linked transfer -> 400
        assert (
            client.put(
                f"/transactions/{tx_tr1_id}/splits",
                json={
                    "splits": [
                        {"amount": 100.0, "category": "Groceries"},
                        {"amount": 100.0, "category": "Household"},
                    ]
                },
            ).status_code
            == 400
        )
        # Fewer than 2 split lines -> 422
        assert (
            client.put(
                f"/transactions/{tx_out_id}/splits",
                json={"splits": [{"amount": 100.0, "category": "Groceries"}]},
            ).status_code
            == 422
        )
        # Sum mismatch -> 422
        assert (
            client.put(
                f"/transactions/{tx_out_id}/splits",
                json={
                    "splits": [
                        {"amount": 60.0, "category": "Groceries"},
                        {"amount": 30.0, "category": "Household"},
                    ]
                },
            ).status_code
            == 422
        )
        # Unknown loan_id -> 404
        assert (
            client.put(
                f"/transactions/{tx_out_id}/splits",
                json={
                    "splits": [
                        {"amount": 60.0, "category": "Debt & Loans", "loan_id": 99999},
                        {"amount": 40.0, "category": "Groceries"},
                    ]
                },
            ).status_code
            == 404
        )
        # loan_id on non-'Debt & Loans' category -> 422
        assert (
            client.put(
                f"/transactions/{tx_out_id}/splits",
                json={
                    "splits": [
                        {"amount": 60.0, "category": "Groceries", "loan_id": lent_id},
                        {"amount": 40.0, "category": "Household"},
                    ]
                },
            ).status_code
            == 422
        )
        # Empty category -> 422
        assert (
            client.put(
                f"/transactions/{tx_out_id}/splits",
                json={
                    "splits": [
                        {"amount": 60.0, "category": "   "},
                        {"amount": 40.0, "category": "Groceries"},
                    ]
                },
            ).status_code
            == 422
        )

        # 2. Outflow split combining lent loan disbursement + Transfers + Groceries keeps exact non-lent amount
        split_mixed_out = client.put(
            f"/transactions/{tx_out_id}/splits",
            json={
                "splits": [
                    {"amount": 50.0, "category": "Debt & Loans", "loan_id": lent_id},
                    {"amount": 20.0, "category": "Transfers"},
                    {"amount": 30.0, "category": "Groceries"},
                ]
            },
        )
        assert split_mixed_out.status_code == 200

        bd = client.get("/budgets/breakdown?period=all").json()
        assert bd["total_spend"] == 30.0
        assert bd["total_transfers"] == 420.0
        burn = client.get("/budgets/average-burn?lookback_weeks=4").json()
        burn_cats = {c["category"]: c["variable_weekly_burn"] for c in burn["categories"]}
        assert burn_cats == {"Groceries": 7.5}

        # 3. Recurring lent-only disbursement split is excluded from recurring commitments
        rec_tx = client.post(
            "/transactions",
            json={
                "transaction_date": today.isoformat(),
                "account_id": acc1_id,
                "amount": -200.0,
                "currency": "NZD",
                "merchant": "Monthly Friend Lend",
                "category": "General",
                "recurrence": "monthly",
            },
        ).json()
        assert (
            client.put(
                f"/transactions/{rec_tx['id']}/splits",
                json={
                    "splits": [
                        {"amount": 100.0, "category": "Debt & Loans", "loan_id": lent_id},
                        {"amount": 100.0, "category": "Transfers"},
                    ]
                },
            ).status_code
            == 200
        )
        burn_after_rec = client.get("/budgets/average-burn?lookback_weeks=4").json()
        assert burn_after_rec["recurring_weekly_burn"] == 0.0
        assert burn_after_rec["recurring_commitments"] == []

        # 4. POST /rules/apply-all preserves manual splits untouched
        rule_res = client.post(
            "/rules",
            json={
                "name": "Countdown Rule",
                "description_pattern": "COUNTDOWN",
                "target_category": "Shopping",
                "target_tags": ["supermarket"],
                "apply_retroactively": False,
            },
        )
        assert rule_res.status_code == 201
        apply_res = client.post("/rules/apply-all")
        assert apply_res.status_code == 200

        with TestingSessionLocal() as db:
            reloaded = db.scalar(select(Transaction).where(Transaction.id == tx_out_id))
            assert reloaded is not None
            assert reloaded.category == "General"
            assert reloaded.tags == "[]"
            assert len(reloaded.splits) == 3

        # 5. Clearing splits with [] restores unsplit state
        clear_res = client.put(f"/transactions/{tx_out_id}/splits", json={"splits": []})
        assert clear_res.status_code == 200
        cleared = clear_res.json()
        assert cleared["is_split"] is False
        assert cleared["splits"] == []
    finally:
        app.dependency_overrides.clear()


def test_signed_splits_zero_rejection_legacy_db_rows_tags_and_recurrence():
    from app.models import TransactionSplit

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
            db.commit()
            db.refresh(acc)
            acc_id = acc.id

        # 1. Zero split amount rejected with 422
        tx_neg = client.post(
            "/transactions",
            json={
                "transaction_date": today.isoformat(),
                "account_id": acc_id,
                "amount": -120.0,
                "currency": "NZD",
                "merchant": "Split Merchant",
                "category": "General",
            },
        ).json()
        zero_res = client.put(
            f"/transactions/{tx_neg['id']}/splits",
            json={
                "splits": [
                    {"amount": 0.0, "category": "Groceries"},
                    {"amount": -120.0, "category": "Household"},
                ]
            },
        )
        assert zero_res.status_code == 422
        assert "Split amount cannot be zero" in zero_res.text

        # 2. Signed negative splits on negative parent persist as signed
        signed_neg_res = client.put(
            f"/transactions/{tx_neg['id']}/splits",
            json={
                "splits": [
                    {"amount": -80.0, "category": "Groceries"},
                    {"amount": -40.0, "category": "Household"},
                ]
            },
        )
        assert signed_neg_res.status_code == 200
        assert [s["amount"] for s in signed_neg_res.json()["splits"]] == [-80.0, -40.0]

        # 3. Signed netted split on net-positive parent + tag summary
        tx_net_pos = client.post(
            "/transactions",
            json={
                "transaction_date": today.isoformat(),
                "account_id": acc_id,
                "amount": 137.17,
                "currency": "NZD",
                "merchant": "Netted Travel Refund",
                "category": "General",
            },
        ).json()
        net_split_res = client.put(
            f"/transactions/{tx_net_pos['id']}/splits",
            json={
                "splits": [
                    {"amount": 1000.0, "category": "Income"},
                    {"amount": -862.83, "category": "Travel", "tags": ["japan-trip"]},
                ]
            },
        )
        assert net_split_res.status_code == 200

        tag_summary = client.get("/transactions/summary/tags?period=all").json()
        assert tag_summary["total_tagged_spend"] == 862.83
        assert len(tag_summary["tags"]) == 1
        assert tag_summary["tags"][0]["tag"] == "japan-trip"
        assert tag_summary["tags"][0]["total_spend"] == 862.83

        # 4. Recurring net-positive parent with signed negative outflow split contributes to recurring burn
        tx_rec = client.post(
            "/transactions",
            json={
                "transaction_date": today.isoformat(),
                "account_id": acc_id,
                "amount": 50.0,
                "currency": "NZD",
                "merchant": "Monthly Split Host",
                "category": "General",
                "recurrence": "monthly",
            },
        ).json()
        assert (
            client.put(
                f"/transactions/{tx_rec['id']}/splits",
                json={
                    "splits": [
                        {"amount": 150.0, "category": "Income"},
                        {"amount": -100.0, "category": "Utilities"},
                    ]
                },
            ).status_code
            == 200
        )
        burn = client.get("/budgets/average-burn?lookback_weeks=4").json()
        assert burn["recurring_weekly_burn"] == round(100.0 * (12.0 / 52.0), 2)

        # 5. Legacy unsigned positive splits directly in DB on a negative parent still resolve as outflows
        with TestingSessionLocal() as db:
            legacy_tx = Transaction(
                transaction_date=today,
                account_id=acc_id,
                amount=-120.0,
                currency="NZD",
                merchant="Legacy Unsigned Split",
                category="General",
                raw_hash="legacy-unsigned-split",
            )
            db.add(legacy_tx)
            db.flush()
            db.add_all(
                [
                    TransactionSplit(
                        transaction_id=legacy_tx.id,
                        amount=70.0,
                        category="Dining",
                        tags="[]",
                    ),
                    TransactionSplit(
                        transaction_id=legacy_tx.id,
                        amount=50.0,
                        category="Alcohol",
                        tags="[]",
                    ),
                ]
            )
            db.commit()

        dining_rows = client.get("/transactions?category=Dining").json()
        assert len(dining_rows) == 1
        assert dining_rows[0]["amount"] == -70.0

        bd = client.get("/budgets/breakdown?period=all").json()
        bd_cats = {c["category"]: c["total_spend"] for c in bd["categories"]}
        assert bd_cats["Dining"] == 70.0
        assert bd_cats["Alcohol"] == 50.0
        assert bd_cats["Travel"] == 862.83
    finally:
        app.dependency_overrides.clear()
