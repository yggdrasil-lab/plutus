"""Line-item heuristic classification, rule/Jev tagging, and discount attribution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
import json
import re
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CategorizationRule, Receipt, ReceiptItem
from app.services.ai_gateway import AiGateway
from app.services.jev_client import (
    call_jev,
    classify_transaction_jev_with_tags,
    get_extra_jev_categories,
)
from app.services.product_catalog import get_or_create_product
from app.services.receipt_ingestion._types import (
    ItemClassification,
    LineItemHeuristic,
    ReceiptItemDraft,
    ReceiptItemSnapshot,
    ReceiptNotFoundError,
)
from app.services.rules_engine import (
    collect_existing_tags,
    match_receipt_item_rule,
    normalize_tags,
    parse_rule_tags,
    strip_redundant_tags,
)

_SKINCARE_KEYWORDS = (
    "serum",
    "vitamin c",
    "retinol",
    "cleanser",
    "moisturizer",
    "spf",
    "sunscreen",
    "hyaluronic",
    "niacinamide",
    "toner",
    "exfoliant",
    "adapalene",
    "haircut",
    "barber",
    "shampoo",
    "conditioner",
    "deodorant",
    "fragrance",
)
_SUPPLEMENT_KEYWORDS = (
    "protein",
    "whey",
    "creatine",
    "collagen",
    "magnesium",
    "zinc",
    "omega 3",
    "fish oil",
    "vitamin d",
    "multivitamin",
    "electrolytes",
)
_CLOTHING_KEYWORDS = (
    "pant",
    "shirt",
    "hoodie",
    "liner",
    "sock",
    "slide",
    "shoe",
    "jacket",
    "short",
    "jeans",
    "sneaker",
)
_TECH_KEYWORDS = (
    "cable",
    "charger",
    "adapter",
    "usb",
    "ssd",
    "camera",
    "mouse",
    "keyboard",
    "monitor",
)
_HOUSEHOLD_KEYWORDS = ("bulb", "filter", "vacuum", "appliance", "detergent", "towel")
_GROCERY_KEYWORDS = (
    "milk",
    "bread",
    "eggs",
    "apple",
    "banana",
    "chicken",
    "beef",
    "rice",
    "coffee",
    "oats",
    "noodle",
    "lamington",
    "coca cola",
    "cola",
)
_TAG_KEYWORD_MAP: tuple[tuple[str, str], ...] = (
    ("vitamin c", "vitamin-c"),
    ("serum", "serum"),
    ("retinol", "retinol"),
    ("cleanser", "cleanser"),
    ("sunscreen", "sunscreen"),
    ("spf", "sunscreen"),
    ("moisturizer", "moisturizer"),
    ("cream", "skincare"),
    ("protein", "protein"),
    ("whey", "whey"),
    ("creatine", "creatine"),
    ("magnesium", "magnesium"),
    ("collagen", "collagen"),
    ("supplement", "supplements"),
    ("adapter", "adapter"),
    ("charger", "charger"),
    ("cable", "cable"),
    ("bulb", "lighting"),
    ("coffee", "coffee"),
    ("groceries", "groceries"),
    ("milk", "groceries"),
    ("bread", "groceries"),
)
_DISCOUNT_STOPWORDS = frozenset(
    {"tpd", "discount", "off", "save", "savings", "promo", "coupon", "instant"}
)


def _classify_skincare_item(name: str) -> LineItemHeuristic:
    if "vitamin c" in name or "ascorbic" in name:
        p_type = "vitamin_c_serum"
    elif "retinol" in name or "retinoid" in name:
        p_type = "retinol"
    elif "cleanser" in name or "wash" in name:
        p_type = "cleanser"
    elif "sunscreen" in name or "spf" in name:
        p_type = "sunscreen"
    elif "moisturizer" in name or "cream" in name:
        p_type = "moisturizer"
    else:
        p_type = "serum"

    volume = next(
        (vol for vol in ("30ml", "50ml", "100ml", "150ml", "200ml", "15ml") if vol in name),
        None,
    )
    return LineItemHeuristic("Personal Care", "02-skin", p_type, volume)


def _classify_supplement_item(name: str) -> LineItemHeuristic:
    if "creatine" in name:
        p_type = "creatine"
    elif "protein" in name or "whey" in name:
        p_type = "protein_powder"
    elif "magnesium" in name:
        p_type = "magnesium"
    else:
        p_type = "supplement"

    volume = next(
        (
            vol
            for vol in ("1kg", "500g", "250g", "60 caps", "90 caps", "120 caps", "30 caps")
            if vol in name
        ),
        None,
    )
    return LineItemHeuristic("Supplements", "01-health", p_type, volume)


def classify_line_item(item_name: str) -> LineItemHeuristic:
    """Heuristically infer category, domain, product type, and volume for a line item."""
    name = item_name.lower()
    if any(k in name for k in _SKINCARE_KEYWORDS):
        return _classify_skincare_item(name)
    if any(k in name for k in _SUPPLEMENT_KEYWORDS):
        return _classify_supplement_item(name)
    if any(k in name for k in _CLOTHING_KEYWORDS):
        return LineItemHeuristic("Clothing", "general", "clothing", None)
    if any(k in name for k in _TECH_KEYWORDS):
        return LineItemHeuristic("Tech", "20-home", "tech", None)
    if any(k in name for k in _HOUSEHOLD_KEYWORDS) or re.search(
        r"\b(?:pan|pans|frypan|saucepan)\b", name
    ):
        return LineItemHeuristic("Household", "20-home", "appliance", None)
    if re.search(r"\b(?:wine|beer|whisky|whiskey|vodka|gin|cider|champagne|liquor)\b", name):
        return LineItemHeuristic("Alcohol", "general", "alcohol", None)
    if any(k in name for k in _GROCERY_KEYWORDS):
        return LineItemHeuristic("Groceries", "general", "grocery", None)
    return LineItemHeuristic("General", "general", None, None)


def extract_tags_for_line_item(
    item_name: str,
    domain: Optional[str] = None,
    product_type: Optional[str] = None,
) -> tuple[str, ...]:
    """Extract sorted normalized search tags for an itemized product."""
    tags: set[str] = set()
    name = item_name.lower()
    if domain:
        clean_dom = (
            domain.replace("02-", "").replace("01-", "").replace("20-", "").strip().lower()
        )
        if clean_dom and clean_dom != "general":
            tags.add(clean_dom)

    if product_type:
        tags.add(product_type.replace("_", "-").lower())
        for part in product_type.split("_"):
            if len(part) > 2:
                tags.add(part.lower())

    for kw, tag in _TAG_KEYWORD_MAP:
        if kw in name:
            tags.add(tag)

    return tuple(sorted(tags))


def _resolve_product_tags(item_name: str, domain: Optional[str]) -> tuple[str, ...]:
    heuristic = classify_line_item(item_name)
    return extract_tags_for_line_item(
        item_name,
        heuristic.domain or domain,
        heuristic.product_type,
    )


def _match_existing_tags_heuristic(
    item_name: str,
    brand: Optional[str],
    existing_tags: Sequence[str],
) -> list[str]:
    if not existing_tags:
        return []
    combined = f"{brand or ''} {item_name}".lower()
    norm_text = re.sub(r"[^a-z0-9]+", " ", combined).strip()
    if not norm_text:
        return []
    matched: list[str] = []
    for tag in existing_tags:
        phrase = re.sub(r"[^a-z0-9]+", " ", tag.lower()).strip()
        if len(phrase) >= 4 and re.search(rf"\b{re.escape(phrase)}\b", norm_text):
            matched.append(tag)
    return matched


def _finalize_receipt_item_tags(
    *,
    item_name: str,
    resolved_cat: str,
    resolved_dom: Optional[str],
    resolved_ptype: Optional[str],
    base_explicit_tags: list[str],
    jev_tags: list[str],
    matched_existing: list[str],
) -> tuple[str, ...]:
    combined_tags = [
        t
        for t in normalize_tags([*base_explicit_tags, *jev_tags, *matched_existing])
        if t != "general"
    ]
    non_cat_tags = strip_redundant_tags(combined_tags, resolved_cat)
    if non_cat_tags:
        return tuple(non_cat_tags)
    if combined_tags:
        return tuple(combined_tags)
    return extract_tags_for_line_item(item_name, resolved_dom, resolved_ptype)


def _load_receipt_rule_context(
    db: Session,
) -> tuple[list[CategorizationRule], list[str], list[str]]:
    active_rules = list(
        db.scalars(
            select(CategorizationRule)
            .where(CategorizationRule.is_active == True)
            .order_by(CategorizationRule.priority.asc(), CategorizationRule.id.asc())
        ).all()
    )
    return active_rules, collect_existing_tags(db), get_extra_jev_categories(db)


def _classify_draft_with_context(
    *,
    draft: ReceiptItemDraft,
    merchant: str,
    active_rules: list[CategorizationRule],
    existing_tags: Sequence[str],
    extra_categories: Sequence[str],
    gateway: AiGateway | None = None,
) -> tuple[ItemClassification, bool]:
    heuristic = classify_line_item(draft.item_name)
    resolved_dom = draft.explicit_domain or heuristic.domain
    resolved_ptype = draft.explicit_product_type or heuristic.product_type
    resolved_vol = draft.explicit_volume_ml or heuristic.volume_ml
    base_explicit_tags = normalize_tags(draft.explicit_tags)

    matched_rule = match_receipt_item_rule(
        draft.item_name, draft.brand, draft.total_price, active_rules
    )
    if matched_rule is not None:
        matched_rule.match_count = int(matched_rule.match_count or 0) + 1
        rule_tags = parse_rule_tags(matched_rule.target_tags)
        combined = normalize_tags([*base_explicit_tags, *rule_tags])
        tags = tuple(combined) if combined else extract_tags_for_line_item(
            draft.item_name, resolved_dom, resolved_ptype
        )
        return (
            ItemClassification(
                category=matched_rule.target_category,
                domain=resolved_dom,
                product_type=resolved_ptype,
                volume_ml=resolved_vol,
                tags=tags,
            ),
            True,
        )

    raw_cat = (draft.explicit_category or "").strip()
    resolved_cat = (
        raw_cat if (raw_cat and raw_cat.lower() != "general") else heuristic.category
    )
    matched_existing = _match_existing_tags_heuristic(
        draft.item_name, draft.brand, existing_tags
    )
    jev_tags: list[str] = []
    if float(draft.total_price) >= 0 and (resolved_cat == "General" or not base_explicit_tags):
        brand_prefix = draft.brand.strip() if draft.brand else ""
        jev_label = f"{brand_prefix} {draft.item_name}".strip() if brand_prefix else draft.item_name
        jev_cat, _, jev_tags = classify_transaction_jev_with_tags(
            jev_label,
            -abs(float(draft.total_price)),
            notes=f"Receipt merchant: {merchant}",
            existing_tags=list(existing_tags),
            extra_categories=list(extra_categories),
            gateway=gateway,
        )
        if resolved_cat == "General" and jev_cat and jev_cat != "General":
            resolved_cat = jev_cat

    final_tags = _finalize_receipt_item_tags(
        item_name=draft.item_name,
        resolved_cat=resolved_cat,
        resolved_dom=resolved_dom,
        resolved_ptype=resolved_ptype,
        base_explicit_tags=base_explicit_tags,
        jev_tags=jev_tags,
        matched_existing=matched_existing,
    )
    return (
        ItemClassification(
            category=resolved_cat,
            domain=resolved_dom,
            product_type=resolved_ptype,
            volume_ml=resolved_vol,
            tags=final_tags,
        ),
        False,
    )


def classify_receipt_item_draft(
    db: Session,
    *,
    draft: ReceiptItemDraft,
    merchant: str,
    gateway: AiGateway | None = None,
) -> ItemClassification:
    """Resolve category, domain, product metadata, and tags for one receipt item draft."""
    active_rules, existing_tags, extra_categories = _load_receipt_rule_context(db)
    classification, rule_matched = _classify_draft_with_context(
        draft=draft,
        merchant=merchant,
        active_rules=active_rules,
        existing_tags=existing_tags,
        extra_categories=extra_categories,
        gateway=gateway,
    )
    if rule_matched:
        db.flush()
    return classification


def _to_receipt_item_snapshot(
    row: ReceiptItem,
    tags: tuple[str, ...],
) -> ReceiptItemSnapshot:
    return ReceiptItemSnapshot(
        id=row.id,
        receipt_id=row.receipt_id,
        product_id=row.product_id,
        item_name=row.item_name,
        brand=row.brand,
        quantity=float(row.quantity),
        unit_price=float(row.unit_price),
        discount_amount=float(row.discount_amount) if row.discount_amount is not None else None,
        total_price=float(row.total_price),
        category=row.category,
        domain=row.domain,
        product_type=row.product_type,
        tags=tags,
        volume_ml=row.volume_ml,
        notes=row.notes,
        inherited_tags=tuple(row.inherited_tags),
        created_at=row.created_at,
    )


def persist_classified_receipt_items(
    db: Session,
    *,
    receipt_id: int,
    merchant: str,
    drafts: Sequence[ReceiptItemDraft],
    gateway: AiGateway | None = None,
) -> tuple[ReceiptItemSnapshot, ...]:
    """Classify, link products, persist receipt items in one batch, and return frozen snapshots."""
    if db.get(Receipt, receipt_id) is None:
        raise ReceiptNotFoundError(f"Receipt {receipt_id} not found")
    if not drafts:
        return ()

    active_rules, existing_tags, extra_categories = _load_receipt_rule_context(db)
    created: list[tuple[ReceiptItem, tuple[str, ...]]] = []

    for draft in drafts:
        clean_name = (draft.item_name.strip() or "Item")[:200]
        clean_brand = draft.brand.strip()[:100] if draft.brand and draft.brand.strip() else None
        normalized_draft = replace(draft, item_name=clean_name, brand=clean_brand)
        classification, _ = _classify_draft_with_context(
            draft=normalized_draft,
            merchant=merchant,
            active_rules=active_rules,
            existing_tags=existing_tags,
            extra_categories=extra_categories,
            gateway=gateway,
        )

        prod_id: Optional[int] = None
        if float(draft.total_price) >= 0:
            prod = get_or_create_product(
                db=db,
                item_name=clean_name,
                brand=clean_brand,
                category=classification.category,
                domain=classification.domain,
                tags=classification.tags,
                volume_ml=classification.volume_ml,
                tag_resolver=_resolve_product_tags,
            )
            prod_id = prod.id

        row = ReceiptItem(
            receipt_id=receipt_id,
            product_id=prod_id,
            item_name=clean_name,
            brand=clean_brand,
            quantity=float(draft.quantity),
            unit_price=float(draft.unit_price),
            discount_amount=draft.discount_amount,
            total_price=float(draft.total_price),
            category=(classification.category or "General")[:50],
            domain=classification.domain[:50] if classification.domain else None,
            product_type=(
                classification.product_type[:50] if classification.product_type else None
            ),
            tags=json.dumps(list(classification.tags)),
            volume_ml=classification.volume_ml[:50] if classification.volume_ml else None,
            notes=draft.notes,
        )
        db.add(row)
        created.append((row, classification.tags))

    db.flush()
    return tuple(_to_receipt_item_snapshot(row, tags) for row, tags in created)


def _match_discount_target_heuristic(
    discount_name: str,
    positive_items: Sequence[ReceiptItemDraft],
) -> Optional[int]:
    d_tokens = {
        tok
        for tok in "".join(c if c.isalnum() else " " for c in discount_name.lower()).split()
        if len(tok) >= 3 and tok not in _DISCOUNT_STOPWORDS
    }
    if not d_tokens:
        return None

    best_idx: Optional[int] = None
    best_score = 0
    for idx, item in enumerate(positive_items):
        i_tokens = {
            tok
            for tok in "".join(c if c.isalnum() else " " for c in item.item_name.lower()).split()
            if len(tok) >= 3
        }
        overlap = len(d_tokens & i_tokens)
        if overlap > best_score:
            best_score = overlap
            best_idx = idx
    return best_idx if best_score > 0 else None


def _parse_jev_discount_target(
    content: Optional[str],
    item_count: int,
) -> tuple[bool, Optional[int]]:
    if not content:
        return False, None
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return False, None
    if not isinstance(parsed, dict):
        return False, None
    raw_idx = str(parsed.get("target_index", "none")).strip()
    if raw_idx == "none":
        return True, None
    if raw_idx.isdigit():
        idx = int(raw_idx)
        if 0 <= idx < item_count:
            return True, idx
    return False, None


def _match_discount_target_jev(
    discount: ReceiptItemDraft,
    positive_items: Sequence[ReceiptItemDraft],
    *,
    gateway: AiGateway | None = None,
) -> Optional[int]:
    choices = {str(i): it.item_name for i, it in enumerate(positive_items)}
    choices["none"] = "General basket discount or no matching item"
    schema = {
        "questions": {
            "target_index": {
                "type": "choice",
                "criteria": choices,
            }
        }
    }
    system_prompt = (
        "Match the receipt discount line to the purchased item it discounts:\n"
        f"```json\n{json.dumps(schema)}\n```"
    )
    candidates_desc = ", ".join(
        f"[{i}] {it.item_name} (${float(it.total_price):.2f})"
        for i, it in enumerate(positive_items)
    )
    user_content = (
        f"Discount: {discount.item_name} ({float(discount.total_price):.2f})\n"
        f"Items: {candidates_desc}"
    )
    content, _ = call_jev(
        system_prompt,
        user_content,
        response_format={"type": "json_object"},
        gateway=gateway,
    )
    handled, target_idx = _parse_jev_discount_target(content, len(positive_items))
    if handled:
        return target_idx
    return _match_discount_target_heuristic(discount.item_name, positive_items)


def apply_receipt_discounts_with_jev(
    drafts: Sequence[ReceiptItemDraft],
    *,
    gateway: AiGateway | None = None,
) -> tuple[ReceiptItemDraft, ...]:
    """Fold negative discount lines into matching purchased drafts via Jev without mutation."""
    positive_items = [d for d in drafts if float(d.total_price or 0.0) >= 0]
    discount_items = [d for d in drafts if float(d.total_price or 0.0) < 0]

    if not discount_items or not positive_items:
        return tuple(drafts)

    unmatched_discounts: list[ReceiptItemDraft] = []
    for disc in discount_items:
        disc_total = float(disc.total_price or 0.0)
        target_idx = _match_discount_target_jev(disc, positive_items, gateway=gateway)
        if target_idx is not None:
            target = positive_items[target_idx]
            new_total = round(float(target.total_price or 0.0) + disc_total, 2)
            if new_total >= 0:
                existing_disc = float(target.discount_amount or 0.0)
                disc_note = (
                    f"Includes {disc_total:.2f} discount ({disc.item_name or 'Discount'})"
                )
                existing_note = (target.notes or "").strip()
                combined_note = f"{existing_note}; {disc_note}" if existing_note else disc_note
                positive_items[target_idx] = replace(
                    target,
                    discount_amount=round(existing_disc + abs(disc_total), 2),
                    total_price=new_total,
                    notes=combined_note,
                )
                continue
        unmatched_discounts.append(disc)

    return tuple(positive_items + unmatched_discounts)


def _draft_from_mapping(item: Mapping[str, Any]) -> ReceiptItemDraft:
    raw_tags = item.get("tags")
    explicit_tags = (
        tuple(str(tag) for tag in raw_tags) if isinstance(raw_tags, (list, tuple)) else ()
    )
    raw_discount = item.get("discount_amount")
    return ReceiptItemDraft(
        item_name=str(item.get("item_name") or "Item"),
        brand=str(item["brand"]) if item.get("brand") is not None else None,
        quantity=float(item.get("quantity", 1.0) or 1.0),
        unit_price=float(item.get("unit_price", 0.0) or 0.0),
        discount_amount=float(raw_discount) if raw_discount is not None else None,
        total_price=float(item.get("total_price", 0.0) or 0.0),
        explicit_category=str(item["category"]) if item.get("category") is not None else None,
        explicit_domain=str(item["domain"]) if item.get("domain") is not None else None,
        explicit_product_type=(
            str(item["product_type"]) if item.get("product_type") is not None else None
        ),
        explicit_volume_ml=str(item["volume_ml"]) if item.get("volume_ml") is not None else None,
        explicit_tags=explicit_tags,
        notes=str(item["notes"]) if item.get("notes") is not None else None,
    )


def drafts_from_parsed_items(raw_items: Sequence[Any]) -> tuple[ReceiptItemDraft, ...]:
    """Convert raw parsed LLM line-item dicts or drafts into immutable ReceiptItemDraft tuples."""
    drafts: list[ReceiptItemDraft] = []
    for item in raw_items:
        if isinstance(item, ReceiptItemDraft):
            drafts.append(item)
        elif isinstance(item, Mapping):
            drafts.append(_draft_from_mapping(item))
    return tuple(drafts)
