import logging
import time
import uuid

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot_api.logging_config import add_run_fields, log_event, start_run
from bot_api.services.business_service import get_business_by_id
from bot_api.services.llm_client import DailyLimitReached
from bot_api.services.redis_client import get_redis
from bot_api.services.session import correct_last_edit_turn
from db.base import session_scope
from db.models import Business, EditLog, SiteVersion
from worker.codegen.builder import (
    EditNotApplied,
    GenerationFailed,
    build_site,
    patch_site_files,
    repair_files,
    spec_from_business,
)
from worker.codegen.style_ops import StyleAlreadySet, StyleOpFailed, apply_style_changes
from worker.codegen.validate import failed as failed_checks
from worker.codegen.validate import validate_files
from worker.codegen.quota import FREE_TIER_TOKEN_LIMIT, QuotaExceeded, get_tokens_used
from worker.tasks.web_analytics import strip_beacon
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


async def _record_no_change(business: Business, patch: dict | None) -> None:
    """Tell the next parse that this edit changed nothing, so it stops repeating itself.

    Best-effort by design: the owner has already been told what happened, and a Redis
    problem here must not turn a clean "nothing to change" into a crashed job.
    """
    user_message = (patch or {}).get("user_message")
    if not user_message:
        return
    try:
        await correct_last_edit_turn(
            get_redis(),
            business.id,
            user_message,
            {"rejected": "that changed nothing -- the site already looked exactly like "
                         "that. If they ask for it again they want it pushed further "
                         "than it is now, not the same value repeated."},
        )
    except Exception:
        logger.warning("could not record the no-change outcome for %s", business.id, exc_info=True)


async def run_generation_pipeline(
    ctx: dict, business_id: str, trigger: str = "create", patch: dict | None = None
) -> None:
    """Single Arq job: generate -> sandbox test -> deploy -> notify, updating DB status
    at every stage boundary. Fails cleanly on the first error (no per-stage retry --
    WorkerSettings.max_tries=1 also prevents arq from retrying the whole job).
    """
    bot: Bot = ctx["bot"]
    start_run(business_id=business_id, trigger=trigger, mode="patch" if patch else "full")
    started = time.monotonic()

    async with session_scope() as session:
        business = await get_business_by_id(session, uuid.UUID(business_id))
        if business is None:
            log_event(logger, "build.business_missing", logging.ERROR, business_id=business_id)
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
        await _link_edit_log(session, patch, site_version.id)
        add_run_fields(slug=business.slug, version=version_number)
        log_event(logger, "build.started")
        await notify_owner_progress(bot, business, "generating")

        stage_started = time.monotonic()
        try:
            live_files = await _live_files(session, business)
            # Patch the live site whenever we can. Regenerating from the spec produces a
            # brand-new site every time -- new colours, resequenced sections, reworded
            # copy -- which is exactly what owners experience as "it changed my whole
            # site when I asked for one small thing".
            if patch and patch.get("style_changes") and live_files and trigger != "rebuild":
                # No model call at all. The values were decided by the parser, checked
                # against the live stylesheet in the chat handler, and are applied here by
                # editing the declarations in place -- so this route cannot truncate a
                # file, cannot restyle something nobody asked about, and cannot come back
                # byte-identical without saying so.
                files, changed = apply_style_changes(live_files, patch["style_changes"])
                usage = {"model": "", "input_tokens": 0, "output_tokens": 0, "requests": 0}
                log_event(logger, "style.applied", changes=changed)
            elif patch and patch.get("instruction") and live_files and trigger != "rebuild":
                files, usage = await patch_site_files(
                    live_files,
                    patch["instruction"],
                    patch.get("targets") or [],
                    user_message=patch.get("user_message"),
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
        except StyleAlreadySet as exc:
            # Every value asked for is already in force. Normally the chat handler catches
            # this before anything is queued; reaching here means the site moved between
            # the check and the build. Either way it is not a fault, so say what is
            # actually set rather than apologising for a failure that did not happen.
            await _mark_failed(session, business, site_version, "already_set", str(exc))
            await notify_owner_failure(bot, business, "already_set", detail=str(exc))
            await _record_no_change(business, patch)
            return
        except StyleOpFailed as exc:
            await _mark_failed(session, business, site_version, "not_applied", str(exc))
            await notify_owner_failure(bot, business, "not_applied")
            await _record_no_change(business, patch)
            return
        except EditNotApplied as exc:
            # Must precede GenerationFailed: it's a subclass, and this case is not a fault
            # in the site -- the change simply didn't land, and the owner needs to know
            # that rather than being congratulated on an unchanged site.
            await _mark_failed(session, business, site_version, "not_applied", str(exc))
            await notify_owner_failure(bot, business, "not_applied")
            await _record_no_change(business, patch)
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

        log_event(
            logger, "stage.completed", stage="generate",
            duration_ms=int((time.monotonic() - stage_started) * 1000),
            files=len(files), api_requests=usage.get("requests"),
            tokens=usage["input_tokens"] + usage["output_tokens"], model=usage["model"],
        )

        # Pre-flight: everything checkable from the markup alone, before a container
        # exists. A build doomed by an empty link now fails (and gets repaired) in
        # milliseconds instead of after a ~40-90s sandbox it was always going to lose.
        stage_started = time.monotonic()
        preflight = validate_files(files)
        if failed_checks(preflight):
            try:
                files, repair_usage, remaining = await repair_files(
                    files, preflight,
                    session=session,
                    owner_telegram_id=business.owner_telegram_id,
                    business_id=business.id,
                )
            except Exception as exc:
                logger.exception("repair raised for %s", business_id)
                repair_usage, remaining = None, failed_checks(preflight)
            if repair_usage:
                usage["input_tokens"] += repair_usage["input_tokens"]
                usage["output_tokens"] += repair_usage["output_tokens"]
            if remaining:
                detail = _describe_checks(remaining)
                site_version.files = files
                await _mark_failed(session, business, site_version, "preflight", detail)
                await notify_owner_failure(bot, business, "sandbox", detail=detail)
                return
        log_event(
            logger, "stage.completed", stage="preflight",
            duration_ms=int((time.monotonic() - stage_started) * 1000),
        )

        site_version.status = "testing"
        business.generation_status = "testing"
        await session.commit()
        await notify_owner_progress(bot, business, "testing")

        stage_started = time.monotonic()
        # An edit is measured against what it replaces: both versions are served in the
        # same sandbox and photographed, so "did this change anything?" stops being a
        # question about bytes and becomes one about pixels.
        # Strip the analytics beacon before anything is tested. It is added at deploy and
        # cannot reach Cloudflare from inside the sandbox, so leaving it in made every
        # page log a failed request, failed `no_console_errors`, and commissioned a
        # four-page repair for a script that was never broken. Deploy puts it back.
        files = strip_beacon(files)
        compare_against = strip_beacon(live_files) if trigger == "edit" else None
        style_only = _is_stylesheet_only(patch)
        try:
            report = await sandbox_test(
                files, previous_files=compare_against, require_visible_change=style_only
            )
        except Exception as exc:
            logger.exception("run_generation_pipeline: sandbox test failed for %s", business_id)
            await _mark_failed(session, business, site_version, "unknown", str(exc))
            await notify_owner_failure(bot, business, "unknown")
            return

        # PNG bytes: they belong in a Telegram message, not in a JSONB column.
        screenshots = report.pop("screenshots", {}) or {}
        site_version.sandbox_report = report
        site_version.sandbox_status = "passed" if report["passed"] else "failed"

        if _rendered_identically(report):
            # The bytes moved and the page did not. Three real versions shipped this way,
            # each announced as live, each looking exactly like the one before it.
            await _mark_failed(
                session, business, site_version, "not_applied",
                "the new version renders exactly like the current one",
            )
            await notify_owner_failure(bot, business, "not_applied")
            await _record_no_change(business, patch)
            return
        if not report["passed"]:
            # One repair round on browser-only defects too, then re-test. Anything the
            # markup-level checks could see was already fixed before we got here.
            broken = [c for c in report["checks"] if not c["passed"]]
            before_repair = files
            try:
                files, repair_usage, _ = await repair_files(
                    files, broken,
                    session=session,
                    owner_telegram_id=business.owner_telegram_id,
                    business_id=business.id,
                )
                if repair_usage:
                    usage["input_tokens"] += repair_usage["input_tokens"]
                    usage["output_tokens"] += repair_usage["output_tokens"]
                # Re-test whenever the repair actually moved the files. Keying this on
                # repair_usage instead meant a free deterministic fix -- sanitizing, which
                # costs no call and reports no usage -- was never re-tested, and the build
                # failed on the stale report of a file it had already corrected.
                if files != before_repair:
                    report = await sandbox_test(
                        files, previous_files=compare_against,
                        require_visible_change=style_only,
                    )
                    screenshots = report.pop("screenshots", {}) or screenshots
                    site_version.sandbox_report = report
                    site_version.sandbox_status = "passed" if report["passed"] else "failed"
            except Exception:
                logger.exception("sandbox repair raised for %s", business_id)

            if not report["passed"]:
                detail = _describe_checks([c for c in report["checks"] if not c["passed"]])
                # Keep the files that failed. Without them a sandbox failure leaves only a
                # one-line reason and no way to see the page that caused it -- diagnosing
                # the first broken-JavaScript build meant regenerating a whole site to
                # guess at what the original had looked like. Safe to store: a version is
                # only ever served once current_version_id points at it, which happens
                # after a successful deploy and never here.
                site_version.files = files
                await _mark_failed(session, business, site_version, "sandbox", detail)
                await notify_owner_failure(bot, business, "sandbox", detail=detail)
                return

        log_event(
            logger, "stage.completed", stage="sandbox",
            duration_ms=int((time.monotonic() - stage_started) * 1000),
            checks_passed=sum(1 for c in report["checks"] if c["passed"]),
            checks_total=len(report["checks"]),
        )

        site_version.status = "deploying"
        business.generation_status = "deploying"
        await session.commit()
        await notify_owner_progress(bot, business, "deploying")

        stage_started = time.monotonic()
        try:
            # `files` comes back rewritten: a first build learns its own address here,
            # and the version record has to hold what was actually served.
            project_name, live_url, files = await deploy_to_cloudflare_pages(business, files)
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
        log_event(
            logger, "stage.completed", stage="deploy",
            duration_ms=int((time.monotonic() - stage_started) * 1000), url=live_url,
        )
        log_event(
            logger, "build.succeeded",
            duration_ms=int((time.monotonic() - started) * 1000),
            url=live_url, tokens_total_for_owner=used,
            tokens=usage["input_tokens"] + usage["output_tokens"],
        )
        await notify_owner_success(
            bot, business, usage=usage, remaining=max(FREE_TIER_TOKEN_LIMIT - used, 0),
            screenshot=screenshots.get("after"),
            parse_tokens=(patch or {}).get("parse_tokens", 0),
        )


CHECK_EXPLANATIONS = {
    "contact_links_valid": "a contact link had no address behind it",
    "html_well_formed": "one of the pages had broken HTML",
    "script_sources_valid": "the page loaded a script file that isn't part of your site",
    "javascript_parses": "the page's own code had a syntax error",
    "internal_links_valid": "a menu link pointed at a page that doesn't exist",
    "content_present": "one of the pages came out too short",
    "images_have_src": "an image had no file behind it",
    "css_loads": "the stylesheet didn't load",
    "no_console_errors": "the page reported errors when it opened",
    "images_resolve": "an image on the page couldn't be loaded",
    "page_loads": "one of the pages didn't open properly",
    "page_visibly_changed": "the new version looked exactly like the current one",
    "fits_every_screen": "a page didn't fit the screen on phones or laptops",
}


async def _link_edit_log(session: AsyncSession, patch: dict | None, version_id: uuid.UUID) -> None:
    """Point the owner's message at the version it produced.

    The column has existed since the first migration and had never once been written, so
    none of the 19 versions on the site that prompted this work could be traced back to
    the message that caused it without matching timestamps by hand.
    """
    edit_log_id = (patch or {}).get("edit_log_id")
    if not edit_log_id:
        return
    try:
        entry = await session.get(EditLog, uuid.UUID(str(edit_log_id)))
        if entry is not None:
            entry.triggered_version_id = version_id
            await session.commit()
    except Exception:
        logger.warning("could not link edit_log %s to version", edit_log_id, exc_info=True)


def _is_stylesheet_only(patch: dict | None) -> bool:
    """Whether this edit touches nothing but the stylesheet.

    A style change that renders identically to the version before it did nothing, with no
    other explanation available. The same cannot be said of a page edit.
    """
    if not patch:
        return False
    if patch.get("style_changes"):
        return True
    return set(patch.get("targets") or []) == {"style.css"}


def _rendered_identically(report: dict) -> bool:
    """True when the before/after comparison ran and found no visible difference."""
    return any(
        check["name"] == "page_visibly_changed" and not check["passed"]
        for check in report.get("checks", [])
    )


def _describe_checks(checks: list[dict]) -> str:
    """Turn failed checks into something an owner can actually act on."""
    names = [c["name"] for c in checks]
    described = [CHECK_EXPLANATIONS.get(n, n.replace("_", " ")) for n in names]
    seen: list[str] = []
    for item in described:
        if item not in seen:
            seen.append(item)
    return "; ".join(seen)


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
    # One event for every failure, whatever the cause -- this is what makes "which stage
    # fails most often, and why" answerable instead of a guess.
    log_event(logger, "build.failed", logging.ERROR, stage=stage, error=detail[:300])
