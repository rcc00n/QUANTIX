from datetime import UTC, datetime
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from arbiq.collector.config import CollectorConfig
from arbiq.core.secrets import read_secret_file
from arbiq.opportunities.config import EngineConfig
from arbiq.paper_trading.config import PaperConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", hide_input_in_errors=True, env_nested_delimiter="__"
    )

    database_url: SecretStr
    database_url_file: str | None = None
    admin_username: str = Field(default="quantix", min_length=3, max_length=80)
    admin_previous_username: str | None = None
    admin_password_hash: SecretStr
    admin_password_hash_file: str | None = None
    trading_mode: Literal["paper"] = "paper"
    cookie_secure: bool = False
    allowed_origins: list[str] = ["http://localhost:3100", "http://127.0.0.1:3100"]
    session_ttl_seconds: int = Field(default=28800, ge=300, le=86400)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    collector: CollectorConfig = Field(default_factory=CollectorConfig)
    arbitrage: EngineConfig = Field(default_factory=EngineConfig)
    paper: PaperConfig = Field(default_factory=PaperConfig)
    collector_url: str = "http://collector:8001"
    collector_internal_token: SecretStr = SecretStr("")
    collector_internal_token_file: str | None = None
    deployment_environment: Literal["development", "production"] = "development"
    production_domain: str = "quantix.raccncode.com"
    public_app_url: str = "http://localhost:3100"
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: SecretStr = SecretStr("")
    telegram_token_file: str = "/run/secrets/telegram_bot_token"
    notification_worker_enabled: bool = False
    nvidia_api_key: SecretStr = SecretStr("")
    nvidia_api_key_file: str | None = None
    nvidia_model: str = ""
    ai_enabled: bool = False
    ai_free_allowance_authorized: bool = False
    ai_free_allowance_expires_at: datetime | None = None
    ai_max_requests_per_day: int = Field(default=20, ge=1, le=100)
    ai_min_request_interval_seconds: int = Field(default=60, ge=10, le=86400)
    ai_cache_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    ai_request_timeout_seconds: int = Field(default=10, ge=1, le=30)
    ai_max_input_bytes: int = Field(default=12000, ge=1000, le=32000)
    ai_max_output_tokens: int = Field(default=400, ge=100, le=1000)

    @field_validator("ai_free_allowance_expires_at", mode="before")
    @classmethod
    def empty_optional_expiry(cls, value):
        return None if value == "" else value

    @model_validator(mode="before")
    @classmethod
    def load_secret_files(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            for name in (
                "database_url", "admin_password_hash", "collector_internal_token", "nvidia_api_key"
            ):
                filename = data.get(f"{name}_file")
                if filename:
                    data[name] = read_secret_file(filename)
        return data

    @model_validator(mode="after")
    def validate_security(self) -> "Settings":
        url = self.database_url.get_secret_value()
        if not url.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use PostgreSQL with asyncpg")
        value = self.admin_password_hash.get_secret_value()
        parts = value.split("$")
        if len(parts) != 3 or parts[0] != "scrypt":
            raise ValueError("Generate ADMIN_PASSWORD_HASH using scripts/setup.py")
        try:
            if len(bytes.fromhex(parts[1])) != 16 or len(bytes.fromhex(parts[2])) != 64:
                raise ValueError
        except ValueError as exc:
            raise ValueError("Invalid scrypt password hash") from exc
        if self.ai_free_allowance_expires_at is not None and (
            self.ai_free_allowance_expires_at.tzinfo is None
            or self.ai_free_allowance_expires_at.utcoffset() is None
        ):
            raise ValueError("AI allowance expiry requires a timezone")
        if self.ai_enabled and (
            not self.ai_free_allowance_authorized
            or not self.nvidia_api_key.get_secret_value()
            or not self.nvidia_model
            or self.ai_free_allowance_expires_at is None
            or self.ai_free_allowance_expires_at <= datetime.now(UTC)
        ):
            raise ValueError(
                "AI requires explicit unexpired free allowance authorization and credentials"
            )
        if self.deployment_environment == "production":
            origin = urlsplit(self.public_app_url)
            if (
                not self.cookie_secure
                or origin.scheme != "https"
                or origin.netloc != self.production_domain
                or origin.username
                or origin.password
                or origin.path not in ("", "/")
                or origin.query
                or origin.fragment
                or self.allowed_origins != [f"https://{self.production_domain}"]
            ):
                raise ValueError("Production requires secure cookies and the exact HTTPS origin")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
