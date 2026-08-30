"""A site can fit a monitor perfectly and still be laid out for a phone.

`fits_every_screen` asks one question -- does the page scroll sideways -- and a live site
answered no while showing a 1116px-wide "Book a demo" button and a call-to-action band that
stopped 370px short of each screen edge. Nine checks passed. The owner's report was "mobile
is very great but for desktop some elements are not aligned", which is exactly right: at
390px both of those are the correct answer, and nothing in the build ever looked wider.

These tests pin the check that closes that hole, and the two limits it draws the line at.
The browser tests build the failure by hand rather than trusting the description of it.
"""

import pytest

from worker.codegen import validate
from worker.tasks import sandbox
from worker.tasks.generate import CHECK_EXPLANATIONS

CHECK = "works_on_desktop"


# --------------------------------------------------- what gets measured, and where

def test_the_sweep_covers_the_monitor_and_the_laptop_above_the_last_breakpoint():
    widths = [w for w, _ in sandbox.DESKTOP_WIDTHS]
    assert max(widths) >= 1920, "the monitor the fault was reported on has to be measured"
    assert any(1300 <= w <= 1500 for w in widths), (
        "1025-1439px is the range no generated stylesheet writes a rule for"
    )


def test_the_limits_leave_room_for_a_real_button():
    """Too tight and every long label fails a build that was fine."""
    assert sandbox.CONTROL_MAX_PX >= 360, "a wordy button label would fail a good page"
    assert sandbox.CONTROL_MAX_PX <= 600, "a limit this loose would have passed the 1116px slab"
    assert 0.9 <= sandbox.BAND_MIN_FRACTION < 1.0, "a band must be allowed a scrollbar's slack"


def test_a_form_submit_is_not_treated_as_a_stretched_button():
    """A form is already a narrow column and a full-width submit inside one is a design
    people choose. Failing it would fail the enquiry form this project just shipped."""
    assert "el.closest('form')" in sandbox.DESKTOP_PROBE


# --------------------------------------------------- the finding reaches someone

def test_the_check_has_an_explanation_an_owner_can_read():
    assert CHECK in CHECK_EXPLANATIONS
    text = CHECK_EXPLANATIONS[CHECK]
    for jargon in ("viewport", "px", "css", "breakpoint", "flex"):
        assert jargon not in text.lower(), f"{jargon!r} is not a word a shop owner uses"


def test_a_desktop_fault_sends_the_repair_at_the_stylesheet():
    """A width is set in the stylesheet. Routed at the page, the repair rewrites the markup
    and the button is still 1116px wide."""
    detail = (
        "style.css: on a desktop monitor (1920px) index.html .btn is 1116px wide "
        "-- a button or badge should be as wide as its text, not as wide as the column"
    )
    per_file = validate.files_needing_repair(
        [{"name": CHECK, "passed": False, "detail": [detail]}],
        {"index.html": "", "style.css": ""},
    )
    assert list(per_file) == ["style.css"]
    assert "index.html" in per_file["style.css"][0], "the page is still named in the text"


def test_the_per_page_result_carries_the_finding():
    import inspect

    assert '"desktop_faults"' in inspect.getsource(sandbox._check_page)


# --------------------------------------------------- measured in a real browser

async def _measure(html: str, css: str, width: int = 1920) -> dict:
    """Run the real probe against real markup in real Chromium."""
    playwright = pytest.importorskip("playwright.async_api")
    async with playwright.async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": width, "height": 900})
        await page.set_content(f"<style>{css}</style>{html}")
        result = await page.evaluate(
            sandbox.DESKTOP_PROBE,
            {"control": sandbox.CONTROL_MAX_PX, "band": sandbox.BAND_MIN_FRACTION},
        )
        await browser.close()
    return result


GOOD_CSS = """
* { margin: 0; box-sizing: border-box; }
.container { width: 100%; max-width: 1180px; margin: 0 auto; }
.hero, .cta-band, .site-footer { width: 100%; padding: 2rem 0; }
.btn { display: inline-block; padding: .7rem 1.4rem; }
"""

PAGE = """
<section class="hero"><div class="container"><a class="btn" href="#">Book a demo</a></div></section>
<section class="cta-band"><div class="container"><p>Ready?</p></div></section>
<footer class="site-footer"><div class="container"><p>&copy; 2026</p></div></footer>
"""


@pytest.mark.asyncio
async def test_a_well_built_page_reports_nothing():
    result = await _measure(PAGE, GOOD_CSS)
    assert result["stretched"] == []
    assert result["short"] == []


@pytest.mark.asyncio
async def test_the_button_that_started_this_is_caught():
    """The live failure: `display: block` on the hero call-to-action. Correct at 390px,
    a 1116px slab on a monitor, and invisible to every check that existed."""
    css = GOOD_CSS + ".btn { display: block; width: 100%; }"
    result = await _measure(PAGE, css)

    assert len(result["stretched"]) == 1
    assert ".btn" in result["stretched"][0]
    assert "1180px" in result["stretched"][0], "the measured width has to be in the message"


@pytest.mark.asyncio
async def test_the_capped_band_that_started_this_is_caught():
    """The other half: `.cta-band` given a max-width of its own, so its colour floats in
    the middle of a 1920 monitor while the hero and footer run edge to edge."""
    css = GOOD_CSS + ".cta-band { max-width: 1180px; margin: 0 auto; }"
    result = await _measure(PAGE, css)

    assert len(result["short"]) == 1
    assert ".cta-band" in result["short"][0]
    assert "1920" in result["short"][0], "the screen width has to be in the message"


@pytest.mark.asyncio
async def test_neither_fault_fires_on_a_phone():
    """The whole point: at 390px a full-width button and a full-width band are correct.
    A check that fired here would fail every well-built site."""
    css = GOOD_CSS + ".btn { display: block; width: 100%; }"
    result = await _measure(PAGE, css, width=390)

    assert result["stretched"] == [], "a full-width button is right on a phone"
    assert result["short"] == []
