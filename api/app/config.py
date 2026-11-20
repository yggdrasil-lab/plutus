"""Configuration management for Plutus API."""

from pathlib import Path
from urllib.parse import quote_plus

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration pulled from environment variables."""

    database_url: str = "postgresql+psycopg://plutus:plutus@localhost:5432/plutus"
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "plutus"
    db_user: str = "plutus"
    db_password: str = ""
    db_password_file: str = ""
    mcp_token: str = ""
    mcp_token_file: str = ""
    webhook_secret: str = ""
    environment: str = "development"
    timezone: str = "Pacific/Auckland"
    base_currency: str = "NZD"
    default_inflation_rate: float = 0.025
    llm_base_url: str = "http://muninn:4000/v1"
    llm_api_key: str = ""
    llm_model: str = "app-synthesize"
    receipt_llm_model: str = "gemini-4-hydrogen"
    jev_model: str = "jev-decision"
    akahu_app_token: str = Field(
        default="",
        validation_alias=AliasChoices("PLUTUS_AKAHU_APP_TOKEN", "AKAHU_APP_TOKEN"),
    )
    akahu_user_token: str = Field(
        default="",
        validation_alias=AliasChoices("PLUTUS_AKAHU_USER_TOKEN", "AKAHU_USER_TOKEN"),
    )
    wise_api_token: str = Field(
        default="",
        validation_alias=AliasChoices("PLUTUS_WISE_API_TOKEN", "WISE_API_TOKEN"),
    )
    btc_watch_sources: str = Field(
        default="",
        validation_alias=AliasChoices("PLUTUS_BTC_WATCH_SOURCES", "PLUTUS_BTC_XPUB", "BTC_WATCH_SOURCES"),
    )
    sync_interval_hours: int = 6
    discord_bot_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PLUTUS_DISCORD_BOT_TOKEN", "DISCORD_BOT_TOKEN"),
    )
    discord_wealth_channel_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "PLUTUS_DISCORD_WEALTH_CHANNEL_ID",
            "DISCORD_CHANNEL_WEALTH_ID",
            "DISCORD_WEALTH_CHANNEL_ID",
        ),
    )
    discord_user_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PLUTUS_DISCORD_USER_ID", "DISCORD_USER_ID"),
    )

    model_config = SettingsConfigDict(
        env_prefix="PLUTUS_",
        case_sensitive=False,
    )

    @model_validator(mode="after")
    def _load_secret_files(self) -> "Settings":
        """Load Docker Swarm secret files when *_FILE paths are configured."""
        if not self.mcp_token and self.mcp_token_file:
            secret_path = Path(self.mcp_token_file)
            if secret_path.is_file():
                self.mcp_token = secret_path.read_text(encoding="utf-8").strip()

        if self.db_password_file:
            secret_path = Path(self.db_password_file)
            if not secret_path.is_file():
                raise ValueError(f"Database password file not found: {self.db_password_file}")
            secret_value = secret_path.read_text(encoding="utf-8").strip()
            if not secret_value:
                raise ValueError(f"Database password file is empty: {self.db_password_file}")
            if not self.db_password:
                self.db_password = secret_value

        if "database_url" not in self.model_fields_set and self.db_password:
            self.database_url = (
                f"postgresql+psycopg://{quote_plus(self.db_user)}:{quote_plus(self.db_password)}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}"
            )

        return self


settings = Settings()
