"""Real photographs for a generated site, chosen to fit the business.

The contract used to hand the model `https://picsum.photos/seed/<word>/1600/900` whenever
it wanted atmosphere. That always resolves, so the build passed -- and put a stranger's
holiday snap on a plumber's home page. Owners do not upload photos (the one who prompted
this wrote "there are no images whole website is empty except header and bottom"), so the
pictures have to be found, not asked for.

Two steps, deliberately separate:

  1. A model call decides *what to photograph* -- a handful of shots described in the
     business's own terms ("stone-baked sourdough loaves cooling on a rack"), each with
     the search words to find it and the alt text it should carry.
  2. Those searches run against Pexels, which returns real photographs under a licence
     that permits commercial use.

Only step 2 needs a key. With no key configured the whole thing returns nothing and the
build carries on without photographs -- a plainer site, never a failed one.

URLs point at Pexels' own CDN rather than being copied into our storage: that is what the
provider expects, it keeps builds fast, and the sandbox check already fails any build
whose <img> does not actually load, so a dead URL cannot reach an owner silently.
"""
import asyncio
import json
import logging
import re

import httpx

from bot_api.config import get_settings
from bot_api.services.llm_client import (
    DailyLimitReached,
    LLMCallFailed,
    call_forced_tool,
)

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.pexels.com/v1/search"
# Enough for a hero and a few sections. Every extra shot is another search and another
# image the page has to earn -- past this a small-business site starts looking like a
# stock-photo catalogue.
MAX_SHOTS = 5
SEARCH_TIMEOUT_SECONDS = 15
# Pexels serves each photo at several widths. `large` is 940px: right for a card or a
# section image, and visibly soft stretched across a hero on any normal desktop, so the
# hero takes `large2x` (1880px) instead. Neither is the original, which can be several
# megabytes and would cost the visitor far more than the sharpness is worth.
SECTION_SIZE = "large"
HERO_SIZE = "large2x"
# What the planner calls the shot that goes at the top of the home page.
HERO_PURPOSE = "hero"

# Pexels asks for a visible credit in return for the free tier. It is rendered by the
# shell into the footer of every page that a build found photographs for, rather than left
# to the model to remember.
ATTRIBUTION_TEXT = "Photos provided by Pexels"
ATTRIBUTION_URL = "https://www.pexels.com"
STOCK_PHOTO_CREDIT_HTML = (
    f'<p class="footer-note"><a href="{ATTRIBUTION_URL}" rel="noopener" target="_blank">'
    f"{ATTRIBUTION_TEXT}</a></p>"
)


PLAN_TOOL = {
    "name": "choose_photographs",
    "description": (
        "Decide which photographs this particular business's website should show. Think "
        "about what a customer would want to see before they buy."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "shots": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "purpose": {
                            "type": "string",
                            "description": (
                                "where it goes: 'hero' for the big picture at the top, or a "
                                "short label for the section it belongs to, e.g. 'about', "
                                "'services', 'gallery'"
                            ),
                        },
                        "query": {
                            "type": "string",
                            "description": (
                                "two to four plain words to search a stock photo library "
                                "with. Describe the SUBJECT, not the business: for a bakery "
                                "called Rise & Crumb, 'sourdough bread bakery' -- never "
                                "'Rise & Crumb', which no photo library has heard of. No "
                                "brand names, no people's names, no place names unless the "
                                "place itself is the subject."
                            ),
                        },
                        "alt": {
                            "type": "string",
                            "description": (
                                "the alt text for this image, describing what is in it for "
                                "someone who cannot see it"
                            ),
                        },
                    },
                    "required": ["purpose", "query", "alt"],
                },
            }
        },
        "required": ["shots"],
    },
}

PLAN_PROMPT = """Choose the photographs for a small business's website.

## The business

{spec_json}

## What to choose

Pick between 2 and {max_shots} photographs this site should show, in the order they would
appear. The first should be the hero -- the one big image at the top of the home page.

The point is that the pictures look like *this* business. A photograph of the actual
trade, the actual product, the actual kind of room a customer walks into. A generic office
stock photo on a bakery's page is worse than no photograph at all, because it tells the
visitor the site was made without looking at them.

Search terms are read by a stock photo library, not by a person who knows this business:

- Describe what is IN the picture: "wood fired pizza oven", "hands kneading dough".
- Never the business's own name, and never a made-up place. The library has never heard
  of them and will return something unrelated.
- Two to four words. One word is too broad ("food"), a sentence finds nothing.

If the business is one where a photograph would not help -- a purely digital product with
nothing to show -- return fewer shots, or an empty list. An honest empty list is better
than five irrelevant pictures.

If the data above already lists `photo_urls`, those are the owner's own photographs of
their own business, and nothing bought from a library beats them. Choose only what they do
not already cover, and fewer shots -- or none at all if their photographs are enough.

Now call choose_photographs exactly once."""


def _normalise_shot(raw) -> dict | None:
    """One entry of the model's `shots` list, or None when there is nothing usable in it.

    The tool schema asks for objects with `purpose`, `query` and `alt`. That is a request,
    not a guarantee -- the schema is not declared `strict`, and on a real build the model
    answered with a plain list of search phrases instead. The parser assumed the schema had
    been honoured, so the build died on `'str' object has no attribute 'get'` before a
    single page was written, over an optional feature.

    A bare string is read as the search query. It is the one field that cannot be guessed
    from the others, so a string on its own is still a photograph worth finding.
    """
    if isinstance(raw, str):
        query = raw.strip()
        return {"purpose": "section", "query": query, "alt": query} if query else None
    if not isinstance(raw, dict):
        return None
    query = str(raw.get("query") or "").strip()
    if not query:
        return None
    return {
        "purpose": str(raw.get("purpose") or "").strip() or "section",
        "query": query,
        "alt": str(raw.get("alt") or "").strip() or query,
    }


async def _plan_shots(spec: dict) -> tuple[list[dict], dict | None]:
    """What to photograph, in the business's own terms. Never raises: a build without
    photographs is a plainer site, while a build that dies over one is a failure the
    owner sees."""
    prompt = PLAN_PROMPT.format(
        spec_json=json.dumps(spec, indent=2, ensure_ascii=False), max_shots=MAX_SHOTS
    )
    try:
        op, usage = await call_forced_tool(prompt, [PLAN_TOOL])
    except (LLMCallFailed, DailyLimitReached) as exc:
        logger.warning("photos.plan_failed", extra={"event": "photos.plan_failed", "error": str(exc)[:200]})
        return [], None

    raw_shots = op.get("shots")
    if not isinstance(raw_shots, list):
        # Not the shape the schema asked for and nothing to salvage from it.
        logger.warning(
            "photos.plan_malformed",
            extra={"event": "photos.plan_malformed", "type": type(raw_shots).__name__},
        )
        return [], usage

    shots = [s for s in (_normalise_shot(r) for r in raw_shots[:MAX_SHOTS]) if s]
    # The prompt asks for the shots "in the order they would appear", first one the hero.
    # When the model gives no shot that purpose -- which is every degraded response, since
    # a bare string carries no purpose at all -- honouring that ordering is what keeps the
    # home page's one big image. Without it the site is built entirely from section
    # photographs and the top of the front page is bare.
    if shots and not any(s["purpose"].lower() == HERO_PURPOSE for s in shots):
        shots[0]["purpose"] = HERO_PURPOSE
    return shots, usage


async def _search_one(client: httpx.AsyncClient, shot: dict) -> dict | None:
    """The best photograph for one shot, or None if the search found nothing usable."""
    try:
        response = await client.get(
            SEARCH_URL,
            params={"query": shot["query"], "per_page": 1, "orientation": "landscape"},
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "photos.search_failed",
            extra={"event": "photos.search_failed", "query": shot["query"], "error": str(exc)[:200]},
        )
        return None

    if response.status_code != 200:
        logger.warning(
            "photos.search_rejected",
            extra={
                "event": "photos.search_rejected", "query": shot["query"],
                "status": response.status_code, "body": response.text[:200],
            },
        )
        return None

    results = (response.json() or {}).get("photos") or []
    if not results:
        return None
    photo = results[0]
    sources = photo.get("src") or {}
    wanted = HERO_SIZE if shot["purpose"].lower() == HERO_PURPOSE else SECTION_SIZE
    # Falls back to the other size rather than dropping a good photograph over a missing
    # variant.
    url = sources.get(wanted) or sources.get(SECTION_SIZE)
    if not url:
        return None
    return {
        "purpose": shot["purpose"],
        "query": shot["query"],
        # Pexels writes its own alt text and it is usually a better description of what is
        # actually in the frame than the guess made before the photo was chosen.
        "alt": (photo.get("alt") or "").strip() or shot["alt"],
        "url": url,
        # The photograph's own identity, not the URL: the hero takes a different size
        # variant from a section image, so the same picture answering two searches comes
        # back under two different URLs and would otherwise be treated as two pictures.
        "photo_id": photo.get("id") or url,
        "photographer": (photo.get("photographer") or "").strip(),
    }


async def find_photos(spec: dict) -> tuple[list[dict], dict | None]:
    """Photographs for this business, and what the planning call cost.

    Returns ([], None) when no key is configured, when the planner declines, or when every
    search comes back empty -- all of which mean "build the site without photographs".

    Never raises. Photographs are an enhancement, and the whole module is written on the
    promise that a build which cannot find any still produces a site. That promise used to
    live only in the docstring: the model call was guarded but the code reading its answer
    was not, so one unexpected shape in the response took down a build that had nothing
    else wrong with it. The guard belongs around everything the response touches, which is
    all of this. Same reasoning as research.gather_facts -- an optional call must not be
    able to fail a build.
    """
    usage = None
    try:
        api_key = get_settings().pexels_api_key
        if not api_key:
            return [], None

        shots, usage = await _plan_shots(spec)
        if not shots:
            return [], usage

        async with httpx.AsyncClient(
            timeout=SEARCH_TIMEOUT_SECONDS, headers={"Authorization": api_key}
        ) as client:
            found = await asyncio.gather(*(_search_one(client, shot) for shot in shots))

        photos: list[dict] = []
        seen: set = set()
        for photo in found:
            # The same stock photograph answering two different searches would otherwise
            # appear twice on one site.
            if photo and photo["photo_id"] not in seen:
                seen.add(photo["photo_id"])
                photos.append(photo)

        log_extra = {
            "event": "photos.found", "wanted": len(shots), "found": len(photos),
            "queries": [s["query"] for s in shots],
        }
        logger.info("photos.found", extra=log_extra)
        return photos, usage
    except Exception:
        # `usage` is returned even here: when the planning call succeeded and something
        # after it failed, those tokens were still spent and still have to be billed.
        logger.warning("photos raised, continuing without photographs", exc_info=True)
        return [], usage


async def find_one_photo(spec: dict, hint: str = "") -> dict | None:
    """One stock photograph for a business, without a model call.

    `find_photos` above plans a whole shoot and pays a model to choose the search words.
    That is right when a site is being written from nothing and wrong for "add a picture to
    my home page", where the answer is one photograph and the business already says what it
    should be of. The query is built from the owner's own words and their category, which
    is what a person would type into a photo library.

    Returns None whenever a photograph cannot be found, including with no key configured.
    Never raises: the caller is answering a chat message, not running a build.
    """
    try:
        api_key = get_settings().pexels_api_key
        if not api_key:
            return None

        # Their words first -- "a picture of bread" is a better search than "Bakery" -- with
        # the category behind it so a hint like "add a photo" still finds something on topic.
        words = " ".join(part for part in (hint.strip(), str(spec.get("category") or "").strip())
                         if part)
        query = " ".join(_SEARCH_STOPWORDS_RE.sub(" ", words).split())[:80]
        if not query:
            return None

        async with httpx.AsyncClient(
            timeout=SEARCH_TIMEOUT_SECONDS, headers={"Authorization": api_key}
        ) as client:
            photo = await _search_one(client, {"purpose": HERO_PURPOSE, "query": query, "alt": query})
        logger.info(
            "photos.one_found",
            extra={"event": "photos.one_found", "query": query, "found": bool(photo)},
        )
        return photo
    except Exception:
        logger.warning("find_one_photo raised, continuing without a photograph", exc_info=True)
        return None


# Words that describe the request rather than the picture. Left in, they are what the photo
# library actually searches for, and "can you add a nice image to my page" returns pictures
# of pages.
_SEARCH_STOPWORDS_RE = re.compile(
    r"\b(?:can|could|you|please|add|put|insert|place|show|include|want|need|like|get|find|"
    r"me|my|a|an|the|some|any|to|on|in|at|of|for|with|and|is|it|this|that|there|"
    r"image|images|picture|pictures|photo|photos|photograph|photographs|pic|pics|"
    r"page|pages|site|website|top|section|nice|good|better|new)\b",
    re.IGNORECASE,
)


def allocate_photos(
    photos: list[dict], groups: tuple[tuple[str, ...], ...]
) -> dict[tuple[str, ...], list[dict]]:
    """Give each concurrent page call its own photographs, with no overlap.

    A four-page site is written by two calls that run at the same time, so neither can know
    what the other chose. Handed the same list, they both reached for the same picture --
    on a real build the about page and the contact page came out with the identical
    photograph of a toolset. Telling the model not to is useless when the other call has
    not happened yet, so the list is split here instead and the overlap is made impossible.

    The hero goes to whichever group writes index.html; the rest are dealt round-robin
    from that group onwards, so the home page keeps first pick.
    """
    if not photos or not groups:
        return {group: [] for group in groups}
    if len(groups) == 1:
        return {groups[0]: list(photos)}

    home = next((i for i, g in enumerate(groups) if "index.html" in g), 0)
    order = [groups[(home + offset) % len(groups)] for offset in range(len(groups))]

    allocation: dict[tuple[str, ...], list[dict]] = {group: [] for group in groups}
    hero = [p for p in photos if p["purpose"].lower() == HERO_PURPOSE]
    rest = [p for p in photos if p["purpose"].lower() != HERO_PURPOSE]
    for photo in hero:
        allocation[order[0]].append(photo)
    for i, photo in enumerate(rest):
        allocation[order[i % len(order)]].append(photo)
    return allocation


def photos_section(photos: list[dict], page_names: tuple[str, ...] = ()) -> str:
    """The photographs, rendered for a generation prompt.

    `page_names` is the group of pages this particular call is writing. The pages of a
    four-page site are generated by two concurrent calls that each receive the whole list,
    and on a real build the second call used none of it at all -- services.html and
    contact.html came out with no picture on them while the home and about pages had one
    each. Naming the pages lets the coverage rule below be about the page in hand.
    """
    if not photos:
        return ""
    lines = [
        "## Photographs you may use",
        "",
        "These are real photographs, already chosen for this business and already checked "
        "to load. Use them by their exact URL -- copy it character for character.",
        "",
    ]
    for photo in photos:
        lines.append(f'- {photo["purpose"]}: {photo["url"]}')
        lines.append(f'  alt: "{photo["alt"]}"')
    pages = " and ".join(page_names) if page_names else "each page"
    lines += [
        "",
        "Rules for these:",
        f"- **Every page you are writing ({pages}) should carry at least one of these** "
        "where one genuinely fits what that page is about. They are shared with the other "
        "pages of this site, so a page left with none reads as unfinished next to the "
        "rest. The exception is a page whose subject none of them suits -- a contact page "
        "that is a form and an address does not need one.",
        "- Use each at most once per page, and put the `hero` one at the top of the home "
        "page.",
        "- Never edit a URL, never invent another one, and never use an image address that "
        "is not in this list. An <img> whose URL does not load fails the build.",
        "- Always give an <img> the alt text listed with it.",
        "- A clean section still beats a wrong picture: never stretch one to fit a section "
        "it has nothing to do with.",
    ]
    return "\n".join(lines)


