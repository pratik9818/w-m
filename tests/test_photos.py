"""Photographs have to be found for the owner, because owners do not supply them.

The site that prompted this went out with no pictures at all -- "There are no images whole
website is empty except header and bottom" -- and before that the contract filled gaps with
`picsum.photos/seed/<word>`, which always loads and is always a photograph of something
else entirely.
"""

import httpx
import pytest

from worker.codegen import builder, photos, shell
from worker.codegen.photos import allocate_photos


def _settings(key):
    return type("S", (), {"pexels_api_key": key})()


def _pexels_response(alt="A baker sliding loaves into a stone oven"):
    return {
        "photos": [{
            "src": {
                "large": "https://images.pexels.com/photos/1/bread-940.jpeg",
                "large2x": "https://images.pexels.com/photos/1/bread-1880.jpeg",
                "tiny": "x",
            },
            "id": 12345,
            "alt": alt,
            "photographer": "A Photographer",
        }]
    }


SPEC = {"name": "Rise & Crumb", "category": "Bakery", "layout": "landing"}


@pytest.fixture
def pexels(monkeypatch):
    """A stand-in Pexels, recording what was asked of it."""
    calls = {"queries": [], "auth": None, "status": 200, "payload": _pexels_response()}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["queries"].append(request.url.params.get("query"))
        calls["auth"] = request.headers.get("Authorization")
        return httpx.Response(calls["status"], json=calls["payload"])

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        return real_client(*args, **kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(photos.httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(photos, "get_settings", lambda: _settings("test-key"))
    return calls


@pytest.fixture
def planner(monkeypatch):
    """The model call that decides what to photograph."""
    plan = {"shots": [
        {"purpose": "hero", "query": "sourdough bread bakery", "alt": "Fresh loaves"},
        {"purpose": "about", "query": "hands kneading dough", "alt": "Kneading dough"},
    ]}
    state = {"plan": plan, "raises": None}

    async def fake_tool(prompt, tools):
        if state["raises"]:
            raise state["raises"]
        assert tools[0]["name"] == "choose_photographs"
        return {"operation": "choose_photographs", **state["plan"]}, {
            "model": "m", "input_tokens": 10, "output_tokens": 5
        }

    monkeypatch.setattr(photos, "call_forced_tool", fake_tool)
    return state


@pytest.mark.asyncio
async def test_photos_are_found_for_the_business(pexels, planner):
    found, usage = await photos.find_photos(SPEC)

    # One photograph, not two: the stub answers every query with the same picture, and the
    # hero's 1880px URL differs from the section's 940px URL for that same photograph -- so
    # deduplication has to work on the photo's identity, not on the URL.
    assert [p["url"] for p in found] == ["https://images.pexels.com/photos/1/bread-1880.jpeg"]
    assert found[0]["purpose"] == "hero"
    assert pexels["queries"] == ["sourdough bread bakery", "hands kneading dough"]
    assert pexels["auth"] == "test-key"
    assert usage["input_tokens"] == 10


@pytest.mark.asyncio
async def test_no_key_means_no_photos_and_no_calls(monkeypatch, planner):
    monkeypatch.setattr(photos, "get_settings", lambda: _settings(""))
    called = False

    async def must_not_run(*a, **k):
        nonlocal called
        called = True

    monkeypatch.setattr(photos, "call_forced_tool", must_not_run)

    found, usage = await photos.find_photos(SPEC)
    assert (found, usage) == ([], None)
    assert not called, "planning a photo shoot costs tokens; without a key it can never be used"


@pytest.mark.asyncio
async def test_a_rejected_key_does_not_fail_the_build(pexels, planner):
    """A wrong or expired key returns 401 on every search. That must cost the owner a
    plainer site, not a failed one."""
    pexels["status"] = 401
    pexels["payload"] = {"error": "Unauthorized"}

    found, _ = await photos.find_photos(SPEC)
    assert found == []


@pytest.mark.asyncio
async def test_a_failed_planning_call_does_not_fail_the_build(pexels, planner):
    from bot_api.services.llm_client import LLMCallFailed

    planner["raises"] = LLMCallFailed("model unavailable")
    found, _ = await photos.find_photos(SPEC)
    assert found == []


@pytest.mark.asyncio
async def test_a_business_with_nothing_to_photograph_gets_nothing(pexels, planner):
    planner["plan"] = {"shots": []}
    found, _ = await photos.find_photos(SPEC)
    assert found == []
    assert pexels["queries"] == []


# ------------------------------------------------- when the answer ignores the schema
#
# All of these are real shapes a model can return for a tool whose schema is not declared
# `strict`. The first one took down a live build: the parser called .get() on what it
# assumed was an object, and an owner watching their site being made was told something
# went wrong -- over photographs, which the whole module treats as optional.

@pytest.mark.asyncio
async def test_shots_returned_as_bare_strings_still_find_photographs(pexels, planner):
    planner["plan"] = {"shots": ["sourdough bread bakery", "hands kneading dough"]}

    found, _ = await photos.find_photos(SPEC)

    assert pexels["queries"] == ["sourdough bread bakery", "hands kneading dough"]
    assert found, "a list of plain search phrases is still enough to search with"


@pytest.mark.asyncio
async def test_a_string_only_plan_still_produces_a_hero(pexels, planner):
    """A bare string carries no purpose, so nothing is labelled hero. Left alone, the site
    is built entirely from section photographs and the top of the home page is bare."""
    planner["plan"] = {"shots": ["sourdough bread bakery"]}

    found, _ = await photos.find_photos(SPEC)

    assert [p["purpose"] for p in found] == ["hero"]


@pytest.mark.asyncio
async def test_junk_entries_are_dropped_without_losing_the_good_ones(pexels, planner):
    planner["plan"] = {"shots": [
        None,
        42,
        {"purpose": "hero", "query": "sourdough bread bakery", "alt": "Loaves"},
        {"purpose": "about", "alt": "no query at all"},
        "",
    ]}

    found, _ = await photos.find_photos(SPEC)

    assert pexels["queries"] == ["sourdough bread bakery"]
    assert len(found) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("shots", ["sourdough bakery", {"query": "x"}, 7, None])
async def test_a_shots_field_that_is_not_a_list_does_not_fail_the_build(
    pexels, planner, shots
):
    """A string here is the dangerous one: it slices and iterates like a list, so without
    a type check the build would search for 's', 'o', 'u', 'r'..."""
    planner["plan"] = {"shots": shots}

    found, usage = await photos.find_photos(SPEC)

    assert found == []
    assert pexels["queries"] == []
    assert usage is not None, "the planning call was still made and still has to be billed"


@pytest.mark.asyncio
async def test_an_unexpected_failure_costs_photographs_not_the_build(pexels, planner):
    """The guarantee this module is written on, tested at its edge: whatever goes wrong
    after the planning call, the caller gets an empty list rather than an exception."""
    def explode(*a, **k):
        raise RuntimeError("pexels client blew up")

    pexels_module_client = photos.httpx.AsyncClient
    photos.httpx.AsyncClient = explode
    try:
        found, usage = await photos.find_photos(SPEC)
    finally:
        photos.httpx.AsyncClient = pexels_module_client

    assert found == []
    assert usage["input_tokens"] == 10, "tokens already spent are still reported"


# ------------------------------------------------------------------ into the prompt

def test_the_pages_prompt_carries_the_real_urls():
    found = [{"purpose": "hero", "url": "https://images.pexels.com/photos/1/bread.jpeg",
              "alt": "Fresh loaves", "query": "q", "photographer": "P"}]
    prompt = builder._pages_prompt(
        {"name": "Rise & Crumb", "layout": "landing", "services": []},
        ("index.html",), builder.fallback_brief("modern"), "", found,
    )
    assert "https://images.pexels.com/photos/1/bread.jpeg" in prompt
    assert "## Photographs you may use" in prompt
    assert 'alt: "Fresh loaves"' in prompt
    # The pages this call is writing are named, so "every page should carry one" is about
    # the page in hand. A real build left services.html and contact.html with no picture
    # because the second of the two concurrent page calls was told nothing about coverage.
    assert "index.html" in prompt.split("## Photographs you may use", 1)[1]


def test_a_site_with_no_photos_is_not_told_to_invent_any():
    prompt = builder._pages_prompt(
        {"name": "Rise & Crumb", "layout": "landing", "services": []},
        ("index.html",), builder.fallback_brief("modern"), "", [],
    )
    # The contract mentions the list by name; what must be absent is the list itself.
    assert "## Photographs you may use" not in prompt
    # The old escape hatch: always resolves, never relevant.
    assert "picsum" not in prompt
    assert "never invent an image" in prompt.lower()


# ------------------------------------------------------------------ the licence

def test_the_pexels_credit_appears_only_when_photos_were_used():
    spec = {"name": "Rise & Crumb", "layout": "landing", "current_year": 2026}
    with_photos = shell.render_page(spec, "index.html", "T", "D", "<main>x</main>",
                                    stock_photo_credit=True)
    without = shell.render_page(spec, "index.html", "T", "D", "<main>x</main>")

    assert photos.ATTRIBUTION_TEXT in with_photos
    assert "https://www.pexels.com" in with_photos
    assert photos.ATTRIBUTION_TEXT not in without


# ------------------------------------------------- one photograph, one place on the site

def _photo(purpose, n):
    return {"purpose": purpose, "url": f"https://images.pexels.com/photos/{n}/p.jpeg",
            "alt": f"alt {n}", "query": "q", "photo_id": n, "photographer": "P"}


def test_concurrent_page_calls_never_get_the_same_photograph():
    """A four-page site is written by two calls at the same time. Offered one shared list
    they both reached for the same picture -- a real build put the identical toolset photo
    on the about page and the contact page."""
    found = [_photo("hero", 1), _photo("about", 2), _photo("services", 3), _photo("gallery", 4)]
    allocation = allocate_photos(found, builder.PAGE_GROUPS)

    dealt = [p["photo_id"] for group in builder.PAGE_GROUPS for p in allocation[group]]
    assert sorted(dealt) == [1, 2, 3, 4], "every photograph is used, exactly once"
    assert len(set(dealt)) == len(dealt), "no photograph reaches two concurrent calls"


def test_the_hero_goes_to_whichever_call_writes_the_home_page():
    found = [_photo("hero", 1), _photo("services", 2)]
    allocation = allocate_photos(found, builder.PAGE_GROUPS)
    home = next(g for g in builder.PAGE_GROUPS if "index.html" in g)
    assert 1 in [p["photo_id"] for p in allocation[home]]


def test_a_landing_page_keeps_every_photograph():
    """One page, one call -- nothing to split, and splitting would throw pictures away."""
    found = [_photo("hero", 1), _photo("about", 2), _photo("services", 3)]
    allocation = allocate_photos(found, (("index.html",),))
    assert [p["photo_id"] for p in allocation[("index.html",)]] == [1, 2, 3]


def test_no_photos_allocates_nothing_without_failing():
    assert allocate_photos([], builder.PAGE_GROUPS) == {g: [] for g in builder.PAGE_GROUPS}
