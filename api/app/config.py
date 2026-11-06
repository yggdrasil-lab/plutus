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
    llm_base_url: str = "http://muninn:4000/v1"
    llm_api_key: str = ""
    llm_model: str = "app-synthesize"
    jev_model: str = "jev-decision"

    model_config = SettingsConfigDict(
        env_prefix="PLUTUS_",
        case_sensitive=False,
    )


settings = Settings()
