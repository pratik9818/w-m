import asyncio
import logging
import re
import time

import httpx
from daytona import (
    AsyncDaytona,
    CreateSandboxFromSnapshotParams,
    DaytonaConfig,
    SessionExecuteRequest,
)
from playwright.async_api import async_playwright

from bot_api.config import get_settings
from worker.codegen.html_check import html_problems

logger = logging.getLogger(__name__)

SANDBOX_TIMEOUT_SECONDS = 60
READY_POLL_TIMEOUT_SECONDS = 15
READY_POLL_INTERVAL_SECONDS = 0.5
SERVER_SESSION_ID = "site-server"
# Where the previous version is served from, inside the same static server.
PREVIOUS_DIR = "__previous"
CONTACT_HREF_PATTERN = re.compile(r"^(tel:|mailto:)\S+$")
# Catches the "empty section heading with nothing under it" failure the prompt forbids --
# a page that renders but says almost nothing is a defect, not a pass.
MIN_PAGE_WORDS = 150

# Deliberately broken output for proving the harness actually catches failures:
# a <script> tag (violates Part 2's no-JS constraint), a thrown JS error, and a
# broken <img> reference. Costs no API calls.
BROKEN_FIXTURE: dict[str, str] = {
    "index.html": """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Broken Test Page</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <h1>Broken Test Page</h1>
  <img src="does-not-exist.png" alt="missing image">
  <script>throw new Error("deliberate test error");</script>
</body>
</html>
""",
    "style.css": "body { color: red; }\n",
}


async def sandbox_test(
    files: dict[str, str],
    previous_files: dict[str, str] | None = None,
    require_visible_change: bool = False,
) -> dict:
    """Serve `files` in an isolated Daytona sandbox and smoke-test them with Playwright.

    Returns {"passed": bool, "checks": [...], "console_errors": [...], "screenshots": {...}}.
    The screenshots hold raw PNG bytes and must be popped off before the report is stored
    in site_versions.sandbox_report, which is JSONB.

    When `previous_files` is given, the old version is served alongside the new one and
    both are photographed. With `require_visible_change`, a rendering identical to the
    previous version becomes a failed check -- the only one here that asks the question
    the owner actually cares about, *did anything change?* The other nine all passed,
    repeatedly, on versions that were pixel-for-pixel identical to the one before.
    """
    checks: list[dict] = []
    console_errors: list[str] = []
    screenshots: dict = {}
    sandbox = None
    daytona = AsyncDaytona(DaytonaConfig(api_key=get_settings().daytona_api_key))

    try:
        # Creation is the flakiest step and costs no tokens, so one retry is free
        # insurance -- a single 60s timeout destroyed a whole real build.
        for attempt in (1, 2):
            try:
                sandbox = await daytona.create(
                    CreateSandboxFromSnapshotParams(language="python", public=True),
                    timeout=SANDBOX_TIMEOUT_SECONDS,
                )
                break
            except Exception:
                if attempt == 2:
                    raise
                logger.warning("sandbox creation failed, retrying once", exc_info=True)

        home_dir = await sandbox.get_user_home_dir()
        site_dir = f"{home_dir}/site"
        await sandbox.fs.create_folder(site_dir, "755")
        for filename, content in files.items():
            await sandbox.fs.upload_file(content.encode("utf-8"), f"{site_dir}/{filename}")

        # The previous version goes in a subfolder of the same server, so one sandbox and
        # one browser can photograph both. Nothing here is ever deployed -- the deploy
        # step uploads the `files` dict, not the sandbox.
        if previous_files:
            previous_dir = f"{site_dir}/{PREVIOUS_DIR}"
            await sandbox.fs.create_folder(previous_dir, "755")
            for filename, content in previous_files.items():
                await sandbox.fs.upload_file(
                    content.encode("utf-8"), f"{previous_dir}/{filename}"
                )

        await sandbox.process.create_session(SERVER_SESSION_ID)
        await sandbox.process.execute_session_command(
            SERVER_SESSION_ID,
            SessionExecuteRequest(
                command=f"cd {site_dir} && python3 -m http.server 8000", run_async=True
            ),
        )

        preview = await sandbox.get_preview_link(8000)
        base_url = preview.url
        pages = [name for name in files if name.endswith(".html")]
        if not pages:
            raise RuntimeError("No HTML pages to test")
        pages.sort(key=lambda name: name != "index.html")  # index first
        await _wait_until_ready(f"{base_url}/{pages[0]}")

        failed_loads: list[str] = []
        page_errors: list[str] = []
        script_tags: list[str] = []
        thin_pages: list[str] = []
        broken_images: list[str] = []
        bad_contact: list[str] = []
        broken_internal: list[str] = []
        css_statuses: list[int | None] = []

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            # Every generated page gets the full check pass, not just index.html -- a
            # broken sub-page is just as public as a broken home page. Run them
            # concurrently: checking four pages one at a time was measured at ~250s of a
            # 696s pipeline, and the pages are entirely independent of each other.
            results = await asyncio.gather(
                *(_check_page(browser, base_url, name, files) for name in pages)
            )
            after_shots, before_shots = await _capture_versions(
                browser, base_url, pages, previous_files
            )
            await browser.close()

        for result in results:
            failed_loads += result["failed_loads"]
            console_errors += result["console_errors"]
            page_errors += result["page_errors"]
            script_tags += result["script_tags"]
            thin_pages += result["thin_pages"]
            broken_images += result["broken_images"]
            bad_contact += result["bad_contact"]
            broken_internal += result["broken_internal"]
            css_statuses.append(result["css_status"])

        css_status = next((s for s in css_statuses if s is not None), None)

        checks.append(
            {"name": "page_loads", "passed": not failed_loads, "detail": failed_loads}
        )
        all_errors = console_errors + page_errors
        checks.append(
            {"name": "no_console_errors", "passed": not all_errors, "detail": all_errors}
        )
        checks.append(
            {"name": "no_script_tags", "passed": not script_tags, "detail": script_tags}
        )
        checks.append(
            {"name": "css_loads", "passed": css_status == 200, "detail": f"status={css_status}"}
        )
        checks.append(
            {"name": "content_present", "passed": not thin_pages, "detail": thin_pages}
        )
        checks.append(
            {"name": "images_resolve", "passed": not broken_images, "detail": broken_images}
        )
        checks.append(
            {"name": "contact_links_valid", "passed": not bad_contact, "detail": bad_contact}
        )
        checks.append(
            {"name": "internal_links_valid", "passed": not broken_internal, "detail": broken_internal}
        )

        # Source-level, not DOM-level: Chromium repairs malformed markup before the DOM
        # exists, so every other check here is structurally blind to it. A real page with
        # `</h>` shipped and passed all nine of the checks above.
        malformed = [
            f"{name}: {problem}"
            for name in pages
            for problem in html_problems(files[name])
        ]
        checks.append(
            {"name": "html_well_formed", "passed": not malformed, "detail": malformed}
        )

        changed_pages = _visibly_changed(before_shots, after_shots)
        # Only a stylesheet-only edit can be judged on pixels alone, so only that caller
        # asks for the check. A page edit may legitimately render identically -- a link
        # repointed, a phone number changed behind the same words -- and failing those
        # would trade one false report for another.
        if before_shots and require_visible_change:
            checks.append({
                "name": "page_visibly_changed",
                "passed": bool(changed_pages),
                "detail": changed_pages or "every page renders exactly as it did before",
            })
        focus = changed_pages[0] if changed_pages else pages[0]
        screenshots = {
            "page": focus,
            "after": (after_shots.get(focus) or (None, None))[0],
            "before": (before_shots.get(focus) or (None, None))[0],
        }
    finally:
        if sandbox is not None:
            await sandbox.delete()
        await daytona.close()

    passed = bool(checks) and all(c["passed"] for c in checks)
    return {
        "passed": passed,
        "checks": checks,
        "console_errors": console_errors,
        "screenshots": screenshots,
    }


async def _capture_versions(
    browser, base_url: str, pages: list[str], previous_files: dict[str, str] | None
) -> tuple[dict, dict]:
    """Photograph every page of the new version, and of the old one where it exists."""
    after = dict(
        zip(pages, await asyncio.gather(*(
            _capture(browser, f"{base_url}/{name}") for name in pages
        )))
    )
    if not previous_files:
        return after, {}
    shared = [name for name in pages if name in previous_files]
    before = dict(
        zip(shared, await asyncio.gather(*(
            _capture(browser, f"{base_url}/{PREVIOUS_DIR}/{name}") for name in shared
        )))
    )
    return after, before


async def _capture(browser, url: str) -> tuple[bytes, bytes] | None:
    """(viewport PNG, full-page PNG) for one URL, or None if it would not render.

    The viewport shot is what the owner is sent; the full-page shot is what gets compared,
    because a change below the fold is still a change.
    """
    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
        return await page.screenshot(), await page.screenshot(full_page=True)
    except Exception:
        logger.warning("could not screenshot %s", url, exc_info=True)
        return None
    finally:
        await page.close()


def _visibly_changed(before: dict, after: dict) -> list[str]:
    """Pages whose rendering differs from the previous version.

    Byte comparison of two PNGs taken by the same browser in the same run: identical
    input renders to identical output, so a difference here is a real one. This is what
    would have caught the three edits that changed bytes in a stylesheet block the
    cascade ignores -- all three deployed, all nine checks green, page identical.
    """
    return [
        name for name, shot in before.items()
        if shot and after.get(name) and shot[1] != after[name][1]
    ]


async def _check_page(browser, base_url: str, page_name: str, files: dict[str, str]) -> dict:
    """Run the full check pass against one page in its own browser tab.

    Returns per-page findings rather than mutating shared state, so these can safely run
    concurrently.
    """
    out: dict[str, list] = {
        "failed_loads": [], "console_errors": [], "page_errors": [], "script_tags": [],
        "thin_pages": [], "broken_images": [], "bad_contact": [], "broken_internal": [],
    }
    out["css_status"] = None  # type: ignore[assignment]

    page = await browser.new_page()
    responses: dict[str, int] = {}
    page.on(
        "console",
        lambda msg: out["console_errors"].append(msg.text) if msg.type == "error" else None,
    )
    page.on("pageerror", lambda exc: out["page_errors"].append(str(exc)))
    page.on("response", lambda resp: responses.update({resp.url: resp.status}))

    try:
        await page.goto(f"{base_url}/{page_name}", wait_until="networkidle", timeout=30000)
    except Exception as exc:
        out["failed_loads"].append(f"{page_name}: {exc}")
        await page.close()
        return out

    out["css_status"] = responses.get(f"{base_url}/style.css")  # type: ignore[assignment]

    script_count = await page.locator("script").count()
    if script_count:
        out["script_tags"].append(f"{page_name}: {script_count}")

    title = await page.title()
    heading_count = await page.locator("h1, h2").count()
    word_count = len((await page.locator("body").inner_text()).split())
    if not title.strip() or heading_count == 0 or word_count < MIN_PAGE_WORDS:
        out["thin_pages"].append(
            f"{page_name}: title={title!r}, headings={heading_count}, words={word_count}"
        )

    img_srcs = await page.locator("img").evaluate_all("els => els.map(e => e.src)")
    out["broken_images"] += [
        f"{page_name}: {src}" for src in img_srcs if responses.get(src) != 200
    ]

    contact_hrefs = await page.locator("a[href^='tel:'], a[href^='mailto:']").evaluate_all(
        "els => els.map(e => e.getAttribute('href'))"
    )
    out["bad_contact"] += [
        f"{page_name}: {h!r}" for h in contact_hrefs if not CONTACT_HREF_PATTERN.match(h or "")
    ]

    # Nav integrity: a link to a page we never generated is a live 404.
    hrefs = await page.locator("a[href]").evaluate_all(
        "els => els.map(e => e.getAttribute('href'))"
    )
    for href in hrefs:
        target = (href or "").split("#")[0].split("?")[0]
        if not target or "://" in target or ":" in target.split("/")[0]:
            continue
        if target.lstrip("./") not in files:
            out["broken_internal"].append(f"{page_name} -> {href!r}")

    await page.close()
    return out


async def _wait_until_ready(url: str) -> None:
    deadline = time.monotonic() + READY_POLL_TIMEOUT_SECONDS
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=5) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return
            except Exception as exc:
                last_error = exc
            await asyncio.sleep(READY_POLL_INTERVAL_SECONDS)
    raise RuntimeError(f"Sandbox server did not become ready in time: {last_error}")
