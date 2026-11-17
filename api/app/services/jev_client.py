"""TypeSafe Jev System One decision client for Plutus.

Routes fast transaction classification, category gating, and anomaly detection
via Muninn's `jev-decision` model on the Docker Swarm overlay network (http://muninn:4000/v1).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Optional, Tuple

from app.config import settings

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

TRANSACTION_GATES_SCHEMA = {
    "questions": {
        "category": {
            "type": "choice",
            "criteria": {
                "Groceries": "Supermarkets, food stores, weekly pantry shopping (e.g. Pak n Save, Woolworths, Countdown, New World).",
                "Transport": "Fuel, petrol, transit, parking, rideshare (e.g. BP, Z Energy, Mobil, Caltex, Auckland Transport, AT Hop, Uber).",
                "Food": "Dining out, cafes, takeaways, coffee, restaurants, fast food (e.g. McDonalds, cafes, sushi).",
                "Utilities": "Electricity, mobile, internet, water, cloud or telco bills (e.g. Spark, 2degrees, Mercury, Watercare).",
                "Health": "Medical, dental, pharmacy, gym, fitness, physio (e.g. chemist, doctor, gym memberships).",
                "Leisure": "Entertainment, games, streaming, recreation (e.g. Steam, Netflix, Spotify, cinemas).",
                "Housing": "Rent, mortgage, home insurance, council rates, hardware, maintenance.",
                "Transfers": "Money moving between own bank accounts, cards, or platforms (e.g. Wise, TransferWise, ASB to ANZ, Sharesies, InvestNow, savings transfers, internal bank transfers).",
                "Insurance": "Insurance premiums (e.g. AA Insurance, Southern Cross, State, AMI).",
                "Travel": "Airlines, flights, hotels, overseas accommodations (e.g. Air New Zealand, Qantas, Jetstar).",
                "Income": "Salary, wages, consulting, dividends, interest earnings.",
                "General": "Uncategorized retail, miscellaneous supplies, general expenditure.",
            },
        },
        "is_anomaly": {
            "type": "noul",
            "instructions": "True if the spend amount is unusually high, erratic, or unexpected for the merchant type; False if it is standard routine spending.",
        },
        "tags": {
            "type": "list",
            "instructions": "1 to 3 short atomic composable lowercase tags for cross-category querying (e.g. ['gym', 'subscription'], ['mobile', 'subscription'], ['car'], ['badminton', 'sports']). Never repeat the category name itself as a tag (e.g. never return 'insurance' for Insurance or 'food' for Food).",
        },
    }
}


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
) -> Tuple[str, bool, list[str]]:
    """Evaluates category, anomaly status, and granular tags for a transaction via Jev."""
    if not merchant or not merchant.strip():
        return "General", False, []

    system_prompt = f"Categorize expense, detect anomalies, and assign granular tags:\n```json\n{json.dumps(TRANSACTION_GATES_SCHEMA)}\n```"
    abs_amount = abs(amount)
    user_content = f"Merchant: {merchant.strip()}, Amount: ${abs_amount:.2f} NZD"
    if notes and notes.strip():
        user_content += f", Notes: {notes.strip()}"

    try:
        content, _ = call_jev(system_prompt, user_content, response_format={"type": "json_object"})
        if content:
            parsed = json.loads(content)
            cat = parsed.get("category", "General")
            valid_cats = {
                "Groceries", "Transport", "Food", "Utilities",
                "Health", "Leisure", "Housing", "Income", "Transfers",
                "Insurance", "Travel", "General"
            }
            resolved_cat = cat if cat in valid_cats else "General"
            raw_anomaly = parsed.get("is_anomaly", "false")
            is_anomaly = raw_anomaly in (True, "true", "True", 1)
            raw_tags = parsed.get("tags", [])
            tags: list[str] = []
            if isinstance(raw_tags, list):
                for t in raw_tags[:3]:
                    cleaned = str(t).strip().lower().lstrip("#").replace(" ", "-")
                    if cleaned and cleaned not in tags:
                        tags.append(cleaned[:40])
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
