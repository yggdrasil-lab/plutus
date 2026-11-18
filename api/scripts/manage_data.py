#!/usr/bin/env python3
"""Read-only CLI status utility for Plutus wealth data."""

import argparse
import sys
from pathlib import Path

# Add parent directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal
from app.models import Account, AssetSnapshot, BudgetRule, Transaction
from sqlalchemy import select


def print_status():
    with SessionLocal() as db:
        accounts = db.scalars(select(Account)).all()
        snapshots = db.scalars(select(AssetSnapshot)).all()
        transactions = db.scalars(select(Transaction)).all()
        budget_rules = db.scalars(select(BudgetRule)).all()

        print("=== Plutus Data Status ===")
        print(f"Accounts:     {len(accounts)}")
        for acc in accounts:
            print(f"  - [{acc.tier.upper()}] {acc.name} ({acc.institution})")
        print(f"Snapshots:    {len(snapshots)}")
        print(f"Transactions: {len(transactions)}")
        print(f"Budget Rules: {len(budget_rules)}")


def main():
    parser = argparse.ArgumentParser(description="Plutus wealth database inspection CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Show summary of existing accounts, snapshots, and transactions")

    args = parser.parse_args()
    if args.command == "status":
        print_status()


if __name__ == "__main__":
    main()
