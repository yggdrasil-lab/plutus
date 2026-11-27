"""FX rate response schemas for cached read-only rate lookups."""

from datetime import date

from pydantic import BaseModel, ConfigDict


class FxRateEntryRead(BaseModel):
    """Single cached exchange rate entry keyed by source currency."""

    model_config = ConfigDict(frozen=True)

    rate: float
    rate_date: date | None = None
    source: str


class FxRatesRead(BaseModel):
    """Read-only map of latest cached exchange rates into a target currency."""

    model_config = ConfigDict(frozen=True)

    to_currency: str
    rates: dict[str, FxRateEntryRead]
