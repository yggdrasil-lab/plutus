"""Financial institutions directory and suggestion registry."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Account
from app.schemas import InstitutionInfo

router = APIRouter(prefix="/institutions", tags=["Institutions"])

# Standard New Zealand and global homelab financial institutions
DEFAULT_INSTITUTIONS: list[InstitutionInfo] = [
    InstitutionInfo(name="ASB", default_tier="cash", default_type="checking"),
    InstitutionInfo(name="ANZ", default_tier="cash", default_type="checking"),
    InstitutionInfo(name="BNZ", default_tier="cash", default_type="checking"),
    InstitutionInfo(name="Westpac", default_tier="cash", default_type="checking"),
    InstitutionInfo(name="Kiwibank", default_tier="cash", default_type="checking"),
    InstitutionInfo(name="Rabobank", default_tier="cash", default_type="savings"),
    InstitutionInfo(name="TSB", default_tier="cash", default_type="checking"),
    InstitutionInfo(name="Sharesies", default_tier="growth", default_type="brokerage"),
    InstitutionInfo(name="Hatch", default_tier="growth", default_type="brokerage"),
    InstitutionInfo(name="Kernel", default_tier="growth", default_type="brokerage"),
    InstitutionInfo(name="Milford", default_tier="growth", default_type="brokerage"),
    InstitutionInfo(name="InvestNow", default_tier="growth", default_type="brokerage"),
    InstitutionInfo(name="Interactive Brokers", default_tier="growth", default_type="brokerage"),
    InstitutionInfo(name="Simplicity", default_tier="locked", default_type="kiwisaver"),
    InstitutionInfo(name="DBS Bank", default_tier="cash", default_type="savings"),
]


@router.get("", response_model=list[InstitutionInfo])
def list_institutions(db: Session = Depends(get_db)) -> list[InstitutionInfo]:
    """Retrieve known institutions merged with custom institutions from active accounts."""
    results: list[InstitutionInfo] = list(DEFAULT_INSTITUTIONS)
    known_names = {inst.name.lower() for inst in results}

    # Query distinct active accounts in DB to include any custom ones
    active_accounts = db.scalars(
        select(Account).where(Account.is_active == True)
    ).all()

    for acc in active_accounts:
        if acc.institution and acc.institution.lower() not in known_names:
            results.append(
                InstitutionInfo(
                    name=acc.institution,
                    default_tier=acc.tier,
                    default_type=acc.account_type,
                    currency=acc.currency,
                )
            )
            known_names.add(acc.institution.lower())

    return results
