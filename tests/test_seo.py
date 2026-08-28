"""Making a generated site findable, without asking the owner to know what that means.

The owner who asked for this said: "i dont know about seo stuff so whatever niche or
category website modal build it should seo optimze very well for that category". What that
means in practice for a small local business is mostly mechanical, and the mechanical parts
are done by code here rather than by a model on purpose.

The reason is in `test_the_record_is_valid_json`: a model asked to write structured data
writes *plausible* structured data, and a plausible-but-invalid LocalBusiness block is
silently discarded by Google. That failure is invisible -- the page looks finished, the
record is there, and it does nothing. Built from the business row, it is either correct or
absent, and both of those are honest.
"""

import json
import re

import pytest

from worker.codegen import seo, shell

# The art direction a page is assembled with. Nothing here affects the description; it is
# required only because `_assemble_page` renders a whole page.
_BRIEF = {
    "display_font": "Bebas Neue", "display_stack": "sans-serif",
    "body_font": "Inter", "body_stack": "sans-serif",
    "accent": "#ff0066", "accent_ink": "#ffffff", "theme_mood": "bold",
    "signature": "hard edges", "palette": {}, "neutral": "#111111",
}

from worker.codegen.seo import (
    URL_MARKER,
    crawl_files,
    finalise_urls,
    head_tags,
    local_business_jsonld,
    opening_hours,
    robots_txt,
    schema_type_for,
    sitemap_xml,
)

SPEC = {
    "name": "Rise & Crumb",
    "category": "Artisan bakery and cafe",
    "layout": "multipage",
    "tagline": "Stone-baked sourdough, every morning",
    "about": "A small bakery in Leeds.",
    "phone": "0113 496 0000",
    "email": "hi@riseandcrumb.co.uk",
    "address": "12 Kirkgate, Leeds LS1 6BY",
    "hours": "Mon-Fri 7-5, Sat 8-4",
    "services": [{"name": "Sourdough loaves"}, {"name": "Celebration cakes"}],
    "photo_urls": ["https://images.pexels.com/photos/1/bread.jpeg"],
    "logo_url": None,
    "social_links": {"instagram": "https://instagram.com/riseandcrumb"},
    "current_year": 2026,
    "site_url": "https://rise-and-crumb.pages.dev",
}


def _record(spec=None, site_url="https://example.pages.dev"):
    block = local_business_jsonld(spec or SPEC, site_url)
    body = re.search(r'<script type="application/ld\+json">\n(.*)\n</script>', block, re.S)
    return json.loads(body.group(1))


# --------------------------------------------------- the type that matches the trade

@pytest.mark.parametrize("category, expected", [
    ("Artisan bakery and cafe", "Bakery"),
    ("Bakery", "Bakery"),
    ("Coffee shop", "CafeOrCoffeeShop"),
    ("Restaurant / Cafe", "Restaurant"),
    ("Italian pizzeria", "Restaurant"),
    ("Barber shop", "HairSalon"),
    ("Hairdresser", "HairSalon"),
    ("Nail salon", "NailSalon"),
    ("Salon / Spa", "BeautySalon"),
    ("Day spa", "DaySpa"),
    ("Tattoo studio", "TattooParlor"),
    ("Fitness / Gym", "ExerciseGym"),
    ("Yoga studio", "ExerciseGym"),
    ("Dentist", "Dentist"),
    ("Veterinary practice", "VeterinaryCare"),
    ("Physiotherapy clinic", "MedicalClinic"),
    ("Plumber", "Plumber"),
    ("Emergency plumbing services", "Plumber"),
    ("Electrician", "Electrician"),
    ("Roofing contractor", "RoofingContractor"),
    ("Locksmith", "Locksmith"),
    ("Painter and decorator", "HousePainter"),
    ("Solicitors", "Attorney"),
    ("Accountancy firm", "AccountingService"),
    ("Estate agent", "RealEstateAgent"),
    ("Car garage and MOT centre", "AutoRepair"),
    ("Nursery", "ChildCare"),
    ("Maths tutoring", "EducationalOrganization"),
    ("Florist", "Florist"),
    ("Jeweller", "JewelryStore"),
    ("Butcher", "GroceryStore"),
    ("Pet shop", "PetStore"),
    ("Clothing boutique", "ClothingStore"),
    ("Retail Shop", "Store"),
    ("Bed and breakfast", "LodgingBusiness"),
    ("Wedding photographer", "Photographer"),
    ("Wedding venue", "EntertainmentBusiness"),
])
def test_the_schema_type_matches_the_actual_trade(category, expected):
    """Google reads the most specific type it is given. "Bakery" says what the business
    does; "LocalBusiness" says almost nothing."""
    assert schema_type_for(category) == expected


def test_a_trade_with_no_matching_type_still_gets_a_valid_one():
    assert schema_type_for("Alpaca shearing") == "LocalBusiness"
    assert schema_type_for(None) == "LocalBusiness"
    assert schema_type_for("") == "LocalBusiness"


def test_the_first_word_wins_when_two_trades_are_named():
    """"Bakery and cafe" is a bakery. People lead with what they are."""
    assert schema_type_for("Bakery and cafe") == "Bakery"
    assert schema_type_for("Cafe and bakery") == "CafeOrCoffeeShop"
    assert schema_type_for("Restaurant / Cafe") == "Restaurant"


def test_a_business_with_nowhere_to_visit_is_not_a_local_business():
    """A crypto project with a LocalBusiness record is claiming a shopfront it does not
    have, and a claim the page cannot back up is the kind of mismatch that gets structured
    data thrown out."""
    assert schema_type_for("Crypto token project", has_address=False) == "Organization"
    assert schema_type_for("SaaS platform", has_address=False) == "Organization"
    assert schema_type_for("Online course platform", has_address=False) == "Organization"
    # The one that actually exercises the rule: "store" matches a trade pattern, so
    # without the digital check this would come back as a Store with a shopfront.
    assert schema_type_for("Online store", has_address=False) == "Organization"
    assert schema_type_for("Online store", has_address=True) == "Store"
    # A trade without a shopfront is still that trade -- mobile plumbers exist, and calling
    # one an "Organization" throws away the only word that matters.
    assert schema_type_for("Plumber", has_address=False) == "Plumber"
    # And with nothing recognisable at all, the address decides.
    assert schema_type_for("Alpaca shearing", has_address=False) == "Organization"
    assert schema_type_for("Alpaca shearing", has_address=True) == "LocalBusiness"


# --------------------------------------------------- the record itself

def test_the_record_is_valid_json():
    """The whole reason this is not written by a model."""
    data = _record()
    assert data["@context"] == "https://schema.org"
    assert data["@type"] == "Bakery"
    assert data["name"] == "Rise & Crumb"


def test_the_record_carries_what_a_searcher_needs():
    data = _record()
    assert data["telephone"] == "0113 496 0000"
    assert data["address"] == {"@type": "PostalAddress",
                               "streetAddress": "12 Kirkgate, Leeds LS1 6BY"}
    assert data["url"] == "https://example.pages.dev"
    assert data["sameAs"] == ["https://instagram.com/riseandcrumb"]


def test_what_they_sell_is_named_in_the_record():
    """For a service business this is the part that matches what someone typed in."""
    offers = _record()["hasOfferCatalog"]["itemListElement"]
    assert [o["itemOffered"]["name"] for o in offers] == ["Sourdough loaves", "Celebration cakes"]


def test_nothing_the_business_did_not_give_us_appears():
    """An invented field is worse than a missing one: Google cross-checks the record
    against the page."""
    bare = {"name": "Raj Plumbing", "category": "Plumber"}
    data = _record(bare, site_url=None)

    assert data["@type"] == "Plumber", "the trade is known even when nothing else is"
    for absent in ("telephone", "email", "address", "openingHours", "hasOfferCatalog",
                   "sameAs", "image", "url"):
        assert absent not in data, f"{absent} was invented from nothing"


def test_a_closing_script_tag_cannot_break_out_of_the_block():
    """A business name is owner-supplied text sitting inside a <script>."""
    block = local_business_jsonld(
        {**SPEC, "name": "Bad </script><script>alert(1)</script> Co"}, None
    )
    assert "</script><script>" not in block
    assert block.count("</script>") == 1


# --------------------------------------------------- opening hours

@pytest.mark.parametrize("written, expected", [
    ("Mon-Fri 9-6", ["Mo-Fr 09:00-18:00"]),
    ("Mon-Fri 7-5, Sat 8-4", ["Mo-Fr 07:00-17:00", "Sa 08:00-16:00"]),
    ("Monday to Friday 9am - 5.30pm", ["Mo-Fr 09:00-17:00"]),
    ("Mon-Sat 10:00-18:00", ["Mo-Sa 10:00-18:00"]),
    ("Sun 11am-4pm", ["Su 11:00-16:00"]),
])
def test_hours_are_translated_into_the_format_search_engines_read(written, expected):
    assert opening_hours(written) == expected


def test_an_afternoon_close_is_read_as_an_afternoon():
    """"9-6" on a shopfront never means 09:00 to 06:00. A business that closes before it
    opens is the giveaway."""
    assert opening_hours("Mon-Fri 9-6") == ["Mo-Fr 09:00-18:00"]


@pytest.mark.parametrize("written", [
    "open when we're open",
    "call for opening times",
    "",
    None,
    "24/7",
])
def test_hours_that_cannot_be_read_are_left_out_rather_than_guessed(written):
    """Invalid structured data can cost the whole block, so a guess is more expensive than
    a gap -- and the human-readable hours are still on the page for a person to read."""
    assert opening_hours(written) == []


@pytest.mark.parametrize("written", ["Mon-Fri 25-30", "Mon-Fri 99:00-99:00"])
def test_an_impossible_time_is_dropped_rather_than_written_out(written):
    """These get past the pattern and fail on the clock. Without the check they would be
    written into the record as-is, which is the invalid-data case that costs the block."""
    assert opening_hours(written) == []


def test_unreadable_hours_do_not_appear_in_the_record():
    assert "openingHours" not in _record({**SPEC, "hours": "call us"})


# --------------------------------------------------- the head

def test_the_head_says_the_page_may_be_indexed():
    tags = head_tags(SPEC, "index.html", "T", "D", "https://example.pages.dev")
    assert 'name="robots" content="index, follow' in tags


def test_the_head_carries_the_share_card():
    tags = head_tags(SPEC, "about.html", "About | Rise & Crumb", "About us.",
                     "https://example.pages.dev")
    assert 'property="og:title" content="About | Rise &amp; Crumb"' in tags
    assert 'property="og:description" content="About us."' in tags
    assert 'name="twitter:card" content="summary_large_image"' in tags
    assert 'property="og:image" content="https://images.pexels.com/photos/1/bread.jpeg"' in tags


def test_the_home_page_canonical_is_the_bare_address():
    """That is the address people link to and the one a search engine lands on; /index.html
    competing with / is the same page twice."""
    tags = head_tags(SPEC, "index.html", "T", "D", "https://example.pages.dev")
    assert '<link rel="canonical" href="https://example.pages.dev">' in tags


def test_an_inner_page_canonical_names_that_page():
    tags = head_tags(SPEC, "contact.html", "T", "D", "https://example.pages.dev")
    assert '<link rel="canonical" href="https://example.pages.dev/contact.html">' in tags


def test_a_site_that_does_not_know_its_address_yet_leaves_a_marker_not_a_broken_tag():
    """A canonical pointing at a placeholder is worse than no canonical at all. An HTML
    comment is the right thing to leave behind."""
    tags = head_tags(SPEC, "index.html", "T", "D", None)
    assert URL_MARKER in tags
    assert "canonical" not in tags


def test_the_rendered_page_actually_contains_all_of_it():
    """The pieces can each be right and still not reach the page."""
    page = shell.render_page(SPEC, "index.html", "Sourdough Bakery in Leeds | Rise & Crumb",
                             "Stone-baked sourdough in Leeds.", "<main><h1>Hi</h1></main>")

    assert '<html lang="en">' in page
    assert "<title>Sourdough Bakery in Leeds | Rise &amp; Crumb</title>" in page
    assert 'rel="canonical"' in page
    assert '"@type": "Bakery"' in page
    assert 'property="og:title"' in page
    # The stylesheet still has to win the cascade, so the SEO block must not have displaced it.
    assert page.index('<link rel="stylesheet" href="style.css">') > page.index("og:title")


# --------------------------------------------------- crawl files

def test_the_sitemap_lists_every_page_with_the_home_page_first():
    xml = sitemap_xml(["about.html", "index.html", "contact.html", "style.css"],
                      "https://example.pages.dev")
    locations = re.findall(r"<loc>([^<]+)</loc>", xml)

    assert locations == [
        "https://example.pages.dev",
        "https://example.pages.dev/about.html",
        "https://example.pages.dev/contact.html",
    ]
    assert "style.css" not in xml, "a stylesheet is not a page anyone searches for"


def test_the_sitemap_is_well_formed_xml():
    from xml.etree import ElementTree

    xml = sitemap_xml(["index.html", "about.html"], "https://example.pages.dev")
    root = ElementTree.fromstring(xml)
    assert root.tag.endswith("urlset")


def test_robots_points_at_the_sitemap():
    text = robots_txt("https://example.pages.dev")
    assert "User-agent: *" in text
    assert "Allow: /" in text
    assert "Sitemap: https://example.pages.dev/sitemap.xml" in text


def test_robots_is_still_written_when_the_address_is_unknown():
    """"Everything here is crawlable" is worth saying either way."""
    text = robots_txt(None)
    assert "Allow: /" in text
    assert "Sitemap:" not in text


def test_no_sitemap_is_written_without_an_address():
    """A sitemap needs absolute addresses. One full of relative paths is invalid."""
    files = crawl_files(["index.html"], None)
    assert "robots.txt" in files
    assert "sitemap.xml" not in files


# --------------------------------------------------- filling in the address later

def test_a_first_build_gets_its_address_filled_in_at_deploy():
    """The very first build of a project cannot know its own address -- Cloudflare appends
    a random suffix when the name is taken, so it can only be asked for, never predicted."""
    built = {
        "index.html": f"<head>{URL_MARKER}</head>",
        "about.html": f"<head>{URL_MARKER}</head>",
        "style.css": "body{}",
    }

    deployed = finalise_urls(built, "https://rise-and-crumb-6hf.pages.dev")

    assert URL_MARKER not in deployed["index.html"]
    assert ('<link rel="canonical" href="https://rise-and-crumb-6hf.pages.dev">'
            in deployed["index.html"])
    assert ('<link rel="canonical" href="https://rise-and-crumb-6hf.pages.dev/about.html">'
            in deployed["about.html"])
    assert deployed["style.css"] == "body{}"
    assert "sitemap.xml" in deployed and "robots.txt" in deployed
    assert "rise-and-crumb-6hf.pages.dev" in deployed["sitemap.xml"]


def test_a_later_build_already_knows_its_address_and_is_left_alone():
    already = {"index.html": '<head><link rel="canonical" href="https://x.dev"></head>'}
    deployed = finalise_urls(already, "https://x.dev")
    assert deployed["index.html"] == already["index.html"]


@pytest.mark.asyncio
async def test_a_first_deploy_uploads_pages_that_know_their_own_address(monkeypatch):
    """Driven through the real deploy path rather than by calling the finaliser directly.

    The finaliser can be perfect and simply not be called -- which is exactly what happened
    to an earlier version of this test, where every assertion still passed with the call
    deleted. So this one reads the bytes that were actually handed to Cloudflare.
    """
    import base64
    import json as jsonlib

    import httpx

    from worker.tasks import deploy as deploy_module

    uploaded: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url.endswith("/pages/projects"):
            return httpx.Response(200, json={"success": True,
                                             "result": {"subdomain": "rise-6hf.pages.dev"}})
        if url.endswith("/upload-token"):
            return httpx.Response(200, json={"success": True, "result": {"jwt": "t"}})
        if url.endswith("/pages/assets/upload"):
            for item in jsonlib.loads(request.content):
                uploaded[item["metadata"]["contentType"]] = base64.b64decode(
                    item["value"]).decode("utf-8")
            return httpx.Response(200, json={"success": True})
        if url.endswith("/deployments"):
            return httpx.Response(200, json={"success": True, "result": {"url": "u"}})
        return httpx.Response(404, json={"success": False, "errors": [{"message": url}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        deploy_module.httpx, "AsyncClient",
        lambda *a, **kw: real_client(*a, **kw, transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(deploy_module, "get_settings",
                        lambda: type("S", (), {"cloudflare_account_id": "a",
                                               "cloudflare_api_token": "t"})())

    class NewBusiness:
        slug = "rise-and-crumb"
        cf_pages_project_name = None
        # A site that has never been deployed has no analytics identity yet; deploy reads
        # these to decide whether to provision one.
        cf_rum_site_tag = None
        cf_rum_site_token = None
        analytics_enabled_at = None

    built = {"index.html": f"<head>{URL_MARKER}</head>", "style.css": "body{}"}
    project, url, deployed = await deploy_module.deploy_to_cloudflare_pages(NewBusiness(), built)

    assert url == "https://rise-6hf.pages.dev"
    # The bytes Cloudflare received, not the dict that went in.
    assert 'rel="canonical" href="https://rise-6hf.pages.dev"' in uploaded["text/html"]
    assert "rise-6hf.pages.dev" in uploaded["application/xml"], "no sitemap was uploaded"
    assert "Sitemap:" in uploaded["text/plain"]
    # And what the caller stores has to be the same thing.
    assert deployed["index.html"] == uploaded["text/html"]


def test_the_deployed_bytes_are_what_gets_stored():
    """The version record has to hold what was actually served: the next edit patches
    against it, and a stored copy that differs from the live site is how an edit gets
    applied to bytes nobody is looking at."""
    import inspect

    from worker.tasks import generate

    assert "live_url, files = await deploy_to_cloudflare_pages" in inspect.getsource(
        generate.run_generation_pipeline
    )


@pytest.mark.parametrize("filename, expected", [
    ("sitemap.xml", "application/xml"),
    ("robots.txt", "text/plain"),
    ("index.html", "text/html"),
    ("style.css", "text/css"),
])
def test_crawl_files_are_served_as_themselves(filename, expected):
    """Served as application/octet-stream a sitemap is downloaded rather than read, and the
    whole point of writing one is lost silently."""
    from worker.tasks.deploy import _content_type

    assert _content_type(filename) == expected


# --------------------------------------------------- the words

def test_the_prompt_asks_for_a_title_someone_would_click():
    from pathlib import Path

    prompt = Path("worker/codegen/prompts/pages.md").read_text(encoding="utf-8")
    assert "50–60 characters" in prompt
    assert "town" in prompt.lower() and "city" in prompt.lower()
    # The counter-example matters as much as the rule.
    assert "Sourdough Bakery in Leeds | Rise & Crumb" in prompt
    assert "never invent a town" in prompt.lower()
    # A range with a hard ceiling got written right up to the ceiling and cut mid-word, so
    # the prompt now says which end of the range matters.
    assert "ceiling, not a target" in prompt
    assert f"never exceed {seo.DESCRIPTION_MAX_CHARS}" in prompt, (
        "the number the model is given and the number the code enforces must not drift"
    )


# --------------------------------------------------- the description a searcher sees

def test_a_description_that_fits_is_left_exactly_as_written():
    """The common case must cost nothing and must not reword good copy."""
    text = "Blocked drain or burst pipe? Rivera Plumbing covers Riverside, same day."
    assert seo.tidy_description(text) == text


def test_the_live_truncation_is_repaired():
    """The real one, from xtravu.pages.dev: exactly 160 characters, ending inside the word
    "McDonald's". This is what a searcher saw before deciding whether to click."""
    broken = (
        "Xtravu puts every screen in your network on one dashboard. Schedule content, "
        "build layouts, and manage Android displays anywhere. Trusted by Pizza Hut, McDonald"
    )
    assert len(broken) == 160

    fixed = seo.tidy_description(broken)
    assert len(fixed) <= seo.DESCRIPTION_MAX_CHARS
    assert not fixed.endswith("McDonald"), "the half-written brand name must be gone"
    assert fixed.endswith("Trusted by Pizza Hut"), "everything whole is kept"


def test_a_dangling_connective_goes_with_the_word_it_introduced():
    """Stopping on "and" reads as a cut-off sentence even though the word is complete."""
    text = "We fit kitchens, bathrooms and lofts across the whole of Greater Manchester and"
    assert seo.tidy_description(text, limit=len(text)).endswith("Greater Manchester")


def test_trimming_never_splits_a_word():
    long_text = "Riverside plumbing specialists " * 20
    fixed = seo.tidy_description(long_text)
    assert len(fixed) <= seo.DESCRIPTION_MAX_CHARS
    # Every word in the result is a whole word from the original.
    assert set(fixed.split()) <= set(long_text.split())


def test_an_empty_description_stays_empty_rather_than_failing():
    assert seo.tidy_description("") == ""
    assert seo.tidy_description(None) == ""


def test_the_page_that_gets_built_carries_the_tidied_description():
    """Fixing the helper is no use if the builder never calls it -- and the meta tag,
    og:description and twitter:description all read from the same string."""
    from worker.codegen import builder

    broken = "x" * 40 + " " + "word " * 40
    fragment = (
        f"<title>A Page</title>\n"
        f'<meta name="description" content="{broken}">\n'
        f"<main><section class=\"section\"><p>Body copy.</p></section></main>"
    )
    page = builder._assemble_page(
        {"name": "Test Co", "category": "Plumber"}, "index.html", fragment, _BRIEF
    )
    served = re.search(r'name="description" content="([^"]*)"', page).group(1)
    assert 0 < len(served) <= seo.DESCRIPTION_MAX_CHARS
    assert page.count(served) >= 2, "og/twitter descriptions carry the same tidied text"
