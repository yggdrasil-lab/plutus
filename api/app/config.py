"""Configuration management for Plutus API."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration pulled from environment variables."""

    database_url: str = "postgresql+psycopg://plutus:plutus@localhost:5432/plutus"
    mcp_token: str = ""
    webhook_secret: str = ""
    environment: str = "development"
    timezone: str = "Pacific/Auckland"
    base_currency: str = "NZD"
    default_inflation_rate: float = 0.025

    model_config = SettingsConfigDict(
        env_prefix="PLUTUS_",
        case_sensitive=False,
    )


settings = Settings()
