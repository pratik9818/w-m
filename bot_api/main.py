from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bot_api.bot.webhook import router as webhook_router
from bot_api.config import get_settings
from bot_api.logging_config import configure_logging
from bot_api.web.routes import router as billing_router
from db.base import init_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging("bot_api")
    settings = get_settings()
    if settings.database_url:
        init_engine(settings.database_url)
    yield


app = FastAPI(title="website-maker bot", lifespan=lifespan)
app.include_router(webhook_router)
# The customer-facing payment page and Razorpay's webhook. Deliberately the same app as
# the Telegram webhook: it already holds the database, Redis, and a bot instance to send
# the confirmation with once the money has actually moved.
app.include_router(billing_router)

# The payment site in web/ is hosted on its own domain, so its calls to /api/checkout are
# cross-origin and a browser blocks them unless this says otherwise. Named explicitly
# rather than "*": the allowlist is the only thing stopping any page on the internet from
# reading a checkout token's details out of a customer's browser. Nothing is added at all
# when the site is not deployed, which keeps the default deployment with no CORS surface.
_checkout_origin = get_settings().checkout_site_url.rstrip("/")
if _checkout_origin:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[_checkout_origin],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Accept"],
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
