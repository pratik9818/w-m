from contextlib import asynccontextmanager

from fastapi import FastAPI

from bot_api.bot.webhook import router as webhook_router
from bot_api.config import get_settings
from bot_api.logging_config import configure_logging
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


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
