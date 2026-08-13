import logging

from aiogram.types import Update
from fastapi import APIRouter, HTTPException, Request, Response

from bot_api.bot.bot import get_bot, get_dispatcher
from bot_api.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request) -> Response:
    settings = get_settings()
    if secret != settings.telegram_webhook_secret:
        raise HTTPException(status_code=404)

    payload = await request.json()
    update = Update.model_validate(payload)

    try:
        await get_dispatcher().feed_update(get_bot(), update)
    except Exception:
        logger.exception("Failed to process Telegram update %s", update.update_id)

    return Response(status_code=200)
