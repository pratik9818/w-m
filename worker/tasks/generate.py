import logging
import uuid

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot_api.services.business_service import get_business_by_id
from bot_api.services.openrouter_client import DailyLimitReached
from db.base import session_scope
from db.models import Business, SiteVersion
from worker.codegen.builder import (
    GenerationFailed,
    build_site,
    patch_site_files,
    spec_from_business,
)
from worker.codegen.quota import FREE_TIER_TOKEN_LIMIT, QuotaExceeded, get_tokens_used
from worker.tasks.deploy import CloudflareDeployError, deploy_to_cloudflare_pages
from worker.tasks.notify import notify_owner_failure, notify_owner_progress, notify_owner_success
from worker.tasks.sandbox import sandbox_test

logger = logging.getLogger(__name__)

ERROR_DETAIL_MAX_LEN = 2000


async def _live_files(session: AsyncSession, business: Business) -> dict[str, str] | None:
    """The file set currently deployed for this business, if we have it stored."""
    if business.current_version_id is None:
        return None
    result = await session.execute(
        select(SiteVersion.files).where(SiteVersion.id == business.current_version_id)
    )
    files = result.scalar_one_or_none()
    return files or None


async def run_generation_pipeline(
    ctx: dict, business_id: str, trigger: str = "create", patch: dict | None = None
) -> None:
    """Single Arq job: generate -> sandbox test -> deploy -> notify, updating DB status
    at every stage boundary. Fails cleanly on the first error (no per-stage retry --
    WorkerSettings.max_tries=1 also prevents arq from retrying the whole job).
    """
    bot: Bot = ctx["bot"]
    async with session_scope() as session:
        business = await get_business_by_id(session, uuid.UUID(business_id))
        if business is None:
            logger.error("run_generation_pipeline: business %s not found", business_id)
            return

        version_number = await _next_version_number(session, business.id)
        site_version = SiteVersion(
            business_id=business.id,
            version_number=version_number,
            trigger=trigger,
            status="generating",
        )
        session.add(site_version)
        business.generation_status = "generating"
        await session.commit()
        await notify_owner_progress(bot, business, "generating")

        try:
            live_files = await _live_files(session, business)
            # Patch the live site whenever we can. Regenerating from the spec produces a
            # brand-new site every time -- new colours, resequenced sections, reworded
            # copy -- which is exactly what owners experience as "it changed my whole
            # site when I asked for one small thing".
            if patch and live_files and trigger != "rebuild":
                files, usage = await patch_site_files(
                    live_files,
                    patch["instruction"],
                    patch.get("targets") or [],
                    session=session,
                    owner_telegram_id=business.owner_telegram_id,
                    business_id=business.id,
                )
            else:
                files, usage = await build_site(
                    spec_from_business(business),
                    session=session,
                    owner_telegram_id=business.owner_telegram_id,
                    business_id=business.id,
                    kind="rebuild" if trigger == "rebuild" else trigger,
                )
        except DailyLimitReached as exc:
            # Not a fault in the site or the request -- the provider's daily cap. Says so
            # plainly instead of sending the owner hunting for a bug that isn't there.
            await _mark_failed(session, business, site_version, "daily_limit", str(exc))
            await notify_owner_failure(bot, business, "daily_limit")
            return
        except QuotaExceeded:
            await _mark_failed(session, business, site_version, "quota", "token quota exceeded")
            await notify_owner_failure(bot, business, "quota")
            return
        except GenerationFailed as exc:
            await _mark_failed(session, business, site_version, "generation", str(exc))
            await notify_owner_failure(bot, business, "generation")
            return
        except Exception as exc:
            logger.exception("run_generation_pipeline: generation failed for %s", business_id)
            await _mark_failed(session, business, site_version, "unknown", str(exc))
            await notify_owner_failure(bot, business, "unknown")
            return

        site_version.status = "testing"
        business.generation_status = "testing"
        await session.commit()
        await notify_owner_progress(bot, business, "testing")

        try:
            report = await sandbox_test(files)
        except Exception as exc:
            logger.exception("run_generation_pipeline: sandbox test failed for %s", business_id)
            await _mark_failed(session, business, site_version, "unknown", str(exc))
            await notify_owner_failure(bot, business, "unknown")
            return

        site_version.sandbox_report = report
        site_version.sandbox_status = "passed" if report["passed"] else "failed"
        if not report["passed"]:
            await _mark_failed(session, business, site_version, "sandbox", str(report["checks"]))
            await notify_owner_failure(bot, business, "sandbox")
            return

        site_version.status = "deploying"
        business.generation_status = "deploying"
        await session.commit()
        await notify_owner_progress(bot, business, "deploying")

        try:
            project_name, live_url = await deploy_to_cloudflare_pages(business, files)
        except CloudflareDeployError as exc:
            await _mark_failed(session, business, site_version, "deploy", str(exc))
            await notify_owner_failure(bot, business, "deploy")
            return
        except Exception as exc:
            logger.exception("run_generation_pipeline: deploy failed for %s", business_id)
            await _mark_failed(session, business, site_version, "unknown", str(exc))
            await notify_owner_failure(bot, business, "unknown")
            return

        site_version.deployed_url = live_url
        site_version.status = "live"
        # Only stored after a fully successful deploy, so the next edit always patches the
        # bytes that are genuinely live rather than a version that failed on the way out.
        site_version.files = files
        business.cf_pages_project_name = project_name
        business.deployment_url = live_url
        business.current_version_id = site_version.id
        business.generation_status = "live"
        await session.commit()

        used = await get_tokens_used(session, business.owner_telegram_id)
        await notify_owner_success(
            bot, business, usage=usage, remaining=max(FREE_TIER_TOKEN_LIMIT - used, 0)
        )


async def _next_version_number(session: AsyncSession, business_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.coalesce(func.max(SiteVersion.version_number), 0)).where(
            SiteVersion.business_id == business_id
        )
    )
    return int(result.scalar_one()) + 1


async def _mark_failed(
    session: AsyncSession, business: Business, site_version: SiteVersion, stage: str, detail: str
) -> None:
    site_version.status = "failed"
    site_version.error = f"{stage}: {detail}"[:ERROR_DETAIL_MAX_LEN]
    business.generation_status = "failed"
    await session.commit()
