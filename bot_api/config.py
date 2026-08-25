from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str = ""
    telegram_webhook_secret: str = "change-me-to-a-random-string"

    database_url: str = ""

    redis_url: str = ""

    supabase_url: str = ""
    supabase_service_key: str = ""

    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    openrouter_api_key: str = ""
    # Stock photography for generated sites. Absent, builds simply carry no photographs.
    pexels_api_key: str = ""
    daytona_api_key: str = ""
    cloudflare_api_token: str = ""
    cloudflare_account_id: str = ""

    base_domain: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
