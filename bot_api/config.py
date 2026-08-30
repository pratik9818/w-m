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
    # Where a generated site posts an enquiry. Derived from supabase_url when blank, which
    # is the normal case; set it only to point sites at a different deployment of the
    # `site-form` edge function. Blank on both counts means no form is ever put on a page,
    # because a form that posts nowhere loses every message a customer sends.
    form_endpoint_url: str = ""

    # The only model key the bot uses. Everything -- writing sites, reading edits,
    # onboarding, choosing photographs -- goes through Claude via bot_api/services/llm_client.py.
    anthropic_api_key: str = ""
    # Stock photography for generated sites. Absent, builds simply carry no photographs.
    pexels_api_key: str = ""
    daytona_api_key: str = ""
    cloudflare_api_token: str = ""
    cloudflare_account_id: str = ""

    base_domain: str = ""
    # Where this app is reachable from a customer's browser, e.g.
    # https://bot.example.com -- the payment link sent into a chat is built from it, and
    # so is the return URL Razorpay redirects to. No trailing slash.
    public_base_url: str = ""
    # Without the @. Used to send somebody back into the chat from the payment page.
    bot_username: str = ""
    # Origin of the standalone payment site in web/, e.g. https://pay.example.com --
    # no trailing slash. Set it and the link sent into a chat points there, and that
    # origin is the only one allowed to call the checkout API from a browser. Leave it
    # blank and the server-rendered pages under /pay/... are used instead, which is the
    # fallback that needs no second deployment.
    checkout_site_url: str = ""

    # Razorpay. `key_id` is public and ships inside the payment page; `key_secret` signs
    # the checkout callback and must not; `webhook_secret` is a third, separate value set
    # on the webhook itself in the Razorpay dashboard, and is what proves an incoming
    # event is genuine.
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    # One Razorpay Plan object per tier and billing period, created once in the dashboard.
    # They live in configuration because a Razorpay plan is immutable once it has
    # subscribers -- creating them from code would mean a price change silently stranding
    # everybody already paying the old one.
    razorpay_plan_starter_monthly: str = ""
    razorpay_plan_starter_yearly: str = ""
    razorpay_plan_business_monthly: str = ""
    razorpay_plan_business_yearly: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
