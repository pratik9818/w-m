import asyncio
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

SANDBOX_TIMEOUT_SECONDS = 60
READY_POLL_TIMEOUT_SECONDS = 15
READY_POLL_INTERVAL_SECONDS = 0.5
SERVER_SESSION_ID = "site-server"
CONTACT_HREF_PATTERN = re.compile(r"^(tel:|mailto:)\S+$")

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


async def sandbox_test(files: dict[str, str]) -> dict:
    """Serve `files` in an isolated Daytona sandbox and smoke-test them with Playwright.

    Returns a report shaped like the (currently unused) site_versions.sandbox_report
    column: {"passed": bool, "checks": [...], "console_errors": [...]}.
    """
    checks: list[dict] = []
    console_errors: list[str] = []
    sandbox = None
    daytona = AsyncDaytona(DaytonaConfig(api_key=get_settings().daytona_api_key))

    try:
        sandbox = await daytona.create(
            CreateSandboxFromSnapshotParams(language="python", public=True),
            timeout=SANDBOX_TIMEOUT_SECONDS,
        )

        home_dir = await sandbox.get_user_home_dir()
        site_dir = f"{home_dir}/site"
        await sandbox.fs.create_folder(site_dir, "755")
        for filename, content in files.items():
            await sandbox.fs.upload_file(content.encode("utf-8"), f"{site_dir}/{filename}")

        await sandbox.process.create_session(SERVER_SESSION_ID)
        await sandbox.process.execute_session_command(
            SERVER_SESSION_ID,
            SessionExecuteRequest(
                command=f"cd {site_dir} && python3 -m http.server 8000", run_async=True
            ),
        )

        preview = await sandbox.get_preview_link(8000)
        base_url = preview.url
        index_url = f"{base_url}/index.html"
        await _wait_until_ready(index_url)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page()

            page_errors: list[str] = []
            responses: dict[str, int] = {}
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
            )
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            page.on("response", lambda resp: responses.update({resp.url: resp.status}))

            load_ok, load_detail = True, ""
            try:
                await page.goto(index_url, wait_until="networkidle", timeout=15000)
            except Exception as exc:
                load_ok, load_detail = False, str(exc)
            checks.append({"name": "page_loads", "passed": load_ok, "detail": load_detail})

            all_errors = console_errors + page_errors
            checks.append(
                {"name": "no_console_errors", "passed": not all_errors, "detail": all_errors}
            )

            script_count = await page.locator("script").count()
            checks.append(
                {
                    "name": "no_script_tags",
                    "passed": script_count == 0,
                    "detail": f"{script_count} <script> tag(s) found",
                }
            )

            css_status = responses.get(f"{base_url}/style.css")
            checks.append(
                {"name": "css_loads", "passed": css_status == 200, "detail": f"status={css_status}"}
            )

            title = await page.title()
            heading_count = await page.locator("h1, h2").count()
            checks.append(
                {
                    "name": "content_present",
                    "passed": bool(title.strip()) and heading_count > 0,
                    "detail": f"title={title!r}, headings={heading_count}",
                }
            )

            img_srcs = await page.locator("img").evaluate_all("els => els.map(e => e.src)")
            broken_images = [src for src in img_srcs if responses.get(src) != 200]
            checks.append(
                {"name": "images_resolve", "passed": not broken_images, "detail": broken_images}
            )

            contact_hrefs = await page.locator("a[href^='tel:'], a[href^='mailto:']").evaluate_all(
                "els => els.map(e => e.getAttribute('href'))"
            )
            bad_contact = [h for h in contact_hrefs if not CONTACT_HREF_PATTERN.match(h or "")]
            checks.append(
                {"name": "contact_links_valid", "passed": not bad_contact, "detail": bad_contact}
            )

            await browser.close()
    finally:
        if sandbox is not None:
            await sandbox.delete()
        await daytona.close()

    passed = bool(checks) and all(c["passed"] for c in checks)
    return {"passed": passed, "checks": checks, "console_errors": console_errors}


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
