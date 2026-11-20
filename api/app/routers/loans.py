"""Loans and financing tracker router."""

from datetime import date
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Loan
from app.schemas import LoanCreate, LoanRead, LoansSummaryResponse, LoanUpdate
from app.services.loan_service import (
    compute_loan_status,
    load_loan_with_transactions,
    load_loans_with_transactions,
    summarize_loans,
)

router = APIRouter(prefix="/loans", tags=["Loans & Financing"])


@router.get("", response_model=LoansSummaryResponse)
def list_loans(db: Session = Depends(get_db)):
    """List all active loans with live payment countdown, remaining principal, and net worth impact."""
    loans = load_loans_with_transactions(db, active_only=True)
    return summarize_loans(loans, date.today())


@router.post("", response_model=LoanRead, status_code=status.HTTP_201_CREATED)
def create_loan(payload: LoanCreate, db: Session = Depends(get_db)):
    """Register a new financed loan, credit commitment, or lent receivable."""
    loan = Loan(
        name=payload.name.strip(),
        lender=payload.lender.strip(),
        loan_type=payload.loan_type,
        principal_amount=payload.principal_amount,
        annual_interest_rate=payload.annual_interest_rate,
        term_months=payload.term_months,
        monthly_payment=payload.monthly_payment,
        start_date=payload.start_date,
        asset_value=payload.asset_value,
        currency=payload.currency.strip().upper()[:3] or "NZD",
        notes=payload.notes,
        is_active=payload.is_active,
    )
    db.add(loan)
    db.commit()
    loaded = load_loan_with_transactions(db, loan.id)
    return compute_loan_status(loaded or loan, date.today())


@router.patch("/{loan_id}", response_model=LoanRead)
def update_loan(loan_id: int, payload: LoanUpdate, db: Session = Depends(get_db)):
    """Update loan details such as type, asset value, monthly payment, or active status."""
    loan = db.get(Loan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")

    non_nullable_fields = (
        "name",
        "lender",
        "loan_type",
        "principal_amount",
        "annual_interest_rate",
        "term_months",
        "monthly_payment",
        "start_date",
        "is_active",
    )
    for field in (*non_nullable_fields, "asset_value", "notes"):
        if field in payload.model_fields_set:
            val = getattr(payload, field)
            if field in non_nullable_fields and val is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"{field} cannot be null.",
                )
            if isinstance(val, str):
                val = val.strip()
            setattr(loan, field, val)

    effective_loan_type = (loan.loan_type or "borrowed").strip().lower()
    if effective_loan_type == "borrowed" and (
        int(loan.term_months) < 1 or float(loan.monthly_payment) <= 0.0
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Borrowed loans require term_months >= 1 and monthly_payment > 0.",
        )

    db.commit()
    loaded = load_loan_with_transactions(db, loan.id)
    return compute_loan_status(loaded or loan, date.today())


@router.delete("/{loan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_loan(loan_id: int, db: Session = Depends(get_db)):
    """Delete a loan record."""
    loan = db.get(Loan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    db.delete(loan)
    db.commit()
    return None
