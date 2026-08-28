"""A generated site has to fit the screen it is opened on, and something has to check that.

Owners reported sites that were fine on a phone and inconsistent on a laptop. The cause was
structural rather than any one bad stylesheet: every sandbox check was functional -- does it
load, do the images resolve, is the markup well formed -- and all of them ran at a single
1280px viewport. Nothing in the build had ever looked at layout, at any width, so a site
could pass all eight checks and still not fit the screen.

These tests pin the check that closes that hole, and the wiring around it: a check nobody
can act on is not much better than no check.
"""

import pytest

from worker.codegen import validate
from worker.tasks import sandbox
from worker.tasks.generate import CHECK_EXPLANATIONS

CHECK = "fits_every_screen"


# --------------------------------------------------- the widths that get measured

def test_the_sweep_covers_phone_through_desktop_monitor():
    widths = [w for w, _ in sandbox.LAYOUT_WIDTHS]
    assert min(widths) <= 400, "a small phone has to be in the sweep"
    assert max(widths) >= 1920, "a desktop monitor has to be in the sweep"


def test_the_laptop_range_is_not_skipped():
    """The gap that caused this: stylesheets whose largest breakpoint was 1024px left every
    real laptop untested."""
    widths = [w for w, _ in sandbox.LAYOUT_WIDTHS]
    assert any(1100 <= w <= 1500 for w in widths), "no laptop width is measured"
    assert 1280 in widths, "the one width with a track record must not be dropped"


def test_every_width_is_described_in_words_an_owner_would_use():
    for _, label in sandbox.LAYOUT_WIDTHS:
        assert label and not label[0].isdigit(), f"{label!r} reads like a number, not a place"


def test_the_screenshot_size_is_fixed_independently_of_the_sweep():
    """The checks resize the page; the owner's screenshot and the before/after comparison
    must not move with them, or every edit would look like it changed the layout."""
    assert (sandbox.SHOT_WIDTH, sandbox.SHOT_HEIGHT) == (1280, 900)


# --------------------------------------------------- the finding reaches someone

def test_the_check_has_an_explanation_an_owner_can_read():
    assert CHECK in CHECK_EXPLANATIONS
    text = CHECK_EXPLANATIONS[CHECK]
    assert "screen" in text.lower()
    for jargon in ("viewport", "overflow", "px", "css"):
        assert jargon not in text.lower(), f"{jargon!r} is not a word a shop owner uses"


def test_a_sideways_scroll_sends_the_repair_at_the_stylesheet():
    """Almost every cause is a width, a min-width or a grid in the stylesheet. Routed at the
    page instead, the repair rewrites the HTML and the site still does not fit."""
    detail = (
        "style.css: services.html scrolls sideways on a phone (390px) "
        "-- div.card-grid (520px wide in 390px)"
    )
    per_file = validate.files_needing_repair(
        [{"name": CHECK, "passed": False, "detail": [detail]}],
        {"index.html": "", "services.html": "", "style.css": ""},
    )
    assert list(per_file) == ["style.css"]
    assert "services.html" in per_file["style.css"][0], "the page is still named in the text"


def test_the_probe_asks_for_the_element_responsible():
    """A bare "it scrolls sideways" gives a repair nothing to aim at."""
    assert "widest" in sandbox.OVERFLOW_PROBE
    assert "getBoundingClientRect" in sandbox.OVERFLOW_PROBE
    assert "clientWidth" in sandbox.OVERFLOW_PROBE, (
        "innerWidth includes the scrollbar and reports a false overflow on desktop Chrome"
    )


def test_the_per_page_result_carries_the_finding():
    """_check_page returns per-page findings; a key missing here is a silent KeyError in
    the aggregation loop."""
    import inspect

    source = inspect.getsource(sandbox._check_page)
    assert '"overflowing"' in source


# --------------------------------------------------- the prompt asks for it too

def test_the_stylesheet_prompt_names_real_widths():
    from pathlib import Path

    prompt = Path("worker/codegen/prompts/stylesheet.md").read_text(encoding="utf-8")
    for width in ("390", "768", "1024", "1280", "1920"):
        assert width in prompt, f"{width} is measured but never asked for"
    assert "not** your largest size" in prompt or "not your largest size" in prompt
    assert "sideways" in prompt, "the rule the build enforces must appear in the prompt"
    # The large-screen half: a capped column rather than text stretched across a monitor.
    assert "max-width" in prompt and "centre" in prompt.lower()


@pytest.mark.parametrize("hint", ["auto-fit", "clamp(", "minmax("])
def test_the_prompt_prefers_rules_that_need_no_breakpoint(hint):
    from pathlib import Path

    prompt = Path("worker/codegen/prompts/stylesheet.md").read_text(encoding="utf-8")
    assert hint in prompt
