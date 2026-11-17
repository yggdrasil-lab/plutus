"""Akahu Personal App connector for New Zealand bank accounts and transactions."""

# ============================================================================
# 1. IMPORTS & CONSTANTS
# ============================================================================
from datetime import date, datetime, timedelta, timezone
import re
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.schemas import SyncResult
from app.services.connectors import ConnectorConfigError, ConnectorUpstreamError
from app.services.ingestion_service import (
    BankFeedGateway,
    ExternalAccountSpec,
    NormalizedTx,
    ProviderAccountFeed,
    resolve_utc_sync_window,
    sync_provider_feeds,
)
from app.services.statement_parser import auto_categorize

AKAHU_BASE_URL = "https://api.akahu.io/v1"

AKAHU_TYPE_MAP: dict[str, tuple[str, str]] = {
    "KIWISAVER": ("kiwisaver", "locked"),
    "INVESTMENT": ("brokerage", "growth"),
    "SHARES": ("brokerage", "growth"),
    "SUPERANNUATION": ("brokerage", "growth"),
    "CREDITCARD": ("credit_card", "cash"),
    "SAVINGS": ("savings", "cash"),
}

CANONICAL_CATEGORIES: frozenset[str] = frozenset(
    {
        "Groceries",
        "Food",
        "Transport",
        "Utilities",
        "Health",
        "Leisure",
        "Insurance",
        "Travel",
        "Housing",
        "Transfers",
        "Income",
        "General",
    }
)

AKAHU_CATEGORY_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("gym", "fitness", "health", "medical", "pharmac", "chemist", "dent", "doctor", "physio", "optom", "clinic", "hospital"), "Health"),
    (("supermarket", "grocer"), "Groceries"),
    (("cafe", "restaurant", "dining", "takeaway", "fast food", "bakery", "bar", "pub", "food"), "Food"),
    (("transport", "fuel", "petrol", "parking", "taxi", "rideshare", "uber", "transit", "toll", "vehicle"), "Transport"),
    (("utilit", "electric", "power", "gas", "water", "internet", "broadband", "mobile", "phone", "telco"), "Utilities"),
    (("entertainment", "leisure", "recreation", "gaming", "streaming", "sport", "hobby", "cinema", "movie"), "Leisure"),
    (("insurance",), "Insurance"),
    (("travel", "airline", "flight", "hotel", "accommodation"), "Travel"),
    (("housing", "rent", "mortgage", "rates", "hardware", "home"), "Housing"),
    (("transfer", "credit card payment"), "Transfers"),
    (("income", "salary", "wage", "interest", "dividend", "refund"), "Income"),
)


AKAHU_TAG_HINTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("gym", "fitness"), ("gym", "subscription")),
    (("aquatic", "pool", "swim"), ("swimming",)),
    (("pharmac", "chemist"), ("pharmacy",)),
    (("dent",), ("dental",)),
    (("physio",), ("physio",)),
    (("optom",), ("optometry",)),
    (("doctor", "medical", "clinic", "hospital"), ("medical",)),
    (("supermarket", "grocer"), ("supermarket",)),
    (("cafe", "coffee"), ("cafe",)),
    (("fast food", "takeaway"), ("fast-food", "takeaway")),
    (("bakery",), ("bakery",)),
    (("restaurant", "dining"), ("dining",)),
    (("bar", "pub"), ("drinks",)),
    (("fuel", "petrol"), ("fuel",)),
    (("parking",), ("parking",)),
    (("uber", "taxi", "rideshare"), ("rideshare",)),
    (("transit", "transport"), ("transit",)),
    (("electric", "power"), ("electricity",)),
    (("internet", "broadband"), ("internet", "subscription")),
    (("mobile", "phone", "telco"), ("mobile", "subscription")),
    (("gaming",), ("gaming",)),
    (("streaming",), ("streaming", "subscription")),
    (("cinema", "movie"), ("movies",)),
    (("sport",), ("sports",)),
    (("airline", "flight"), ("flights",)),
    (("hotel", "accommodation"), ("accommodation",)),
)


def _concise_akahu_tags(lower_category: str, canonical: str) -> tuple[str, ...]:
    """Derive concise, composable atomic tags from Akahu's verbose category string."""
    if not lower_category or lower_category in ("unknown", "other", "general", canonical.lower()):
        return ()
    for keywords, tags in AKAHU_TAG_HINTS:
        if any(kw in lower_category for kw in keywords):
            return tags
    # Fallback to first clause before comma/slash, capped at 24 chars
    head = re.split(r"[,/&]", lower_category, maxsplit=1)[0].strip()
    slug = re.sub(r"[^a-z0-9]+", "-", head).strip("-")[:24]
    if slug and slug != canonical.lower():
        return (slug,)
    return ()


def normalize_akahu_category(
    raw_category: Optional[str],
    merchant: str,
    raw_description: str,
) -> tuple[str, tuple[str, ...]]:
    """Map Akahu's raw category name into Plutus canonical categories plus concise tags."""
    cleaned = (raw_category or "").strip()
    lower = cleaned.lower()

    canonical = ""
    if lower and lower not in ("unknown", "other", "general"):
        for keywords, target in AKAHU_CATEGORY_RULES:
            if any(kw in lower for kw in keywords):
                canonical = target
                break

    if not canonical:
        canonical = auto_categorize(f"{merchant} {raw_description}".strip())

    return canonical, _concise_akahu_tags(lower, canonical)


def normalize_existing_legacy_categories(db: Session) -> int:
    """Normalize any non-canonical transaction categories in-place with tags."""
    import json
    from app.models import Transaction

    rows = (
        db.query(Transaction)
        .filter(Transaction.category.notin_(CANONICAL_CATEGORIES))
        .all()
    )
    if not rows:
        return 0

    for tx in rows:
        canonical, new_tags = normalize_akahu_category(
            tx.category, tx.merchant or "", tx.raw_description or ""
        )
        try:
            existing_tags = json.loads(tx.tags) if tx.tags else []
            if not isinstance(existing_tags, list):
                existing_tags = []
        except (ValueError, TypeError):
            existing_tags = []
        for tag in new_tags:
            if tag not in existing_tags:
                existing_tags.append(tag)
        tx.category = canonical
        tx.tags = json.dumps(existing_tags)

    db.commit()
    return len(rows)


# ============================================================================
# 2. GATEWAY / ADAPTER (All HTTP I/O)
# ============================================================================
class HttpAkahuGateway:
    """HTTP adapter for Akahu Personal App REST API."""

    def __init__(self, app_token: str, user_token: str, timeout: float = 20.0) -> None:
        if not app_token or not user_token:
            raise ConnectorConfigError(
                "Akahu credentials missing: set PLUTUS_AKAHU_APP_TOKEN and PLUTUS_AKAHU_USER_TOKEN."
            )
        self._headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Akahu-Id": app_token,
            "Accept": "application/json",
        }
        self._timeout = timeout

    def fetch_feeds(
        self,
        *,
        days: int = 30,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> list[ProviderAccountFeed]:
        """Fetch raw Akahu accounts and paginated transactions, then normalize."""
        start_dt, end_dt = resolve_utc_sync_window(
            days=days,
            start_date=start_date,
            end_date=end_date,
        )
        with httpx.Client(timeout=self._timeout) as client:
            raw_accounts = self._get_accounts(client)
            raw_transactions = self._get_transactions(client, start_dt=start_dt, end_dt=end_dt)
        return normalize_akahu_feeds(raw_accounts, raw_transactions)

    def _get_accounts(self, client: httpx.Client) -> list[dict]:
        resp = client.get(f"{AKAHU_BASE_URL}/accounts", headers=self._headers)
        if resp.status_code >= 400:
            raise ConnectorUpstreamError(
                f"Akahu /accounts failed ({resp.status_code}): {resp.text[:200]}"
            )
        return resp.json().get("items", [])

    def _get_transactions(
        self,
        client: httpx.Client,
        *,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[dict]:
        base_params = {
            "start": start_dt.isoformat().replace("+00:00", "Z"),
            "end": end_dt.isoformat().replace("+00:00", "Z"),
        }
        items: list[dict] = []
        cursor: Optional[str] = None
        while True:
            params = {**base_params, "cursor": cursor} if cursor else base_params
            resp = client.get(f"{AKAHU_BASE_URL}/transactions", headers=self._headers, params=params)
            if resp.status_code >= 400:
                raise ConnectorUpstreamError(
                    f"Akahu /transactions failed ({resp.status_code}): {resp.text[:200]}"
                )
            payload = resp.json()
            items.extend(payload.get("items", []))
            cursor = (payload.get("cursor") or {}).get("next")
            if not cursor:
                return items


# ============================================================================
# 3. PURE NORMALIZERS (Zero I/O)
# ============================================================================
def _parse_iso_date(value: Optional[str]) -> date:
    """Parse Akahu ISO-8601 UTC timestamp into the configured local date (e.g. Pacific/Auckland)."""
    if not value:
        return datetime.now(ZoneInfo(settings.timezone)).date()
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(ZoneInfo(settings.timezone))
    return dt.date()


def normalize_akahu_feeds(
    raw_accounts: list[dict],
    raw_transactions: list[dict],
) -> list[ProviderAccountFeed]:
    """Transform raw Akahu payloads into immutable ProviderAccountFeed contracts."""
    tx_by_account: dict[str, list[NormalizedTx]] = {acc["_id"]: [] for acc in raw_accounts}
    currency_by_account: dict[str, str] = {}

    specs: list[ExternalAccountSpec] = []
    for item in raw_accounts:
        ext_id = item["_id"]
        acc_type, tier = AKAHU_TYPE_MAP.get((item.get("type") or "").upper(), ("checking", "cash"))
        institution = (item.get("connection") or {}).get("name") or "NZ Bank"
        balance_obj = item.get("balance") or {}
        currency = (balance_obj.get("currency") or "NZD").upper()
        currency_by_account[ext_id] = currency
        raw_bal = balance_obj.get("current")

        specs.append(
            ExternalAccountSpec(
                provider="akahu",
                external_id=ext_id,
                name=item.get("name") or f"{institution} {acc_type.capitalize()}",
                institution=institution,
                account_type=acc_type,
                tier=tier,
                currency=currency,
                current_balance=float(raw_bal) if raw_bal is not None else None,
                snapshot_notes="Synced from Akahu /v1/accounts",
            )
        )

    for tx in raw_transactions:
        ext_acc_id = tx.get("_account")
        if ext_acc_id not in tx_by_account:
            continue
        merchant_obj = tx.get("merchant") or {}
        category_obj = tx.get("category") or {}
        raw_desc = tx.get("description") or ""
        merchant_name = merchant_obj.get("name") or raw_desc or "Unknown"
        canonical_cat, akahu_tags = normalize_akahu_category(
            category_obj.get("name"),
            merchant_name,
            raw_desc,
        )
        tx_by_account[ext_acc_id].append(
            NormalizedTx(
                transaction_date=_parse_iso_date(tx.get("date")),
                amount=float(tx["amount"]),
                currency=currency_by_account[ext_acc_id],
                merchant=merchant_name,
                category=canonical_cat,
                raw_description=raw_desc or None,
                tags=akahu_tags,
                notes=f"akahu:{tx.get('_id', '')}" if tx.get("_id") else None,
            )
        )

    return [
        ProviderAccountFeed(account=spec, transactions=tuple(tx_by_account[spec.external_id]))
        for spec in specs
    ]


# ============================================================================
# 4. ORCHESTRATION
# ============================================================================
def sync_akahu(
    db: Session,
    *,
    days: int = 14,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    gateway: Optional[BankFeedGateway] = None,
) -> SyncResult:
    """Pull Akahu accounts and transactions via injected or HTTP gateway."""
    resolved_gateway = gateway or HttpAkahuGateway(
        app_token=settings.akahu_app_token,
        user_token=settings.akahu_user_token,
    )
    feeds = resolved_gateway.fetch_feeds(
        days=days,
        start_date=start_date,
        end_date=end_date,
    )
    return sync_provider_feeds(db, provider="akahu", feeds=feeds)
