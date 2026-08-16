"""Arq worker entrypoint. Run from the repo root with:

    python -m arq worker.worker.WorkerSettings
"""
from arq.connections import RedisSettings

from bot_api.bot.bot import get_bot
from bot_api.config import get_settings
from db.base import init_engine
from worker.tasks.generate import run_generation_pipeline


async def on_startup(ctx: dict) -> None:
    settings = get_settings()
    init_engine(settings.database_url)
    ctx["bot"] = get_bot()


async def on_shutdown(ctx: dict) -> None:
    await ctx["bot"].session.close()


class WorkerSettings:
    functions = [run_generation_pipeline]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    on_startup = on_startup
    on_shutdown = on_shutdown
    max_tries = 1
    job_timeout = 300
    # Short on purpose: enqueue_job's _job_id dedup (used to prevent a double-tapped
    # confirm button from queuing two concurrent runs) blocks re-enqueueing under the
    # same id for as long as arq keeps the job's result around. With the default 1h
    # keep_result, a failed run would silently block the owner's next retry for an
    # hour despite our own failure message telling them to try again shortly. We never
    # read job results (status lives in the DB, not arq), so a short window is safe.
    keep_result = 60
