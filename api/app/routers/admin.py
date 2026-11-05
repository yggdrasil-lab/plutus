"""Administrative router for test data seeding and database reset."""

from typing import Any
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.routers.integrations import verify_integration_token
from app.services.seed import clear_all_data, seed_initial_data

router = APIRouter(prefix="/admin", tags=["Admin & Data Management"])


@router.post("/seed", status_code=status.HTTP_201_CREATED)
def seed_test_data(
    db: Session = Depends(get_db),
    _authorized: bool = Depends(verify_integration_token),
) -> dict[str, Any]:
    """Seed initial realistic financial baseline dataset for Midas PWA and Plutus testing."""
    return seed_initial_data(db)


@router.post("/reset", status_code=status.HTTP_200_OK)
def reset_database(
    clear_accounts: bool = Query(
        default=False,
        description="Whether to also remove account shells. Defaults to false so accounts remain ready.",
    ),
    db: Session = Depends(get_db),
    _authorized: bool = Depends(verify_integration_token),
) -> dict[str, Any]:
    """Clear financial transactions, snapshots, and budgets to prepare for real data ingestion."""
    return clear_all_data(db, clear_accounts=clear_accounts)
