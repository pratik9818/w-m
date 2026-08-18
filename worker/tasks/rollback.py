"""Redeploy a previous version's stored files.

Deliberately does not touch the model at all: the bytes being restored were already
generated, tested and published once, so re-running generation could only introduce
something new. That makes /undo free in both quota and risk -- which is the point, since
it exists to rescue an owner from an edit that changed more than they wanted.
"""
import logging
import uuid

from aiogram import Bot
from sqlalchemy import func, select

from bot_api.services.business_service import get_business_by_id
from db.base import session_scope
from db.models import SiteVersion
from worker.tasks.deploy import deploy_to_cloudflare_pages
from worker.tasks.notify import notify_owner_failure

logger = logging.getLogger(__name__)


async def rollback_site(ctx: dict, business_id: str, version_id: str) -> None:
    bot: Bot = ctx["bot"]
    async with session_scope() as session:
        business = await get_business_by_id(session, uuid.UUID(business_id))
        if business is None:
            logger.error("rollback_site: business %s not found", business_id)
            return

        target = (await session.execute(
            select(SiteVersion).where(SiteVersion.id == uuid.UUID(version_id))
        )).scalar_one_or_none()
        if target is None or not target.files:
            logger.error("rollback_site: version %s has no stored files", version_id)
            business.generation_status = "live"
            await session.commit()
            await notify_owner_failure(bot, business, "unknown")
            return

        next_number = int((await session.execute(
            select(func.coalesce(func.max(SiteVersion.version_number), 0))
            .where(SiteVersion.business_id == business.id)
        )).scalar_one()) + 1

        restored = SiteVersion(
            business_id=business.id,
            version_number=next_number,
            trigger="rollback",
            status="deploying",
            # Same bytes, recorded again so the history stays truthful about what is live
            # rather than silently repointing at an old row.
            files=target.files,
            sandbox_status="passed",
            sandbox_report={"skipped": f"rollback to version {target.version_number}"},
        )
        session.add(restored)
        business.generation_status = "deploying"
        await session.commit()

        try:
            project_name, live_url = await deploy_to_cloudflare_pages(business, target.files)
        except Exception as exc:
            logger.exception("rollback_site: deploy failed for %s", business_id)
            restored.status = "failed"
            restored.error = f"deploy: {exc}"[:2000]
            business.generation_status = "failed"
            await session.commit()
            await notify_owner_failure(bot, business, "deploy")
            return

        restored.deployed_url = live_url
        restored.status = "live"
        business.cf_pages_project_name = project_name
        business.deployment_url = live_url
        business.current_version_id = restored.id
        business.generation_status = "live"
        await session.commit()

        await bot.send_message(
            business.owner_telegram_id,
            f"↩️ <b>{business.name}</b> is back to how it was in version "
            f"{target.version_number}. {live_url}",
        )
