"""The design brief has to reach every part of a build, or it changes nothing.

These run entirely offline: both model calls are stubbed, so the test asserts the wiring —
that the fonts are linked in each page's head, that the palette is written into the
stylesheet as real CSS rather than merely requested in a prompt, and that both prompts
carry the same direction. That is the half of "make the sites look better" that can be
checked without spending a request.
"""

import pytest

from worker.codegen import builder
from worker.codegen.css_values import GENERATED_MARKER, effective_styles
from worker.codegen.design_brief import brief_block, design_tokens_css, fallback_brief

SPEC = {
    "name": "Ink & Iron",
    "category": "Tattoo studio",
    "theme": "bold",
    "layout": "landing",
    "tagline": "Hand-drawn work, booked one at a time",
    "services": [{"name": "Custom piece", "price_label": "from 6000"}],
    "photo_urls": [],
    "logo_url": None,
    "current_year": 2026,
}

PAGE_FRAGMENT = """===FILE: index.html===
<title>Ink &amp; Iron</title>
<meta name="description" content="Hand-drawn tattoo work in the old city.">
<main>
  <section class="hero"><div class="container hero-inner">
    <h1 class="hero-title">Hand-drawn work</h1>
    <p class="hero-subtitle">Booked one at a time.</p>
  </div></section>
  <section class="section"><div class="container">
    <h2 class="section-title">What we do</h2>
    <div class="card-grid"><div class="card">
      <h3 class="card-title">Custom piece</h3><p class="card-text">From 6000.</p>
    </div></div>
  </div></section>
</main>
===END==="""

STYLESHEET = """===FILE: style.css===
.hero { padding: 6rem 0; background: var(--color-accent); color: var(--color-accent-ink); }
.hero-title { font-size: clamp(3rem, 9vw, 6rem); }
.section { padding: 5rem 0; }
.card { background: var(--color-surface); border: 1px solid var(--color-border); }
.card-grid { display: grid; gap: 2rem; }
===END==="""


@pytest.fixture
def stubbed(monkeypatch):
    """Both model calls answered from canned text; records the prompts they were sent."""
    prompts: list[str] = []
    brief = fallback_brief("bold")

    async def fake_completion(prompt, *, reduced_reasoning=False):
        prompts.append(prompt)
        body = STYLESHEET if "the stylesheet only" in prompt else PAGE_FRAGMENT
        return body, {"model": "stub", "input_tokens": 10, "output_tokens": 20}

    async def fake_brief(spec, spec_json):
        return brief, {"model": "stub", "input_tokens": 5, "output_tokens": 5}

    monkeypatch.setattr(builder, "call_plain_completion", fake_completion)
    monkeypatch.setattr(builder, "make_design_brief", fake_brief)
    return prompts, brief


@pytest.mark.asyncio
async def test_the_fonts_are_linked_in_the_page_head(stubbed):
    _, brief = stubbed
    files, _ = await builder.build_site(SPEC)
    head = files["index.html"].split("</head>")[0]
    assert "fonts.googleapis.com" in head
    assert brief["display_font"].replace(" ", "+") in head
    assert brief["body_font"].replace(" ", "+") in head


@pytest.mark.asyncio
async def test_the_palette_is_written_into_the_stylesheet_not_just_asked_for(stubbed):
    _, brief = stubbed
    files, _ = await builder.build_site(SPEC)
    css = files["style.css"]
    # Below the marker, so an owner's later edit can still see and change it.
    generated = css.split(GENERATED_MARKER, 1)[1]
    assert brief["accent"] in generated
    assert f'"{brief["display_font"]}"' in generated
    styles, _unused = effective_styles(css)
    assert styles["hero-title"]["font-size"].startswith("clamp")


@pytest.mark.asyncio
async def test_both_calls_receive_the_same_direction(stubbed):
    prompts, brief = stubbed
    await builder.build_site(SPEC)
    assert len(prompts) == 2  # one landing page, one stylesheet
    block = brief_block(brief)
    for prompt in prompts:
        assert block in prompt


@pytest.mark.asyncio
async def test_the_brief_costs_are_counted_into_the_build(stubbed):
    _, _brief = stubbed
    _files, usage = await builder.build_site(SPEC)
    # two generation calls (10+20 each) plus the brief (5+5)
    assert usage["input_tokens"] == 25
    assert usage["output_tokens"] == 45
    assert usage["requests"] == 3


def test_the_tokens_block_gives_every_heading_the_display_face():
    brief = fallback_brief("classic")
    css = design_tokens_css(brief)
    styles, _ = effective_styles(css)
    expected = f'"{brief["display_font"]}", {brief["display_stack"]}'
    for name in ("hero-title", "section-title", "card-title", "cta-title", "footer-title"):
        assert styles[name]["font-family"] == expected
    assert f"--color-accent: {brief['accent']}" in css
