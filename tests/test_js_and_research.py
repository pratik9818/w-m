"""The site may now ship JavaScript, CDN assets and real photographs, and may look facts
up before it writes.

Each test here pins one half of that: the guards that were removed really are gone, and
the guards that replaced them really do catch the failure they were put there for. All
offline -- the research module's own client is stubbed, so no test spends a search.
"""

import pytest

from worker.codegen import builder, research, validate

SPEC = {
    "name": "NovaToken",
    "category": "Crypto project",
    "layout": "landing",
    "current_year": 2026,
}

BRIEF = {
    "display_font": "Bebas Neue", "display_stack": "sans-serif",
    "body_font": "Inter", "body_stack": "sans-serif",
    "accent": "#ff0066", "accent_ink": "#ffffff", "theme_mood": "bold",
    "signature": "hard edges", "palette": {}, "neutral": "#111111",
}


# --------------------------------------------------------------- JavaScript is allowed

def test_scripts_are_no_longer_stripped_out():
    html = '<main><p>Hi</p><script>document.body.dataset.ok = "1";</script></main>'
    assert "<script>" in builder._sanitize_html(html)


def test_dead_links_and_empty_images_are_still_stripped():
    html = '<main><a href="mailto:">Email us</a><img src=""></main>'
    cleaned = builder._sanitize_html(html)
    assert "<a" not in cleaned and "Email us" in cleaned
    assert "<img" not in cleaned


def test_a_local_script_src_fails_the_build_but_a_cdn_one_passes():
    def check(page):
        files = {"index.html": page, "style.css": ""}
        result = next(
            c for c in validate.validate_files(files) if c["name"] == "script_sources_valid"
        )
        return result["passed"]

    page = "<title>T</title><h1>T</h1><main>%s</main>"
    # There is no local .js in the five files, so this can only ever 404.
    assert not check(page % '<script src="script.js"></script>')
    assert check(page % '<script src="https://cdnjs.cloudflare.com/x/aos.js" defer></script>')
    assert check(page % "<script>console.log(1)</script>")


# --------------------------------------------------------------- head assets

def _fragment(before_main=""):
    return (
        "<title>NovaToken</title>\n"
        '<meta name="description" content="A token.">\n'
        f"{before_main}"
        "<main><section class='hero'><h1>NovaToken</h1></section></main>"
    )


def test_a_cdn_stylesheet_written_before_main_is_lifted_into_the_head():
    link = '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/font-awesome/all.min.css">'
    page = builder._assemble_page(SPEC, "index.html", _fragment(link + "\n"), BRIEF)
    head = page.split("</head>")[0]
    assert link in head
    # Ahead of style.css, so the site's own rules still win the cascade.
    assert head.index(link) < head.index('href="style.css"')


def test_a_relative_head_asset_is_dropped_rather_than_linked_to_a_404():
    page = builder._assemble_page(
        SPEC, "index.html", _fragment('<script src="script.js"></script>\n'), BRIEF
    )
    assert "script.js" not in page


def test_an_inline_script_inside_main_reaches_the_finished_page():
    fragment = _fragment().replace("</main>", "<script>const a = 1;</script></main>")
    page = builder._assemble_page(SPEC, "index.html", fragment, BRIEF)
    assert "const a = 1;" in page


def test_icon_classes_are_not_mistaken_for_a_mismatched_stylesheet():
    icons = " ".join(f'<i class="fa fa-icon-{n}"></i>' for n in range(20))
    files = {
        "index.html": (
            '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/fa/all.min.css">'
            f"<main>{icons}</main>"
        ),
        "style.css": ".hero { color: red; }",
    }
    # Twenty unknown classes would trip the drift check if style.css were the only
    # stylesheet. It is not, so the check has nothing to say.
    assert builder._style_drift(files) is None


# --------------------------------------------------------------- imagery

@pytest.mark.asyncio
async def test_an_image_url_that_does_not_load_is_removed(monkeypatch):
    async def fake_live(client, url):
        return "good" in url

    monkeypatch.setattr(builder, "_url_is_live", fake_live)
    files = {
        "index.html": (
            '<main><img src="https://example.com/good.jpg" alt="a">'
            '<img src="https://example.com/bad.jpg" alt="b"></main>'
        ),
        "style.css": "",
    }
    out = await builder._drop_dead_images(files)
    assert "good.jpg" in out["index.html"]
    assert "bad.jpg" not in out["index.html"]


@pytest.mark.asyncio
async def test_a_relative_image_is_left_alone(monkeypatch):
    async def explode(client, url):  # must never be reached
        raise AssertionError(f"checked a non-external url: {url}")

    monkeypatch.setattr(builder, "_url_is_live", explode)
    files = {"index.html": '<main><img src="logo.png"></main>', "style.css": ""}
    assert await builder._drop_dead_images(files) == files


# --------------------------------------------------------------- research

def _stub_calls(monkeypatch, replies):
    """Answer research's model calls from `replies`, recording how each was made."""
    seen = []

    async def fake(prompt, *, reduced_reasoning=False, models=None, online=False):
        seen.append({"prompt": prompt, "online": online})
        return replies[len(seen) - 1], {
            "model": "stub", "input_tokens": 10, "output_tokens": 5
        }

    monkeypatch.setattr(research, "call_plain_completion", fake)
    return seen


@pytest.mark.asyncio
async def test_an_ordinary_business_never_pays_for_a_search(monkeypatch):
    seen = _stub_calls(monkeypatch, ["NONE"])
    facts, usage = await research.gather_facts("Build a site for a plumber")
    assert facts == ""
    assert len(seen) == 1 and seen[0]["online"] is False
    assert usage["requests"] == 1


@pytest.mark.asyncio
async def test_only_the_lookup_call_is_allowed_to_search(monkeypatch):
    seen = _stub_calls(
        monkeypatch,
        [
            "SEARCH: largest cryptocurrencies by market cap",
            "- Bitcoin (BTC)\n- Ethereum (ETH)",
        ],
    )
    facts, usage = await research.gather_facts("Add the other coins")

    assert [c["online"] for c in seen] == [False, True]
    assert "largest cryptocurrencies by market cap" in facts
    assert "- Bitcoin (BTC)" in facts
    # The licence and its limit both have to travel with the facts.
    assert "not claims about this business" in facts
    assert usage["requests"] == 2


@pytest.mark.asyncio
async def test_a_lookup_that_found_nothing_adds_no_facts(monkeypatch):
    _stub_calls(monkeypatch, ["SEARCH: some query", "NONE"])
    facts, _ = await research.gather_facts("Add the other coins")
    assert facts == ""


@pytest.mark.asyncio
async def test_research_failing_never_fails_the_build(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("openrouter is down")

    monkeypatch.setattr(research, "call_plain_completion", boom)
    assert await research.gather_facts("anything") == ("", None)


@pytest.mark.asyncio
async def test_the_query_is_found_even_when_the_model_narrates_first(monkeypatch):
    # Small models narrate before answering despite being told not to.
    _stub_calls(
        monkeypatch,
        ["Let me think about this.\nThe user wants coins.\nSEARCH: top crypto 2026", "- BTC"],
    )
    facts, _ = await research.gather_facts("Add the other coins")
    assert "top crypto 2026" in facts
    assert "Let me think" not in facts


@pytest.mark.asyncio
async def test_a_stray_closing_line_is_not_mistaken_for_a_query(monkeypatch):
    """The failure this parser was rebuilt for.

    Live, the decide step answered a plumber with a bare "Raj Plumbing" on its own line.
    Reading the last line as the query turned that into a paid search for a business that
    needed none. Nothing without the SEARCH: prefix may ever start one.
    """
    seen = _stub_calls(monkeypatch, ["NONE\nRaj Plumbing"])
    facts, _ = await research.gather_facts("Build a site for a plumber")
    assert facts == ""
    assert len(seen) == 1  # the online call never happened
