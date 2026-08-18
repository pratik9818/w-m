import uuid

from arq.connections import ArqRedis, RedisSettings, create_pool

from bot_api.config import get_settings

_pool: ArqRedis | None = None


async def get_arq_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
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
