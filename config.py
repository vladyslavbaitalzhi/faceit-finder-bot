from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    """Настройки приложения, загружаемые из переменных окружения и .env."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    bot_token: SecretStr
    steam_api_key: SecretStr | None = None
    faceit_api_key: SecretStr | None = None

    rate_limit_requests: int = 10
    rate_limit_window_seconds: float = 60.0
    rate_limit_heavy_weight: int = 2

    faceit_max_concurrency: int = 5
    faceit_max_requests_per_second: float = 4.0
    faceit_rate_limit_cooldown_seconds: float = 30.0
    faceit_rate_limit_cooldown_max_seconds: float = 300.0
    faceit_cache_max_entries: int = 2500
    faceit_negative_cache_ttl_seconds: float = 60.0

    refresh_cooldown_seconds: float = 60.0


settings = Settings()
