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

# The screenshot size, unchanged: this is what the owner is sent and what the before/after
# comparison is measured on, so it must stay fixed even though the checks now resize.
SHOT_WIDTH, SHOT_HEIGHT = 1280, 900

# The widths every page is measured at. Chosen to be the edges people actually own rather
# than a tidy ladder: a small phone, the tablet/large-phone crossover, the narrowest common
# laptop, the laptop the sandbox already used, and a full desktop monitor. 1280 is included
# even though it was already the only size tested -- dropping it would lose the one width
# with a track record.
LAYOUT_WIDTHS = (
    (390, "a phone"),
    (768, "a tablet"),
    (1024, "a small laptop"),
    (1280, "a laptop"),
    (1920, "a desktop monitor"),
)

# The widths a desktop-only fault actually shows at. Both are machines an owner reported
# on: the 1920 monitor, and the 1440 laptop that sits just above 1024px -- the largest
# breakpoint most generated stylesheets bother to write, so everything wider than it
# inherits rules that were reasoned about for a phone.
DESKTOP_WIDTHS = ((1440, "a laptop"), (1920, "a desktop monitor"))

# A button or badge sized to its own text is never this wide. 420px clears the longest
# real label ("Book a consultation today" at a generous size) and sits far below the
# 1116px slab that prompted this check -- a hero call-to-action stretched edge to edge
# across the content column because nothing ever told it not to.
CONTROL_MAX_PX = 420

# A band is full-bleed by definition in the contract: its background runs to both screen
# edges and a `.container` inside holds the words. One that stops short is unambiguous --
# it is the one element on the page not touching the edges, and it reads as a floating box.
BAND_MIN_FRACTION = 0.98

# Both faults are invisible below ~1100px, because at phone width "as wide as the column"
# is the correct answer for a button and the bands are the full screen anyway. That is why
# every existing check passes them: they are not wrong until the screen is wide.
DESKTOP_PROBE = """
(limits) => {
  const vw = window.innerWidth;
  const name = (el) => {
    const cls = (el.className && el.className.baseVal !== undefined)
      ? el.className.baseVal : (el.className || '');
    return (typeof cls === 'string' && cls.trim())
      ? '.' + cls.trim().split(/\\s+/)[0]
      : el.tagName.toLowerCase();
  };

  // Controls that should hug their text. A submit button inside a form is excluded on
  // purpose: a form is already a narrow column, and a full-width submit in it is a real
  // design people choose rather than a mistake.
  const stretched = new Map();
  const CONTROLS = '.btn, .btn-primary, .btn-secondary, .btn-ghost, .button, button,' +
                   ' .badge, .pill, .tag, .chip, .eyebrow';
  for (const el of document.querySelectorAll(CONTROLS)) {
    if (el.closest('form')) continue;
    const r = el.getBoundingClientRect();
    if (r.height <= 0 || r.width <= limits.control) continue;
    stretched.set(name(el), Math.round(r.width));
  }

  // Bands whose background should reach both edges.
  const short = new Map();
  const BANDS = '.cta-band, .section-alt, .site-footer, .hero, .page-hero';
  for (const el of document.querySelectorAll(BANDS)) {
    const r = el.getBoundingClientRect();
    if (r.height <= 0 || r.width >= vw * limits.band) continue;
    short.set(name(el), Math.round(r.width));
  }

  return {
    stretched: [...stretched].map(([k, w]) => k + ' is ' + w + 'px wide'),
    short: [...short].map(([k, w]) => k + ' stops at ' + w + 'px of ' + vw + 'px'),
  };
}
"""

# Sideways scroll, plus the widest offending element so a repair has something to aim at.
# `documentElement.scrollWidth` is the whole rendered page, so this catches an element that
# pushes the page wide even when nothing visibly hangs off the edge.
OVERFLOW_PROBE = """() => {
  const vw = document.documentElement.clientWidth;
  const overflow = document.documentElement.scrollWidth > vw + 1;
  let widest = null, widestPx = vw + 1;
  if (overflow) {
    for (const el of document.querySelectorAll('body *')) {
      const r = el.getBoundingClientRect();
      if (r.right > widestPx || r.width > widestPx) {
        widestPx = Math.max(r.right, r.width);
        widest = el.tagName.toLowerCase() + (
          typeof el.className === 'string' && el.className.trim()
            ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.')
            : ''
        ) + ' (' + Math.round(Math.max(r.right, r.width)) + 'px wide in ' + vw + 'px)';
      }
    }
  }
  return {overflow, widest};
}"""

# Deliberately broken output for proving the harness actually catches failures: a thrown
# JS error and a broken <img> reference. Costs no API calls.
#
# The script element itself is no longer the defect -- sites may ship JavaScript now. What
# this fixture proves is the check that replaced no_script_tags as the real guard: a script
# that throws must fail the build, because that is exactly what a broken page looks like.
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
        thin_pages: list[str] = []
        broken_images: list[str] = []
        bad_contact: list[str] = []
        broken_internal: list[str] = []
        overflowing: list[str] = []
        desktop_faults: list[str] = []
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
            thin_pages += result["thin_pages"]
            broken_images += result["broken_images"]
            bad_contact += result["bad_contact"]
            broken_internal += result["broken_internal"]
            overflowing += result["overflowing"]
            desktop_faults += result["desktop_faults"]
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
        # The only check here that asks whether the page *fits the screen it is on*. Every
        # other one would pass a site that scrolls sideways on every phone.
        checks.append(
            {"name": "fits_every_screen", "passed": not overflowing, "detail": overflowing}
        )
        # Fitting the screen and being laid out for it are different questions, and the
        # check above only answers the first. A site can fit a 1920 monitor perfectly and
        # still show a 1116px button, because nothing is overflowing -- it is just wrong.
        checks.append(
            {"name": "works_on_desktop", "passed": not desktop_faults, "detail": desktop_faults}
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
    page = await browser.new_page(viewport={"width": SHOT_WIDTH, "height": SHOT_HEIGHT})
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
        "failed_loads": [], "console_errors": [], "page_errors": [],
        "thin_pages": [], "broken_images": [], "bad_contact": [], "broken_internal": [],
        "overflowing": [],
        "desktop_faults": [],
    }
    out["css_status"] = None  # type: ignore[assignment]

    page = await browser.new_page()
    responses: dict[str, int] = {}
    # Prefixed with the page name so a repair can be aimed at the file that produced it:
    # files_needing_repair() reads the filename off the front of each detail, and a bare
    # "Unexpected token ')'" belongs to no file, so it got broadcast to all of them.
    def _record_console(msg) -> None:
        if msg.type != "error":
            return
        # A page is allowed to load a font, an icon pack or a CSS library from a CDN, and
        # none of those can be reached from inside an isolated sandbox. The browser logs a
        # failed request for each, which is a fact about the sandbox's network and not
        # about the owner's site -- and acting on it commissioned a whole-site repair for
        # a script that was never broken. Only errors raised by the page's own code count.
        source = (msg.location or {}).get("url") or ""
        if source and not source.startswith(base_url):
            return
        out["console_errors"].append(f"{page_name}: {msg.text}")

    page.on("console", _record_console)
    page.on("pageerror", lambda exc: out["page_errors"].append(f"{page_name}: {exc}"))
    page.on("response", lambda resp: responses.update({resp.url: resp.status}))

    try:
        await page.goto(f"{base_url}/{page_name}", wait_until="networkidle", timeout=30000)
    except Exception as exc:
        out["failed_loads"].append(f"{page_name}: {exc}")
        await page.close()
        return out

    out["css_status"] = responses.get(f"{base_url}/style.css")  # type: ignore[assignment]

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

    out["overflowing"] += await _overflow_at_each_width(page, page_name)
    out["desktop_faults"] += await _desktop_faults(page, page_name)

    await page.close()
    return out


async def _overflow_at_each_width(page, page_name: str) -> list[str]:
    """Widths where the page scrolls sideways, and the element responsible.

    Every check above this one is functional -- does it load, do the images resolve, is the
    markup well formed. None of them looks at *layout*, and the single 1280px viewport
    everything ran at is one laptop size out of the range real people use. So a site could
    pass all eight checks and still be broken on a phone or a large monitor, and three
    owners reported exactly that: fine on mobile, "inconsistent" on a laptop.

    Sideways scroll is the one layout fault worth failing a build over: it is unambiguous
    (no judgement about whether a design looks right), it is always a defect, and it is what
    a person actually notices. The viewport is resized on the page already open rather than
    reloading it, so the whole sweep costs a few hundred milliseconds.
    """
    found: list[str] = []
    for width, label in LAYOUT_WIDTHS:
        try:
            await page.set_viewport_size({"width": width, "height": 900})
            result = await page.evaluate(OVERFLOW_PROBE)
        except Exception:
            logger.warning("could not measure %s at %dpx", page_name, width, exc_info=True)
            continue
        if result["overflow"]:
            culprit = result["widest"] or "unknown element"
            # Deliberately prefixed with style.css, not the page. `files_needing_repair`
            # routes a problem to whatever filename it starts with, and a page that
            # scrolls sideways is almost always a width, a min-width or a grid in the
            # stylesheet -- sending the repair at the HTML would rewrite the wrong file.
            found.append(
                f"style.css: {page_name} scrolls sideways on {label} ({width}px) "
                f"-- {culprit}"
            )
    # Put it back, so the screenshot the owner is sent is the size it was always taken at.
    await page.set_viewport_size({"width": SHOT_WIDTH, "height": SHOT_HEIGHT})
    return found


async def _desktop_faults(page, page_name: str) -> list[str]:
    """Layout that is only wrong once the screen is wide.

    The sweep above asks one question -- does the page scroll sideways -- and a site can be
    plainly broken on a monitor while answering no. Both faults below did exactly that on a
    live site: a "Book a demo" button 1116px wide, and a call-to-action band that stopped
    370px short of each edge while every other band ran the full 1920. Nine checks passed.

    They share a cause. A stylesheet whose largest media query is `max-width: 1024px` has
    no rule that applies to a monitor at all, so the desktop layout is whatever the mobile
    reasoning left behind -- and on a phone, a full-width button and a band as wide as the
    screen are both exactly right. The fault is not in the rule, it is in its absence.

    Only faults with no design judgement in them are reported, on the same principle as the
    overflow check: failing a build costs a repair pass, so a check that fires on a page
    somebody meant to look that way is worse than no check. "This section leaves half the
    column empty" is a real complaint and is deliberately not here -- a left-aligned hero
    over a photograph measures identically and is a perfectly good design.
    """
    found: list[str] = []
    limits = {"control": CONTROL_MAX_PX, "band": BAND_MIN_FRACTION}
    for width, label in DESKTOP_WIDTHS:
        try:
            await page.set_viewport_size({"width": width, "height": 900})
            result = await page.evaluate(DESKTOP_PROBE, limits)
        except Exception:
            logger.warning("could not measure %s at %dpx", page_name, width, exc_info=True)
            continue
        # Prefixed with style.css for the same reason as the overflow check: a width is set
        # in the stylesheet, and `files_needing_repair` routes by the leading filename.
        for fault in result["stretched"]:
            found.append(
                f"style.css: on {label} ({width}px) {page_name} {fault} -- a button or badge "
                f"should be as wide as its text, not as wide as the column"
            )
        for fault in result["short"]:
            found.append(
                f"style.css: on {label} ({width}px) {page_name} {fault} -- a full-width band "
                f"must reach both screen edges, with a .container inside holding its content"
            )
    await page.set_viewport_size({"width": SHOT_WIDTH, "height": SHOT_HEIGHT})
    return found


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
