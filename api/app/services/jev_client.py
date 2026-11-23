"""TypeSafe Jev System One decision client for Plutus.

Routes fast transaction classification, category gating, and anomaly detection
through the unified AI Gateway port (`AiGateway`).
"""

from __future__ import annotations

from collections.abc import Mapping
import copy
import json
import logging
import re
from types import MappingProxyType
from typing import Any, Optional, Tuple

from sqlalchemy.orm import Session

from app.services.ai_gateway import (
    AiCircuitOpenError,
    AiGateway,
    AiGatewayError,
    JevGateRequest,
    get_default_ai_gateway,
)
from app.services.category_taxonomy import (
    DEFAULT_CATEGORY_NAMES,
    get_canonical_category_names,
)

logger = logging.getLogger(__name__)

USER_AGENT = "Yggdrasil-Plutus/1.0 (PlutusApi)"

VALID_TYPESAFE_QUESTION_TYPES: frozenset[str] = frozenset({"choice", "noul", "score", "box"})
MAX_TYPESAFE_CHOICE_OPTIONS = 255
_MAX_PRIMARY_TAG_VOCAB = 250
_EXCLUDED_EXISTING_TAG_VOCAB: frozenset[str] = frozenset({"general", "none", "subscription"})
_TRUTHY_JEV_VALUES = (True, "true", "True", 1)

_DEFAULT_CATEGORY_CRITERIA: Mapping[str, str] = MappingProxyType(
    {
        "Food": "General food spending when dining vs groceries vs alcohol cannot be distinguished.",
        "Dining": "Dining out, cafes, takeaways, coffee, restaurants, fast food, vending (e.g. McDonalds, cafes, sushi).",
        "Groceries": "Supermarkets, food stores, weekly pantry shopping (e.g. Pak n Save, Woolworths, Countdown, New World, Costco).",
        "Alcohol": "Bottle shops, liquor stores, wineries, beer/wine/spirits purchases (e.g. Super Liquor, Liquorland).",
        "Housing": "General housing costs when rent vs household supplies cannot be distinguished.",
        "Rent": "Recurring residential rent or board payments.",
        "Household": "Home supplies, appliances, furniture, cleaning, cookware, lighting, hardware (e.g. Bunnings, Briscoes, Mitre 10, IKEA).",
        "Transport": "General transport when fuel vs transit vs vehicle maintenance cannot be distinguished.",
        "Fuel": "Petrol, diesel, EV charging at service stations (e.g. BP fuel, Z Energy, Mobil, Caltex).",
        "Transit": "Public transport, AT Hop, ferries, buses, rideshare, Uber, parking, tolls.",
        "Vehicle": "Car dealers, mechanics, servicing, WOF, rego, tyres, batteries, auto glass, car rentals, traffic fines (e.g. Lexus, Keightley Motors, VTNZ, NZTA).",
        "Health": "General health expenses when fitness vs medical vs personal care cannot be distinguished.",
        "Fitness": "Gyms, swimming pools, sports clubs, badminton, workout memberships (e.g. CityFitness, Tepid Baths, NZBA).",
        "Medical": "Doctors, dentists, clinics, physio, pharmacies, prescriptions (e.g. Chemist Warehouse, Bargain Chemist, Dental).",
        "Supplements": "Protein powder, creatine, vitamins, minerals, electrolytes, nutritional supplements.",
        "Personal Care": "Skincare, cosmetics, haircuts, barber, salon, grooming, fragrance (e.g. Sharing Shed, Mecca, Sephora).",
        "Shopping": "Online marketplaces, department stores, general retail (e.g. Amazon, Temu, Kmart, AliExpress).",
        "Clothing": "Apparel, shoes, footwear, underwear, socks, fashion accessories (e.g. Barkers, Calvin Klein, Uniqlo, Nike).",
        "Tech": "Electronics, computers, cameras, gadgets, hardware (e.g. PB Tech, Insta360, NuPhy, Apple Store).",
        "Utilities": "General utilities when telco vs software/AI cannot be distinguished.",
        "Bills & Telco": "Electricity, mobile, internet, broadband, water, gas bills (e.g. Skinny, Simba, Spark, 2degrees, Mercury, Watercare).",
        "Software & AI": "Cloud storage, AI subscriptions, SaaS, VPNs, domains (e.g. Google One, iCloud, OpenAI, DeepSeek, Cloudflare).",
        "Insurance": "Insurance premiums and claims (e.g. AA Insurance, Aioi Nissay Dowa, Southern Cross, AIA).",
        "Leisure": "Entertainment, streaming, bars, movies, recreation, attractions (e.g. Netflix, Spotify, cinemas).",
        "Gaming": "Video games and gaming platforms (e.g. Steam, PlayStation, Nintendo).",
        "Travel": "Airlines, flights, hotels, overseas accommodations, travel visas (e.g. Air NZ, Singapore Airlines, Jetstar).",
        "Family": "Family support, allowances, gifts, personal remittances to relatives.",
        "Finance": "General financial administration.",
        "Fees & Admin": "Bank fees, credit card annual fees, service charges, immigration/government fees.",
        "Debt & Loans": "Loan repayments and credit line installment plans.",
        "General": "Miscellaneous uncategorized expenditure that does not fit any specific category.",
        "Transfers": "Money moving between own bank accounts, cards, or platforms (e.g. Wise, TransferWise, ASB to ANZ, Sharesies, InvestNow, internal bank transfers).",
        "Income": "Salary, wages, consulting, dividends, interest earnings, cashback rewards.",
    }
)


def _build_base_category_criteria() -> dict[str, str]:
    """Derive Jev's 33 base category criteria from Category Taxonomy default names."""
    expected = set(DEFAULT_CATEGORY_NAMES)
    configured = set(_DEFAULT_CATEGORY_CRITERIA.keys())
    if configured != expected:
        missing = sorted(expected - configured)
        extra = sorted(configured - expected)
        raise RuntimeError(
            f"Jev base category criteria drifted from Category Taxonomy: missing={missing}, extra={extra}"
        )
    return {name: _DEFAULT_CATEGORY_CRITERIA[name] for name in DEFAULT_CATEGORY_NAMES}


def _validate_typesafe_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Enforce TypeSafe System One question types and <= 255 choice bounds."""
    questions = schema.get("questions", {})
    for gate_name, gate in questions.items():
        q_type = gate.get("type")
        if q_type not in VALID_TYPESAFE_QUESTION_TYPES:
            raise ValueError(f"Invalid TypeSafe question type '{q_type}' on gate '{gate_name}'.")
        if q_type == "choice":
            criteria = gate.get("criteria", {})
            if not criteria or len(criteria) > MAX_TYPESAFE_CHOICE_OPTIONS:
                raise ValueError(
                    f"TypeSafe choice gate '{gate_name}' must have 1..{MAX_TYPESAFE_CHOICE_OPTIONS} options."
                )
    return schema


TRANSACTION_GATES_SCHEMA: dict[str, Any] = _validate_typesafe_schema(
    {
        "questions": {
            "category": {
                "type": "choice",
                "criteria": _build_base_category_criteria(),
            },
            "is_anomaly": {
                "type": "noul",
                "instructions": "True if the spend amount is unusually high, erratic, or unexpected for the merchant type; False if it is standard routine spending.",
            },
            "is_subscription": {
                "type": "noul",
                "instructions": "True if the transaction is a recurring subscription, membership, or ongoing plan (e.g. monthly/fortnightly/annual software, mobile, gym, cloud, streaming); False for one-off or variable purchases.",
            },
        }
    }
)

VALID_JEV_CATEGORIES: frozenset[str] = frozenset(DEFAULT_CATEGORY_NAMES)


def is_jev_circuit_open(gateway: AiGateway | None = None) -> bool:
    """Return True when the Jev circuit breaker is currently open."""
    resolved_gateway = gateway or get_default_ai_gateway()
    return resolved_gateway.is_jev_circuit_open()


def reset_jev_circuit(gateway: AiGateway | None = None) -> None:
    """Reset the Jev circuit breaker state."""
    resolved_gateway = gateway or get_default_ai_gateway()
    resolved_gateway.reset_jev_circuit()


def build_transaction_gates_schema(
    existing_tags: list[str] | tuple[str, ...] | None = None,
    extra_categories: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Build a validated TypeSafe System One schema with dynamic categories and tag choices."""
    schema = copy.deepcopy(TRANSACTION_GATES_SCHEMA)
    cat_criteria: dict[str, str] = schema["questions"]["category"]["criteria"]
    _append_extra_category_criteria(cat_criteria, extra_categories)

    clean_tags = _clean_existing_tags(existing_tags, _MAX_PRIMARY_TAG_VOCAB)
    if clean_tags:
        schema["questions"]["primary_tag"] = {
            "type": "choice",
            "instructions": "Select the single best existing tag for this item, or 'none' if none fit.",
            "criteria": {
                **{t: f"Item matches existing tag '{t}'" for t in clean_tags},
                "none": "No existing tag applies",
            },
        }

    return _validate_typesafe_schema(schema)


def get_extra_jev_categories(db: Session) -> list[str]:
    """Return active DB category names not already present in VALID_JEV_CATEGORIES."""
    return [name for name in get_canonical_category_names(db) if name not in VALID_JEV_CATEGORIES]


def call_jev(
    system_prompt: str,
    user_content: str,
    model: str | None = None,
    timeout: float = 4.0,
    response_format: dict | None = None,
    gateway: AiGateway | None = None,
) -> Tuple[Optional[str], float]:
    """Invoke Jev System One via the injected or default AI Gateway.

    Returns:
        (verdict_text, duration_seconds)
    """
    resolved_gateway = gateway or get_default_ai_gateway()
    request = JevGateRequest(
        system_prompt=system_prompt,
        user_content=user_content,
        model=model,
        timeout=timeout,
        response_format=response_format,
        user_agent=USER_AGENT,
    )
    try:
        response = resolved_gateway.call_jev_gate(request)
        return response.content, response.elapsed_seconds
    except AiCircuitOpenError:
        return None, 0.0
    except AiGatewayError as exc:
        logger.warning(
            "Jev invocation failed (%s); falling back to deterministic heuristics.",
            exc,
        )
        return None, 0.0


def classify_transaction_jev_with_tags(
    merchant: str,
    amount: float,
    notes: str | None = None,
    existing_tags: list[str] | tuple[str, ...] | None = None,
    extra_categories: list[str] | tuple[str, ...] | None = None,
    gateway: AiGateway | None = None,
) -> Tuple[str, bool, list[str]]:
    """Evaluate category, anomaly status, and granular tags for a transaction via Jev."""
    if not merchant or not merchant.strip():
        return "General", False, []

    schema = build_transaction_gates_schema(
        existing_tags=existing_tags,
        extra_categories=extra_categories,
    )
    system_prompt = (
        "Categorize expense, detect anomalies, subscription status, and match primary tag:\n"
        f"```json\n{json.dumps(schema)}\n```"
    )
    user_content = _build_classification_user_content(
        merchant=merchant,
        amount=amount,
        notes=notes,
        existing_tags=existing_tags,
        extra_categories=extra_categories,
    )
    valid_cats = set(schema["questions"]["category"]["criteria"].keys())
    if gateway is None:
        content, _ = call_jev(system_prompt, user_content, response_format={"type": "json_object"})
    else:
        content, _ = call_jev(
            system_prompt,
            user_content,
            response_format={"type": "json_object"},
            gateway=gateway,
        )
    if not content:
        return "General", False, []
    return _parse_classification_verdict(content, valid_cats)


def classify_transaction_jev(
    merchant: str,
    amount: float,
    notes: str | None = None,
    gateway: AiGateway | None = None,
) -> Tuple[str, bool]:
    """Evaluate category and anomaly status for a transaction via Jev."""
    cat, is_anomaly, _ = classify_transaction_jev_with_tags(
        merchant,
        amount,
        notes,
        gateway=gateway,
    )
    return cat, is_anomaly


def _append_extra_category_criteria(
    cat_criteria: dict[str, str],
    extra_categories: list[str] | tuple[str, ...] | None,
) -> None:
    for raw_cat in extra_categories or ():
        if raw_cat is None:
            continue
        cat_name = str(raw_cat).strip()
        if not cat_name or cat_name in cat_criteria:
            continue
        if len(cat_criteria) >= MAX_TYPESAFE_CHOICE_OPTIONS:
            break
        cat_criteria[cat_name] = f"Custom user category: {cat_name}."


def _normalize_tag(raw_tag: object) -> str:
    if raw_tag is None:
        return ""
    return re.sub(r"\s+", "-", str(raw_tag).strip().lower().lstrip("#").strip())[:40]


def _clean_existing_tags(
    existing_tags: list[str] | tuple[str, ...] | None,
    limit: int,
) -> list[str]:
    clean_tags: list[str] = []
    seen_tags: set[str] = set()
    for raw_tag in existing_tags or ():
        cleaned = _normalize_tag(raw_tag)
        if not cleaned or cleaned in _EXCLUDED_EXISTING_TAG_VOCAB or cleaned in seen_tags:
            continue
        seen_tags.add(cleaned)
        clean_tags.append(cleaned)
        if len(clean_tags) >= limit:
            break
    return clean_tags


def _build_classification_user_content(
    *,
    merchant: str,
    amount: float,
    notes: str | None,
    existing_tags: list[str] | tuple[str, ...] | None,
    extra_categories: list[str] | tuple[str, ...] | None,
) -> str:
    user_content = f"Merchant: {merchant.strip()}, Amount: ${abs(amount):.2f} NZD"
    if notes and notes.strip():
        user_content += f", Notes: {notes.strip()}"
    if existing_tags:
        clean_existing = _clean_existing_tags(existing_tags, 80)
        if clean_existing:
            user_content += f", Existing tags: {', '.join(clean_existing)}"
    clean_extra = [
        cat_name
        for c in (extra_categories or ())
        if c is not None and (cat_name := str(c).strip())
    ]
    if clean_extra:
        user_content += f", Additional custom categories: {', '.join(clean_extra)}"
    return user_content


def _parse_classification_verdict(
    content: str,
    valid_cats: set[str],
) -> Tuple[str, bool, list[str]]:
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError) as exc:
        logger.warning("Invalid JSON from Jev classification (%s); falling back to General", exc)
        return "General", False, []
    if not isinstance(parsed, dict):
        return "General", False, []
    raw_cat = parsed.get("category", "General")
    resolved_cat = raw_cat if raw_cat in valid_cats else "General"
    is_anomaly = parsed.get("is_anomaly", "false") in _TRUTHY_JEV_VALUES
    tags = _extract_jev_tags(parsed, resolved_cat)
    return resolved_cat, is_anomaly, tags


def _extract_jev_tags(parsed: dict[str, Any], resolved_cat: str) -> list[str]:
    cat_lower = resolved_cat.strip().lower()
    excluded_tags = {"general", "none", cat_lower, re.sub(r"\s+", "-", cat_lower)}
    tags: list[str] = []

    raw_primary = parsed.get("primary_tag")
    if raw_primary is not None:
        primary_clean = _normalize_tag(raw_primary)
        if primary_clean and primary_clean not in excluded_tags:
            tags.append(primary_clean)

    is_sub = parsed.get("is_subscription", "false") in _TRUTHY_JEV_VALUES
    if is_sub and "subscription" not in excluded_tags and "subscription" not in tags:
        tags.append("subscription")

    raw_tags = parsed.get("tags", [])
    if not isinstance(raw_tags, list):
        return tags
    for raw_tag in raw_tags:
        if len(tags) >= 3:
            break
        cleaned = _normalize_tag(raw_tag)
        if cleaned and cleaned not in excluded_tags and cleaned not in tags:
            tags.append(cleaned)
    return tags
