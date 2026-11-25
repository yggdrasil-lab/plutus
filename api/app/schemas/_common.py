"""Shared normalization and parsing helpers across Plutus schema modules."""

import json
from typing import Any, Optional


RECURRENCE_CHOICES = {"weekly", "fortnightly", "monthly", "quarterly", "annual", "one-off"}


def normalize_recurrence(value: Any) -> Optional[str]:
    """Normalize recurrence string to a canonical cadence or None."""
    if value is None:
        return None
    cleaned = str(value).strip().lower().replace("_", "-")
    if not cleaned or cleaned in ("none", "variable"):
        return None
    if cleaned not in RECURRENCE_CHOICES:
        raise ValueError(
            f"Unsupported recurrence '{value}'. Use one of: {', '.join(sorted(RECURRENCE_CHOICES))}."
        )
    return cleaned


def parse_tags_if_string(value: Any) -> list[str]:
    """Parse tag collections from JSON array strings, comma-separated text, or sequences."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(t) for t in parsed if str(t).strip()]
        except Exception:
            return [t.strip() for t in value.split(",") if t.strip()]
    if isinstance(value, (list, tuple)):
        return [str(t) for t in value if str(t).strip()]
    return []
