"""TypeSafe Jev System One decision client for Plutus.

Routes fast transaction classification, category gating, and anomaly detection
via Muninn's `jev-decision` model on the Docker Swarm overlay network (http://muninn:4000/v1).
"""

from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.config import settings
from app.services.category_taxonomy import get_canonical_category_names

logger = logging.getLogger(__name__)

USER_AGENT = "Yggdrasil-Plutus/1.0 (PlutusApi)"

CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_COOLDOWN_SECONDS = 60.0
_CIRCUIT_LOCK = threading.Lock()
_CONSECUTIVE_FAILURES = 0
_CIRCUIT_OPEN_UNTIL = 0.0


def is_jev_circuit_open() -> bool:
    """Return True when the Jev circuit breaker is currently open."""
    with _CIRCUIT_LOCK:
        return time.monotonic() < _CIRCUIT_OPEN_UNTIL


def reset_jev_circuit() -> None:
    """Reset the Jev circuit breaker state (used on success and in tests)."""
    global _CONSECUTIVE_FAILURES, _CIRCUIT_OPEN_UNTIL
    with _CIRCUIT_LOCK:
        _CONSECUTIVE_FAILURES = 0
        _CIRCUIT_OPEN_UNTIL = 0.0


def _record_jev_failure() -> None:
    """Record a failed Jev call and open the circuit after consecutive failures."""
    global _CONSECUTIVE_FAILURES, _CIRCUIT_OPEN_UNTIL
    with _CIRCUIT_LOCK:
        _CONSECUTIVE_FAILURES += 1
        if _CONSECUTIVE_FAILURES >= CIRCUIT_FAILURE_THRESHOLD:
            _CIRCUIT_OPEN_UNTIL = time.monotonic() + CIRCUIT_COOLDOWN_SECONDS
            logger.warning(
                "Jev circuit breaker opened for %.0fs after %d consecutive failures.",
                CIRCUIT_COOLDOWN_SECONDS,
                _CONSECUTIVE_FAILURES,
            )

VALID_TYPESAFE_QUESTION_TYPES: frozenset[str] = frozenset({"choice", "noul", "score", "box"})
MAX_TYPESAFE_CHOICE_OPTIONS = 255
_MAX_PRIMARY_TAG_VOCAB = 250
_EXCLUDED_EXISTING_TAG_VOCAB: frozenset[str] = frozenset({"general", "none", "subscription"})

TRANSACTION_GATES_SCHEMA = {
    "questions": {
        "category": {
            "type": "choice",
            "criteria": {
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
                "Personal Care": "Skincare, cosmetics, haircuts, barber, salon, grooming, fragrance (e.g. Sharing Shed, Mecca, Sephora).",
                "Supplements": "Protein powder, creatine, vitamins, minerals, electrolytes, nutritional supplements.",
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
                "Transfers": "Money moving between own bank accounts, cards, or platforms (e.g. Wise, TransferWise, ASB to ANZ, Sharesies, InvestNow, internal bank transfers).",
                "Income": "Salary, wages, consulting, dividends, interest earnings, cashback rewards.",
                "General": "Miscellaneous uncategorized expenditure that does not fit any specific category.",
            },
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

VALID_JEV_CATEGORIES: frozenset[str] = frozenset(
    TRANSACTION_GATES_SCHEMA["questions"]["category"]["criteria"].keys()
)


def _normalize_tag(raw_tag: object) -> str:
    """Normalize a candidate tag to lowercase kebab-case capped at 40 characters."""
    if raw_tag is None:
        return ""
    return re.sub(r"\s+", "-", str(raw_tag).strip().lower().lstrip("#").strip())[:40]


def _clean_existing_tags(
    existing_tags: list[str] | tuple[str, ...] | None,
    limit: int,
) -> list[str]:
    """Clean, deduplicate, and cap existing tags while excluding reserved values."""
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


def build_transaction_gates_schema(
    existing_tags: list[str] | tuple[str, ...] | None = None,
    extra_categories: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """Build a valid TypeSafe System One schema with dynamic categories and tag choices."""
    schema = copy.deepcopy(TRANSACTION_GATES_SCHEMA)
    cat_criteria: dict[str, str] = schema["questions"]["category"]["criteria"]

    for raw_cat in extra_categories or ():
        if raw_cat is None:
            continue
        cat_name = str(raw_cat).strip()
        if not cat_name or cat_name in cat_criteria:
            continue
        if len(cat_criteria) >= MAX_TYPESAFE_CHOICE_OPTIONS:
            break
        cat_criteria[cat_name] = f"Custom user category: {cat_name}."

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

    return schema


def get_extra_jev_categories(db: Session) -> list[str]:
    """Return active DB category names not already present in VALID_JEV_CATEGORIES."""
    return [name for name in get_canonical_category_names(db) if name not in VALID_JEV_CATEGORIES]


def call_jev(
    system_prompt: str,
    user_content: str,
    model: str | None = None,
    timeout: float = 4.0,
    response_format: dict | None = None,
) -> Tuple[Optional[str], float]:
    """Invokes Jev System One via Muninn LiteLLM router.

    Returns:
        (verdict_text, duration_seconds)
    """
    model_name = model or settings.jev_model
    endpoint = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    api_key = settings.llm_api_key

    payload: dict[str, object] = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.0,
    }
    if response_format:
        payload["response_format"] = response_format

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    if is_jev_circuit_open():
        return None, 0.0

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed = time.time() - t0
            if resp.status == 200:
                body = json.loads(resp.read().decode("utf-8"))
                choices = body.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "").strip()
                    reset_jev_circuit()
                    return content, elapsed
    except Exception as exc:
        elapsed = time.time() - t0
        logger.warning("Jev invocation failed (%s) in %.3fs; falling back to deterministic heuristics.", exc, elapsed)
        _record_jev_failure()
        return None, elapsed

    _record_jev_failure()
    return None, time.time() - t0


def classify_transaction_jev_with_tags(
    merchant: str,
    amount: float,
    notes: str | None = None,
    existing_tags: list[str] | tuple[str, ...] | None = None,
    extra_categories: list[str] | tuple[str, ...] | None = None,
) -> Tuple[str, bool, list[str]]:
    """Evaluates category, anomaly status, and granular tags for a transaction or receipt item via Jev."""
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
    abs_amount = abs(amount)
    user_content = f"Merchant: {merchant.strip()}, Amount: ${abs_amount:.2f} NZD"
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

    valid_cats = set(schema["questions"]["category"]["criteria"].keys())
    try:
        content, _ = call_jev(system_prompt, user_content, response_format={"type": "json_object"})
        if content:
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                return "General", False, []
            cat = parsed.get("category", "General")
            resolved_cat = cat if cat in valid_cats else "General"
            raw_anomaly = parsed.get("is_anomaly", "false")
            is_anomaly = raw_anomaly in (True, "true", "True", 1)
            cat_lower = resolved_cat.strip().lower()
            excluded_tags = {"general", "none", cat_lower, re.sub(r"\s+", "-", cat_lower)}

            tags: list[str] = []
            raw_primary = parsed.get("primary_tag")
            if raw_primary is not None:
                primary_clean = _normalize_tag(raw_primary)
                if primary_clean and primary_clean not in excluded_tags:
                    tags.append(primary_clean)

            raw_subscription = parsed.get("is_subscription", "false")
            if (
                raw_subscription in (True, "true", "True", 1)
                and "subscription" not in excluded_tags
                and "subscription" not in tags
            ):
                tags.append("subscription")

            raw_tags = parsed.get("tags", [])
            if isinstance(raw_tags, list) and len(tags) < 3:
                for t in raw_tags:
                    cleaned = _normalize_tag(t)
                    if cleaned and cleaned not in excluded_tags and cleaned not in tags:
                        tags.append(cleaned)
                        if len(tags) >= 3:
                            break
            return resolved_cat, is_anomaly, tags
    except Exception as exc:
        logger.warning("Error in classify_transaction_jev_with_tags (%s); falling back to heuristic", exc)

    return "General", False, []


def classify_transaction_jev(
    merchant: str,
    amount: float,
    notes: str | None = None,
) -> Tuple[str, bool]:
    """Evaluates category and anomaly status for a transaction via Jev.

    Returns:
        (category, is_anomaly)
    """
    cat, is_anomaly, _ = classify_transaction_jev_with_tags(merchant, amount, notes)
    return cat, is_anomaly
