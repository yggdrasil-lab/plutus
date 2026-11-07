"""Service for receipt parsing, line item extraction, cross-domain tagging, and reconciliation."""

from datetime import date, datetime, timedelta
import json
import logging
from typing import Any, Optional
import urllib.request

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Product, Receipt, ReceiptItem, Transaction
from app.schemas import PricePoint, ProductRead, ProductStatisticItem, ProductStatisticsResponse

logger = logging.getLogger(__name__)


def classify_line_item(item_name: str) -> tuple[str, Optional[str], Optional[str], Optional[str]]:
    """Heuristic domain, category, product_type, and volume detection for a line item.

    Returns:
        (category, domain, product_type, volume_ml)
    """
    name = item_name.lower()

    # 1. Skincare (Area 02-Skin)
    if any(k in name for k in ["serum", "vitamin c", "retinol", "cleanser", "moisturizer", "spf", "sunscreen", "hyaluronic", "niacinamide", "toner", "exfoliant", "adapalene"]):
        domain = "02-skin"
        category = "Skincare"
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

        volume = None
        for vol in ["30ml", "50ml", "100ml", "150ml", "200ml", "15ml"]:
            if vol in name:
                volume = vol
                break
        return category, domain, p_type, volume

    # 2. Health & Supplements (Area 01-Health / Eir)
    if any(k in name for k in ["protein", "whey", "creatine", "collagen", "magnesium", "zinc", "omega 3", "fish oil", "vitamin d", "multivitamin", "electrolytes"]):
        domain = "01-health"
        category = "Supplements"
        if "creatine" in name:
            p_type = "creatine"
        elif "protein" in name or "whey" in name:
            p_type = "protein_powder"
        elif "magnesium" in name:
            p_type = "magnesium"
        else:
            p_type = "supplement"

        volume = None
        for vol in ["1kg", "500g", "250g", "60 caps", "90 caps", "120 caps", "30 caps"]:
            if vol in name:
                volume = vol
                break
        return category, domain, p_type, volume

    # 3. Home & Tech (Area 20-Home)
    if any(k in name for k in ["cable", "charger", "adapter", "bulb", "filter", "vacuum", "pan", "appliance"]):
        return "Household", "20-home", "appliance", None

    # 4. Groceries
    if any(k in name for k in ["milk", "bread", "eggs", "apple", "banana", "chicken", "beef", "rice", "coffee", "oats"]):
        return "Groceries", "general", "grocery", None

    return "General", "general", None, None


def extract_tags_for_line_item(
    item_name: str,
    domain: Optional[str] = None,
    product_type: Optional[str] = None,
) -> list[str]:
    """Extracts normalized search tags for an itemized product."""
    tags: set[str] = set()
    name = item_name.lower()

    if domain:
        clean_dom = domain.replace("02-", "").replace("01-", "").replace("20-", "").strip().lower()
        if clean_dom:
            tags.add(clean_dom)

    if product_type:
        tags.add(product_type.replace("_", "-").lower())
        for part in product_type.split("_"):
            if len(part) > 2:
                tags.add(part.lower())

    keyword_map = [
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
    ]

    for kw, tag in keyword_map:
        if kw in name:
            tags.add(tag)

    if not tags:
        tags.add("general")

    return sorted(list(tags))


def get_or_create_product(
    db: Session,
    item_name: str,
    brand: Optional[str] = None,
    category: Optional[str] = None,
    domain: Optional[str] = None,
    tags: Optional[list[str]] = None,
    volume_ml: Optional[str] = None,
) -> Product:
    """Finds an existing catalog product by name or creates a new one, merging tags and metadata."""
    canonical = item_name.strip()
    stmt = select(Product).where(func.lower(Product.name) == canonical.lower())
    product = db.scalars(stmt).first()

    tags_list = tags or []
    if not tags_list:
        _, auto_dom, auto_ptype, _ = classify_line_item(canonical)
        tags_list = extract_tags_for_line_item(canonical, auto_dom or domain, auto_ptype)

    if product:
        existing_tags = []
        if product.tags:
            try:
                existing_tags = json.loads(product.tags)
            except Exception:
                existing_tags = [t.strip() for t in product.tags.split(",") if t.strip()]
        combined_tags = sorted(list(set(existing_tags + tags_list)))
        product.tags = json.dumps(combined_tags)
        if not product.brand and brand:
            product.brand = brand.strip()
        if not product.volume_ml and volume_ml:
            product.volume_ml = volume_ml.strip()
        db.flush()
        return product

    new_prod = Product(
        name=canonical,
        brand=brand.strip() if brand else None,
        category=category or "General",
        domain=domain,
        tags=json.dumps(tags_list),
        volume_ml=volume_ml.strip() if volume_ml else None,
    )
    db.add(new_prod)
    db.flush()
    return new_prod


def build_product_read(db: Session, product: Product) -> ProductRead:
    """Computes price telemetry, price change history, and usage duration for a Product."""
    stmt = (
        select(ReceiptItem)
        .join(Receipt)
        .where(ReceiptItem.product_id == product.id)
        .order_by(Receipt.receipt_date.asc(), ReceiptItem.id.asc())
    )
    items = db.scalars(stmt).all()

    # Fallback name matching if items were uploaded before explicit linking
    if not items:
        stmt_fallback = (
            select(ReceiptItem)
            .join(Receipt)
            .where(func.lower(ReceiptItem.item_name) == product.name.lower())
            .order_by(Receipt.receipt_date.asc(), ReceiptItem.id.asc())
        )
        items = db.scalars(stmt_fallback).all()
        for it in items:
            it.product_id = product.id
        if items:
            db.flush()

    price_history: list[PricePoint] = []
    unit_prices: list[float] = []
    total_spent = 0.0

    for it in items:
        u_price = float(it.unit_price)
        t_price = float(it.total_price)
        unit_prices.append(u_price)
        total_spent += t_price
        price_history.append(
            PricePoint(
                date=it.receipt.receipt_date,
                merchant=it.receipt.merchant,
                unit_price=u_price,
                total_price=t_price,
                quantity=float(it.quantity),
                receipt_id=it.receipt_id,
                receipt_item_id=it.id,
            )
        )

    purchase_count = len(items)
    latest_price = unit_prices[-1] if unit_prices else None
    lowest_price = min(unit_prices) if unit_prices else None
    highest_price = max(unit_prices) if unit_prices else None
    average_price = round(sum(unit_prices) / len(unit_prices), 2) if unit_prices else None
    price_change_latest = round(unit_prices[-1] - unit_prices[-2], 2) if len(unit_prices) >= 2 else None

    first_purchased = items[0].receipt.receipt_date if items else None
    last_purchased = items[-1].receipt.receipt_date if items else None

    avg_days: Optional[float] = None
    depletion_date: Optional[date] = None
    if purchase_count > 1 and first_purchased and last_purchased:
        total_days = (last_purchased - first_purchased).days
        avg_days = round(total_days / (purchase_count - 1), 1)
        if avg_days > 0:
            depletion_date = last_purchased + timedelta(days=int(avg_days))

    tags_list: list[str] = []
    if product.tags:
        try:
            tags_list = json.loads(product.tags)
        except Exception:
            tags_list = [t.strip() for t in product.tags.split(",") if t.strip()]

    return ProductRead(
        id=product.id,
        name=product.name,
        brand=product.brand,
        category=product.category,
        domain=product.domain,
        tags=tags_list,
        volume_ml=product.volume_ml,
        notes=product.notes,
        rating=product.rating,
        is_favorite=product.is_favorite,
        purchase_count=purchase_count,
        total_spent=round(total_spent, 2),
        latest_price=latest_price,
        lowest_price=lowest_price,
        highest_price=highest_price,
        average_price=average_price,
        price_change_latest=price_change_latest,
        first_purchased=first_purchased,
        last_purchased=last_purchased,
        avg_days_between_purchases=avg_days,
        estimated_depletion_date=depletion_date,
        price_history=price_history,
        created_at=product.created_at,
        updated_at=product.updated_at,
    )


def parse_receipt_text_with_llm(raw_text: str) -> Optional[dict[str, Any]]:
    """Invokes Jev / Muninn LLM to extract structured line items from raw receipt text."""
    if not settings.llm_api_key and not settings.llm_base_url:
        return None

    prompt = f"""You are a specialized receipt parsing engine.
Extract the merchant name, receipt date (YYYY-MM-DD), total amount, tax amount (if any), and itemized line items from this receipt text:

{raw_text}

Respond STRICTLY with valid JSON following this exact schema:
{{
  "merchant": "Merchant Name",
  "receipt_date": "YYYY-MM-DD",
  "total_amount": 0.00,
  "tax_amount": 0.00,
  "items": [
    {{
      "item_name": "Product Name",
      "brand": "Brand or null",
      "quantity": 1.0,
      "unit_price": 0.00,
      "total_price": 0.00,
      "category": "Skincare | Supplements | Groceries | Household | General",
      "domain": "02-skin | 01-health | 20-home | general",
      "product_type": "serum | cleanser | sunscreen | protein | creatine | null",
      "volume_ml": "e.g. 30ml, 1kg, 60 caps or null"
    }}
  ]
}}"""

    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.llm_api_key or 'no-key'}",
        "User-Agent": "Yggdrasil-Plutus/1.0",
    }
    payload = {
        "model": settings.llm_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            content = res_data["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            return json.loads(content)
    except Exception as e:
        logger.warning(f"LLM receipt parsing failed: {e}; falling back to deterministic extraction")
        return None


def match_receipt_to_transaction(receipt: Receipt, db: Session) -> Optional[Transaction]:
    """Finds a matching transaction for a receipt based on amount, date proximity (+/- 3 days), and merchant similarity."""
    target_amt = float(receipt.total_amount)
    r_date = receipt.receipt_date
    date_min = r_date - timedelta(days=3)
    date_max = r_date + timedelta(days=3)

    # Search transactions with negative amount matching receipt total
    stmt = (
        select(Transaction)
        .where(Transaction.transaction_date >= date_min)
        .where(Transaction.transaction_date <= date_max)
    )
    txs = db.scalars(stmt).all()

    best_match: Optional[Transaction] = None
    for tx in txs:
        tx_amt = abs(float(tx.amount))
        # Amount match within 5 cents (handles rounding / small tax variations)
        if abs(tx_amt - target_amt) <= 0.05:
            # Check merchant similarity
            r_merch = receipt.merchant.strip().lower()
            tx_merch = tx.merchant.strip().lower()
            tx_desc = (tx.raw_description or "").strip().lower()

            if (
                r_merch in tx_merch
                or tx_merch in r_merch
                or r_merch in tx_desc
                or any(w in tx_merch for w in r_merch.split() if len(w) > 3)
            ):
                best_match = tx
                break
            elif not best_match:
                best_match = tx

    if best_match:
        receipt.transaction_id = best_match.id
        receipt.status = "matched"
        db.commit()
        db.refresh(receipt)
        return best_match

    return None


def reconcile_unlinked_receipts(db: Session) -> int:
    """Evaluates all unlinked receipts and attempts to match them against current transactions."""
    unlinked = db.scalars(
        select(Receipt).where(Receipt.transaction_id.is_(None))
    ).all()

    matched_count = 0
    for receipt in unlinked:
        if match_receipt_to_transaction(receipt, db):
            matched_count += 1

    return matched_count


def compute_product_statistics(
    db: Session,
    domain: Optional[str] = None,
    product_type: Optional[str] = None,
) -> ProductStatisticsResponse:
    """Aggregates product purchase history and usage duration across receipt line items."""
    stmt = select(ReceiptItem).join(Receipt)
    if domain:
        stmt = stmt.where(ReceiptItem.domain == domain)
    if product_type:
        stmt = stmt.where(ReceiptItem.product_type == product_type)

    items = db.scalars(stmt.order_by(Receipt.receipt_date.asc())).all()

    # Group by canonical product identifier: (item_name.lower().strip())
    products_map: dict[str, list[tuple[ReceiptItem, date]]] = {}
    total_spend = 0.0

    for item in items:
        key = item.item_name.strip()
        r_date = item.receipt.receipt_date
        products_map.setdefault(key, []).append((item, r_date))
        total_spend += float(item.total_price)

    stats_list: list[ProductStatisticItem] = []
    today = date.today()

    for item_name, occurrences in products_map.items():
        occurrences.sort(key=lambda x: x[1])
        first_item, first_date = occurrences[0]
        latest_item, last_date = occurrences[-1]

        purchase_count = len(occurrences)
        prod_spend = sum(float(it.total_price) for it, _ in occurrences)
        latest_unit_price = float(latest_item.unit_price)

        avg_days: Optional[float] = None
        depletion_date: Optional[date] = None

        if purchase_count > 1:
            total_days = (last_date - first_date).days
            avg_days = round(total_days / (purchase_count - 1), 1)
            if avg_days > 0:
                depletion_date = last_date + timedelta(days=int(avg_days))

        stats_list.append(
            ProductStatisticItem(
                item_name=item_name,
                brand=latest_item.brand,
                category=latest_item.category,
                domain=latest_item.domain,
                product_type=latest_item.product_type,
                volume_ml=latest_item.volume_ml,
                purchase_count=purchase_count,
                total_spent=prod_spend,
                unit_price_latest=latest_unit_price,
                first_purchased=first_date,
                last_purchased=last_date,
                avg_days_between_purchases=avg_days,
                estimated_depletion_date=depletion_date,
            )
        )

    # Sort by total spent descending
    stats_list.sort(key=lambda x: x.total_spent, reverse=True)

    return ProductStatisticsResponse(
        domain=domain,
        total_products=len(stats_list),
        total_spend=total_spend,
        products=stats_list,
    )
