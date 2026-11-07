"""Outlook-style transaction categorization rules router."""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import CategorizationRule
from app.schemas import (
    CategorizationRuleCreate,
    CategorizationRuleRead,
    CategorizationRuleUpdate,
)
from app.services.rules_engine import apply_rules_to_existing_transactions

router = APIRouter(prefix="/rules", tags=["Categorization Rules"])


@router.get("", response_model=list[CategorizationRuleRead])
def list_rules(
    is_active: Optional[bool] = Query(default=None),
    db: Session = Depends(get_db),
):
    """List all Outlook-style categorization rules."""
    stmt = select(CategorizationRule)
    if is_active is not None:
        stmt = stmt.where(CategorizationRule.is_active == is_active)
    return db.scalars(stmt.order_by(CategorizationRule.priority.asc(), CategorizationRule.id.asc())).all()


@router.post("", response_model=CategorizationRuleRead, status_code=status.HTTP_201_CREATED)
def create_rule(
    payload: CategorizationRuleCreate,
    db: Session = Depends(get_db),
):
    """Create a new transaction categorization rule and optionally apply it retroactively."""
    amt = (
        float(payload.amount_equals)
        if (payload.amount_equals is not None and float(payload.amount_equals) > 0)
        else None
    )
    rule = CategorizationRule(
        name=payload.name.strip(),
        description_pattern=payload.description_pattern.strip(),
        match_type=payload.match_type.strip(),
        amount_equals=amt,
        target_category=payload.target_category.strip(),
        rename_merchant=payload.rename_merchant.strip() if payload.rename_merchant else None,
        priority=payload.priority,
        is_active=payload.is_active,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)

    if payload.apply_retroactively:
        apply_rules_to_existing_transactions(db, [rule.id])
        db.refresh(rule)

    return rule


@router.get("/{rule_id}", response_model=CategorizationRuleRead)
def get_rule(
    rule_id: int,
    db: Session = Depends(get_db),
):
    """Retrieve an existing categorization rule."""
    rule = db.get(CategorizationRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Categorization rule not found")
    return rule


@router.put("/{rule_id}", response_model=CategorizationRuleRead)
def update_rule(
    rule_id: int,
    payload: CategorizationRuleUpdate,
    db: Session = Depends(get_db),
):
    """Update conditions or target category of an existing categorization rule."""
    rule = db.get(CategorizationRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Categorization rule not found")

    if "name" in payload.model_fields_set and payload.name is not None:
        rule.name = payload.name.strip()
    if "description_pattern" in payload.model_fields_set and payload.description_pattern is not None:
        rule.description_pattern = payload.description_pattern.strip()
    if "match_type" in payload.model_fields_set and payload.match_type is not None:
        rule.match_type = payload.match_type.strip()
    if "amount_equals" in payload.model_fields_set:
        rule.amount_equals = (
            float(payload.amount_equals)
            if (payload.amount_equals is not None and float(payload.amount_equals) > 0)
            else None
        )
    if "target_category" in payload.model_fields_set and payload.target_category is not None:
        rule.target_category = payload.target_category.strip()
    if "rename_merchant" in payload.model_fields_set:
        rule.rename_merchant = payload.rename_merchant.strip() if payload.rename_merchant else None
    if "priority" in payload.model_fields_set and payload.priority is not None:
        rule.priority = payload.priority
    if "is_active" in payload.model_fields_set and payload.is_active is not None:
        rule.is_active = payload.is_active

    db.commit()
    db.refresh(rule)

    if payload.apply_retroactively:
        apply_rules_to_existing_transactions(db, [rule.id])
        db.refresh(rule)

    return rule


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(
    rule_id: int,
    db: Session = Depends(get_db),
):
    """Delete a categorization rule."""
    rule = db.get(CategorizationRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Categorization rule not found")
    db.delete(rule)
    db.commit()
    return None


@router.post("/apply-all")
def apply_all_rules(
    db: Session = Depends(get_db),
):
    """Re-run all active categorization rules across all transactions."""
    result = apply_rules_to_existing_transactions(db)
    return result
