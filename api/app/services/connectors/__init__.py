"""External financial data provider connectors for Plutus."""


class ConnectorConfigError(RuntimeError):
    """Raised when required provider credentials are missing."""


class ConnectorUpstreamError(RuntimeError):
    """Raised when an upstream provider API returns an unexpected error."""


from app.services.connectors.dates import parse_iso_date

__all__ = [
    "ConnectorConfigError",
    "ConnectorUpstreamError",
    "parse_iso_date",
]
