from typing import Literal
from urllib.parse import urlparse
import re

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    bot_token: SecretStr
    webhook_secret: SecretStr
    webhook_path: str
    webhook_base_url: str
    database_url: SecretStr
    redis_url: SecretStr
    admin_telegram_ids: list[int]
    support_contact: str
    terms_url: str
    terms_text: str = "Цифровой доступ к закрытым каналам. По вопросам возврата: /paysupport."
    app_env: Literal["development", "production", "test"] = "production"
    log_level: str = "INFO"
    expire_interval: int = 60
    cleanup_interval: int = 300
    reconciliation_interval: int = 7200
    invite_ttl: int = 600
    order_ttl: int = 1800

    @field_validator("webhook_path")
    @classmethod
    def path_valid(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value):
            raise ValueError("WEBHOOK_PATH requires 32–128 random URL-safe characters")
        return value

    @field_validator("webhook_secret")
    @classmethod
    def secret_valid(cls, value: SecretStr) -> SecretStr:
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value.get_secret_value()):
            raise ValueError("WEBHOOK_SECRET requires 32–256 random URL-safe characters")
        return value

    @model_validator(mode="after")
    def validate_config(self):
        if not self.admin_telegram_ids or any(type(x) is not int or x <= 0 for x in self.admin_telegram_ids):
            raise ValueError("At least one numeric administrator ID is required")
        if not self.database_url.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("PostgreSQL with asyncpg required")
        if self.app_env == "production" and any(
            "CHANGE_ME" in value.get_secret_value()
            for value in (self.bot_token, self.database_url, self.redis_url)
        ):
            raise ValueError("Replace placeholder credentials")
        if self.app_env == "production" and urlparse(self.webhook_base_url).scheme != "https":
            raise ValueError("Production webhook requires HTTPS")
        if urlparse(self.terms_url).scheme != "https":
            raise ValueError("TERMS_URL requires HTTPS")
        if min(self.expire_interval, self.cleanup_interval, self.reconciliation_interval, self.order_ttl) < 1:
            raise ValueError("Intervals must be positive")
        if not 60 <= self.invite_ttl <= 600:
            raise ValueError("Invite TTL must be 60–600 seconds")
        if self.webhook_path == self.webhook_secret.get_secret_value():
            raise ValueError("Webhook path and secret must differ")
        return self
