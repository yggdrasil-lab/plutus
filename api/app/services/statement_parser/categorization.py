"""Keyword categorization, merchant name cleanup, and institution detection helpers."""

from __future__ import annotations

import re
from typing import Optional

CATEGORIZATION_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:pak\s*n\s*save|countdown|woolworths|new\s*world|four\s*square|costco|supermarket)\b", re.I), "Groceries"),
    (re.compile(r"\b(?:super\s*liquor|liquorland|liquor\s*centre|bottle-?o)\b", re.I), "Alcohol"),
    (re.compile(r"\b(?:lexus|keightley\s*motors|aa\s*battery|novus\s*(?:autoglass|westgate)|vtnz|nzta|mechanic|tyre|wof|panelbeater)\b", re.I), "Vehicle"),
    (re.compile(r"\b(?:bp|z\s*energy|caltex|mobil|fuel)\b", re.I), "Fuel"),
    (re.compile(r"\b(?:at\s*hop|auckland\s*transport|uber|parking|transit)\b", re.I), "Transit"),
    (re.compile(r"\b(?:cafe|coffee|flat\s*white|mcdonald(?:'?s)?|burger|kfc|pizza|bakery|restaurant|sushi|subway|domino)\b", re.I), "Dining"),
    (re.compile(r"\b(?:spark|one\s*nz|vodafone|2degrees|skinny|mercury|contact\s*energy|electric\s*kiwi|watercare|simbatelecom)\b", re.I), "Bills & Telco"),
    (re.compile(r"\b(?:google\s*one|icloud|apple\s*bill|apple\.com/bill|aws|digitalocean|openai|deepseek|cloudflare)\b", re.I), "Software & AI"),
    (re.compile(r"\b(?:barber|haircut|salon|sephora|mecca|skincare|grooming)\b", re.I), "Personal Care"),
    (re.compile(r"\b(?:supplement|protein|creatine|whey)\b", re.I), "Supplements"),
    (re.compile(r"\b(?:gym|fitness|cityfitness|tepid\s*baths|badminton)\b", re.I), "Fitness"),
    (re.compile(r"\b(?:physio|pharmacy|chemist|doctor|dentist|dental|medical|clinic)\b", re.I), "Medical"),
    (re.compile(r"\bhealth\b", re.I), "Health"),
    (re.compile(r"\b(?:barkers|calvin\s*klein|hallenstein|cotton\s*on|uniqlo|nike|adidas)\b", re.I), "Clothing"),
    (re.compile(r"\b(?:pb\s*tech|pb\s*technologies|insta360|jb\s*hi-?fi|noel\s*leeming|apple\s*store)\b", re.I), "Tech"),
    (re.compile(r"\b(?:bunnings|mitre\s*10|briscoes|ikea|nood)\b", re.I), "Household"),
    (re.compile(r"\b(?:amazon|temu|kmart|the\s*warehouse|aliexpress)\b", re.I), "Shopping"),
    (re.compile(r"\b(?:steam|steamgames|playstation|nintendo)\b", re.I), "Gaming"),
    (re.compile(r"\b(?:netflix|spotify|cinema|event\s*cinemas|hoyts|disney|movie)\b", re.I), "Leisure"),
    (re.compile(r"\b(?:annual\s*fee|service\s*charge|monthly\s*a/c\s*fee|interest\s*charged)\b", re.I), "Fees & Admin"),
    (re.compile(r"\b(?:salary|wages|payroll|nzx|dividend|ird|tax\s*refund|invoice|freelance|consulting|client\s*payment|cashback|interest)\b", re.I), "Income"),
    (re.compile(r"\b(?:insurance|aa\s*insurance|iag|ami|state\s*insurance|southern\s*cross|aia)\b", re.I), "Insurance"),
    (re.compile(r"\b(?:air\s*nz|air\s*new\s*zealand|singaporeair|jetstar|cebu\s*air|flight|airline|qantas|emirates|hotel|montebello|picasso\s*b(?:o|ou)tique)\b", re.I), "Travel"),
    (
        re.compile(
            r"\b(?:wise|transferwise|investnow|sharesies|kernel|simplicity|hatch|stake|"
            r"transfer\s+to|transfer\s+from|tfr\s+to|tfr\s+from|internal\s+transfer|"
            r"internal\s+tfr|funds\s+transfer|trsf\s+to|trsf\s+from|internet\s+tfr|"
            r"acc\s+tfr|account\s+transfer|to\s+account|from\s+account|"
            r"dbs|posb|bdo|bpi|unionbank|metrobank|paynow|instapay|pesonet|"
            r"remit|remittance|wire\s+transfer|swift|crypto|bitcoin|btc|sats|"
            r"binance|easycrypto|coinbase|kraken)\b",
            re.I,
        ),
        "Transfers",
    ),
]

MONTH_NAMES: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

INSTITUTION_DETECTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bASB\s+Bank\b|\bASB\b|asb\.co\.nz", re.I), "ASB"),
    (re.compile(r"\bANZ\s+Bank\b|\bANZ\b|anz\.co\.nz", re.I), "ANZ"),
    (re.compile(r"\bBank\s+of\s+New\s+Zealand\b|\bBNZ\b|bnz\.co\.nz", re.I), "BNZ"),
    (re.compile(r"\bWestpac\b|westpac\.co\.nz", re.I), "Westpac"),
    (re.compile(r"\bKiwibank\b|kiwibank\.co\.nz", re.I), "Kiwibank"),
    (re.compile(r"\bRabobank\b", re.I), "Rabobank"),
    (re.compile(r"\bTSB\s+Bank\b|\bTSB\b", re.I), "TSB"),
    (re.compile(r"\bSharesies\b", re.I), "Sharesies"),
    (re.compile(r"\bSimplicity\b", re.I), "Simplicity"),
    (re.compile(r"\bKernel\s+Wealth\b|\bKernel\b", re.I), "Kernel"),
    (re.compile(r"\bMilford\s+Asset\s+Management\b|\bMilford\b", re.I), "Milford"),
    (re.compile(r"\bInvestNow\b|investnow\.co\.nz", re.I), "InvestNow"),
    (re.compile(r"\bInteractive\s+Brokers\b|\bIBKR\b", re.I), "Interactive Brokers"),
    (re.compile(r"\bWise\b|\bTransferWise\b|wise\.com", re.I), "Wise"),
    (re.compile(r"\bDBS\s+Bank\b|\bDBS\b|\bPOSB\b|dbs\.com|posb\.com", re.I), "DBS Bank"),
    (re.compile(r"\bBDO\s+Unibank\b|\bBDO\b|bdo\.com\.ph", re.I), "BDO Unibank"),
    (re.compile(r"\bBank\s+of\s+the\s+Philippine\s+Islands\b|\bBPI\b|bpi\.com\.ph|bpiexpressonline", re.I), "BPI"),
    (re.compile(r"\bUnionBank\b|unionbankph\.com", re.I), "UnionBank"),
    (re.compile(r"\bMetrobank\b|metrobank\.com\.ph", re.I), "Metrobank"),
    (re.compile(r"\bBitcoin\b|\bBTC\b|\bBinance\b|\bEasyCrypto\b|\bCoinbase\b|\bKraken\b", re.I), "Crypto"),
]

INSTITUTION_DEFAULT_CURRENCIES: dict[str, str] = {
    "DBS Bank": "SGD",
    "BDO Unibank": "PHP",
    "BPI": "PHP",
    "UnionBank": "PHP",
    "Metrobank": "PHP",
    "Interactive Brokers": "USD",
}


def auto_categorize(merchant_text: str) -> str:
    """Classify transaction into a budget category based on keywords."""
    for pattern, category in CATEGORIZATION_RULES:
        if pattern.search(merchant_text):
            return category
    return "General"


def clean_merchant_name(desc: str) -> str:
    """Strip banking transaction prefixes and filler terms to expose clear merchant names."""
    cleaned = re.sub(r"^(?:AP|BP|DC|DD|VT|EP|IA|AT|ED|FX|IP|IF|CQ)\s+", "", desc.strip())
    cleaned = re.sub(
        r"\b(?:AUTOMATIC PAYMENT|BILL PAYMENT|DIRECT DEBIT|DIRECT CREDIT|DEBIT|CREDIT)\b",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or desc.strip()


def detect_institution(text: str) -> Optional[str]:
    """Scan statement text for known banking and investment institution keywords."""
    for pattern, name in INSTITUTION_DETECTION_PATTERNS:
        if pattern.search(text):
            return name
    return None
