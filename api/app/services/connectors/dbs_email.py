"""Hardened DBS Singapore transaction email alert parser and account router."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
import logging
import re
from typing import Any, Callable, Optional
import urllib.request

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Account
from app.schemas import DbsAlertPayload, DbsAlertResult
from app.services.connectors import ConnectorUpstreamError
from app.services.email_utils import (
    SGT_TZ,
    extract_and_strip_forwarded_header,
    html_to_plain_text,
)
from app.services.ingestion_service import (
    ExternalAccountSpec,
    NormalizedTx,
    get_or_create_external_account,
    ingest_normalized_transactions_detailed,
)

logger = logging.getLogger(__name__)

AMOUNT_PATTERN = re.compile(
    r"(?:\b(SGD|USD|AUD|NZD|EUR|GBP|JPY|MYR)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)\b|"
    r"(S\$)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?))",
    re.IGNORECASE,
)
BARE_AMOUNT_PATTERN = re.compile(r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
CURRENCY_PATTERN = re.compile(
    r"\b(SGD|USD|AUD|NZD|EUR|GBP|JPY|MYR)\b|S\$",
    re.IGNORECASE,
)
MERCHANT_PATTERN = re.compile(
    r"(?:\bat\b|\bto\b|\bfrom\b)\s+([A-Za-z0-9 &.,'/\-]{2,80}?)"
    r"(?=\s+\bon\b|\s+via\b|\s+with\s+your\b|\s+using\b|\s+ending\b|\s+ref\b|\.|,|\n|$)",
    re.IGNORECASE,
)
DATE_WITH_YEAR_RE = re.compile(
    r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}"
    r"|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})",
    re.IGNORECASE,
)
DATE_YEARLESS_RE = re.compile(
    r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*)\b",
    re.IGNORECASE,
)

CREDIT_RE = re.compile(
    r"\b(?:received|credited|incoming|refund|deposit)\b",
    re.IGNORECASE,
)
DEBIT_RE = re.compile(
    r"\b(?:outgoing|debited|debit|card\s+txn|card\s+transaction|bill\s+payment|"
    r"fast\s+transfer|payment\s+to|transferred|purchase|spent|charged|withdrawal)\b",
    re.IGNORECASE,
)
STRUCTURED_FIELD_RE = re.compile(
    r"^\s*(Transaction\s+Ref(?:erence)?|Ref(?:erence)?(?:\s+No\.?)?|"
    r"Date\s*(?:&|and|/)\s*Time|Date|"
    r"Amount|From(?:\s+Account)?|To(?:\s+Account)?|Card|Account|Merchant|Description)\s*:\s*(.*)$",
    re.IGNORECASE,
)
ACCOUNT_NUMBER_TOKEN_RE = re.compile(r"\b[0-9Xx*]+(?:[-\s][0-9Xx*]+){1,4}\b")
EXPLICIT_SUFFIX_RE = re.compile(
    r"(?:"
    r"(?:\b(?:a/c|account|card)\b(?:\s+(?:no\.?|number|ending(?:\s+(?:in|with))?))?)"
    r"|(?:\bending(?:\s+(?:in|with))?\b)"
    r"|(?:[*Xx]{2,})"
    r")"
    r"[:\s#-]*((?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,8})\b",
    re.IGNORECASE,
)

_OWN_DBS_KEYWORDS = (
    "dbs",
    "posb",
    "cashline",
    "live fresh",
    "multiplier",
    "my account",
    "own account",
    "card ending",
    "a/c ending",
)


@dataclass(frozen=True)
class ParsedDbsAlert:
    """Structured parse output for a DBS email alert."""

    normalized: NormalizedTx
    account_hint: str
    counterparty_hint: str
    direction: str
    tx_ref: Optional[str]
    cleaned_combined: str = ""


def _parse_alert_date(
    text: str,
    ref_sgt_dt: datetime,
) -> date:
    """Extract transaction date in Asia/Singapore, inferring year for yearless DBS timestamps."""
    match_full = DATE_WITH_YEAR_RE.search(text)
    if match_full:
        raw = match_full.group(1).strip()
        for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue

    match_short = DATE_YEARLESS_RE.search(text)
    if match_short:
        raw = match_short.group(1).strip()
        for fmt in ("%d %b", "%d %B"):
            try:
                parsed = datetime.strptime(raw, fmt)
                candidate = parsed.replace(year=ref_sgt_dt.year).date()
                if candidate > ref_sgt_dt.date() + timedelta(days=30):
                    candidate = candidate.replace(year=ref_sgt_dt.year - 1)
                elif candidate < ref_sgt_dt.date() - timedelta(days=330):
                    candidate = candidate.replace(year=ref_sgt_dt.year + 1)
                return candidate
            except ValueError:
                continue

    return ref_sgt_dt.date()


def _parse_structured_fields(cleaned_body: str) -> dict[str, str]:
    """Extract key-value fields from structured DBS notification templates."""
    fields: dict[str, str] = {}
    lines = [line.strip() for line in cleaned_body.splitlines() if line.strip()]
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        m = STRUCTURED_FIELD_RE.match(line)
        if m:
            raw_key = m.group(1).strip().lower()
            val = m.group(2).strip()
            if not val and idx + 1 < len(lines) and not STRUCTURED_FIELD_RE.match(lines[idx + 1]):
                val = lines[idx + 1].strip()
                idx += 1
            if raw_key.startswith("transaction ref") or raw_key.startswith("ref"):
                fields.setdefault("tx_ref", val)
            elif "time" in raw_key or raw_key == "date":
                fields.setdefault("date_time", val)
            elif raw_key == "amount":
                fields.setdefault("amount", val)
            elif raw_key.startswith("from"):
                fields.setdefault("from", val)
            elif raw_key.startswith("to"):
                fields.setdefault("to", val)
            elif raw_key in ("card", "account"):
                fields.setdefault("account", val)
            elif raw_key in ("merchant", "description"):
                fields.setdefault("merchant", val)
        idx += 1
    return fields


def _extract_amount_and_currency(
    amount_field: Optional[str],
    combined: str,
) -> Optional[tuple[float, str]]:
    """Extract positive monetary amount and normalized 3-letter ISO currency."""
    search_targets = [amount_field, combined] if amount_field else [combined]
    for target in search_targets:
        m = AMOUNT_PATTERN.search(target)
        if m:
            if m.group(1) and m.group(2):
                currency = m.group(1).upper()
                amount = float(m.group(2).replace(",", ""))
                return amount, currency
            if m.group(3) and m.group(4):
                amount = float(m.group(4).replace(",", ""))
                return amount, "SGD"

    if amount_field:
        bare = BARE_AMOUNT_PATTERN.search(amount_field)
        if bare:
            cur_m = CURRENCY_PATTERN.search(combined)
            currency = "SGD"
            if cur_m:
                tok = cur_m.group(0).upper()
                currency = "SGD" if tok == "S$" else tok
            return float(bare.group(1).replace(",", "")), currency
    return None


def _text_matches_suffix(text: str, suffix: str, *, strict_marker: bool = False) -> bool:
    """Return True if text contains account suffix via explicit marker or standalone/masked token."""
    clean_suffix = re.sub(r"[^A-Za-z0-9]", "", suffix).lower()
    if not clean_suffix:
        return False

    lowered = text.lower()
    for m in EXPLICIT_SUFFIX_RE.finditer(lowered):
        tok = re.sub(r"[^A-Za-z0-9]", "", m.group(1))
        if tok == clean_suffix or tok.endswith(clean_suffix):
            return True

    for m in ACCOUNT_NUMBER_TOKEN_RE.finditer(lowered):
        digits = re.sub(r"[^0-9]", "", m.group(0))
        if digits and (digits == clean_suffix or digits.endswith(clean_suffix)):
            return True

    if not strict_marker:
        digit_compact = re.sub(r"(?<=\d)[\s-]+(?=\d)", "", lowered)
        if re.search(rf"(?<![a-z0-9]){re.escape(clean_suffix)}(?![a-z0-9])", digit_compact):
            return True
    return False


def _looks_like_own_account(text: str, own_suffixes: set[str]) -> bool:
    """Return True when a From/To field references an internal own DBS/bank account."""
    if not text:
        return False
    lowered = text.lower()
    if any(kw in lowered for kw in _OWN_DBS_KEYWORDS):
        return True
    return any(_text_matches_suffix(text, s, strict_marker=False) for s in own_suffixes)


def _extract_legacy_merchant(body: str, subject: str) -> str:
    """Extract counterparty name from unstructured legacy DBS alert sentences."""
    m = MERCHANT_PATTERN.search(body) or MERCHANT_PATTERN.search(subject)
    if m:
        candidate = m.group(1).strip(" .,:;")
        if candidate:
            return candidate[:150]
    return (subject.strip()[:150] or "DBS Transaction")


def _invoke_dbs_llm_fallback(
    subject: str,
    body: str,
    ref_sgt_dt: datetime,
) -> Optional[dict[str, Any]]:
    """Call Muninn LLM gateway as a fail-loud fallback for unfamiliar DBS alert templates."""
    base_url = settings.resolved_llm_base_url
    api_key = settings.resolved_llm_api_key
    if not base_url or not api_key:
        return None

    url = f"{base_url.rstrip('/')}/chat/completions"
    prompt = (
        "Extract DBS bank alert transaction fields strictly as JSON:\n"
        '{"is_transaction": true/false, "amount": 0.00, "currency": "SGD", '
        '"direction": "debit" or "credit", "merchant": "string", '
        '"transaction_date": "YYYY-MM-DD", "account_hint": "string or null", '
        '"transaction_ref": "string or null", "is_transfer": true/false}\n'
        f"Reference SGT date: {ref_sgt_dt.date().isoformat()}.\n"
        "Set is_transaction=false for OTP notices, login alerts, or marketing emails."
    )
    payload = {
        "model": settings.resolved_llm_model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Subject: {subject}\n\n{body[:4000]}"},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "Yggdrasil-Plutus/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body_json = json.loads(resp.read().decode("utf-8"))
            content = body_json["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            parsed = json.loads(content)
            return parsed if isinstance(parsed, dict) else None
    except Exception as exc:
        raise ConnectorUpstreamError(f"DBS alert LLM fallback failed: {exc}") from exc


def _collect_db_own_suffixes(db: Optional[Session]) -> set[str]:
    """Collect configured account_number_last4 suffixes from active DB accounts."""
    if db is None:
        return set()
    rows = db.scalars(
        select(Account.account_number_last4).where(
            Account.is_active == True,
            Account.account_number_last4.isnot(None),
        )
    ).all()
    suffixes: set[str] = set()
    for raw in rows:
        if not raw:
            continue
        for part in str(raw).split(","):
            cleaned = part.strip().lower()
            if cleaned:
                suffixes.add(cleaned)
    return suffixes


def parse_dbs_alert_details(
    subject: str,
    text: str,
    *,
    received_at: Optional[datetime] = None,
    own_suffixes: Optional[set[str]] = None,
) -> ParsedDbsAlert:
    """Parse structured or legacy DBS alert emails with SGT timezone and self-transfer detection."""
    clean_subject, cleaned_body, ref_sgt_dt = extract_and_strip_forwarded_header(
        subject, text, received_at, target_tz=SGT_TZ
    )
    combined = f"{clean_subject}\n{cleaned_body}".strip()
    fields = _parse_structured_fields(cleaned_body)

    resolved_own_suffixes: set[str] = set(settings.resolved_own_account_suffixes())
    resolved_own_suffixes.update(k.lower() for k in settings.resolved_dbs_account_map())
    if own_suffixes:
        resolved_own_suffixes.update(s.lower() for s in own_suffixes if s)

    amt_res = _extract_amount_and_currency(fields.get("amount"), combined)
    if amt_res is None:
        llm_data = _invoke_dbs_llm_fallback(clean_subject, cleaned_body, ref_sgt_dt)
        if not llm_data or not llm_data.get("is_transaction") or not llm_data.get("amount"):
            raise ValueError("Could not extract transaction amount from DBS alert email.")
        raw_amount = abs(float(llm_data["amount"]))
        currency = str(llm_data.get("currency") or "SGD").strip().upper()[:3]
        direction = (
            "credit"
            if str(llm_data.get("direction") or "").strip().lower() == "credit"
            else "debit"
        )
        signed_amount = raw_amount if direction == "credit" else -raw_amount
        merchant = str(llm_data.get("merchant") or clean_subject or "DBS Transaction").strip()[:150]
        raw_date = llm_data.get("transaction_date")
        try:
            tx_date = date.fromisoformat(str(raw_date)) if raw_date else ref_sgt_dt.date()
        except ValueError:
            tx_date = ref_sgt_dt.date()
        tx_ref_raw = llm_data.get("transaction_ref")
        tx_ref = re.sub(r"[^A-Za-z0-9_-]", "", str(tx_ref_raw or "").strip()) or None
        account_hint = str(llm_data.get("account_hint") or "").strip()
        is_self_transfer = bool(llm_data.get("is_transfer")) or any(
            _text_matches_suffix(merchant, s, strict_marker=False) for s in resolved_own_suffixes
        )
        notes = f"dbs:{tx_ref}" if tx_ref else "dbs_email_alert"
        normalized = NormalizedTx(
            transaction_date=tx_date,
            amount=signed_amount,
            currency=currency,
            merchant=merchant,
            category="Transfers" if is_self_transfer else "General",
            raw_description=f"{clean_subject} | {cleaned_body[:240]}".strip(),
            notes=notes,
        )
        return ParsedDbsAlert(
            normalized=normalized,
            account_hint=account_hint,
            counterparty_hint=merchant,
            direction=direction,
            tx_ref=tx_ref,
            cleaned_combined=combined,
        )

    raw_amount, currency = amt_res

    from_val = fields.get("from", "").strip()
    to_val = fields.get("to", "").strip()
    acct_val = fields.get("account", "").strip()
    merchant_val = fields.get("merchant", "").strip()

    has_credit_kw = bool(CREDIT_RE.search(combined))
    has_debit_kw = bool(DEBIT_RE.search(combined))

    if from_val and to_val:
        from_is_own = _looks_like_own_account(from_val, resolved_own_suffixes)
        to_is_own = _looks_like_own_account(to_val, resolved_own_suffixes)
        if (has_credit_kw and not has_debit_kw) or (to_is_own and not from_is_own):
            direction = "credit"
            account_hint = to_val
            counterparty = from_val
        else:
            direction = "debit"
            account_hint = from_val
            counterparty = to_val
    elif from_val:
        direction = "credit" if (has_credit_kw and not has_debit_kw) else "debit"
        account_hint = from_val
        counterparty = merchant_val or to_val or _extract_legacy_merchant(cleaned_body, clean_subject)
    elif to_val:
        direction = "credit" if (has_credit_kw and not has_debit_kw) else "debit"
        account_hint = acct_val or (to_val if direction == "credit" else "")
        counterparty = merchant_val or (
            to_val if direction == "debit" else _extract_legacy_merchant(cleaned_body, clean_subject)
        )
    elif acct_val or merchant_val:
        direction = "credit" if (has_credit_kw and not has_debit_kw) else "debit"
        account_hint = acct_val or cleaned_body
        counterparty = merchant_val or _extract_legacy_merchant(cleaned_body, clean_subject)
    else:
        direction = "credit" if has_credit_kw else "debit"
        account_hint = cleaned_body
        counterparty = _extract_legacy_merchant(cleaned_body, clean_subject)

    signed_amount = abs(raw_amount) if direction == "credit" else -abs(raw_amount)
    date_source = fields.get("date_time") or cleaned_body
    tx_date = _parse_alert_date(date_source, ref_sgt_dt)

    raw_ref = fields.get("tx_ref")
    tx_ref = re.sub(r"[^A-Za-z0-9_-]", "", raw_ref.strip()) if raw_ref else None
    if tx_ref == "":
        tx_ref = None

    merchant = (counterparty or clean_subject or "DBS Transaction").strip()[:150]
    is_self_transfer = any(
        _text_matches_suffix(counterparty, s, strict_marker=False)
        for s in resolved_own_suffixes
    ) or (
        bool(from_val and to_val)
        and _looks_like_own_account(from_val, resolved_own_suffixes)
        and _looks_like_own_account(to_val, resolved_own_suffixes)
    )

    notes = f"dbs:{tx_ref}" if tx_ref else "dbs_email_alert"
    normalized = NormalizedTx(
        transaction_date=tx_date,
        amount=signed_amount,
        currency=currency,
        merchant=merchant,
        category="Transfers" if is_self_transfer else "General",
        raw_description=f"{clean_subject} | {cleaned_body[:240]}".strip(),
        notes=notes,
    )
    return ParsedDbsAlert(
        normalized=normalized,
        account_hint=account_hint,
        counterparty_hint=counterparty,
        direction=direction,
        tx_ref=tx_ref,
        cleaned_combined=combined,
    )


def parse_dbs_alert_text(
    subject: str,
    text: str,
    *,
    received_at: Optional[datetime] = None,
) -> NormalizedTx:
    """Parse amount, currency, merchant, and date from a DBS alert email."""
    return parse_dbs_alert_details(subject, text, received_at=received_at).normalized


_DBS_PRODUCT_MATCHERS: tuple[tuple[tuple[str, ...], Callable[[Account], bool]], ...] = (
    (("cashline",), lambda a: "cashline" in a.name.lower()),
    (
        ("live fresh", "visa", "credit card", "card ending"),
        lambda a: a.account_type == "credit_card"
        or "live fresh" in a.name.lower()
        or "card" in a.name.lower(),
    ),
    (("multiplier",), lambda a: "multiplier" in a.name.lower()),
    (("savings",), lambda a: "savings" in a.name.lower()),
)


def _load_dbs_account_candidates(db: Session, currency: str) -> tuple[list[Account], list[Account]]:
    """Return active currency-matched DBS accounts and active accounts with last-4 suffixes."""
    active_dbs = list(
        db.scalars(
            select(Account)
            .where(Account.institution.ilike("%DBS%"), Account.currency == currency, Account.is_active == True)
            .order_by(Account.id.asc())
        ).all()
    )
    last4_accounts = list(
        db.scalars(
            select(Account)
            .where(Account.account_number_last4.isnot(None), Account.is_active == True)
            .order_by(Account.id.asc())
        ).all()
    )
    return active_dbs, last4_accounts


def _match_dbs_account_in_text(
    db: Session,
    search_text: str,
    *,
    strict_marker: bool,
    dbs_map: dict[str, int],
    all_last4_accounts: list[Account],
    active_dbs_accounts: list[Account],
) -> Optional[Account]:
    """Match a DBS account inside a single text scope via map -> last4 -> product keywords."""
    if not search_text:
        return None
    for suffix, acc_id in dbs_map.items():
        if _text_matches_suffix(search_text, suffix, strict_marker=strict_marker):
            mapped_acc = db.get(Account, acc_id)
            if mapped_acc is not None and mapped_acc.is_active:
                return mapped_acc
    for acc in all_last4_accounts:
        parts = [p.strip() for p in str(acc.account_number_last4 or "").split(",") if p.strip()]
        if any(_text_matches_suffix(search_text, part, strict_marker=strict_marker) for part in parts):
            return acc
    lowered = search_text.lower()
    for keywords, predicate in _DBS_PRODUCT_MATCHERS:
        if any(kw in lowered for kw in keywords):
            match = next((a for a in active_dbs_accounts if predicate(a)), None)
            if match is not None:
                return match
    return None


def _fallback_dbs_account(
    db: Session,
    *,
    currency: str,
    active_dbs_accounts: list[Account],
    explicit_account_name: Optional[str],
) -> Account:
    """Return default DBS savings/first active account or create external dbs_email account."""
    default_savings = next(
        (a for a in active_dbs_accounts if a.account_type != "credit_card" and "cashline" not in a.name.lower()),
        None,
    )
    if default_savings is not None:
        return default_savings
    if active_dbs_accounts:
        return active_dbs_accounts[0]
    return get_or_create_external_account(
        db,
        ExternalAccountSpec(
            provider="dbs_email",
            external_id=f"dbs-{currency.lower()}",
            name=explicit_account_name or f"DBS Singapore ({currency})",
            institution="DBS",
            account_type="checking",
            tier="cash",
            currency=currency,
        ),
    )


def resolve_dbs_account(
    db: Session,
    *,
    currency: str,
    account_hint: str,
    combined_text: str,
    explicit_account_name: Optional[str] = None,
) -> Account:
    """Resolve target DBS account with precedence: explicit name -> account_hint -> combined_text -> default."""
    if explicit_account_name:
        explicit = db.scalar(
            select(Account).where(Account.name.ilike(f"%{explicit_account_name}%"), Account.is_active == True)
        )
        if explicit is not None:
            return explicit
    dbs_map = settings.resolved_dbs_account_map()
    active_dbs, last4_accounts = _load_dbs_account_candidates(db, currency)
    for search_text, strict_flag in ((account_hint, False), (combined_text, True)):
        matched = _match_dbs_account_in_text(
            db,
            search_text,
            strict_marker=strict_flag,
            dbs_map=dbs_map,
            all_last4_accounts=last4_accounts,
            active_dbs_accounts=active_dbs,
        )
        if matched is not None:
            return matched
    return _fallback_dbs_account(
        db, currency=currency, active_dbs_accounts=active_dbs, explicit_account_name=explicit_account_name
    )


def ingest_dbs_alert(
    db: Session,
    payload: DbsAlertPayload,
    *,
    received_at: Optional[datetime] = None,
) -> DbsAlertResult:
    """Parse and commit a forwarded DBS alert email into Plutus."""
    db_own_suffixes = _collect_db_own_suffixes(db)
    parsed = parse_dbs_alert_details(
        payload.subject,
        payload.text,
        received_at=received_at,
        own_suffixes=db_own_suffixes,
    )
    normalized = parsed.normalized
    account = resolve_dbs_account(
        db,
        currency=normalized.currency,
        account_hint=parsed.account_hint,
        combined_text=parsed.cleaned_combined or f"{payload.subject}\n{payload.text}",
        explicit_account_name=payload.account_name,
    )

    created_count, skipped_count, created_txs, matched_existing = (
        ingest_normalized_transactions_detailed(db, account, [normalized])
    )
    if created_count == 0 and skipped_count > 0:
        matched_tx = matched_existing[0] if matched_existing else None
        return DbsAlertResult(status="duplicate", transaction=matched_tx, duplicate=True)

    return DbsAlertResult(
        status="created",
        transaction=created_txs[0] if created_txs else None,
        duplicate=False,
    )
