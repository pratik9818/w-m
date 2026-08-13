import logging

from aiogram.types import ErrorEvent

logger = logging.getLogger(__name__)


async def on_error(event: ErrorEvent) -> bool:
    logger.exception("Unhandled error while processing update", exc_info=event.exception)
    update = event.update
    message = getattr(update, "message", None) or getattr(getattr(update, "callback_query", None), "message", None)
    if message is not None:
        try:
            await message.answer("Something went wrong on my end — please try again in a moment.")
        except Exception:
            logger.exception("Failed to send error notice to user")
    return True
