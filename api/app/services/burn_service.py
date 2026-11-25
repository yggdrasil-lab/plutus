"""Account balance and tier label repository helpers."""

# ============================================================================
# 1. IMPORTS & CONSTANTS
# ============================================================================
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import Account, AssetSnapshot


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
