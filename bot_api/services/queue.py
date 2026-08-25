import uuid

from arq.connections import ArqRedis, RedisSettings, create_pool
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff

from bot_api.config import get_settings

_pool: ArqRedis | None = None

# arq's defaults assume Redis is on the same machine. Ours is Upstash, reached over the
# public internet, where a TLS handshake regularly takes longer than arq's 1-second
# default connect timeout. That matters more than it sounds: arq's poll loop does not
# catch the resulting TimeoutError, so the worker *process exits*. Observed live -- the
# worker died 60s after starting, a build enqueued six minutes later sat untouched, and
# the owner watched a "building your site" message for 20 minutes.
CONNECT_TIMEOUT_SECONDS = 10
CONNECT_RETRIES = 10
CONNECT_RETRY_DELAY_SECONDS = 2
COMMAND_RETRIES = 3


def redis_settings() -> RedisSettings:
    """Connection settings tolerant of a slow or briefly unreachable Redis.

    Shared by the enqueue side and the worker so the two cannot drift apart.
    """
    settings = RedisSettings.from_dsn(get_settings().redis_url)
    settings.conn_timeout = CONNECT_TIMEOUT_SECONDS
    settings.conn_retries = CONNECT_RETRIES
    settings.conn_retry_delay = CONNECT_RETRY_DELAY_SECONDS
    # Retries the individual command as well, so a blip *mid-poll* is absorbed rather
    # than propagating out of the poll loop and taking the process down with it.
    settings.retry_on_timeout = True
    settings.retry = Retry(ExponentialBackoff(cap=5, base=0.5), retries=COMMAND_RETRIES)
    return settings


async def get_arq_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(redis_settings())
    return _pool


async def enqueue_generation(
    business_id: uuid.UUID, trigger: str = "create", patch: dict | None = None
) -> None:
    pool = await get_arq_pool()
    job_id = f"generate:{business_id}:{trigger}"
    if trigger != "create":
        # "create" intentionally dedups a double-tapped confirm button. Repeated
        # edits on the same business must NOT share this id, or a second edit sent
        # soon after the first one finishes silently no-ops (arq's dedup checks the
        # job's kept result too, not just whether it's still running) -- the real
        # double-submit guard for edits is the busy-status check in edit_ops.py.
        job_id += f":{uuid.uuid4().hex[:8]}"
    # `patch` carries {"instruction": str, "targets": [filename, ...]} for a surgical edit;
    # None means build the whole site from the spec (first build, or an explicit rebuild).
    await pool.enqueue_job(
        "run_generation_pipeline", str(business_id), trigger, patch, _job_id=job_id
    )


async def enqueue_rollback(business_id: uuid.UUID, version_id: uuid.UUID) -> None:
    pool = await get_arq_pool()
    await pool.enqueue_job(
        "rollback_site",
        str(business_id),
        str(version_id),
        _job_id=f"rollback:{business_id}:{uuid.uuid4().hex[:8]}",
    )
