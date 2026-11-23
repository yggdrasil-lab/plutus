"""Service for receipt parsing, line item extraction, cross-domain tagging, and reconciliation."""

import base64
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import io
import json
import logging
import math
import mimetypes
import re
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.models import CategorizationRule, Product, Receipt, ReceiptItem, Transaction
from app.schemas import (
    InboundEmailDispatchPayload,
    PricePoint,
    ProductRead,
    ProductStatisticItem,
    ProductStatisticsResponse,
    TransactionCandidateRead,
)
from app.services.ai_gateway import (
    AiConfigError,
    AiGateway,
    AiGatewayError,
    AiInvalidResponseError,
    AiJsonResponse,
    AiTextRequest,
    AiVisionRequest,
    get_default_ai_gateway,
)
from app.services.category_taxonomy import get_canonical_category_names
from app.services.connectors import ConnectorUpstreamError
from app.services.email_utils import (
    extract_and_strip_forwarded_header,
    strip_invisible_unicode,
)
from app.services.fx_service import get_exchange_rate
from app.services.jev_client import call_jev, classify_transaction_jev_with_tags, get_extra_jev_categories
from app.services.rules_engine import (
    collect_existing_tags,
    match_receipt_item_rule,
    normalize_tags,
    parse_rule_tags,
    strip_redundant_tags,
)

logger = logging.getLogger(__name__)

MIN_RECEIPT_ATTACHMENT_BYTES = 15 * 1024
MIN_PAYSLIP_PDF_BYTES = 512
_TRACKING_URL_RE = re.compile(r"https?://\S{40,}", re.IGNORECASE)
_INVISIBLE_FORMAT_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff\u00ad\u034f]")
_ORIG_FX_DESC_RE = re.compile(
    r"\bof\s+([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s+([A-Z]{3})\b",
    re.IGNORECASE,
)
_BANK_FX_DESC_RE = re.compile(
    r"(?:^|[\s(])([A-Z]{3})\s+([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:@|converted\s+at\b)",
    re.IGNORECASE,
)
_FX_DESC_PATTERNS: tuple[tuple[re.Pattern[str], bool], ...] = (
    (_ORIG_FX_DESC_RE, False),
    (_BANK_FX_DESC_RE, True),
)
_CURRENCY_EXACT_PREFIXES: dict[str, str] = {
    "S$": "SGD",
    "SG$": "SGD",
    "NZ$": "NZD",
    "AU$": "AUD",
    "A$": "AUD",
    "US$": "USD",
}
_CURRENCY_SYMBOL_CODES: tuple[tuple[str, str], ...] = (
    ("₱", "PHP"),
    ("€", "EUR"),
    ("£", "GBP"),
)
_MONTH_CODES = frozenset(
    {"JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"}
)
_MERCHANT_STOPWORDS = frozenset(
    {
        "the",
        "pty",
        "ltd",
        "inc",
        "limited",
        "co",
        "corp",
        "llc",
        "card",
        "transaction",
        "issued",
        "by",
        "payment",
        "purchase",
        "pos",
        "eftpos",
        "online",
        "store",
        "shop",
        "marketplace",
        "com",
        "www",
        "nz",
        "au",
        "sg",
        "us",
        "uk",
        "at",
        "to",
        "of",
        "in",
        "on",
    }
)

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass

_RECEIPT_CATEGORY_OPTIONS = " | ".join(
    get_canonical_category_names(include_system_non_expense=False)
)

RECEIPT_PARSING_PROMPT = f"""You are a specialized receipt parsing engine.
Extract the merchant name, receipt date (YYYY-MM-DD), 3-letter ISO currency code (e.g. NZD, AUD, USD, SGD, EUR, GBP, JPY, MYR), order number (if any), total amount, tax amount (if any), and itemized line items from this receipt.

Respond STRICTLY with valid JSON following this exact schema:
{{
  "merchant": "Merchant Name",
  "receipt_date": "YYYY-MM-DD",
  "currency": "NZD",
  "order_number": "Order number or null",
  "total_amount": 0.00,
  "tax_amount": 0.00,
  "items": [
    {{
      "item_name": "Product Name",
      "brand": "Brand or null",
      "quantity": 1.0,
      "unit_price": 0.00,
      "total_price": 0.00,
      "category": "{_RECEIPT_CATEGORY_OPTIONS}",
      "domain": "02-skin | 01-health | 20-home | general",
      "product_type": "serum | cleanser | sunscreen | protein | creatine | null",
      "volume_ml": "e.g. 30ml, 1kg, 60 caps or null"
    }}
  ]
}}"""

MULTI_ORDER_EMAIL_RECEIPT_PROMPT = f"""You are a specialized e-commerce and purchase receipt extraction engine.
An email may contain ONE or MULTIPLE distinct orders or shipments (for example, split Amazon orders with separate order numbers and totals).
Extract EVERY distinct purchase order in the email as its own entry inside `orders`.

Respond STRICTLY with valid JSON following this exact schema:
{{
  "orders": [
    {{
      "order_number": "Order ID or null",
      "merchant": "Merchant Name",
      "receipt_date": "YYYY-MM-DD",
      "currency": "NZD | AUD | USD | SGD | EUR | GBP | JPY | MYR",
      "total_amount": 0.00,
      "tax_amount": 0.00,
      "items": [
        {{
          "item_name": "Full Product Name (max 200 chars)",
          "brand": "Brand or null",
          "quantity": 1.0,
          "unit_price": 0.00,
          "total_price": 0.00,
          "category": "{_RECEIPT_CATEGORY_OPTIONS}",
          "domain": "02-skin | 01-health | 20-home | general",
          "product_type": "serum | cleanser | sunscreen | protein | creatine | null",
          "volume_ml": "e.g. 30ml, 1kg, 60 caps or null"
        }}
      ]
    }}
  ]
}}"""

PAYSLIP_PARSING_PROMPT = """You are a specialized payroll and payslip extraction engine.
Extract structured header details from this payslip document or payroll notification.

Rules:
1. Extract "Net Pay" or "Total Banked" (the actual take-home cash deposited to the employee's bank account) as `net_pay`. NEVER use Gross Earnings, Taxable Earnings, Year-To-Date (YTD) totals, or Annual Salary for `net_pay`.
2. Prefer `Pay Date` / `Date Paid` / `Payment Date` for `pay_date` (YYYY-MM-DD) over `Pay Period End` whenever a payment date is present.
3. Extract PAYE / income tax deducted for this pay run into `tax_amount` (or null if not shown).
4. Return a 3-letter ISO currency code for `currency` (e.g. NZD, AUD, USD, SGD, EUR, GBP). Default `currency` to "NZD" when New Zealand payroll markers (such as KiwiSaver, PAYE, IRD, or NZ bank accounts) appear without an explicit ISO currency code.
5. Extract `pay_period_start` (YYYY-MM-DD or null), `pay_period_end` (YYYY-MM-DD or null), and a concise `pay_period_label` (e.g. "2026-11-01 to 2026-11-30" or "Nov 2026", or null).

Respond STRICTLY with valid JSON following this exact schema:
{
  "employer": "Employer Name",
  "pay_date": "YYYY-MM-DD",
  "pay_period_start": "YYYY-MM-DD or null",
  "pay_period_end": "YYYY-MM-DD or null",
  "currency": "NZD",
  "net_pay": 0.00,
  "tax_amount": 0.00,
  "pay_period_label": "Period label or null"
}"""


def classify_line_item(item_name: str) -> tuple[str, Optional[str], Optional[str], Optional[str]]:
    """Heuristic domain, category, product_type, and volume detection for a line item.

    Returns:
        (category, domain, product_type, volume_ml)
    """
    name = item_name.lower()

    # 1. Personal Care / Skincare (Area 02-Skin)
    if any(k in name for k in ["serum", "vitamin c", "retinol", "cleanser", "moisturizer", "spf", "sunscreen", "hyaluronic", "niacinamide", "toner", "exfoliant", "adapalene", "haircut", "barber", "shampoo", "conditioner", "deodorant", "fragrance"]):
        domain = "02-skin"
        category = "Personal Care"
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

    # 3. Clothing & Footwear (checked before Household so "pant" never collides with cookware "pan")
    if any(k in name for k in ["pant", "shirt", "hoodie", "liner", "sock", "slide", "shoe", "jacket", "short", "jeans", "sneaker"]):
        return "Clothing", "general", "clothing", None

    # 4. Home & Tech (Area 20-Home)
    if any(k in name for k in ["cable", "charger", "adapter", "usb", "ssd", "camera", "mouse", "keyboard", "monitor"]):
        return "Tech", "20-home", "tech", None
    if any(k in name for k in ["bulb", "filter", "vacuum", "appliance", "detergent", "towel"]) or re.search(
        r"\b(?:pan|pans|frypan|saucepan)\b", name
    ):
        return "Household", "20-home", "appliance", None

    # 5. Alcohol & Groceries
    if re.search(r"\b(?:wine|beer|whisky|whiskey|vodka|gin|cider|champagne|liquor)\b", name):
        return "Alcohol", "general", "alcohol", None
    if any(k in name for k in ["milk", "bread", "eggs", "apple", "banana", "chicken", "beef", "rice", "coffee", "oats", "noodle", "lamington", "coca cola", "cola"]):
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
        if clean_dom and clean_dom != "general":
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

    return sorted(list(tags))


def _match_existing_tags_heuristic(
    item_name: str,
    brand: Optional[str],
    existing_tags: list[str],
) -> list[str]:
    """Match existing ledger/rule tags verbatim as whole words against a receipt item."""
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
) -> list[str]:
    combined_tags = [
        t
        for t in normalize_tags([*base_explicit_tags, *jev_tags, *matched_existing])
        if t != "general"
    ]
    non_cat_tags = strip_redundant_tags(combined_tags, resolved_cat)
    if non_cat_tags:
        return non_cat_tags
    if combined_tags:
        return combined_tags
    return extract_tags_for_line_item(item_name, resolved_dom, resolved_ptype)


def classify_and_tag_receipt_item(
    *,
    item_name: str,
    brand: Optional[str],
    total_price: float,
    merchant: str,
    explicit_category: Optional[str],
    explicit_domain: Optional[str],
    explicit_product_type: Optional[str],
    explicit_volume_ml: Optional[str],
    explicit_tags: Optional[list[str]],
    active_rules: list[CategorizationRule],
    existing_tags: list[str],
    extra_categories: Optional[list[str]] = None,
    gateway: AiGateway | None = None,
) -> tuple[str, Optional[str], Optional[str], Optional[str], list[str]]:
    """Resolve (category, domain, product_type, volume_ml, tags) for a new receipt item.

    Evaluates Outlook rules first, then explicit/heuristic fields, and falls
    back to Jev System One (with existing-tag reuse) when no rule matched.
    """
    h_cat, h_dom, h_ptype, h_vol = classify_line_item(item_name)
    resolved_dom = explicit_domain or h_dom
    resolved_ptype = explicit_product_type or h_ptype
    resolved_vol = explicit_volume_ml or h_vol
    base_explicit_tags = normalize_tags(explicit_tags or [])

    matched_rule = match_receipt_item_rule(item_name, brand, total_price, active_rules)
    if matched_rule:
        matched_rule.match_count = int(matched_rule.match_count or 0) + 1
        rule_tags = parse_rule_tags(matched_rule.target_tags)
        combined = normalize_tags([*base_explicit_tags, *rule_tags]) or extract_tags_for_line_item(
            item_name, resolved_dom, resolved_ptype
        )
        return matched_rule.target_category, resolved_dom, resolved_ptype, resolved_vol, combined

    raw_cat = (explicit_category or "").strip()
    resolved_cat = raw_cat if (raw_cat and raw_cat.lower() != "general") else h_cat
    matched_existing = _match_existing_tags_heuristic(item_name, brand, existing_tags)
    jev_tags: list[str] = []
    if float(total_price) >= 0 and (resolved_cat == "General" or not base_explicit_tags):
        jev_label = f"{brand.strip()} {item_name}".strip() if (brand and brand.strip()) else item_name
        jev_cat, _, jev_tags = classify_transaction_jev_with_tags(
            jev_label,
            -abs(float(total_price)),
            notes=f"Receipt merchant: {merchant}",
            existing_tags=existing_tags,
            extra_categories=extra_categories,
            gateway=gateway,
        )
        if resolved_cat == "General" and jev_cat and jev_cat != "General":
            resolved_cat = jev_cat

    final_tags = _finalize_receipt_item_tags(
        item_name=item_name,
        resolved_cat=resolved_cat,
        resolved_dom=resolved_dom,
        resolved_ptype=resolved_ptype,
        base_explicit_tags=base_explicit_tags,
        jev_tags=jev_tags,
        matched_existing=matched_existing,
    )
    return resolved_cat, resolved_dom, resolved_ptype, resolved_vol, final_tags


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
                discount_amount=float(it.discount_amount) if it.discount_amount is not None else None,
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


def infer_receipt_mime_type(filename: Optional[str], content_type: Optional[str]) -> str:
    """Resolve an accurate MIME type from upload headers or filename extension."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct and ct != "application/octet-stream":
        return ct
    if filename:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in ("heic", "heif"):
            return f"image/{ext}"
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            return guessed
    return "application/octet-stream"


def _normalize_image_rgb(img: Any) -> Any:
    """Apply EXIF orientation and convert an image to RGB for WebP/JPEG encoding."""
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        background = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        background.paste(rgba, mask=rgba.split()[3])
        return background
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def compress_receipt_image(
    content: bytes,
    mime_type: str,
    max_dimension: int = 1400,
    quality: int = 75,
) -> tuple[bytes, str]:
    """Downscale and WebP-compress a processed receipt image to minimize DB storage."""
    if not mime_type.startswith("image/"):
        return content, mime_type

    try:
        from PIL import Image

        with Image.open(io.BytesIO(content)) as raw_img:
            img = _normalize_image_rgb(raw_img)
            if max(img.size) > max_dimension:
                img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            img.save(out, format="WEBP", quality=quality, method=4)
            compressed = out.getvalue()
            if len(compressed) < len(content):
                return compressed, "image/webp"
            return content, mime_type
    except Exception as exc:
        logger.warning("Receipt image compression skipped (%s); retaining original bytes.", exc)
        return content, mime_type


def prepare_image_for_llm(
    content: bytes,
    mime_type: str,
    max_dimension: int = 2048,
) -> tuple[bytes, str]:
    """Normalize image format (e.g. HEIC -> JPEG) and cap resolution for LLM vision."""
    if not mime_type.startswith("image/"):
        return content, mime_type

    needs_transcode = mime_type not in ("image/jpeg", "image/png", "image/webp")
    try:
        from PIL import Image

        with Image.open(io.BytesIO(content)) as raw_img:
            if not needs_transcode and max(raw_img.size) <= max_dimension:
                return content, mime_type
            img = _normalize_image_rgb(raw_img)
            if max(img.size) > max_dimension:
                img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=88, optimize=True)
            return out.getvalue(), "image/jpeg"
    except Exception as exc:
        logger.warning("Failed to normalize image for LLM (%s); sending raw bytes.", exc)
        return content, mime_type


def extract_pdf_text(content: bytes) -> str:
    """Extract readable text from an uploaded PDF receipt using pdfplumber."""
    try:
        import pdfplumber

        pages: list[str] = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(text.strip())
        return "\n\n".join(pages)
    except Exception as exc:
        logger.warning("PDF receipt text extraction failed: %s", exc)
        return ""


def _execute_receipt_gateway_call(
    call_fn: Callable[[], AiJsonResponse],
    *,
    label: str,
    raise_on_error: bool,
) -> Optional[dict[str, Any]]:
    """Execute one AI Gateway call and map typed gateway errors to ValueError or 503."""
    try:
        return call_fn().data
    except (AiConfigError, AiInvalidResponseError) as exc:
        logger.warning("LLM %s failed: %s", label, exc)
        if raise_on_error:
            raise ValueError(f"Receipt LLM {label} failed: {exc}") from exc
        return None
    except AiGatewayError as exc:
        logger.warning("LLM %s upstream failed: %s", label, exc)
        if raise_on_error:
            raise ConnectorUpstreamError(f"Receipt LLM {label} request failed: {exc}") from exc
        return None


def _invoke_text_prompt(
    prompt: str,
    content_label: str,
    raw_text: str,
    *,
    raise_on_error: bool = False,
    gateway: AiGateway | None = None,
) -> Optional[dict[str, Any]]:
    """Invoke configured text LLM in JSON mode for receipt or payslip text."""
    resolved_gateway = gateway or get_default_ai_gateway()
    text_model = settings.resolved_llm_model or settings.resolved_receipt_llm_model
    request = AiTextRequest(
        system_prompt=None,
        user_prompt=f"{prompt}\n\n{content_label}:\n{raw_text}",
        model=text_model,
        temperature=0.1,
        timeout=60.0,
        response_format_json=True,
    )
    return _execute_receipt_gateway_call(
        lambda: resolved_gateway.complete_json(request),
        label="text parsing",
        raise_on_error=raise_on_error,
    )


def parse_receipt_text_with_llm(
    raw_text: str,
    *,
    raise_on_error: bool = False,
    gateway: AiGateway | None = None,
) -> Optional[dict[str, Any]]:
    """Invoke text LLM to extract structured line items from raw receipt text."""
    return _invoke_text_prompt(
        RECEIPT_PARSING_PROMPT,
        "Receipt text",
        raw_text,
        raise_on_error=raise_on_error,
        gateway=gateway,
    )


def parse_receipt_orders_text_with_llm(
    raw_text: str,
    *,
    raise_on_error: bool = True,
    gateway: AiGateway | None = None,
) -> Optional[dict[str, Any]]:
    """Invoke text LLM to extract one or multiple structured orders from email/document text."""
    return _invoke_text_prompt(
        MULTI_ORDER_EMAIL_RECEIPT_PROMPT,
        "Email / Receipt content",
        raw_text,
        raise_on_error=raise_on_error,
        gateway=gateway,
    )


def _invoke_vision_prompt(
    content: bytes,
    mime_type: str,
    prompt: str,
    *,
    raise_on_error: bool = False,
    gateway: AiGateway | None = None,
) -> Optional[dict[str, Any]]:
    """Prepare image/PDF bytes as base64 and invoke the vision LLM via AI Gateway."""
    llm_bytes, llm_mime = prepare_image_for_llm(content, mime_type)
    b64_data = base64.b64encode(llm_bytes).decode("ascii")
    resolved_gateway = gateway or get_default_ai_gateway()
    request = AiVisionRequest(
        system_prompt=None,
        user_prompt=prompt,
        image_b64=b64_data,
        image_mime_type=llm_mime,
        model=settings.resolved_receipt_llm_model,
        temperature=0.1,
        timeout=105.0,
        response_format_json=False,
    )
    return _execute_receipt_gateway_call(
        lambda: resolved_gateway.complete_vision_json(request),
        label="vision parsing",
        raise_on_error=raise_on_error,
    )


def parse_receipt_with_llm(
    content: bytes,
    filename: Optional[str],
    mime_type: str,
    *,
    gateway: AiGateway | None = None,
) -> Optional[dict[str, Any]]:
    """Extract structured receipt fields from an image, PDF, or text document."""
    if mime_type.startswith("image/"):
        return _invoke_vision_prompt(content, mime_type, RECEIPT_PARSING_PROMPT, gateway=gateway)

    if mime_type == "application/pdf" or (filename and filename.lower().endswith(".pdf")):
        pdf_text = extract_pdf_text(content)
        if not pdf_text:
            return None
        return parse_receipt_text_with_llm(pdf_text, gateway=gateway)

    try:
        raw_text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not raw_text.strip():
        return None
    return parse_receipt_text_with_llm(raw_text, gateway=gateway)


def parse_payslip_text_with_llm(
    raw_text: str,
    *,
    raise_on_error: bool = True,
    gateway: AiGateway | None = None,
) -> Optional[dict[str, Any]]:
    """Invoke text LLM to extract structured payslip header fields from raw text."""
    return _invoke_text_prompt(
        PAYSLIP_PARSING_PROMPT,
        "Payslip content",
        raw_text,
        raise_on_error=raise_on_error,
        gateway=gateway,
    )


def parse_payslip_with_llm(
    content: bytes,
    filename: Optional[str],
    mime_type: str,
    *,
    gateway: AiGateway | None = None,
) -> Optional[dict[str, Any]]:
    """Extract structured payslip fields from an image, PDF, or text document."""
    if mime_type.startswith("image/"):
        return _invoke_vision_prompt(content, mime_type, PAYSLIP_PARSING_PROMPT, gateway=gateway)

    is_pdf = mime_type == "application/pdf" or (
        filename is not None and filename.lower().endswith(".pdf")
    )
    if is_pdf:
        pdf_text = extract_pdf_text(content)
        if pdf_text:
            parsed = parse_payslip_text_with_llm(pdf_text, raise_on_error=False, gateway=gateway)
            if parsed:
                return parsed
        return _invoke_vision_prompt(
            content, "application/pdf", PAYSLIP_PARSING_PROMPT, gateway=gateway
        )

    try:
        raw_text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not raw_text.strip():
        return None
    return parse_payslip_text_with_llm(raw_text, raise_on_error=False, gateway=gateway)


class _ReceiptHTMLSanitizer(HTMLParser):
    """Sanitize HTML purchase receipts while preserving aria-label prices and img alt titles."""

    _SKIP_TAGS = {"script", "style", "noscript", "head", "title"}
    _BLOCK_TAGS = {
        "p",
        "div",
        "tr",
        "li",
        "br",
        "hr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "table",
        "section",
        "article",
        "header",
        "footer",
        "blockquote",
    }
    _NOISE_ALT_VALUES = frozenset({"logo", "spacer", "pixel", "icon", "image", "banner"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        lower = tag.lower()
        if lower in self._SKIP_TAGS:
            self._skip += 1
            return
        if self._skip > 0:
            return

        attr_map = {k.lower(): (v or "").strip() for k, v in attrs}
        aria_label = attr_map.get("aria-label")
        if aria_label and len(aria_label) >= 2 and aria_label.lower() not in {"close", "menu", "logo"}:
            self._chunks.append(f" [aria: {aria_label}] ")

        if lower == "img":
            alt_text = attr_map.get("alt")
            if alt_text and len(alt_text) >= 4 and alt_text.lower() not in self._NOISE_ALT_VALUES:
                self._chunks.append(f" [item: {alt_text}] ")

        if lower in self._BLOCK_TAGS:
            self._chunks.append("\n")
        elif lower in ("td", "th", "span"):
            self._chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower in self._SKIP_TAGS:
            if self._skip > 0:
                self._skip -= 1
            return
        if self._skip == 0:
            if lower in self._BLOCK_TAGS:
                self._chunks.append("\n")
            elif lower in ("td", "th", "span"):
                self._chunks.append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip == 0 and data:
            self._chunks.append(data)

    def get_text(self) -> str:
        raw = "".join(self._chunks)
        return _clean_receipt_text_lines(raw)


def _clean_receipt_text_lines(raw: str) -> str:
    """Normalize whitespace, strip invisible spacers and long tracking URLs from text."""
    cleaned = strip_invisible_unicode(raw)
    cleaned = _TRACKING_URL_RE.sub("", cleaned)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in cleaned.splitlines()]
    return "\n".join(line for line in lines if line)


def sanitize_receipt_email_content(
    *,
    subject: str,
    from_address: Optional[str],
    received_at: Optional[datetime],
    text_body: Optional[str],
    html_excerpt: Optional[str],
    raw_excerpt: Optional[str],
) -> tuple[str, date]:
    """Sanitize forwarded receipt email HTML/text and return (prompt_text, fallback_date)."""
    local_tz = ZoneInfo(settings.timezone)

    html_clean = ""
    if html_excerpt and html_excerpt.strip():
        parser = _ReceiptHTMLSanitizer()
        parser.feed(html_excerpt)
        parser.close()
        html_clean = parser.get_text()

    raw_source_for_fwd = (text_body or raw_excerpt or "").strip() or html_clean
    clean_subject, stripped_text, ref_local_dt = extract_and_strip_forwarded_header(
        subject or "", raw_source_for_fwd, received_at, target_tz=local_tz
    )
    fallback_date = ref_local_dt.date()

    if html_clean:
        _, stripped_html, ref_html_dt = extract_and_strip_forwarded_header(
            subject or "", html_clean, received_at, target_tz=local_tz
        )
        if not (text_body or raw_excerpt or "").strip():
            fallback_date = ref_html_dt.date()
        html_clean = _clean_receipt_text_lines(stripped_html)

    text_clean = _clean_receipt_text_lines(stripped_text)
    if html_clean and len(html_clean) >= 60:
        body_section = html_clean
    elif text_clean:
        body_section = text_clean
    elif html_clean:
        body_section = html_clean
    elif raw_excerpt:
        body_section = _clean_receipt_text_lines(raw_excerpt)
    else:
        body_section = ""

    header_lines: list[str] = []
    if clean_subject:
        header_lines.append(f"Subject: {clean_subject}")
    if from_address:
        header_lines.append(f"From: {from_address.strip()}")
    header_lines.append(f"Email Date: {fallback_date.isoformat()}")

    if not body_section:
        return "", fallback_date

    combined = "\n".join(header_lines) + "\n\n" + body_section
    return combined[:48000], fallback_date


def normalize_receipt_currency(raw_currency: Any, *, fallback: str = "NZD") -> str:
    """Normalize a parsed currency string to a 3-letter uppercase ISO currency code."""
    fb = (fallback or "NZD").strip().upper()[:3]
    if not fb or len(fb) != 3 or not fb.isalpha():
        fb = "NZD"
    if raw_currency is None:
        return fb
    cleaned = str(raw_currency).strip().upper()
    if cleaned in _CURRENCY_EXACT_PREFIXES:
        return _CURRENCY_EXACT_PREFIXES[cleaned]
    for symbol, code in _CURRENCY_SYMBOL_CODES:
        if symbol in cleaned:
            return code
    m = re.search(r"\b([A-Z]{3})\b", cleaned)
    if m:
        return m.group(1)
    return fb


def normalize_order_number(raw_order: Any) -> Optional[str]:
    """Normalize an optional purchase order identifier."""
    if raw_order is None:
        return None
    cleaned = str(raw_order).strip()
    if not cleaned or cleaned.lower() in ("null", "none", "n/a"):
        return None
    return cleaned[:128]


def _normalize_merchant_key(merchant: str) -> str:
    """Normalize merchant names for semantic receipt deduplication."""
    tokens = [
        tok
        for tok in re.findall(r"[a-z0-9]+", (merchant or "").lower())
        if tok not in _MERCHANT_STOPWORDS
    ]
    return " ".join(tokens) or re.sub(r"[^a-z0-9]+", " ", (merchant or "").lower()).strip()


def load_receipt_rule_context(db: Session) -> tuple[list[CategorizationRule], list[str], list[str]]:
    """Fetch active priority-ordered rules, existing tag vocabulary, and custom Jev categories."""
    active_rules = db.scalars(
        select(CategorizationRule)
        .where(CategorizationRule.is_active == True)
        .order_by(CategorizationRule.priority.asc(), CategorizationRule.id.asc())
    ).all()
    return list(active_rules), collect_existing_tags(db), get_extra_jev_categories(db)


def create_classified_receipt_item(
    db: Session,
    *,
    receipt: Receipt,
    item_name: str,
    brand: Optional[str],
    quantity: float,
    unit_price: float,
    discount_amount: Optional[float],
    total_price: float,
    explicit_category: Optional[str],
    explicit_domain: Optional[str],
    explicit_product_type: Optional[str],
    explicit_volume_ml: Optional[str],
    explicit_tags: Optional[list[str]],
    notes: Optional[str],
    active_rules: list[CategorizationRule],
    existing_tags: list[str],
    extra_categories: Optional[list[str]] = None,
    gateway: AiGateway | None = None,
) -> None:
    """Resolve rules/Jev classification and persist one ReceiptItem and Product."""
    clean_name = (item_name.strip() or "Item")[:200]
    clean_brand = brand.strip()[:100] if brand and brand.strip() else None
    cat, dom, ptype, vol, tags_list = classify_and_tag_receipt_item(
        item_name=clean_name,
        brand=clean_brand,
        total_price=total_price,
        merchant=receipt.merchant,
        explicit_category=explicit_category,
        explicit_domain=explicit_domain,
        explicit_product_type=explicit_product_type,
        explicit_volume_ml=explicit_volume_ml,
        explicit_tags=explicit_tags,
        active_rules=active_rules,
        existing_tags=existing_tags,
        extra_categories=extra_categories,
        gateway=gateway,
    )
    prod_id: Optional[int] = None
    if total_price >= 0:
        prod = get_or_create_product(
            db=db,
            item_name=clean_name,
            brand=clean_brand,
            category=cat,
            domain=dom,
            tags=tags_list,
            volume_ml=vol,
        )
        prod_id = prod.id

    db.add(
        ReceiptItem(
            receipt_id=receipt.id,
            product_id=prod_id,
            item_name=clean_name,
            brand=clean_brand,
            quantity=quantity,
            unit_price=unit_price,
            discount_amount=discount_amount,
            total_price=total_price,
            category=(cat or "General")[:50],
            domain=dom[:50] if dom else None,
            product_type=ptype[:50] if ptype else None,
            tags=json.dumps(tags_list),
            volume_ml=vol[:50] if vol else None,
            notes=notes,
        )
    )


def _match_discount_target_heuristic(
    discount_name: str,
    positive_items: list[dict[str, Any]],
) -> Optional[int]:
    """Fallback token-overlap matcher when Jev is unavailable."""
    stop = {"tpd", "discount", "off", "save", "savings", "promo", "coupon", "instant"}
    d_tokens = {
        tok
        for tok in "".join(c if c.isalnum() else " " for c in discount_name.lower()).split()
        if len(tok) >= 3 and tok not in stop
    }
    if not d_tokens:
        return None

    best_idx: Optional[int] = None
    best_score = 0
    for idx, item in enumerate(positive_items):
        i_name = str(item.get("item_name", "")).lower()
        i_tokens = {
            tok
            for tok in "".join(c if c.isalnum() else " " for c in i_name).split()
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
    """Parse Jev discount target JSON, returning (handled_by_jev, target_index)."""
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
    discount: dict[str, Any],
    positive_items: list[dict[str, Any]],
    *,
    gateway: AiGateway | None = None,
) -> Optional[int]:
    """Use Jev System One to attribute a negative receipt discount line to its target item."""
    choices = {str(i): str(it.get("item_name", "")) for i, it in enumerate(positive_items)}
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
        f"Match the receipt discount line to the purchased item it discounts:\n```json\n{json.dumps(schema)}\n```"
    )
    candidates_desc = ", ".join(
        f"[{i}] {it.get('item_name')} (${float(it.get('total_price', 0.0)):.2f})"
        for i, it in enumerate(positive_items)
    )
    user_content = (
        f"Discount: {discount.get('item_name')} ({float(discount.get('total_price', 0.0)):.2f})\n"
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
    return _match_discount_target_heuristic(str(discount.get("item_name", "")), positive_items)


def apply_receipt_discounts_with_jev(
    items: list[dict[str, Any]],
    *,
    gateway: AiGateway | None = None,
) -> list[dict[str, Any]]:
    """Fold negative discount/savings lines into their matching purchased line items via Jev."""
    positive_items: list[dict[str, Any]] = []
    discount_items: list[dict[str, Any]] = []

    for raw in items:
        copy = dict(raw)
        if float(copy.get("total_price", 0.0) or 0.0) < 0:
            discount_items.append(copy)
        else:
            positive_items.append(copy)

    if not discount_items or not positive_items:
        return items

    unmatched_discounts: list[dict[str, Any]] = []
    for disc in discount_items:
        disc_total = float(disc.get("total_price", 0.0) or 0.0)
        target_idx = _match_discount_target_jev(disc, positive_items, gateway=gateway)
        if target_idx is not None:
            target = positive_items[target_idx]
            new_total = round(float(target.get("total_price", 0.0) or 0.0) + disc_total, 2)
            if new_total >= 0:
                existing_disc = float(target.get("discount_amount") or 0.0)
                target["discount_amount"] = round(existing_disc + abs(disc_total), 2)
                target["total_price"] = new_total
                disc_note = f"Includes {disc_total:.2f} discount ({disc.get('item_name', 'Discount')})"
                existing_note = str(target.get("notes") or "").strip()
                target["notes"] = f"{existing_note}; {disc_note}" if existing_note else disc_note
                continue
        unmatched_discounts.append(disc)

    return positive_items + unmatched_discounts


@dataclass(frozen=True)
class _ScoredCandidate:
    """Internal candidate match score between a Receipt and an expense Transaction."""

    tx: Transaction
    tier_rank: int
    match_reason: str
    confidence: float
    has_merchant_overlap: bool
    sim_score: float
    date_diff: int
    amt_diff: float
    original_amount: Optional[float] = None
    original_currency: Optional[str] = None
    estimated_receipt_amount: Optional[float] = None


def parse_original_fx_from_description(raw_description: Optional[str]) -> Optional[tuple[float, str]]:
    """Extract original foreign currency amount and ISO code from bank/Wise raw_description."""
    if not raw_description:
        return None
    for pattern, code_first in _FX_DESC_PATTERNS:
        for match in pattern.finditer(raw_description):
            code, amt_str = match.groups() if code_first else (match.group(2), match.group(1))
            upper_code = code.upper()
            if upper_code in _MONTH_CODES:
                continue
            try:
                amt_val = float(amt_str.replace(",", ""))
            except ValueError:
                continue
            if amt_val > 0:
                return amt_val, upper_code
    return None


def _merchant_tokens(text: str) -> set[str]:
    """Extract normalized alphanumeric merchant tokens excluding generic filler words."""
    return {
        tok
        for tok in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(tok) >= 2 and tok not in _MERCHANT_STOPWORDS
    }


def _merchant_similarity(
    receipt_merchant: str,
    tx_merchant: str,
    tx_raw_desc: Optional[str],
) -> tuple[bool, float]:
    """Compute (has_positive_overlap, similarity_score) between receipt and transaction merchant."""
    r_tokens = _merchant_tokens(receipt_merchant)
    tx_tokens = _merchant_tokens(tx_merchant) | _merchant_tokens(tx_raw_desc or "")

    r_compact = re.sub(r"[^a-z0-9]", "", (receipt_merchant or "").lower())
    tx_m_compact = re.sub(r"[^a-z0-9]", "", (tx_merchant or "").lower())
    tx_d_compact = re.sub(r"[^a-z0-9]", "", (tx_raw_desc or "").lower())

    substring_hit = False
    if len(r_compact) >= 4:
        substring_hit = (
            r_compact in tx_m_compact
            or r_compact in tx_d_compact
            or (len(tx_m_compact) >= 4 and tx_m_compact in r_compact)
        )

    overlap = len(r_tokens & tx_tokens)
    has_overlap = overlap > 0 or substring_hit
    if not r_tokens and not substring_hit:
        return False, 0.0
    union_size = len(r_tokens | tx_tokens) or 1
    jaccard = overlap / union_size
    score = max(jaccard, 0.6 if substring_hit else 0.0)
    return has_overlap, round(score, 4)


def _resolve_tx_to_receipt_fx_rate(
    db: Session,
    tx: Transaction,
    receipt_currency: str,
) -> Optional[float]:
    """Resolve conversion rate from tx.currency to receipt_currency via stored or cached FX rates."""
    tx_curr = (tx.currency or "NZD").strip().upper()
    rec_curr = (receipt_currency or "NZD").strip().upper()
    if tx_curr == rec_curr:
        return 1.0
    if rec_curr == "NZD" and tx.exchange_rate is not None and float(tx.exchange_rate) > 0:
        return float(tx.exchange_rate)
    fx_res = get_exchange_rate(
        db,
        rate_date=tx.transaction_date,
        from_currency=tx_curr,
        to_currency=rec_curr,
    )
    if fx_res is not None and fx_res[0] > 0:
        return float(fx_res[0])
    return None


def _resolve_candidate_window(receipt: Receipt, is_payslip: bool) -> tuple[date, date]:
    """Return inclusive transaction search bounds for a receipt or payslip."""
    r_date = receipt.receipt_date
    if is_payslip:
        return r_date - timedelta(days=7), r_date + timedelta(days=14)
    if receipt.order_number or receipt.source_email_message_id:
        return r_date - timedelta(days=3), r_date + timedelta(days=21)
    return r_date - timedelta(days=1), r_date + timedelta(days=7)


def _score_fx_estimated_candidate(
    db: Session,
    tx: Transaction,
    *,
    tx_amt: float,
    target_amt: float,
    rec_curr: str,
    has_overlap: bool,
    sim_score: float,
    date_diff: int,
    orig_amt: Optional[float],
    orig_curr: Optional[str],
) -> Optional[_ScoredCandidate]:
    """Score cross-currency transaction candidate using cached/daily FX conversion."""
    rate = _resolve_tx_to_receipt_fx_rate(db, tx, rec_curr)
    if rate is None or rate <= 0 or target_amt <= 0:
        return None
    est_amt = round(tx_amt * rate, 2)
    rel_err = abs(est_amt - target_amt) / target_amt
    if rel_err > 0.05:
        return None
    base_conf = 0.85 if has_overlap else 0.68
    conf = round(max(0.40, min(0.89, base_conf - rel_err * 3.0 - 0.005 * min(date_diff, 14))), 2)
    return _ScoredCandidate(
        tx=tx, tier_rank=1, match_reason="fx_estimated", confidence=conf,
        has_merchant_overlap=has_overlap, sim_score=sim_score, date_diff=date_diff,
        amt_diff=abs(est_amt - target_amt), original_amount=orig_amt,
        original_currency=orig_curr, estimated_receipt_amount=est_amt,
    )


def _score_candidate_transaction(
    db: Session,
    tx: Transaction,
    *,
    target_amt: float,
    r_date: date,
    rec_curr: str,
    receipt_merchant: str,
) -> Optional[_ScoredCandidate]:
    """Score one candidate transaction against a receipt or payslip across currency tiers."""
    tx_curr = (tx.currency or "NZD").strip().upper()
    tx_amt = abs(float(tx.amount))
    date_diff = abs((tx.transaction_date - r_date).days)
    has_overlap, sim_score = _merchant_similarity(receipt_merchant, tx.merchant, tx.raw_description)
    orig_amt, orig_curr = parse_original_fx_from_description(tx.raw_description) or (None, None)
    if tx_curr == rec_curr and abs(tx_amt - target_amt) <= 0.05:
        amt_diff = abs(tx_amt - target_amt)
        conf = round(max(0.50, min(0.99, (0.96 if has_overlap else 0.78) - 0.01 * min(date_diff, 14) - 0.5 * amt_diff)), 2)
        return _ScoredCandidate(
            tx=tx, tier_rank=3, match_reason="exact_same_currency", confidence=conf,
            has_merchant_overlap=has_overlap, sim_score=sim_score, date_diff=date_diff,
            amt_diff=amt_diff, original_amount=orig_amt, original_currency=orig_curr,
            estimated_receipt_amount=round(tx_amt, 2),
        )
    if orig_curr == rec_curr and orig_amt is not None and abs(orig_amt - target_amt) <= 0.05:
        amt_diff = abs(orig_amt - target_amt)
        conf = round(max(0.45, min(0.98, (0.93 if has_overlap else 0.65) - 0.008 * min(date_diff, 14) - 0.5 * amt_diff)), 2)
        return _ScoredCandidate(
            tx=tx, tier_rank=2, match_reason="original_fx_description", confidence=conf,
            has_merchant_overlap=has_overlap, sim_score=sim_score, date_diff=date_diff,
            amt_diff=amt_diff, original_amount=orig_amt, original_currency=orig_curr,
            estimated_receipt_amount=round(orig_amt, 2),
        )
    if tx_curr == rec_curr:
        return None
    return _score_fx_estimated_candidate(
        db, tx, tx_amt=tx_amt, target_amt=target_amt, rec_curr=rec_curr,
        has_overlap=has_overlap, sim_score=sim_score, date_diff=date_diff,
        orig_amt=orig_amt, orig_curr=orig_curr,
    )


def _evaluate_receipt_candidates(
    db: Session,
    receipt: Receipt,
) -> list[_ScoredCandidate]:
    """Score unlinked transactions matching the receipt/payslip sign within its date window."""
    target_amt = float(receipt.total_amount)
    r_date = receipt.receipt_date
    rec_curr = (receipt.currency or "NZD").strip().upper()
    is_payslip = getattr(receipt, "document_type", "receipt") == "payslip"
    date_min, date_max = _resolve_candidate_window(receipt, is_payslip)
    sign_filter = Transaction.amount > 0 if is_payslip else Transaction.amount < 0

    linked_tx_subq = select(Receipt.transaction_id).where(
        Receipt.transaction_id.isnot(None), Receipt.id != receipt.id
    )
    stmt = (
        select(Transaction)
        .options(joinedload(Transaction.account))
        .where(sign_filter, Transaction.transaction_date >= date_min)
        .where(Transaction.transaction_date <= date_max, ~Transaction.id.in_(linked_tx_subq))
    )
    candidates = [
        scored
        for tx in db.scalars(stmt).all()
        if (scored := _score_candidate_transaction(
            db, tx, target_amt=target_amt, r_date=r_date,
            rec_curr=rec_curr, receipt_merchant=receipt.merchant,
        )) is not None
    ]
    candidates.sort(
        key=lambda c: (c.tier_rank, 1 if c.has_merchant_overlap else 0, c.confidence, -c.date_diff, -c.amt_diff),
        reverse=True,
    )
    return candidates


def find_receipt_transaction_candidates(
    db: Session,
    receipt: Receipt,
    limit: int = 5,
) -> list[TransactionCandidateRead]:
    """Return top sorted candidate matching transactions for a receipt."""
    scored = _evaluate_receipt_candidates(db, receipt)
    results: list[TransactionCandidateRead] = []
    for item in scored[:limit]:
        tx = item.tx
        results.append(
            TransactionCandidateRead(
                transaction_id=tx.id,
                transaction_date=tx.transaction_date,
                merchant=tx.merchant,
                raw_description=tx.raw_description,
                amount=float(tx.amount),
                currency=tx.currency,
                account_id=tx.account_id,
                account_name=tx.account.name if tx.account else None,
                original_amount=item.original_amount,
                original_currency=item.original_currency,
                estimated_receipt_amount=item.estimated_receipt_amount,
                confidence=item.confidence,
                match_reason=item.match_reason,
            )
        )
    return results


def _select_unambiguous_winner(
    candidates: list[_ScoredCandidate],
) -> Optional[_ScoredCandidate]:
    """Return single unambiguous auto-match winner, or None if tied/empty."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    def rank_key(c: _ScoredCandidate) -> tuple[int, float, int, float]:
        return (
            1 if c.has_merchant_overlap else 0,
            round(c.sim_score, 4),
            -c.date_diff,
            -round(c.amt_diff, 4),
        )

    ranked = sorted(candidates, key=rank_key, reverse=True)
    if rank_key(ranked[0]) > rank_key(ranked[1]):
        return ranked[0]
    return None


def match_receipt_to_transaction(receipt: Receipt, db: Session) -> Optional[Transaction]:
    """Match a receipt unambiguously via same-currency or raw_description FX, else flag needs_review/unlinked."""
    if receipt.transaction_id is not None:
        return receipt.transaction

    candidates = _evaluate_receipt_candidates(db, receipt)
    tier_a = [c for c in candidates if c.match_reason == "exact_same_currency"]
    tier_b = [
        c
        for c in candidates
        if c.match_reason == "original_fx_description" and c.has_merchant_overlap
    ]

    winner = _select_unambiguous_winner(tier_a)
    if winner is None and not tier_a:
        winner = _select_unambiguous_winner(tier_b)

    if winner is not None:
        receipt.transaction_id = winner.tx.id
        receipt.status = "matched"
        db.commit()
        db.refresh(receipt)
        return winner.tx

    if receipt.status != "verified":
        receipt.status = "needs_review" if candidates else "unlinked"
        db.commit()
        db.refresh(receipt)

    return None


def reconcile_unlinked_receipts(db: Session) -> int:
    """Evaluate all receipts without a linked transaction (unlinked + needs_review)."""
    unlinked = db.scalars(
        select(Receipt).where(Receipt.transaction_id.is_(None))
    ).all()

    matched_count = 0
    for receipt in unlinked:
        if match_receipt_to_transaction(receipt, db):
            matched_count += 1

    return matched_count


def _normalize_parsed_orders(parsed: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize single-order or multi-order LLM output into a list of valid order dicts."""
    if not isinstance(parsed, dict):
        return []
    raw_orders = parsed.get("orders")
    if isinstance(raw_orders, list):
        candidates = [o for o in raw_orders if isinstance(o, dict)]
    elif "merchant" in parsed and "total_amount" in parsed:
        candidates = [parsed]
    else:
        return []

    valid: list[dict[str, Any]] = []
    for order in candidates:
        merchant = str(order.get("merchant") or "").strip()
        try:
            total_amt = float(order.get("total_amount"))
        except (TypeError, ValueError):
            continue
        if merchant and total_amt >= 0:
            valid.append(order)
    return valid


def _find_duplicate_by_order_number(
    db: Session,
    *,
    order_number: str,
    curr_upper: str,
    total_amount: float,
    document_type: str,
) -> Optional[Receipt]:
    """Match an existing non-payslip Receipt of the same document_type by order_number."""
    by_order = db.scalars(
        select(Receipt).where(
            Receipt.document_type == document_type,
            func.lower(Receipt.order_number) == order_number.lower(),
            func.upper(Receipt.currency) == curr_upper,
        )
    ).all()
    for existing in by_order:
        if abs(float(existing.total_amount) - total_amount) <= 0.05:
            return existing
    return None


def _find_semantic_duplicate_receipt(
    db: Session,
    *,
    merchant: str,
    receipt_date: date,
    total_amount: float,
    currency: str,
    order_number: Optional[str],
    document_type: str = "receipt",
) -> Optional[Receipt]:
    """Return an existing Receipt matching order_number (non-payslip) or (merchant, date +/- 1d, total, currency)."""
    target_key = _normalize_merchant_key(merchant)
    curr_upper = currency.upper()
    if order_number and document_type != "payslip":
        matched = _find_duplicate_by_order_number(
            db, order_number=order_number, curr_upper=curr_upper,
            total_amount=total_amount, document_type=document_type,
        )
        if matched is not None:
            return matched

    nearby = db.scalars(
        select(Receipt).where(
            Receipt.document_type == document_type,
            Receipt.receipt_date >= receipt_date - timedelta(days=1),
            Receipt.receipt_date <= receipt_date + timedelta(days=1),
            func.upper(Receipt.currency) == curr_upper,
        )
    ).all()
    for existing in nearby:
        if abs(float(existing.total_amount) - total_amount) > 0.05:
            continue
        if order_number and existing.order_number and existing.order_number.lower() != order_number.lower():
            continue
        if _normalize_merchant_key(existing.merchant) == target_key:
            return existing
    return None


@dataclass(frozen=True)
class _StoredReceiptMedia:
    """Compressed or raw media bytes retained for Receipt DB persistence."""

    data: bytes
    mime_type: str
    filename: Optional[str]


@dataclass(frozen=True)
class _AttachmentParseOutcome:
    """Outcome of attachment-first receipt extraction including fallback media."""

    orders: list[dict[str, Any]]
    stored_media: Optional[_StoredReceiptMedia]
    fallback_attachment: Optional[_StoredReceiptMedia]
    raw_text: Optional[str]


def _resolve_email_fallback_date(received_at: Optional[datetime], local_tz: ZoneInfo) -> date:
    """Resolve local fallback date from email received_at timestamp or current time."""
    if not received_at:
        return datetime.now(local_tz).date()
    aware = received_at.replace(tzinfo=timezone.utc) if received_at.tzinfo is None else received_at
    return aware.astimezone(local_tz).date()


def _compress_stored_media(media: _StoredReceiptMedia) -> _StoredReceiptMedia:
    """Compress image attachment bytes to WebP while keeping PDF bytes raw."""
    comp_bytes, comp_mime = compress_receipt_image(media.data, media.mime_type)
    return _StoredReceiptMedia(comp_bytes, comp_mime, media.filename)


def _decode_valid_email_attachment(
    att: Any,
    *,
    min_pdf_bytes: int = MIN_RECEIPT_ATTACHMENT_BYTES,
    min_image_bytes: int = MIN_RECEIPT_ATTACHMENT_BYTES,
) -> Optional[tuple[bytes, str]]:
    """Decode base64 attachment and enforce PDF/image MIME and minimum byte thresholds."""
    if not att.content_base64:
        return None
    try:
        raw_bytes = base64.b64decode(att.content_base64, validate=False)
    except Exception:
        return None
    mime_type = infer_receipt_mime_type(att.filename, att.content_type)
    if mime_type == "application/pdf":
        return (raw_bytes, mime_type) if len(raw_bytes) >= min_pdf_bytes else None
    if mime_type.startswith("image/"):
        return (raw_bytes, mime_type) if len(raw_bytes) >= min_image_bytes else None
    return None


def _parse_single_attachment_doc(
    raw_bytes: bytes,
    filename: Optional[str],
    mime_type: str,
    *,
    text_parser: Callable[[str], Optional[dict[str, Any]]],
    fallback_parser: Callable[[bytes, Optional[str], str], Optional[dict[str, Any]]],
    pdf_vision_fallback: Optional[Callable[[bytes], Optional[dict[str, Any]]]] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Parse one PDF or image attachment once via text-first then vision/fallback parser."""
    if mime_type != "application/pdf":
        return fallback_parser(raw_bytes, filename, mime_type), None
    pdf_text = extract_pdf_text(raw_bytes)
    raw_excerpt = pdf_text[:32000] if pdf_text else None
    if pdf_text:
        parsed_att = text_parser(pdf_text)
        if parsed_att:
            return parsed_att, raw_excerpt
        if pdf_vision_fallback is not None:
            return pdf_vision_fallback(raw_bytes), raw_excerpt
    return fallback_parser(raw_bytes, filename, mime_type), raw_excerpt


def _extract_from_valid_attachments(
    attachments: list[Any],
    *,
    min_pdf_bytes: int,
    attachment_parser: Callable[
        [bytes, Optional[str], str], tuple[Optional[dict[str, Any]], Optional[str]]
    ],
    normalizer: Callable[[Optional[dict[str, Any]]], Any],
) -> tuple[Any, Optional[_StoredReceiptMedia], Optional[_StoredReceiptMedia], Optional[str]]:
    """Extract normalized payload from valid attachments and retain first attachment fallback."""
    fallback_attachment: Optional[_StoredReceiptMedia] = None
    raw_text_for_record: Optional[str] = None

    for att in attachments:
        decoded = _decode_valid_email_attachment(
            att, min_pdf_bytes=min_pdf_bytes, min_image_bytes=MIN_RECEIPT_ATTACHMENT_BYTES
        )
        if decoded is None:
            continue
        raw_bytes, mime_type = decoded
        media = _StoredReceiptMedia(raw_bytes, mime_type, att.filename)
        if fallback_attachment is None:
            fallback_attachment = media

        parsed_att, pdf_excerpt = attachment_parser(raw_bytes, att.filename, mime_type)
        if pdf_excerpt:
            raw_text_for_record = pdf_excerpt
        normalized = normalizer(parsed_att)
        if normalized:
            return normalized, _compress_stored_media(media), fallback_attachment, raw_text_for_record

    return None, None, fallback_attachment, raw_text_for_record


def _parse_single_attachment(
    raw_bytes: bytes,
    filename: Optional[str],
    mime_type: str,
    *,
    gateway: AiGateway | None = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Parse one PDF or image receipt attachment via text-first then vision LLM."""
    return _parse_single_attachment_doc(
        raw_bytes,
        filename,
        mime_type,
        text_parser=lambda txt: parse_receipt_orders_text_with_llm(
            txt, raise_on_error=False, gateway=gateway
        ),
        fallback_parser=lambda b, f, m: parse_receipt_with_llm(b, f, m, gateway=gateway),
    )


def _extract_receipt_orders_from_attachments(
    attachments: list[Any],
    *,
    gateway: AiGateway | None = None,
) -> _AttachmentParseOutcome:
    """Parse orders from valid image/PDF attachments and retain first valid attachment for fallback."""
    extracted, stored_media, fallback_att, raw_text = _extract_from_valid_attachments(
        attachments,
        min_pdf_bytes=MIN_RECEIPT_ATTACHMENT_BYTES,
        attachment_parser=lambda b, f, m: _parse_single_attachment(b, f, m, gateway=gateway),
        normalizer=_normalize_parsed_orders,
    )
    return _AttachmentParseOutcome(
        orders=extracted or [],
        stored_media=stored_media,
        fallback_attachment=fallback_att,
        raw_text=raw_text,
    )


def _create_receipt_items_for_order(
    db: Session,
    *,
    receipt: Receipt,
    raw_items: list[dict[str, Any]],
    active_rules: list[CategorizationRule],
    existing_tags: list[str],
    extra_categories: list[str],
    gateway: AiGateway | None = None,
) -> None:
    """Apply discount folding and persist classified line items for one ingested order."""
    for it in apply_receipt_discounts_with_jev(raw_items, gateway=gateway):
        create_classified_receipt_item(
            db,
            receipt=receipt,
            item_name=str(it.get("item_name") or "Item"),
            brand=it.get("brand"),
            quantity=float(it.get("quantity", 1.0) or 1.0),
            unit_price=float(it.get("unit_price", 0.0) or 0.0),
            discount_amount=(
                float(it["discount_amount"]) if it.get("discount_amount") is not None else None
            ),
            total_price=float(it.get("total_price", 0.0) or 0.0),
            explicit_category=it.get("category"),
            explicit_domain=it.get("domain"),
            explicit_product_type=it.get("product_type"),
            explicit_volume_ml=it.get("volume_ml"),
            explicit_tags=it.get("tags"),
            notes=it.get("notes"),
            active_rules=active_rules,
            existing_tags=existing_tags,
            extra_categories=extra_categories,
            gateway=gateway,
        )


def _resolve_order_date(raw_date: Any, fallback_date: date) -> date:
    """Parse an ISO order receipt_date or fall back to the resolved email date."""
    try:
        return date.fromisoformat(str(raw_date or ""))
    except ValueError:
        return fallback_date


def _persist_ingested_order(
    db: Session,
    *,
    order: dict[str, Any],
    fallback_date: date,
    message_id: str,
    raw_text: Optional[str],
    stored_media: Optional[_StoredReceiptMedia],
    active_rules: list[CategorizationRule],
    existing_tags: list[str],
    extra_categories: list[str],
    gateway: AiGateway | None = None,
) -> Receipt:
    """Persist one parsed email receipt order (with semantic dedup) and match to transaction."""
    merchant = str(order.get("merchant") or "Unknown Merchant").strip()[:150]
    order_number = normalize_order_number(order.get("order_number"))
    currency = normalize_receipt_currency(order.get("currency"), fallback="NZD")
    total_amount = round(float(order.get("total_amount", 0.0) or 0.0), 2)
    tax_amount = round(float(order["tax_amount"]), 2) if order.get("tax_amount") is not None else None
    r_date = _resolve_order_date(order.get("receipt_date"), fallback_date)

    existing_dup = _find_semantic_duplicate_receipt(
        db,
        merchant=merchant,
        receipt_date=r_date,
        total_amount=total_amount,
        currency=currency,
        order_number=order_number,
    )
    if existing_dup is not None:
        return existing_dup

    receipt = Receipt(
        receipt_date=r_date,
        merchant=merchant,
        total_amount=total_amount,
        currency=currency,
        tax_amount=tax_amount,
        order_number=order_number,
        source_email_message_id=message_id[:512],
        status="unlinked",
        is_processed=True,
        raw_text=raw_text,
        image_data=stored_media.data if stored_media else None,
        image_mime_type=stored_media.mime_type if stored_media else None,
        image_filename=stored_media.filename[:255] if stored_media and stored_media.filename else None,
    )
    db.add(receipt)
    db.flush()
    _create_receipt_items_for_order(
        db,
        receipt=receipt,
        raw_items=order.get("items") or [],
        active_rules=active_rules,
        existing_tags=existing_tags,
        extra_categories=extra_categories,
        gateway=gateway,
    )
    db.commit()
    db.refresh(receipt)
    match_receipt_to_transaction(receipt, db)
    return receipt


def _extract_orders_from_email_body(
    payload: InboundEmailDispatchPayload,
    *,
    gateway: AiGateway | None = None,
) -> tuple[list[dict[str, Any]], date, str]:
    """Sanitize email HTML/text body and extract purchase orders via text LLM fallback."""
    sanitized_text, sanitized_date = sanitize_receipt_email_content(
        subject=payload.subject or "",
        from_address=payload.from_address,
        received_at=payload.received_at,
        text_body=payload.text_body,
        html_excerpt=payload.html_excerpt,
        raw_excerpt=payload.raw_excerpt,
    )
    if not sanitized_text:
        raise ValueError(
            "Purchase receipt email contains no readable receipt content or valid attachment."
        )
    parsed_body = parse_receipt_orders_text_with_llm(
        sanitized_text, raise_on_error=True, gateway=gateway
    )
    return _normalize_parsed_orders(parsed_body), sanitized_date, sanitized_text[:32000]


def ingest_purchase_receipt_email(
    db: Session,
    payload: InboundEmailDispatchPayload,
    *,
    gateway: AiGateway | None = None,
) -> list[Receipt]:
    """Ingest a purchase receipt email (attachment-first or HTML/text, 1 or N orders)."""
    local_tz = ZoneInfo(settings.timezone)
    fallback_date = _resolve_email_fallback_date(payload.received_at, local_tz)
    outcome = _extract_receipt_orders_from_attachments(payload.attachments, gateway=gateway)
    orders, stored_media, raw_text_for_record = (
        outcome.orders,
        outcome.stored_media,
        outcome.raw_text,
    )

    if not orders:
        orders, fallback_date, raw_text_for_record = _extract_orders_from_email_body(
            payload, gateway=gateway
        )
        if orders and stored_media is None and outcome.fallback_attachment is not None:
            stored_media = _compress_stored_media(outcome.fallback_attachment)

    if not orders:
        raise ValueError("Could not extract any purchase orders from receipt email.")

    active_rules, existing_tags, extra_categories = load_receipt_rule_context(db)
    return [
        _persist_ingested_order(
            db,
            order=order,
            fallback_date=fallback_date,
            message_id=payload.message_id,
            raw_text=raw_text_for_record,
            stored_media=stored_media,
            active_rules=active_rules,
            existing_tags=existing_tags,
            extra_categories=extra_categories,
            gateway=gateway,
        )
        for order in orders
    ]


@dataclass(frozen=True)
class _NormalizedPayslip:
    """Validated structured payslip fields ready for Receipt persistence."""

    employer: str
    pay_date: date
    net_pay: float
    currency: str
    tax_amount: Optional[float]
    pay_period_ref: Optional[str]
    notes: Optional[str]


@dataclass(frozen=True)
class _PayslipAttachmentOutcome:
    """Outcome of attachment-first payslip extraction including fallback media."""

    payslip: Optional[_NormalizedPayslip]
    stored_media: Optional[_StoredReceiptMedia]
    fallback_attachment: Optional[_StoredReceiptMedia]
    raw_text: Optional[str]


def _clean_payslip_text(value: Any) -> Optional[str]:
    """Return stripped text or None for empty/sentinel LLM string values."""
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned or cleaned.lower() in ("null", "none", "n/a"):
        return None
    return cleaned


def _derive_payslip_period_fields(
    parsed: dict[str, Any],
) -> tuple[Optional[str], Optional[str]]:
    """Build canonical pay_period_ref (order_number) and human summary notes."""
    start_str = _clean_payslip_text(parsed.get("pay_period_start"))
    end_str = _clean_payslip_text(parsed.get("pay_period_end"))
    label = _clean_payslip_text(parsed.get("pay_period_label"))

    range_str = f"{start_str} to {end_str}" if (start_str and end_str) else (end_str or start_str)
    pay_period_ref = normalize_order_number(range_str or label)

    if label and range_str and label.lower() != range_str.lower():
        notes = f"Pay period: {label} ({range_str})"
    elif label or range_str:
        notes = f"Pay period: {label or range_str}"
    else:
        notes = None
    return pay_period_ref, notes


def _normalize_parsed_payslip(
    parsed: Any,
    *,
    fallback_date: date,
    fallback_currency: str = "NZD",
) -> Optional[_NormalizedPayslip]:
    """Validate employer non-empty and net_pay > 0, returning immutable payslip fields."""
    if not isinstance(parsed, dict):
        return None
    employer = str(parsed.get("employer") or "").strip()[:150]
    if not employer:
        return None

    try:
        net_pay = round(float(parsed.get("net_pay")), 2)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(net_pay) or net_pay <= 0:
        return None

    tax_amount: Optional[float] = None
    raw_tax = parsed.get("tax_amount")
    if raw_tax is not None and str(raw_tax).strip() != "":
        try:
            parsed_tax = round(float(raw_tax), 2)
        except (TypeError, ValueError):
            parsed_tax = None
        if parsed_tax is not None and math.isfinite(parsed_tax) and parsed_tax >= 0:
            tax_amount = parsed_tax

    pay_period_ref, notes = _derive_payslip_period_fields(parsed)
    return _NormalizedPayslip(
        employer=employer,
        pay_date=_resolve_order_date(parsed.get("pay_date"), fallback_date),
        net_pay=net_pay,
        currency=normalize_receipt_currency(parsed.get("currency"), fallback=fallback_currency),
        tax_amount=tax_amount,
        pay_period_ref=pay_period_ref,
        notes=notes,
    )


def apply_parsed_payslip_fields(
    receipt: Receipt,
    parsed: Optional[dict[str, Any]],
    db: Session,
) -> bool:
    """Populate header fields on an existing payslip Receipt while skipping line items."""
    normalized = _normalize_parsed_payslip(
        parsed,
        fallback_date=receipt.receipt_date or date.today(),
        fallback_currency=receipt.currency or "NZD",
    )
    if normalized is None:
        return False

    receipt.document_type = "payslip"
    receipt.receipt_date = normalized.pay_date
    receipt.merchant = normalized.employer
    receipt.total_amount = normalized.net_pay
    receipt.currency = normalized.currency
    if normalized.pay_period_ref:
        receipt.order_number = normalized.pay_period_ref
    receipt.tax_amount = normalized.tax_amount
    receipt.notes = normalized.notes
    receipt.is_processed = True
    if receipt.items:
        receipt.items.clear()
        db.flush()
    return True


def _parse_single_payslip_attachment(
    raw_bytes: bytes,
    filename: Optional[str],
    mime_type: str,
    *,
    gateway: AiGateway | None = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Parse one PDF or image payslip attachment once via text-first then vision LLM."""
    return _parse_single_attachment_doc(
        raw_bytes,
        filename,
        mime_type,
        text_parser=lambda txt: parse_payslip_text_with_llm(
            txt, raise_on_error=False, gateway=gateway
        ),
        fallback_parser=lambda b, f, m: parse_payslip_with_llm(b, f, m, gateway=gateway),
        pdf_vision_fallback=lambda b: _invoke_vision_prompt(
            b, "application/pdf", PAYSLIP_PARSING_PROMPT, gateway=gateway
        ),
    )


def _extract_payslip_from_attachments(
    attachments: list[Any],
    fallback_date: date,
    *,
    gateway: AiGateway | None = None,
) -> _PayslipAttachmentOutcome:
    """Parse payslip fields from valid PDF/image attachments and retain first attachment fallback."""
    normalized, stored_media, fallback_att, raw_text = _extract_from_valid_attachments(
        attachments,
        min_pdf_bytes=MIN_PAYSLIP_PDF_BYTES,
        attachment_parser=lambda b, f, m: _parse_single_payslip_attachment(
            b, f, m, gateway=gateway
        ),
        normalizer=lambda parsed: _normalize_parsed_payslip(parsed, fallback_date=fallback_date),
    )
    return _PayslipAttachmentOutcome(
        payslip=normalized,
        stored_media=stored_media,
        fallback_attachment=fallback_att,
        raw_text=raw_text,
    )


def _extract_payslip_from_email_body(
    sanitized_text: str,
    fallback_date: date,
    *,
    gateway: AiGateway | None = None,
) -> tuple[_NormalizedPayslip, str]:
    """Parse payslip fields from sanitized email body text via text LLM fallback."""
    if not sanitized_text:
        raise ValueError(
            "Payslip email contains no readable payslip content or valid attachment."
        )
    parsed_body = parse_payslip_text_with_llm(
        sanitized_text, raise_on_error=True, gateway=gateway
    )
    normalized = _normalize_parsed_payslip(parsed_body, fallback_date=fallback_date)
    if normalized is None:
        raise ValueError(
            "Payslip extraction failed: missing valid employer and positive net_pay."
        )
    return normalized, sanitized_text[:32000]


def _persist_payslip_receipt(
    db: Session,
    *,
    normalized: _NormalizedPayslip,
    message_id: str,
    raw_text: Optional[str],
    stored_media: Optional[_StoredReceiptMedia],
) -> Receipt:
    """Create a payslip Receipt row (zero items/products) and trigger inflow matching."""
    receipt = Receipt(
        document_type="payslip",
        receipt_date=normalized.pay_date,
        merchant=normalized.employer,
        total_amount=normalized.net_pay,
        currency=normalized.currency,
        tax_amount=normalized.tax_amount,
        order_number=normalized.pay_period_ref,
        source_email_message_id=message_id[:512],
        status="unlinked",
        is_processed=True,
        raw_text=raw_text,
        notes=normalized.notes,
        image_data=stored_media.data if stored_media else None,
        image_mime_type=stored_media.mime_type if stored_media else None,
        image_filename=(
            stored_media.filename[:255] if stored_media and stored_media.filename else None
        ),
    )
    db.add(receipt)
    db.commit()
    db.refresh(receipt)
    match_receipt_to_transaction(receipt, db)
    return receipt


def ingest_payslip_email(
    db: Session,
    payload: InboundEmailDispatchPayload,
    *,
    gateway: AiGateway | None = None,
) -> Receipt:
    """Ingest a payslip email (attachment-first, zero ReceiptItem/Product rows, inflow matching)."""
    sanitized_body, fallback_date = sanitize_receipt_email_content(
        subject=payload.subject or "",
        from_address=payload.from_address,
        received_at=payload.received_at,
        text_body=payload.text_body,
        html_excerpt=payload.html_excerpt,
        raw_excerpt=payload.raw_excerpt,
    )
    outcome = _extract_payslip_from_attachments(
        payload.attachments, fallback_date, gateway=gateway
    )
    normalized, stored_media, raw_text = outcome.payslip, outcome.stored_media, outcome.raw_text

    if normalized is None:
        normalized, raw_text = _extract_payslip_from_email_body(
            sanitized_body, fallback_date, gateway=gateway
        )
        if stored_media is None and outcome.fallback_attachment is not None:
            stored_media = _compress_stored_media(outcome.fallback_attachment)

    existing_dup = _find_semantic_duplicate_receipt(
        db, merchant=normalized.employer, receipt_date=normalized.pay_date,
        total_amount=normalized.net_pay, currency=normalized.currency,
        order_number=normalized.pay_period_ref, document_type="payslip",
    )
    if existing_dup is not None:
        return existing_dup

    return _persist_payslip_receipt(
        db, normalized=normalized, message_id=payload.message_id,
        raw_text=raw_text, stored_media=stored_media,
    )


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
