"""Configuration management for Plutus API."""

import json
from pathlib import Path
from typing import Optional
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
    webhook_secret_file: Optional[str] = None
    dbs_account_map: str = ""
    own_account_suffixes: str = ""
    environment: str = "development"
    timezone: str = "Pacific/Auckland"
    base_currency: str = "NZD"
    default_inflation_rate: float = 0.025
    default_real_annual_return: float = 0.05
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

        if not self.webhook_secret and self.webhook_secret_file:
            secret_path = Path(self.webhook_secret_file)
            if secret_path.is_file():
                self.webhook_secret = secret_path.read_text(encoding="utf-8").strip()

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

    @property
    def resolved_mcp_token(self) -> str:
        """Resolve MCP bearer token from Swarm secret file first, else env value."""
        if self.mcp_token_file:
            secret_path = Path(self.mcp_token_file)
            if secret_path.is_file():
                file_val = secret_path.read_text(encoding="utf-8").strip()
                if file_val:
                    return file_val
        return (self.mcp_token or "").strip()

    @property
    def resolved_webhook_secret(self) -> str:
        """Resolve webhook bearer secret from Swarm secret file first, else env value."""
        if self.webhook_secret_file:
            secret_path = Path(self.webhook_secret_file)
            if secret_path.is_file():
                file_val = secret_path.read_text(encoding="utf-8").strip()
                if file_val:
                    return file_val
        return (self.webhook_secret or "").strip()

    @property
    def resolved_llm_base_url(self) -> str:
        """Return configured Muninn/OpenAI-compatible base URL."""
        return (self.llm_base_url or "").strip()

    @property
    def resolved_llm_api_key(self) -> str:
        """Return configured Muninn/OpenAI-compatible API key."""
        return (self.llm_api_key or "").strip()

    @property
    def resolved_llm_model(self) -> str:
        """Return configured general LLM model name."""
        return (self.llm_model or "").strip()

    @property
    def resolved_receipt_llm_model(self) -> str:
        """Return configured receipt vision/parsing LLM model name."""
        return (self.receipt_llm_model or "").strip()

    @property
    def resolved_jev_model(self) -> str:
        """Return configured TypeSafe Jev model name."""
        return (self.jev_model or "").strip()

    def resolved_dbs_account_map(self) -> dict[str, int]:
        """Parse PLUTUS_DBS_ACCOUNT_MAP JSON mapping account suffixes to Plutus account IDs."""
        raw = (self.dbs_account_map or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed PLUTUS_DBS_ACCOUNT_MAP JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(
                "PLUTUS_DBS_ACCOUNT_MAP must be a JSON object mapping suffix strings to integer account IDs."
            )
        mapping: dict[str, int] = {}
        for raw_key, raw_val in parsed.items():
            key = str(raw_key).strip()
            if not key:
                raise ValueError("PLUTUS_DBS_ACCOUNT_MAP contains an empty account suffix key.")
            if isinstance(raw_val, bool):
                raise ValueError(f"Invalid boolean account ID for DBS suffix {key!r}.")
            try:
                acc_id = int(raw_val)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid account ID {raw_val!r} for DBS suffix {key!r}; expected positive integer."
                ) from exc
            if acc_id <= 0:
                raise ValueError(
                    f"Account ID for DBS suffix {key!r} must be positive, got {acc_id}."
                )
            mapping[key] = acc_id
        return mapping

    def resolved_own_account_suffixes(self) -> set[str]:
        """Parse PLUTUS_OWN_ACCOUNT_SUFFIXES (comma-separated or JSON array) into normalized suffixes."""
        raw = (self.own_account_suffixes or "").strip()
        if not raw:
            return set()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Malformed PLUTUS_OWN_ACCOUNT_SUFFIXES JSON: {exc}") from exc
            if not isinstance(parsed, list):
                raise ValueError("PLUTUS_OWN_ACCOUNT_SUFFIXES JSON must be an array of strings.")
            return {str(item).strip().lower() for item in parsed if str(item).strip()}
        return {part.strip().lower() for part in raw.split(",") if part.strip()}


settings = Settings()
