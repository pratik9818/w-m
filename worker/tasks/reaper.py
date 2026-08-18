"""Cleanup for builds abandoned by a worker that died mid-job.

Arq's `max_tries=1` means a crashed job is never retried, and nothing else ever revisits
the row -- so `businesses.generation_status` stays at `generating` forever, and because
that is a BUSY_STATUS the owner's every future edit is refused. Observed live: a
transient Redis DNS failure killed the worker and left a real business stuck.
"""
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from sqlalchemy import select

from bot_api.services.edit_ops import BUSY_STATUSES, STALE_BUILD_MINUTES
from db.base import session_scope
from db.models import Business, SiteVersion
from worker.tasks.notify import notify_owner_failure

logger = logging.getLogger(__name__)


async def reap_stale_builds(ctx: dict) -> None:
    bot: Bot = ctx["bot"]
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_BUILD_MINUTES)

    async with session_scope() as session:
        result = await session.execute(
            select(Business).where(
                Business.generation_status.in_(BUSY_STATUSES),
                Business.updated_at < cutoff,
            )
        )
        stale = result.scalars().all()
        if not stale:
            return

        for business in stale:
            logger.warning(
                "reaping stale build: business=%s status=%s stuck since %s",
                business.id, business.generation_status, business.updated_at,
            )
            versions = await session.execute(
                select(SiteVersion)
                .where(SiteVersion.business_id == business.id, SiteVersion.status.notin_(("live", "failed")))
                .order_by(SiteVersion.created_at.desc())
            )
            for version in versions.scalars().all():
                version.status = "failed"
                version.error = f"abandoned: no progress for over {STALE_BUILD_MINUTES} minutes"
            # Deliberately does not touch deployment_url or current_version_id -- whatever
            # was live before this build stays live and stays correct.
            business.generation_status = "failed"

        await session.commit()

        # Still inside the session, so these are attached and safe to read.
        for business in stale:
            await notify_owner_failure(bot, business, "interrupted")
