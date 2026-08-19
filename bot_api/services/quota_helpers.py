"""Recording token usage from places that don't already hold a database session."""
import logging

from worker.codegen.quota import record_usage
from db.base import session_scope

logger = logging.getLogger(__name__)


async def record_parse_usage(owner_telegram_id: int, usage: dict, business_id=None) -> None:
    """Bill a message-understanding call against the owner's budget.

    Never lets an accounting failure break the user's actual request -- getting their site
    built matters more than the usage row, and the row can be reconciled from the logs.
    """
    try:
        async with session_scope() as session:
            await record_usage(
                session, owner_telegram_id, business_id, usage["model"],
                usage["input_tokens"], usage["output_tokens"], kind="parse",
            )
    except Exception:
        logger.exception("failed to record parse usage for %s", owner_telegram_id)
