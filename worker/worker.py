"""Arq worker entrypoint. Run from the repo root with:

    python -m arq worker.worker.WorkerSettings
"""
from arq import cron
from arq.connections import RedisSettings

from bot_api.bot.bot import get_bot
from bot_api.config import get_settings
from db.base import init_engine
from worker.tasks.generate import run_generation_pipeline
from worker.tasks.reaper import reap_stale_builds
from worker.tasks.rollback import rollback_site


async def on_startup(ctx: dict) -> None:
    settings = get_settings()
    init_engine(settings.database_url)
    ctx["bot"] = get_bot()


async def on_shutdown(ctx: dict) -> None:
    await ctx["bot"].session.close()


class WorkerSettings:
    functions = [run_generation_pipeline, rollback_site]
    # Frees any business left frozen in a busy status by a worker that died mid-job --
    # without it, that owner can never edit their site again.
    cron_jobs = [cron(reap_stale_builds, minute=set(range(0, 60, 5)), run_at_startup=True)]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    on_startup = on_startup
    on_shutdown = on_shutdown
    max_tries = 1
    # Generous: writing a full four-page site on the large free model is the slow stage
    # and can take several minutes on its own, before the sandbox run (~30-60s) and the
    # Cloudflare deploy (~5-10s). The client-side request timeout in openrouter_client
    # is the real bound on a hung call; this only needs to be comfortably above it.
    job_timeout = 1800
    # Short on purpose: enqueue_job's _job_id dedup (used to prevent a double-tapped
    # confirm button from queuing two concurrent runs) blocks re-enqueueing under the
    # same id for as long as arq keeps the job's result around. With the default 1h
    # keep_result, a failed run would silently block the owner's next retry for an
    # hour despite our own failure message telling them to try again shortly. We never
    # read job results (status lives in the DB, not arq), so a short window is safe.
    keep_result = 60
