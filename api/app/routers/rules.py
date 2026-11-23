"""Outlook-style transaction categorization rules router."""

import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.models import Account, CategorizationRule, Loan
from app.schemas import (
    CategorizationRuleCreate,
    CategorizationRuleRead,
    CategorizationRuleUpdate,
    ClassifyUncategorizedRequest,
    ClassifyUncategorizedResponse,
)
from app.services.ai_gateway import AiGateway, get_ai_gateway
from app.services.rules_engine import (
    apply_rules_to_existing_transactions,
    classify_uncategorized_transactions,
    normalize_tags,
)

router = APIRouter(prefix="/rules", tags=["Categorization Rules"])


def _hydrate_rule(rule: CategorizationRule) -> CategorizationRule:
    """Populate derived counterpart_account_name and loan_name for CategorizationRuleRead."""
    rule.counterpart_account_name = (
        rule.counterpart_account.name if rule.counterpart_account else None
    )
    rule.loan_name = rule.loan.name if rule.loan else None
    rule.loan_type = rule.loan.loan_type if rule.loan else None
    return rule


def _validate_counterpart_account(db: Session, counterpart_account_id: Optional[int]) -> None:
    """Ensure referenced counterpart_account_id exists in Account."""
    if counterpart_account_id is not None and db.get(Account, counterpart_account_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Counterpart account not found",
        )


def _validate_loan(db: Session, loan_id: Optional[int]) -> None:
    """Ensure referenced loan_id exists in Loan."""
    if loan_id is not None and db.get(Loan, loan_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Loan not found",
        )


def _load_rule_with_counterpart(db: Session, rule_id: int) -> Optional[CategorizationRule]:
    """Load one rule with its counterpart_account and loan relationships hydrated."""
    return db.scalar(
        select(CategorizationRule)
        .options(
            joinedload(CategorizationRule.counterpart_account),
            joinedload(CategorizationRule.loan),
        )
        .where(CategorizationRule.id == rule_id)
    )


@router.get("", response_model=list[CategorizationRuleRead])
def list_rules(
    is_active: Optional[bool] = Query(default=None),
    db: Session = Depends(get_db),
):
    """List all Outlook-style categorization rules."""
    stmt = select(CategorizationRule).options(
        joinedload(CategorizationRule.counterpart_account),
        joinedload(CategorizationRule.loan),
    )
    if is_active is not None:
        stmt = stmt.where(CategorizationRule.is_active == is_active)
    rules = (
        db.scalars(stmt.order_by(CategorizationRule.priority.asc(), CategorizationRule.id.asc()))
        .unique()
        .all()
    )
    return [_hydrate_rule(rule) for rule in rules]


@router.post("", response_model=CategorizationRuleRead, status_code=status.HTTP_201_CREATED)
def create_rule(
    payload: CategorizationRuleCreate,
    db: Session = Depends(get_db),
):
    """Create a new transaction categorization rule and optionally apply it retroactively."""
    _validate_counterpart_account(db, payload.counterpart_account_id)
    _validate_loan(db, payload.loan_id)
    amt = (
        float(payload.amount_equals)
        if (payload.amount_equals is not None and float(payload.amount_equals) > 0)
        else None
    )
    cleaned_rename = payload.rename_merchant.strip() if payload.rename_merchant else ""
    rule = CategorizationRule(
        name=payload.name.strip(),
        description_pattern=payload.description_pattern.strip(),
        match_type=payload.match_type.strip(),
        amount_equals=amt,
        min_amount=float(payload.min_amount) if payload.min_amount is not None else None,
        max_amount=float(payload.max_amount) if payload.max_amount is not None else None,
        direction=payload.direction,
        target_category=payload.target_category.strip(),
        target_tags=json.dumps(normalize_tags(payload.target_tags)),
        target_recurrence=payload.target_recurrence,
        rename_merchant=cleaned_rename or None,
        counterpart_account_id=payload.counterpart_account_id,
        loan_id=payload.loan_id,
        priority=payload.priority,
        is_active=payload.is_active,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)

    if payload.apply_retroactively:
        apply_rules_to_existing_transactions(db, [rule.id])

    hydrated = _load_rule_with_counterpart(db, rule.id)
    return _hydrate_rule(hydrated or rule)


@router.get("/{rule_id}", response_model=CategorizationRuleRead)
def get_rule(
    rule_id: int,
    db: Session = Depends(get_db),
):
    """Retrieve an existing categorization rule."""
    rule = _load_rule_with_counterpart(db, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Categorization rule not found")
    return _hydrate_rule(rule)


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

    next_min = (
        (float(payload.min_amount) if payload.min_amount is not None else None)
        if "min_amount" in payload.model_fields_set
        else (float(rule.min_amount) if rule.min_amount is not None else None)
    )
    next_max = (
        (float(payload.max_amount) if payload.max_amount is not None else None)
        if "max_amount" in payload.model_fields_set
        else (float(rule.max_amount) if rule.max_amount is not None else None)
    )
    if next_min is not None and next_max is not None and next_min > next_max:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="min_amount must be less than or equal to max_amount.",
        )

    if "counterpart_account_id" in payload.model_fields_set:
        _validate_counterpart_account(db, payload.counterpart_account_id)
    if "loan_id" in payload.model_fields_set:
        _validate_loan(db, payload.loan_id)

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
    if "min_amount" in payload.model_fields_set:
        rule.min_amount = next_min
    if "max_amount" in payload.model_fields_set:
        rule.max_amount = next_max
    if "direction" in payload.model_fields_set:
        rule.direction = payload.direction
    if "target_category" in payload.model_fields_set and payload.target_category is not None:
        rule.target_category = payload.target_category.strip()
    if "target_tags" in payload.model_fields_set:
        rule.target_tags = json.dumps(normalize_tags(payload.target_tags or []))
    if "target_recurrence" in payload.model_fields_set:
        rule.target_recurrence = payload.target_recurrence
    if "rename_merchant" in payload.model_fields_set:
        cleaned_rename = payload.rename_merchant.strip() if payload.rename_merchant else ""
        rule.rename_merchant = cleaned_rename or None
    if "counterpart_account_id" in payload.model_fields_set:
        rule.counterpart_account_id = payload.counterpart_account_id
    if "loan_id" in payload.model_fields_set:
        rule.loan_id = payload.loan_id
    if "priority" in payload.model_fields_set and payload.priority is not None:
        rule.priority = payload.priority
    if "is_active" in payload.model_fields_set and payload.is_active is not None:
        rule.is_active = payload.is_active

    db.commit()
    db.refresh(rule)

    if payload.apply_retroactively:
        apply_rules_to_existing_transactions(db, [rule.id])

    hydrated = _load_rule_with_counterpart(db, rule.id)
    return _hydrate_rule(hydrated or rule)


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


@router.post("/classify-uncategorized", response_model=ClassifyUncategorizedResponse)
def classify_uncategorized(
    payload: Optional[ClassifyUncategorizedRequest] = None,
    db: Session = Depends(get_db),
    ai_gateway: AiGateway = Depends(get_ai_gateway),
):
    """Retroactively classify uncategorized or untagged transactions via Jev System One."""
    req = payload or ClassifyUncategorizedRequest()
    return classify_uncategorized_transactions(
        db,
        limit=req.limit,
        include_untagged=req.include_untagged,
        dry_run=req.dry_run,
        only_outflows=req.only_outflows,
        gateway=ai_gateway,
    )
