"""Loans and financing tracker router."""

from datetime import date
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import LoanCreate, LoanRead, LoansSummaryResponse, LoanUpdate
from app.services.loan_service import (
    LoanCreateInput,
    LoanNotFoundError,
    LoanValidationError,
    create_loan as create_loan_record,
    delete_loan as delete_loan_record,
    get_loan_portfolio_summary,
    update_loan as update_loan_record,
)

router = APIRouter(prefix="/loans", tags=["Loans & Financing"])


@router.get("", response_model=LoansSummaryResponse)
def list_loans(db: Session = Depends(get_db)):
    """List all active loans with live payment countdown, remaining principal, and net worth impact."""
    return get_loan_portfolio_summary(db, as_of=date.today())


@router.post("", response_model=LoanRead, status_code=status.HTTP_201_CREATED)
def create_loan(payload: LoanCreate, db: Session = Depends(get_db)):
    """Register a new financed loan, credit commitment, or lent receivable."""
    try:
        return create_loan_record(
            db,
            LoanCreateInput(**payload.model_dump()),
            as_of=date.today(),
        )
    except LoanValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.patch("/{loan_id}", response_model=LoanRead)
def update_loan(loan_id: int, payload: LoanUpdate, db: Session = Depends(get_db)):
    """Update loan details such as type, asset value, monthly payment, or active status."""
    try:
        return update_loan_record(
            db,
            loan_id,
            payload.model_dump(exclude_unset=True),
            as_of=date.today(),
        )
    except LoanNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Loan not found",
        ) from exc
    except LoanValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.delete("/{loan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_loan(loan_id: int, db: Session = Depends(get_db)):
    """Delete a loan record."""
    try:
        delete_loan_record(db, loan_id)
    except LoanNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Loan not found",
        ) from exc
    return None
