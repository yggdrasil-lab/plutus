"""Shared ISO-8601 date parsing primitive for external provider connectors."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from app.config import settings


def parse_iso_date(value: Optional[str]) -> date:
    """Parse an ISO-8601 timestamp into the configured local date (e.g. Pacific/Auckland)."""
    if not value:
        return datetime.now(ZoneInfo(settings.timezone)).date()
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(ZoneInfo(settings.timezone))
    return dt.date()
