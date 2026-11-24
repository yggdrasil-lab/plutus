"""Dynamic Category Taxonomy prompt builders and LLM parsing entrypoints."""

from __future__ import annotations

import base64
import logging
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from app.config import settings
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
from app.services.receipt_ingestion._types import (
    PdfTextExtractor,
    ReceiptParseError,
    ReceiptUpstreamError,
)
from app.services.receipt_ingestion.media import extract_pdf_text, prepare_image_for_llm

logger = logging.getLogger(__name__)

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


def build_receipt_category_options(db: Optional[Session] = None) -> str:
    """Build pipe-delimited active expense category options from Category Taxonomy."""
    names = get_canonical_category_names(db, include_system_non_expense=False)
    if not names:
        names = get_canonical_category_names(None, include_system_non_expense=False)
    return " | ".join(names)


def build_receipt_parsing_prompt(db: Optional[Session] = None) -> str:
    """Build the single-receipt extraction prompt with live Category Taxonomy options."""
    category_options = build_receipt_category_options(db)
    return f"""You are a specialized receipt parsing engine.
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
      "category": "{category_options}",
      "domain": "02-skin | 01-health | 20-home | general",
      "product_type": "serum | cleanser | sunscreen | protein | creatine | null",
      "volume_ml": "e.g. 30ml, 1kg, 60 caps or null"
    }}
  ]
}}"""


def build_multi_order_email_receipt_prompt(db: Optional[Session] = None) -> str:
    """Build the multi-order email extraction prompt with live Category Taxonomy options."""
    category_options = build_receipt_category_options(db)
    return f"""You are a specialized e-commerce and purchase receipt extraction engine.
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
          "category": "{category_options}",
          "domain": "02-skin | 01-health | 20-home | general",
          "product_type": "serum | cleanser | sunscreen | protein | creatine | null",
          "volume_ml": "e.g. 30ml, 1kg, 60 caps or null"
        }}
      ]
    }}
  ]
}}"""


def _execute_gateway_call(
    call_fn: Callable[[], AiJsonResponse],
    *,
    label: str,
    raise_on_error: bool,
) -> Optional[dict[str, Any]]:
    try:
        return call_fn().data
    except (AiConfigError, AiInvalidResponseError) as exc:
        logger.warning("LLM %s failed: %s", label, exc)
        if raise_on_error:
            raise ReceiptParseError(f"Receipt LLM {label} failed: {exc}") from exc
        return None
    except AiGatewayError as exc:
        logger.warning("LLM %s upstream failed: %s", label, exc)
        if raise_on_error:
            raise ReceiptUpstreamError(f"Receipt LLM {label} request failed: {exc}") from exc
        return None


def _invoke_text_prompt(
    prompt: str,
    content_label: str,
    raw_text: str,
    *,
    raise_on_error: bool = False,
    gateway: AiGateway | None = None,
) -> Optional[dict[str, Any]]:
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
    return _execute_gateway_call(
        lambda: resolved_gateway.complete_json(request),
        label="text parsing",
        raise_on_error=raise_on_error,
    )


def _invoke_vision_prompt(
    content: bytes,
    mime_type: str,
    prompt: str,
    *,
    raise_on_error: bool = False,
    gateway: AiGateway | None = None,
) -> Optional[dict[str, Any]]:
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
    return _execute_gateway_call(
        lambda: resolved_gateway.complete_vision_json(request),
        label="vision parsing",
        raise_on_error=raise_on_error,
    )


def _is_pdf_document(filename: Optional[str], mime_type: str) -> bool:
    return mime_type == "application/pdf" or (
        filename is not None and filename.lower().endswith(".pdf")
    )


def _decode_utf8_text(content: bytes) -> Optional[str]:
    try:
        raw_text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return raw_text if raw_text.strip() else None


def parse_receipt_text_with_llm(
    raw_text: str,
    *,
    db: Optional[Session] = None,
    raise_on_error: bool = False,
    gateway: AiGateway | None = None,
) -> Optional[dict[str, Any]]:
    """Invoke text LLM to extract structured line items from raw receipt text."""
    return _invoke_text_prompt(
        build_receipt_parsing_prompt(db),
        "Receipt text",
        raw_text,
        raise_on_error=raise_on_error,
        gateway=gateway,
    )


def parse_receipt_orders_text_with_llm(
    raw_text: str,
    *,
    db: Optional[Session] = None,
    raise_on_error: bool = True,
    gateway: AiGateway | None = None,
) -> Optional[dict[str, Any]]:
    """Invoke text LLM to extract one or multiple structured orders from email/document text."""
    return _invoke_text_prompt(
        build_multi_order_email_receipt_prompt(db),
        "Email / Receipt content",
        raw_text,
        raise_on_error=raise_on_error,
        gateway=gateway,
    )


def parse_receipt_with_llm(
    content: bytes,
    filename: Optional[str],
    mime_type: str,
    *,
    db: Optional[Session] = None,
    raise_on_error: bool = False,
    gateway: AiGateway | None = None,
    pdf_text_extractor: Optional[PdfTextExtractor] = None,
) -> Optional[dict[str, Any]]:
    """Extract structured receipt fields from an image, PDF, or text document."""
    if mime_type.startswith("image/"):
        return _invoke_vision_prompt(
            content,
            mime_type,
            build_receipt_parsing_prompt(db),
            raise_on_error=raise_on_error,
            gateway=gateway,
        )

    if _is_pdf_document(filename, mime_type):
        extractor = pdf_text_extractor or extract_pdf_text
        pdf_text = extractor(content)
        if not pdf_text.strip():
            return None
        return parse_receipt_text_with_llm(
            pdf_text,
            db=db,
            raise_on_error=raise_on_error,
            gateway=gateway,
        )

    raw_text = _decode_utf8_text(content)
    if raw_text is None:
        return None
    return parse_receipt_text_with_llm(
        raw_text,
        db=db,
        raise_on_error=raise_on_error,
        gateway=gateway,
    )


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
    raise_on_error: bool = False,
    gateway: AiGateway | None = None,
    pdf_text_extractor: Optional[PdfTextExtractor] = None,
) -> Optional[dict[str, Any]]:
    """Extract structured payslip fields from an image, PDF, or text document."""
    if mime_type.startswith("image/"):
        return _invoke_vision_prompt(
            content,
            mime_type,
            PAYSLIP_PARSING_PROMPT,
            raise_on_error=raise_on_error,
            gateway=gateway,
        )

    if _is_pdf_document(filename, mime_type):
        extractor = pdf_text_extractor or extract_pdf_text
        pdf_text = extractor(content)
        if pdf_text.strip():
            parsed = parse_payslip_text_with_llm(
                pdf_text, raise_on_error=False, gateway=gateway
            )
            if parsed:
                return parsed
        return _invoke_vision_prompt(
            content,
            "application/pdf",
            PAYSLIP_PARSING_PROMPT,
            raise_on_error=raise_on_error,
            gateway=gateway,
        )

    raw_text = _decode_utf8_text(content)
    if raw_text is None:
        return None
    return parse_payslip_text_with_llm(
        raw_text,
        raise_on_error=raise_on_error,
        gateway=gateway,
    )
