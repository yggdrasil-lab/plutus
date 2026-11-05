#!/usr/bin/env python3
"""CLI utility to manage, seed, and reset Plutus wealth data."""

import argparse
import json
import sys
from pathlib import Path

# Add parent directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal
from app.models import Account, AssetSnapshot, BudgetRule, Transaction
from app.services.seed import clear_all_data, seed_initial_data
from sqlalchemy import select


def print_status():
    with SessionLocal() as db:
        accounts = db.scalars(select(Account)).all()
        snapshots = db.scalars(select(AssetSnapshot)).all()
        transactions = db.scalars(select(Transaction)).all()
        budget_rules = db.scalars(select(BudgetRule)).all()

        print(f"=== Plutus Data Status ===")
        print(f"Accounts:     {len(accounts)}")
        for acc in accounts:
            print(f"  - [{acc.tier.upper()}] {acc.name} ({acc.institution})")
        print(f"Snapshots:    {len(snapshots)}")
        print(f"Transactions: {len(transactions)}")
        print(f"Budget Rules: {len(budget_rules)}")


def main():
    parser = argparse.ArgumentParser(description="Plutus wealth database management CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    subparsers.add_parser("status", help="Show summary of existing accounts, snapshots, and transactions")

    # seed
    subparsers.add_parser("seed", help="Seed realistic test data for Freedom Clock, Burn Tracker, and Scenarios")

    # clear
    clear_parser = subparsers.add_parser("clear", help="Clear all transactions, snapshots, and budget rules")
    clear_parser.add_argument("--clear-accounts", action="store_true", help="Also remove account shells")

    args = parser.parse_args()

    if args.command == "status":
        print_status()
    elif args.command == "seed":
        with SessionLocal() as db:
            result = seed_initial_data(db)
            print(json.dumps(result, indent=2))
    elif args.command == "clear":
        with SessionLocal() as db:
            result = clear_all_data(db, clear_accounts=args.clear_accounts)
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
