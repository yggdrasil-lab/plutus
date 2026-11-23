"""Account balance, tier label, and startup FX repair helpers."""

# ============================================================================
# 1. IMPORTS & CONSTANTS
# ============================================================================
from typing import Optional

from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session

from app.models import Account, AssetSnapshot, Transaction
from app.services.fx_service import FxRateFetcher, get_exchange_rate


# ============================================================================
# 2. ACCOUNT REPOSITORY HELPERS
# ============================================================================
def get_latest_account_balances(db: Session) -> dict[str, float]:
    """Retrieve the latest balance in NZD for all active accounts grouped by tier."""
    accounts = db.scalars(select(Account).where(Account.is_active == True)).all()
    balances = {"cash": 0.0, "growth": 0.0, "locked": 0.0}

    for acc in accounts:
        latest_snapshot = db.scalar(
            select(AssetSnapshot)
            .where(AssetSnapshot.account_id == acc.id)
            .order_by(desc(AssetSnapshot.snapshot_date))
        )
        bal_nzd = float(latest_snapshot.balance_nzd) if latest_snapshot else 0.0
        tier = acc.tier.lower()
        balances[tier if tier in balances else "cash"] += bal_nzd

    return balances


def resolve_tier_labels(db: Session) -> dict[str, str]:
    """Resolve display labels for Cash, Growth, and Locked tiers from active accounts."""
    active_accounts = db.scalars(select(Account).where(Account.is_active == True)).all()
    locked_accs = [a.name for a in active_accounts if a.tier.lower() == "locked"]
    growth_accs = [a.name for a in active_accounts if a.tier.lower() == "growth"]
    cash_accs = [
        a.name
        for a in active_accounts
        if a.tier.lower() == "cash" and a.account_type != "credit_card"
    ]
    return {
        "cash": cash_accs[0] if len(cash_accs) == 1 else "Cash Floor",
        "growth": growth_accs[0] if len(growth_accs) == 1 else "Growth",
        "locked": locked_accs[0] if len(locked_accs) == 1 else "Locked Reserve",
    }


# ============================================================================
# 3. FX INTEGRITY REPAIR
# ============================================================================
def repair_transaction_fx_rates(
    db: Session,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> int:
    """Repair corrupted NZD exchange rates and backfill missing foreign-currency FX rates."""
    candidates = db.scalars(
        select(Transaction).where(
            Transaction.linked_transaction_id.is_(None),
            or_(
                (func.upper(Transaction.currency) == "NZD")
                & Transaction.exchange_rate.is_not(None)
                & (Transaction.exchange_rate != 1.0),
                (func.upper(Transaction.currency) != "NZD")
                & (
                    Transaction.exchange_rate.is_(None)
                    | (Transaction.exchange_rate <= 0)
                    | (
                        (func.upper(Transaction.currency) == "SGD")
                        & (Transaction.exchange_rate < 1.20)
                    )
                ),
            ),
        )
    ).all()

    repaired = 0
    for tx in candidates:
        code = (tx.currency or "NZD").strip().upper()
        if code == "NZD":
            tx.exchange_rate = 1.0
            repaired += 1
            continue

        resolved = get_exchange_rate(
            db,
            rate_date=tx.transaction_date,
            from_currency=code,
            to_currency="NZD",
            fetcher=fetcher,
        )
        if resolved is not None and resolved[0] > 0:
            tx.exchange_rate = round(float(resolved[0]), 8)
            repaired += 1

    if repaired > 0:
        db.commit()
    return repaired
