"""Seed and reset data service for Plutus wealth engine."""

from datetime import date, timedelta
from typing import Any
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Account, AssetSnapshot, BudgetRule, RunwaySnapshot, Transaction
from app.services.statement_parser import generate_transaction_hash


def clear_all_data(db: Session, clear_accounts: bool = False) -> dict[str, Any]:
    """Delete financial transactions, asset snapshots, runway logs, and budget rules."""
    tx_count = db.scalar(select(Transaction)).all() if False else len(db.scalars(select(Transaction)).all())
    snap_count = len(db.scalars(select(AssetSnapshot)).all())

    db.execute(delete(Transaction))
    db.execute(delete(AssetSnapshot))
    db.execute(delete(RunwaySnapshot))
    db.execute(delete(BudgetRule))

    accounts_cleared = 0
    if clear_accounts:
        accounts_cleared = len(db.scalars(select(Account)).all())
        db.execute(delete(Account))

    db.commit()

    return {
        "status": "success",
        "message": "Data cleared successfully. Fresh slate ready for real financial data.",
        "cleared_transactions": tx_count,
        "cleared_snapshots": snap_count,
        "cleared_accounts": accounts_cleared,
    }


def seed_initial_data(db: Session) -> dict[str, Any]:
    """Populate realistic test data for Freedom Clock, Weekly Burn, and Scenarios."""
    # 1. Clean existing records for deterministic state
    clear_all_data(db, clear_accounts=True)

    today = date.today()

    # 2. Accounts
    asb_checking = Account(
        name="ASB Everyday Checking",
        account_type="checking",
        tier="cash",
        institution="ASB",
        currency="NZD",
        expected_annual_return=0.0,
        is_active=True,
    )
    asb_savings = Account(
        name="ASB Savings Floor",
        account_type="savings",
        tier="cash",
        institution="ASB",
        currency="NZD",
        expected_annual_return=0.045,
        is_active=True,
    )
    sharesies = Account(
        name="Sharesies US500 & NZX",
        account_type="brokerage",
        tier="growth",
        institution="Sharesies",
        currency="NZD",
        expected_annual_return=0.07,
        is_active=True,
    )
    simplicity = Account(
        name="Simplicity KiwiSaver Growth",
        account_type="kiwisaver",
        tier="locked",
        institution="Simplicity",
        currency="NZD",
        expected_annual_return=0.065,
        is_active=True,
    )

    db.add_all([asb_checking, asb_savings, sharesies, simplicity])
    db.commit()
    db.refresh(asb_checking)
    db.refresh(asb_savings)
    db.refresh(sharesies)
    db.refresh(simplicity)

    # 3. Asset Snapshots
    # Cash Floor = $4,000 + $20,000 = $24,000 (24 weeks at $1,000/w)
    # Growth Portfolio = $160,000 (Total liquid = $184,000 -> 184 weeks)
    # KiwiSaver = $85,000
    snapshots = [
        AssetSnapshot(
            account_id=asb_checking.id,
            snapshot_date=today,
            balance=4000.0,
            currency="NZD",
            balance_nzd=4000.0,
            source="seed",
            notes="Primary operating float",
        ),
        AssetSnapshot(
            account_id=asb_savings.id,
            snapshot_date=today,
            balance=20000.0,
            currency="NZD",
            balance_nzd=20000.0,
            source="seed",
            notes="Liquid emergency survival reserve",
        ),
        AssetSnapshot(
            account_id=sharesies.id,
            snapshot_date=today,
            balance=160000.0,
            currency="NZD",
            balance_nzd=160000.0,
            source="seed",
            notes="Core passive index growth funds",
        ),
        AssetSnapshot(
            account_id=simplicity.id,
            snapshot_date=today,
            balance=85000.0,
            currency="NZD",
            balance_nzd=85000.0,
            source="seed",
            notes="Locked retirement superannuation",
        ),
    ]
    db.add_all(snapshots)

    # 4. Budget Rules ($1,000 total weekly budget)
    budget_rules = [
        BudgetRule(category="Groceries", weekly_limit=300.0, monthly_limit=1200.0),
        BudgetRule(category="Food", weekly_limit=200.0, monthly_limit=800.0),
        BudgetRule(category="Transport", weekly_limit=100.0, monthly_limit=400.0),
        BudgetRule(category="Utilities", weekly_limit=150.0, monthly_limit=600.0),
        BudgetRule(category="Leisure", weekly_limit=150.0, monthly_limit=600.0),
        BudgetRule(category="Health", weekly_limit=100.0, monthly_limit=400.0),
    ]
    db.add_all(budget_rules)

    # 5. Transactions
    # Trailing 4 weeks expenses (~$1,000/week)
    transactions = []
    # Seed historical trailing expenses over past 28 days
    historical_spends = [
        # Week -4
        (today - timedelta(days=26), -145.50, "PAK N SAVE ALBANY", "Groceries"),
        (today - timedelta(days=25), -18.50, "ESPRESSO WORKSHOP", "Food"),
        (today - timedelta(days=24), -95.00, "BP CONNECT TAKAPUNA", "Transport"),
        (today - timedelta(days=23), -125.00, "WATERCARE SERVICES", "Utilities"),
        (today - timedelta(days=22), -45.00, "CHEMIST WAREHOUSE", "Health"),
        (today - timedelta(days=21), -135.00, "NEW WORLD VICTORIA PARK", "Groceries"),
        (today - timedelta(days=20), -65.00, "SPOTIFY / STEAM", "Leisure"),
        # Week -3
        (today - timedelta(days=19), -160.00, "PAK N SAVE ALBANY", "Groceries"),
        (today - timedelta(days=18), -24.00, "COLUMBUS COFFEE", "Food"),
        (today - timedelta(days=17), -110.00, "MERCURY ENERGY", "Utilities"),
        (today - timedelta(days=16), -85.00, "Z ENERGY BOTANY", "Transport"),
        (today - timedelta(days=15), -140.00, "COUNTDOWN GREY LYNN", "Groceries"),
        (today - timedelta(days=14), -55.00, "EVENT CINEMAS", "Leisure"),
        # Week -2
        (today - timedelta(days=13), -175.00, "WOOLWORTHS PONSONBY", "Groceries"),
        (today - timedelta(days=12), -32.00, "BURGER BURGER", "Food"),
        (today - timedelta(days=11), -85.00, "SPARK NZ", "Utilities"),
        (today - timedelta(days=10), -70.00, "AT HOP CARD AUTOLOAD", "Transport"),
        (today - timedelta(days=9), -150.00, "PAK N SAVE ALBANY", "Groceries"),
        (today - timedelta(days=8), -80.00, "LES MILLS GYM", "Health"),
        (today - timedelta(days=7), -60.00, "PLAYSTATION NETWORK", "Leisure"),
    ]

    for tx_date, amount, merchant, category in historical_spends:
        raw_hash = generate_transaction_hash(tx_date, amount, merchant, asb_checking.id)
        transactions.append(
            Transaction(
                transaction_date=tx_date,
                account_id=asb_checking.id,
                amount=amount,
                currency="NZD",
                merchant=merchant,
                category=category,
                raw_description=f"Historical: {merchant}",
                raw_hash=raw_hash,
            )
        )

    # Current Calendar Week (Monday to today)
    # Target: $750 spent so far this week against $1,000 budget -> +0.25 weeks banked!
    start_of_week = today - timedelta(days=today.weekday())
    current_week_spends = [
        (start_of_week, -240.00, "PAK N SAVE ALBANY", "Groceries"),
        (start_of_week + timedelta(days=1), -18.50, "COLUMBUS COFFEE", "Food"),
        (start_of_week + timedelta(days=1), -85.00, "BP CONNECT TAKAPUNA", "Transport"),
        (start_of_week + timedelta(days=2), -145.00, "MERCURY ENERGY", "Utilities"),
        (start_of_week + timedelta(days=2), -65.50, "SAL'S AUTHENTIC PIZZA", "Food"),
        (start_of_week + timedelta(days=3), -115.00, "WOOLWORTHS METRO", "Groceries"),
        (min(today, start_of_week + timedelta(days=4)), -81.00, "STEAM GAMES / BOOKS", "Leisure"),
    ]

    for tx_date, amount, merchant, category in current_week_spends:
        raw_hash = generate_transaction_hash(tx_date, amount, merchant, asb_checking.id)
        transactions.append(
            Transaction(
                transaction_date=tx_date,
                account_id=asb_checking.id,
                amount=amount,
                currency="NZD",
                merchant=merchant,
                category=category,
                raw_description=f"CurrentWeek: {merchant}",
                raw_hash=raw_hash,
            )
        )

    db.add_all(transactions)
    db.commit()

    return {
        "status": "success",
        "message": "Seeded initial wealth dataset successfully.",
        "accounts_created": 4,
        "cash_balance": 24000.0,
        "growth_balance": 160000.0,
        "locked_balance": 85000.0,
        "total_liquid": 184000.0,
        "weekly_budget": 1000.0,
        "current_week_spend": 745.0,
        "transactions_seeded": len(transactions),
    }
