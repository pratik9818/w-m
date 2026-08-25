import asyncio
import datetime
import difflib
import json
import logging
import re
import uuid
from pathlib import Path
from string import Template

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from bot_api.services.openrouter_client import (
    DailyLimitReached,
    OpenRouterCallFailed,
    call_plain_completion,
)
from bot_api.logging_config import log_event
from db.models import Business
from worker.codegen import shell, validate
from worker.codegen.design_brief import (
    brief_block,
    design_tokens_css,
    fallback_brief,
    font_link,
    make_design_brief,
)
from worker.codegen.quota import check_quota, record_usage
from worker.codegen.photos import allocate_photos, find_photos, photos_section
from worker.codegen.research import facts_for_build, facts_for_edit

PROMPTS_DIR = Path(__file__).parent / "prompts"

# Art direction now comes from worker/codegen/design_brief.py, which picks a palette,
# a typeface pairing and a signature device per business. The three fixed theme
# descriptions that used to live here survive as THEME_MOODS -- a starting point for
# that call rather than the whole design.

# Plain-text delimiters, not forced tool-calling: verified live that OpenRouter's free
# tier caps individual tool-call argument strings at ~1024 characters regardless of
# model or max_tokens -- far too short for a real HTML document. Plain completions
# don't hit that cap (see openrouter_client.py's module docstring).
FILE_MARKER_RE = re.compile(r"^===FILE:\s*(?P<name>[\w.\-]+)\s*===$", re.MULTILINE)
END_MARKER_RE = re.compile(r"^===END===\s*$", re.MULTILINE)
CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n|\n?```$")
CLASS_ATTR_RE = re.compile(r"""\bclass\s*=\s*["']([^"']+)["']""")
# href="" / href="#" / href="mailto:" / href="tel:" -- a link with no destination.
BROKEN_LINK_RE = re.compile(
    r"""<a\b[^>]*\bhref\s*=\s*["'](?:\s*|#|mailto:\s*|tel:\s*)["'][^>]*>(?P<text>.*?)</a\s*>""",
    re.IGNORECASE | re.DOTALL,
)
EMPTY_IMG_RE = re.compile(r"""<img\b[^>]*\bsrc\s*=\s*["']\s*["'][^>]*/?>""", re.IGNORECASE)
# Tags the model may put before <main> for the document head: a CDN stylesheet, an icon
# pack, a library script. Everything else outside <main> is discarded.
HEAD_ASSET_RE = re.compile(
    r"""<link\b[^>]*>|<script\b[^>]*>.*?</script\s*>|<script\b[^>]*/>""",
    re.IGNORECASE | re.DOTALL,
)
ASSET_URL_RE = re.compile(r"""\b(?:src|href)\s*=\s*["\']([^"\']*)["\']""", re.IGNORECASE)
# A stylesheet served from somewhere other than our own style.css. Its class names cannot
# be known from here, which is the thing _style_drift has to stop assuming.
EXTERNAL_CSS_RE = re.compile(
    r"""<link\b[^>]*\bhref\s*=\s*["\']https?://[^"\']+["\'][^>]*>""", re.IGNORECASE
)
IMG_TAG_RE = re.compile(r"""<img\b[^>]*>""", re.IGNORECASE)
IMG_SRC_RE = re.compile(r"""<img\b[^>]*\bsrc\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
CSS_CLASS_RE = re.compile(r"\.(-?[_a-zA-Z][\w-]*)")
PAGE_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
PAGE_DESC_RE = re.compile(
    r"""<meta[^>]*name\s*=\s*["']description["'][^>]*content\s*=\s*["'](.*?)["']""",
    re.IGNORECASE | re.DOTALL,
)
PAGE_MAIN_RE = re.compile(r"<main\b.*?</main\s*>", re.IGNORECASE | re.DOTALL)
# A patch may legitimately reword or delete an element ("remove the heading from the top"),
# so this is a floor on how much of the page must survive, not a ban on change. 0.75 sits
# well clear of both sides of the measured gap (legitimate edits >=0.956, wholesale <=0.633).
MIN_PATCH_SIMILARITY = 0.75

logger = logging.getLogger(__name__)

# Must mirror the class contract in prompts/_shared.md. These are the classes the
# stylesheet is required to style; anything else the model adds is a decorative extra.
CONTRACT_CLASSES = frozenset({
    "container",
    "site-header", "header-inner", "logo", "logo-text", "main-nav", "nav-list", "nav-link",
    "is-current",
    "hero", "page-hero", "hero-inner", "hero-title", "hero-subtitle", "hero-bg",
    "section", "section-alt", "section-title", "section-intro",
    "card-grid", "card", "card-title", "card-text",
    "steps", "step", "step-number", "step-title", "step-text",
    "faq-list", "faq-item", "faq-question", "faq-answer",
    "cta-band", "cta-title", "cta-text",
    "btn", "btn-primary", "btn-secondary",
    "contact-list", "contact-item", "contact-label", "contact-value",
    "site-footer", "footer-inner", "footer-col", "footer-title", "footer-note",
})
# Above this many unstyled classes, assume the stylesheet genuinely isn't the one these
# pages were written against rather than a few harmless decorative extras.
MAX_UNSTYLED_CLASSES = 12

STYLESHEET_FILE = "style.css"
MULTIPAGE_FILES = ("index.html", "about.html", "services.html", "contact.html")
LANDING_FILES = ("index.html",)
# Kept for callers that just want the widest possible set (e.g. patch target validation).
PAGE_FILES = MULTIPAGE_FILES
REQUIRED_FILES = (*MULTIPAGE_FILES, STYLESHEET_FILE)


def page_files_for(layout: str | None) -> tuple[str, ...]:
    return LANDING_FILES if layout == "landing" else MULTIPAGE_FILES


def required_files_for(layout: str | None) -> tuple[str, ...]:
    return (*page_files_for(layout), STYLESHEET_FILE)


def page_groups_for(layout: str | None) -> tuple[tuple[str, ...], ...]:
    """A landing page is one call; four pages are split into two concurrent calls."""
    return (LANDING_FILES,) if layout == "landing" else PAGE_GROUPS

# The four pages are independent of one another -- they only have to agree on the class
# contract in prompts/_shared.md -- so they are generated concurrently rather than in one
# long sequential response. Measured live: a single combined call emitted ~16k tokens at
# 39 tok/s on the large model (~408s). Splitting into these groups makes wall time the
# slowest group instead of the sum. Two page groups rather than four because the
# stylesheet is the largest single artifact and sets the floor either way -- four page
# calls would cost two extra requests against the free tier for no extra speed.
PAGE_GROUPS = (
    ("index.html", "about.html"),
    ("services.html", "contact.html"),
)

LANDING_REQUIREMENTS = (
    "**index.html** — the entire site, as one scrolling landing page.\n"
    "  Every section below goes on this single page, in this order. Each of the four "
    "anchor sections MUST carry the exact `id` given, because the site menu scrolls to "
    "them:\n"
    "  - A `hero` block (no id needed): a strong headline in `hero-title`, a supporting "
    "`hero-subtitle`, and a `btn btn-primary` link to `#contact`.\n"
    "  - `<section class=\"section\" id=\"about\">` — 3-4 substantial paragraphs on what "
    "the business does, who it serves, and how it works with customers.\n"
    "  - `<section class=\"section section-alt\" id=\"services\">` — what the business "
    "offers, as a `card-grid`. Use the real services and price labels if the data has "
    "them; otherwise describe the kind of work in general terms with nothing invented.\n"
    "  - A 'how it works' process section using `steps`, 3-5 `step` blocks.\n"
    "  - An FAQ using `faq-list`, 4-6 items, each a `<details class=\"faq-item\">` with "
    "`<summary class=\"faq-question\">` and `<div class=\"faq-answer\">` so it opens on click.\n"
    "  - `<section class=\"section\" id=\"contact\">` — a `contact-list` of the real "
    "contact details only, phone as a `tel:` link and email as a `mailto:` link, plus "
    "opening hours if real ones are given. Omit anything the data does not provide.\n"
    "  - A closing `cta-band`.\n"
    "  - **At least 700 words of real body copy**, since this page is the whole site.\n"
    "  - Link only to `#about`, `#services`, `#contact` — never to another .html file."
)

PAGE_REQUIREMENTS = {
    "index.html": (
        "**index.html** — the home page.\n"
        "  - A `hero` block: a strong headline in `hero-title`, a supporting `hero-subtitle`, "
        "and a `btn btn-primary` link to `contact.html`.\n"
        "  - An introduction section of 2-3 substantial paragraphs explaining what the business "
        "does and who it is for.\n"
        "  - A highlights section: a `card-grid` of 3-6 `card` blocks, each with a `card-title` "
        "and 2-3 sentences of `card-text`.\n"
        "  - A section previewing what the business offers, pointing to `services.html`.\n"
        "  - A 'why choose us' section of 3-4 points.\n"
        "  - A closing `cta-band`."
    ),
    "about.html": (
        "**about.html** — about the business.\n"
        "  - A `page-hero`.\n"
        "  - 3-4 substantial paragraphs about what the business does, what it focuses on, and "
        "who it serves. Write about the business as it is today. Do NOT write an origin story, "
        "a founding narrative, or anything about how or when it started unless the business "
        "data explicitly contains that history.\n"
        "  - An approach or values section: a `card-grid` of 3-4 `card` blocks.\n"
        "  - A section on how the business works with its customers and what it cares about.\n"
        "  - A closing `cta-band`."
    ),
    "services.html": (
        "**services.html** — what the business offers.\n"
        "  - A `page-hero`.\n"
        "  - Detailed descriptions of what the business offers. If the business data lists "
        "services, cover each one with its real name and its price label if given. If it lists "
        "none, describe the kinds of work a business in this category does, in general "
        "descriptive terms, with no invented names or prices.\n"
        "  - A 'how it works' process section using `steps`, with 3-5 `step` blocks each "
        "carrying a `step-number`, `step-title` and `step-text`.\n"
        "  - An FAQ using `faq-list`, with 4-6 items. Each item MUST be a "
        "`<details class=\"faq-item\">` containing `<summary class=\"faq-question\">` for the "
        "question and `<div class=\"faq-answer\">` for the answer, so it opens when clicked "
        "with no JavaScript. Write questions a real customer in this category would ask, and "
        "answer them honestly without inventing prices, times, or guarantees.\n"
        "  - A closing `cta-band`."
    ),
    "contact.html": (
        "**contact.html** — how to get in touch.\n"
        "  - A `page-hero`.\n"
        "  - A `contact-list` of the business's real contact details only, each as a "
        "`contact-item` with a `contact-label` and `contact-value`. Phone becomes a `tel:` "
        "link and email a `mailto:` link. Omit anything the business data does not provide — "
        "never invent or leave an empty link.\n"
        "  - The opening hours, exactly as written in the data, only if real hours are given.\n"
        "  - A section on what to expect when getting in touch and how the business responds.\n"
        "  - A closing `cta-band`. Remember there is no working form — contact links only."
    ),
}


class GenerationFailed(Exception):
    pass


class EditNotApplied(GenerationFailed):
    """A patch ran, cost tokens, and changed nothing at all.

    Real case: an owner asked twice to put their photo behind the hero text. The picture
    was an `<img>` in index.html, but only style.css was targeted, and the instruction
    ("set hero background image opacity to 0.35") described a rule that did not exist. The
    model correctly returned the stylesheet untouched -- and the pipeline then validated,
    sandboxed, deployed and announced "your site is live!" for two consecutive versions
    that were byte-identical to the one before. Silence would have been better than that;
    saying so plainly is better still.
    """


def _strip_code_fence(text: str) -> str:
    return CODE_FENCE_RE.sub("", text.strip())


def _read_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _parse_files(content: str) -> dict[str, str]:
    """Split a `===FILE: name===` delimited response into {filename: content}."""
    markers = list(FILE_MARKER_RE.finditer(content))
    if not markers:
        return {}

    end_match = END_MARKER_RE.search(content, markers[-1].end())
    tail = end_match.start() if end_match else len(content)

    files: dict[str, str] = {}
    for index, marker in enumerate(markers):
        stop = markers[index + 1].start() if index + 1 < len(markers) else tail
        body = _strip_code_fence(content[marker.end():stop])
        if body:
            files[marker.group("name")] = body
    return files


# Values the owner typed as a non-answer during onboarding rather than real data. Left in
# the spec they get rendered literally (observed live: `phone: "Skip"` became a `tel:Skip`
# link, `hours: "So not include this"` was printed as opening hours). The prompt also tells
# the model to ignore these, but stripping them here means it never has to notice.
JUNK_VALUES = {
    "skip", "none", "n/a", "na", "-", "--", "no", "nil", "nothing", "null", "later",
    "not now", "no thanks", "dont have", "don't have", "not applicable", "tbd",
}


def _clean(value: str | None) -> str | None:
    """Drop non-answer placeholder values so they never reach the page."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    normalized = stripped.lower().rstrip(".!").strip()
    if normalized in JUNK_VALUES:
        return None
    return stripped


def spec_from_business(business: Business) -> dict:
    logo_url = next((m.url for m in business.media if m.kind == "logo"), None)
    photo_urls = [m.url for m in business.media if m.kind == "photo"]
    return {
        "name": business.name,
        "category": business.category,
        "tagline": _clean(business.tagline),
        "about": _clean(business.about),
        "theme": business.theme,
        "phone": _clean(business.phone),
        "email": _clean(business.email),
        "address": _clean(business.address),
        "hours": _clean(business.hours.get("display_text")) if business.hours else None,
        "services": [
            {"name": s.name, "price_label": _clean(s.price_label)}
            for s in business.services
            if s.is_active
        ],
        "logo_url": logo_url,
        "photo_urls": photo_urls,
        "extra_instructions": _clean(business.extra_instructions),
        "layout": business.layout,
        # Passed in as data so the footer never reaches for `new Date()`. Scripts are
        # allowed on these sites now, but the footer year is still the wrong place for
        # one: the shell writes the footer, the model never sees it, and a value known at
        # build time should not wait on the browser to compute it.
        "current_year": datetime.date.today().year,
    }


# The stylesheet call only needs to know what the site *is* and how it should look -- the
# services list, opening hours and about text influence page copy, never a CSS rule.
DESIGN_SPEC_FIELDS = ("name", "category", "theme", "layout", "logo_url", "extra_instructions")


def _contract_block(spec: dict, spec_json: str, brief: dict) -> str:
    """Class contract, technical constraints and theme -- the part both calls need."""
    contract = Template(_read_prompt("_contract.md")).substitute(
        design_brief=brief_block(brief)
    )
    return f"{contract}\n\n## Business data\n\n```json\n{spec_json}\n```"


def _stylesheet_prompt(spec: dict, brief: dict) -> str:
    # Deliberately excludes _content_rules.md (fabrication and copywriting rules, ~1,100
    # tokens) and the content-bearing spec fields: a stylesheet cannot fabricate a
    # testimonial or render an opening time, so sending those rules every build was
    # ~1,500 tokens of instruction the model had no way to act on.
    design_spec = {k: spec.get(k) for k in DESIGN_SPEC_FIELDS}
    shared = _contract_block(spec, json.dumps(design_spec, indent=2, ensure_ascii=False), brief)
    return Template(_read_prompt("stylesheet.md")).substitute(shared=shared)


def _pages_prompt(
    spec: dict, page_names: tuple[str, ...], brief: dict, research: str = "",
    photos: list[dict] | None = None,
) -> str:
    spec_json = json.dumps(spec, indent=2, ensure_ascii=False)
    shared = _contract_block(spec, spec_json, brief) + "\n\n" + _read_prompt("_content_rules.md")
    return _render_pages_prompt(shared, page_names, spec.get("layout"), research, photos)


def _assemble_page(spec: dict, filename: str, fragment: str, brief: dict,
                   stock_photo_credit: bool = False) -> str:
    """Turn a model-written fragment (title, description, <main>) into a full page."""
    title = PAGE_TITLE_RE.search(fragment)
    desc = PAGE_DESC_RE.search(fragment)
    main = PAGE_MAIN_RE.search(fragment)
    if main is None:
        raise GenerationFailed(f"{filename}: response contained no <main> block")
    return shell.render_page(
        spec,
        filename,
        title.group(1).strip() if title else str(spec.get("name") or "Website"),
        desc.group(1).strip() if desc else (spec.get("tagline") or ""),
        main.group(0),
        font_link(brief),
        # A CDN stylesheet or library the page asked for. It sits before <main> in the
        # response because the shell owns <head> -- the model cannot write into it.
        head_extra=_head_assets(fragment[: main.start()]),
        stock_photo_credit=stock_photo_credit,
    )


def _render_pages_prompt(
    shared: str, page_names: tuple[str, ...], layout: str | None = None, research: str = "",
    photos: list[dict] | None = None,
) -> str:
    output_format = "\n".join(
        f"===FILE: {name}===\n<the title, description and main block for {name}>"
        for name in page_names
    ) + "\n===END==="
    if layout == "landing":
        requirements = LANDING_REQUIREMENTS
    else:
        requirements = "\n\n".join(PAGE_REQUIREMENTS[name] for name in page_names)
    return Template(_read_prompt("pages.md")).substitute(
        shared=shared,
        page_names=" and ".join(page_names),
        output_format=output_format,
        page_requirements=requirements,
        research=f"\n{research}\n" if research else "",
        photos=f"\n{photos_section(photos or [], page_names)}\n" if photos else "",
    )


def _undefined_classes(files: dict[str, str]) -> list[str]:
    """Every class used in the HTML that the stylesheet never defines."""
    defined = set(CSS_CLASS_RE.findall(files.get(STYLESHEET_FILE, "")))
    used: set[str] = set()
    for name, body in files.items():
        if name.endswith(".html"):
            for attr in CLASS_ATTR_RE.findall(body):
                used.update(attr.split())
    return sorted(used - defined)


def _sanitize_html(html: str) -> str:
    """Remove output the contract already forbids, rather than merely reporting it.

    Every defect handled here caused a real failed build: three separate deploys died on
    an empty `mailto:` at 6/7 and 7/8 checks. Each had a prompt rule forbidding it, in two
    files, and the models emitted it anyway. Removal is safe precisely because these are
    things the site is never allowed to contain -- so nothing legitimate can be lost.

    Script elements used to be stripped here too. They are now a supported part of a site,
    so they are left alone: what makes that safe is the sandbox, which loads every page in
    a real browser and fails the build on any console error or thrown exception. A script
    is no longer forbidden output, so removing it would destroy working page behaviour.
    """
    # Dead links: keep the visible text, drop the anchor.
    html = BROKEN_LINK_RE.sub(lambda m: m.group("text"), html)
    # An <img> with no src renders as a broken-image icon on a live customer site.
    html = EMPTY_IMG_RE.sub("", html)
    return html


# Long enough for a slow CDN, short enough that a hung host cannot hold up a build. Every
# URL is checked concurrently, so this is close to the whole cost of the pass.
IMAGE_HEAD_TIMEOUT_SECONDS = 8


def _head_assets(fragment_before_main: str) -> str:
    """Pull the <link>/<script src> tags the model wrote for the document head.

    Anything relative is dropped: this site has no local .js and exactly one local
    stylesheet, already in the shell, so a relative asset path can only ever be a 404.
    """
    kept = []
    for tag in HEAD_ASSET_RE.findall(fragment_before_main):
        url = next(iter(ASSET_URL_RE.findall(tag)), "")
        if url.startswith(("http://", "https://")):
            kept.append(tag.strip())
    return "\n  ".join(kept)


async def _url_is_live(client: httpx.AsyncClient, url: str) -> bool:
    """True unless the URL definitively did not serve an image.

    Deliberately asymmetric: only a real response with a bad status counts as dead. A
    timeout or a DNS failure is our problem, not the page's, and silently deleting a
    perfectly good photo because the build host had a bad moment is the worse mistake.
    """
    try:
        resp = await client.head(url, follow_redirects=True, timeout=IMAGE_HEAD_TIMEOUT_SECONDS)
        if resp.status_code == 405:  # HEAD not allowed; ask for the first byte instead
            resp = await client.get(
                url, follow_redirects=True, timeout=IMAGE_HEAD_TIMEOUT_SECONDS,
                headers={"Range": "bytes=0-0"},
            )
        return resp.status_code < 400
    except httpx.HTTPError:
        return True


async def _drop_dead_images(files: dict[str, str]) -> dict[str, str]:
    """Remove every <img> whose URL does not actually load.

    Now that the model may reach for real photography, an invented image URL is the
    obvious new failure -- and one the sandbox already fails the build over. Checking here
    costs a few HEAD requests and turns a dead build into a page with one fewer picture.
    """
    urls = {
        src
        for name, body in files.items()
        if name.endswith(".html")
        for src in IMG_SRC_RE.findall(body)
        if src.startswith(("http://", "https://"))
    }
    if not urls:
        return files

    ordered = sorted(urls)
    async with httpx.AsyncClient() as client:
        alive = await asyncio.gather(*(_url_is_live(client, url) for url in ordered))
    dead = {url for url, ok in zip(ordered, alive) if not ok}
    if not dead:
        return files

    log_event(logger, "images.dropped", count=len(dead), urls=sorted(dead)[:5])

    def strip(match: re.Match) -> str:
        src = next(iter(IMG_SRC_RE.findall(match.group(0))), "")
        return "" if src in dead else match.group(0)

    return {
        name: (IMG_TAG_RE.sub(strip, body) if name.endswith(".html") else body)
        for name, body in files.items()
    }


def _sanitize_files(files: dict[str, str]) -> dict[str, str]:
    return {
        name: _sanitize_html(body) if name.endswith(".html") else body
        for name, body in files.items()
    }


STYLESHEET_MARKER = "/* ---- generated ---- */"


# Contract classes that are *boxes the page is built out of*. Taking one of these out of
# the document flow is never a design choice -- it is always a mistake, because the element
# stops reserving any height and whatever follows it slides underneath.
#
# This is not hypothetical. A real site shipped with:
#
#     .hero-bg, .page-hero.hero-bg { position: absolute; inset: 0; pointer-events: none; }
#
# base.css defines `.hero-bg` as the hero *section* carrying a background photo; the model,
# which never sees the HTML, read the name as a decorative backdrop layer and positioned it
# like one. The hero left the flow, the next section rendered 681px underneath it, and the
# owner reported overlapping text six times. Every attempted fix added margin or padding --
# which cannot move an absolutely positioned element -- so all six failed, and one of them
# enlarged the hero and made it worse.
#
# Deliberately excludes ::before/::after: a positioned pseudo-element is the normal way to
# build an overlay, and base.css relies on exactly that.
LAYOUT_CONTAINERS = frozenset({
    "container", "hero", "page-hero", "hero-bg", "hero-inner",
    "section", "section-alt", "card-grid", "card", "steps", "step",
    "faq-list", "faq-item", "cta-band", "contact-list", "contact-item",
    "site-footer", "footer-inner", "footer-col", "header-inner",
})
CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
OUT_OF_FLOW_DECL_RE = re.compile(
    r"\s*position\s*:\s*(?:absolute|fixed)\s*(?:!important)?\s*;?", re.IGNORECASE
)


def _selector_targets_container(selector: str) -> bool:
    """True if this selector styles a layout container itself, not a pseudo-element of one."""
    for part in selector.split(","):
        part = part.strip()
        if not part or "::" in part:
            continue
        # The subject of a selector is its last compound: `.hero .card` styles the card.
        subject = part.split()[-1].split(">")[-1].strip()
        classes = set(re.findall(r"\.([\w-]+)", subject))
        if classes & LAYOUT_CONTAINERS:
            return True
    return False


def _keep_layout_in_flow(css: str) -> tuple[str, list[str]]:
    """Drop `position: absolute/fixed` from rules that style a layout container.

    Returns (css, selectors_fixed). Only that one declaration is removed -- everything else
    in the rule is the design the model intended and is left alone.
    """
    fixed: list[str] = []

    def clean(match: re.Match) -> str:
        selector, body = match.group(1), match.group(2)
        if not OUT_OF_FLOW_DECL_RE.search(body) or not _selector_targets_container(selector):
            return match.group(0)
        fixed.append(" ".join(selector.split())[:80])
        return f"{selector}{{{OUT_OF_FLOW_DECL_RE.sub('', body)}}}"

    return CSS_RULE_RE.sub(clean, css), fixed


def _with_base_css(css: str, brief: dict | None = None) -> str:
    """Prepend the fallback stylesheet so a contract class can never end up unstyled.

    Model rules come after and win via the normal cascade. Without this, the stylesheet
    call omitting a single contract class failed the entire build -- which really happened
    to a real business (`find-dog`, missing `btn-primary`).
    """
    base = (Path(__file__).parent / "base.css").read_text(encoding="utf-8")
    tokens = f"{design_tokens_css(brief)}\n" if brief else ""
    css, fixed = _keep_layout_in_flow(css)
    if fixed:
        log_event(logger, "css.flow_restored", selectors=fixed[:5], count=len(fixed))
    return f"{base}\n{STYLESHEET_MARKER}\n{tokens}{css}"


def _split_stylesheet(css: str) -> tuple[str, str]:
    """Separate the fallback block from the design's own rules.

    Both halves define the same class names, so the stored stylesheet contains every
    contract class twice -- and the fallback copy, being first, loses every cascade. It is
    dead weight that reads exactly like the real thing.

    That cost an owner three consecutive edits: asked to enlarge and centre the hero text,
    the model edited `.hero-title` and `.hero-subtitle` at lines 30-31, both overridden by
    the real rules 150 lines below. Each edit changed bytes, passed all nine checks and
    deployed, and the page looked identical every time.

    Returns (prefix_including_marker, editable_rules); the prefix is "" for a stylesheet
    that predates the marker, in which case the whole file is editable.
    """
    index = css.find(STYLESHEET_MARKER)
    if index == -1:
        return "", css
    cut = index + len(STYLESHEET_MARKER)
    return css[:cut], css[cut:].lstrip("\n")


def _patch_similarity(before: str, after: str) -> float:
    """How much of the file survived the patch, by line. 1.0 = byte-identical.

    Measured on real output before picking the threshold: a one-element rename scores
    1.000, rewriting three whole paragraphs 0.968, deleting a section 0.956 -- while
    substituting a genuinely different page of the same site scores 0.633, and an
    unrelated page 0.131. So legitimate edits and wholesale rewrites separate cleanly.

    An earlier version of this guard compared only the structural element/class sequence.
    It was too weak to ship: every page shares the same header/nav/section/footer
    skeleton, so swapping in a completely different page registered just 0.182 churn and
    sailed under the limit. Text has to be part of the comparison.
    """
    return difflib.SequenceMatcher(a=before.splitlines(), b=after.splitlines()).ratio()


def _style_drift(files: dict[str, str]) -> str | None:
    """Detect a stylesheet that genuinely doesn't match the pages, or None if it's fine.

    Needed because the pages and the stylesheet now come from separate model calls (and,
    on a content-only edit, from different runs entirely via the cached stylesheet), so
    they can disagree. Playwright renders unstyled HTML perfectly happily, so no sandbox
    check would catch it.

    Deliberately NOT "every class must be styled". Models add harmless decorative
    modifiers alongside contract classes -- observed live: `class="section faq"`, where
    `faq` has no rule but `section` does, so the element still looks right. Failing that
    build was a false positive. What actually matters is a *contract* class going
    unstyled, or so many unstyled classes that the stylesheet clearly isn't the one these
    pages were written against.
    """
    undefined = set(_undefined_classes(files))
    # A page may now load an icon pack or a CSS library from a CDN, whose class names are
    # not in style.css and cannot be read from here. The count below assumes style.css is
    # the only stylesheet, so once another one is linked the count means nothing -- a site
    # with a dozen `fa-*` icons is not a mismatched build. The contract-class check below
    # still holds either way, because base.css guarantees those regardless.
    external_css = any(
        name.endswith(".html") and EXTERNAL_CSS_RE.search(body) for name, body in files.items()
    )
    missing_contract = sorted(undefined & CONTRACT_CLASSES)
    if missing_contract:
        # Deliberately NOT a failure any more: base.css guarantees a usable rule for every
        # contract class, so this is cosmetic (the model's own styling for that class is
        # missing, the element still renders fine). Raising here killed a real build.
        logger.warning("stylesheet omitted contract class rules, base.css covering: %s", missing_contract)
    if external_css and len(undefined) > MAX_UNSTYLED_CLASSES:
        logger.info(
            "%d unstyled classes, but the page loads an external stylesheet -- not drift",
            len(undefined),
        )
        return None
    if len(undefined) > MAX_UNSTYLED_CLASSES:
        return (
            f"{len(undefined)} classes have no stylesheet rules, which suggests the pages "
            f"and stylesheet don't match: {sorted(undefined)[:15]}"
        )
    return None


def _format_reminder(expected: tuple[str, ...], closing: str) -> str:
    """Told to the model after it replies in the wrong shape."""
    blocks = "\n\n".join(
        f"===FILE: {name}===\n<the complete file>\n===END===" for name in expected
    )
    return (
        "\n\n## Your previous reply was rejected\n\n"
        "It was missing a file marker, or a file stopped before its closing "
        f"`{closing}`. Reply with nothing but the blocks below -- no preamble, no "
        "explanation, each marker alone on its own line, every file complete:\n\n"
        f"{blocks}\n"
    )


def _files_from_response(
    content: str, expected: tuple[str, ...], *, fragments: bool
) -> dict[str, str]:
    """Pull the expected files out of one response, or say why they are not there."""
    files = _parse_files(content)

    # One file expected and no markers at all: the model answered with the file itself.
    # The truncation check below still has to pass, so this accepts a complete page and
    # rejects a fragment or an apology, exactly as it would with the markers present.
    if not files and len(expected) == 1 and content.strip():
        files = {expected[0]: _strip_code_fence(content)}

    missing = [name for name in expected if name not in files]
    if missing:
        raise GenerationFailed(
            f"Model response missing required file(s) {missing}: {content[:400]!r}"
        )
    # Truncation guard: the free tier silently cuts long responses off, and the sandbox
    # can't catch malformed HTML (Chromium renders it permissively). Page responses are
    # now fragments, so the closing element to look for is </main>, not </html>.
    closing = "</main>" if fragments else "</html>"
    for name in expected:
        if name.endswith(".html") and closing not in files[name]:
            raise GenerationFailed(f"Model response looks truncated: {name} lacks a closing {closing}")
    return {name: files[name] for name in expected}


async def _generate(
    prompt: str, expected: tuple[str, ...], *, fragments: bool = False,
    reduced_reasoning: bool = False
) -> tuple[dict[str, str], dict]:
    """One call for a group of files, with a second attempt if the shape is wrong.

    A response that forgets the `===FILE:===` markers or stops mid-page is a formatting
    failure, not a bad site, and it is worth exactly one retry: a build runs two or three
    of these concurrently, so letting one bad shape through un-retried discards every
    other call in the batch. A real first build was lost that way -- the response was a
    complete page that simply opened with `<title>` instead of its marker.
    """
    closing = "</main>" if fragments else "</html>"
    last_error = ""
    for attempt in (1, 2):
        content, usage = await call_plain_completion(
            prompt if attempt == 1 else prompt + _format_reminder(expected, closing),
            reduced_reasoning=reduced_reasoning,
        )
        try:
            return _files_from_response(content, expected, fragments=fragments), usage
        except GenerationFailed as exc:
            last_error = str(exc)
            logger.warning(
                "generation attempt %d/2 rejected for %s: %s", attempt, list(expected), last_error
            )
    raise GenerationFailed(last_error)


def _patch_prompt(
    filename: str,
    content: str,
    instruction: str,
    user_message: str | None = None,
    reference: str | None = None,
    research: str = "",
) -> str:
    # The instruction is written by a parser that has never seen this file, so the names in
    # it are guesses. Carrying the owner's own words alongside it gives the model -- which
    # *can* see the file -- something to fall back on when a guessed name doesn't exist.
    owner_words = (
        f'\nThe owner asked for this in their own words:\n\n"{user_message.strip()}"\n'
        if user_message and user_message.strip()
        else ""
    )
    # Read-only, not editable: these rules load before the file above and are the reason a
    # value can look "already set" without appearing in the editable rules at all. Without
    # seeing them the model has to guess the current size, and it guessed smaller.
    reference_block = (
        "\n## Defaults already in effect (read-only — you cannot change these)\n\n"
        "These load BEFORE the file above, so anything the file above sets wins over them.\n"
        "They are shown only so you can see what a value currently is. To change one, edit "
        "or add a rule in the file above — never reply with these.\n\n"
        f"```css\n{reference.strip()}\n```\n"
        if reference and reference.strip()
        else ""
    )
    return Template(_read_prompt("patch.md")).safe_substitute(
        filename=filename, file_content=content, instruction=instruction,
        owner_words=owner_words, reference=reference_block,
        research=f"\n{research}\n" if research else "",
    )


async def _patch_one(
    filename: str,
    content: str,
    instruction: str,
    user_message: str | None = None,
    reference: str | None = None,
    research: str = "",
) -> tuple[str, dict]:
    """Patch a single file, rejecting a response that restructured too much of it."""
    last_error = ""
    for attempt in (1, 2):
        # Applying a stated change to an existing file is mechanical work: the model was
        # spending ~80% of its output budget deliberating rather than writing the file.
        text, usage = await call_plain_completion(
            _patch_prompt(filename, content, instruction, user_message, reference, research),
            reduced_reasoning=True,
        )
        parsed = _parse_files(text)
        patched = parsed.get(filename) or (next(iter(parsed.values())) if len(parsed) == 1 else None)

        if patched is None:
            last_error = f"response did not contain {filename}"
        elif filename.endswith(".html") and not patched.rstrip().endswith("</html>"):
            last_error = f"{filename} came back truncated (no closing </html>)"
        elif (similarity := _patch_similarity(content, patched)) < MIN_PATCH_SIMILARITY:
            # The mechanical version of "only change what I asked for" -- the prompt says it
            # too, but a rule that lives only in the prompt is a hope, not a guarantee.
            last_error = (
                f"{filename} was {1 - similarity:.0%} rewritten, far more than the change asked for"
            )
        else:
            return patched, usage

        logger.warning("patch attempt %d/2 rejected for %s: %s", attempt, filename, last_error)

    raise GenerationFailed(f"Could not apply that change to {filename} without rewriting it: {last_error}")


async def repair_files(
    files: dict[str, str],
    checks: list[dict],
    *,
    session: AsyncSession | None = None,
    owner_telegram_id: int | None = None,
    business_id: uuid.UUID | None = None,
) -> tuple[dict[str, str], dict | None, list[dict]]:
    """Try once to fix the specific defects `checks` found, without rebuilding anything.

    Returns (files, usage_or_None, remaining_failures). Deterministic sanitizing runs
    first and is free; only what survives that is worth an API call.

    Exists because three real builds were discarded at 6/7 and 7/8 checks over one empty
    `mailto:` -- roughly 24,000 tokens each to regenerate a site that was already correct
    apart from one link. Repair costs one call and a few thousand tokens.
    """
    files = _sanitize_files(files)
    # Re-derive the markup checks, because sanitizing may have just fixed some of them,
    # and carry forward anything `checks` reported that this module cannot judge for
    # itself -- every browser-only verdict: console errors, images that would not load,
    # a stylesheet that 404ed, a page that rendered identically.
    #
    # Carrying them is the whole point. Until this existed the `checks` argument was
    # never read: a sandbox failure re-ran the markup checks, found nothing wrong (the
    # markup was fine -- it was the JavaScript that threw), and returned "nothing to
    # repair" while the build died. A page whose script had one stray bracket could not
    # be repaired at all.
    markup = validate.validate_files(files)
    judged_here = {check["name"] for check in markup}
    carried = [c for c in checks if not c["passed"] and c["name"] not in judged_here]
    remaining = validate.failed(markup) + carried
    if not remaining:
        log_event(logger, "repair.sanitized", files=len(files))
        return files, None, []

    per_file = validate.files_needing_repair(remaining, files)
    if not per_file:
        return files, None, remaining

    if owner_telegram_id is not None:
        assert session is not None, "session is required when owner_telegram_id is given"
        await check_quota(session, owner_telegram_id)

    targets = sorted(per_file)
    log_event(logger, "repair.started", files=targets,
              problems=[c["name"] for c in remaining])

    results = await asyncio.gather(
        *(
            _generate(
                Template(_read_prompt("repair.md")).safe_substitute(
                    filename=name,
                    problems="\n".join(f"- {p}" for p in per_file[name]),
                    file_content=files[name],
                ),
                (name,),
                reduced_reasoning=True,
            )
            for name in targets
        ),
        return_exceptions=True,
    )

    usage = {"model": "", "input_tokens": 0, "output_tokens": 0, "requests": len(targets)}
    repaired = dict(files)
    for name, result in zip(targets, results):
        if isinstance(result, Exception):
            logger.warning("repair failed for %s: %s", name, result)
            continue
        part_files, part_usage = result
        candidate = _sanitize_html(part_files[name])
        # A "repair" that rewrote the page is not a repair. Same guard as patching.
        similarity = _patch_similarity(files[name], candidate)
        if similarity < MIN_PATCH_SIMILARITY:
            logger.warning("repair for %s rewrote %.0f%% of the file; rejected", name, (1 - similarity) * 100)
            continue
        repaired[name] = candidate
        usage["model"] = part_usage["model"]
        usage["input_tokens"] += part_usage["input_tokens"]
        usage["output_tokens"] += part_usage["output_tokens"]

    # The carried checks came from a browser and cannot be re-run here, so they stay on
    # the list. The sandbox caller re-tests and gets the real answer; the pre-flight
    # caller never passes any, so nothing changes for it.
    still_failing = validate.failed(validate.validate_files(repaired)) + carried
    log_event(logger, "repair.finished", fixed=len(remaining) - len(still_failing),
              remaining=[c["name"] for c in still_failing],
              tokens=usage["input_tokens"] + usage["output_tokens"])

    if owner_telegram_id is not None and usage["input_tokens"]:
        await record_usage(
            session, owner_telegram_id, business_id, usage["model"],
            usage["input_tokens"], usage["output_tokens"],
            kind="repair", requests=usage["requests"],
        )
    return repaired, (usage if usage["input_tokens"] else None), still_failing


async def patch_site_files(
    files: dict[str, str],
    instruction: str,
    targets: list[str],
    *,
    user_message: str | None = None,
    session: AsyncSession | None = None,
    owner_telegram_id: int | None = None,
    business_id: uuid.UUID | None = None,
) -> tuple[dict[str, str], dict]:
    """Apply one change to `targets` within an existing file set, leaving the rest alone.

    Files outside `targets` are passed through byte-identical and cost no model call at
    all -- that is the whole point: an edit must not be able to change the pages and the
    design the owner did not ask about.
    """
    if owner_telegram_id is not None:
        assert session is not None, "session is required when owner_telegram_id is given"
        await check_quota(session, owner_telegram_id)

    wanted = [name for name in targets if name in files]
    if not wanted:
        raise GenerationFailed(
            f"Nothing to patch: {targets} not among the stored files {sorted(files)}"
        )

    # Hide the dead fallback block from the model entirely. Left visible, it is the first
    # thing matching any selector it looks for, and an edit made there does nothing.
    prefixes = {name: "" for name in wanted}
    editable = {}
    for name in wanted:
        if name == STYLESHEET_FILE:
            prefixes[name], editable[name] = _split_stylesheet(files[name])
        else:
            editable[name] = files[name]

    # An edit can need facts just as much as a build can -- "add the other coins too"
    # names nothing the model knows. One research pass is shared by every targeted file,
    # rather than each patch call paying for its own search.
    research, research_usage = await facts_for_edit(instruction, user_message)

    results = await asyncio.gather(
        *(_patch_one(name, editable[name], instruction, user_message, prefixes[name], research)
          for name in wanted)
    )

    patched = dict(files)
    usage = {"model": "", "input_tokens": 0, "output_tokens": 0, "requests": len(wanted)}
    if research_usage:
        usage["input_tokens"] += research_usage["input_tokens"]
        usage["output_tokens"] += research_usage["output_tokens"]
        usage["requests"] += research_usage.get("requests", 1)
    for name, (content, part_usage) in zip(wanted, results):
        if prefixes[name]:
            content = f"{prefixes[name]}\n{content}"
        if name.endswith(".html"):
            patched[name] = _sanitize_html(content)
        else:
            # An edit can reintroduce the same defect, and an existing site can still be
            # carrying it from before this guard existed -- so patched stylesheets are
            # cleaned too, which quietly repairs an already-broken site on its next edit.
            patched[name], fixed = _keep_layout_in_flow(content)
            if fixed:
                log_event(logger, "css.flow_restored", selectors=fixed[:5], count=len(fixed))
        usage["model"] = part_usage["model"]
        usage["input_tokens"] += part_usage["input_tokens"]
        usage["output_tokens"] += part_usage["output_tokens"]

    # If nothing in any targeted file moved, the edit did not happen -- usually because the
    # instruction described something that isn't in the files it was given. Checked across
    # all targets rather than per file, since a change spanning two files legitimately
    # leaves one of them alone.
    if all(patched[name] == files[name] for name in wanted):
        raise EditNotApplied(
            f"patch left {wanted} byte-identical; instruction did not match the file contents"
        )

    drift = _style_drift(patched)
    if drift is not None:
        raise GenerationFailed(drift)

    patched = await _drop_dead_images(patched)

    if owner_telegram_id is not None:
        await record_usage(
            session, owner_telegram_id, business_id, usage["model"],
            usage["input_tokens"], usage["output_tokens"],
            kind="edit", requests=usage["requests"],
        )
    return patched, usage


async def _brief_or_fallback(spec: dict) -> tuple[dict, dict | None]:
    """The design brief, or the theme's hand-picked defaults if the call fails."""
    try:
        return await make_design_brief(spec, json.dumps(spec, indent=2, ensure_ascii=False))
    except Exception:
        logger.warning("design brief raised, falling back", exc_info=True)
        return fallback_brief(spec.get("theme") or "classic"), None


async def build_site(
    spec: dict,
    *,
    session: AsyncSession | None = None,
    owner_telegram_id: int | None = None,
    business_id: uuid.UUID | None = None,
    kind: str = "create",
) -> tuple[dict[str, str], dict]:
    """Generate a five-file static site (four pages plus a shared stylesheet) from a spec.

    Used for a first build and for an explicit rebuild. Ordinary edits go through
    patch_site_files() instead, so they can't redesign a site the owner didn't ask about.

    If owner_telegram_id is given, the caller's token quota is checked before calling the
    API and the combined usage across all calls is recorded against it afterward.
    Returns (files, usage) where usage is {"model", "input_tokens", "output_tokens"}.
    """
    if owner_telegram_id is not None:
        assert session is not None, "session is required when owner_telegram_id is given"
        await check_quota(session, owner_telegram_id)

    layout = spec.get("layout")

    # Two short calls before anything else, run together because neither depends on the
    # other. The brief keeps the pages and the stylesheet on one art direction instead of
    # each inventing its own; the research answers anything about this business the model
    # would otherwise have to make up. Neither can fail the build: the brief falls back to
    # the theme's hand-picked defaults, and research falls back to no facts at all.
    # Photographs join the same round: finding them is a model call plus a few searches,
    # and it depends on nothing the brief or the research produces. Owners do not supply
    # pictures, so if this returns nothing the pages are written to work without them
    # rather than reaching for a random-image service.
    (brief, brief_usage), (research, research_usage), (photos, photo_usage) = await asyncio.gather(
        _brief_or_fallback(spec), facts_for_build(spec), find_photos(spec)
    )
    log_event(
        logger, "design.brief", theme=spec.get("theme"),
        display_font=brief["display_font"], body_font=brief["body_font"],
        accent=brief["accent"],
    )

    # Split rather than shared: the page calls below run concurrently, so a photograph
    # offered to both is a photograph that can appear twice on one site.
    groups = page_groups_for(layout)
    per_group = allocate_photos(photos, groups)
    jobs = [
        _generate(
            _pages_prompt(spec, group, brief, research, per_group[group]), group, fragments=True
        )
        for group in groups
    ]
    jobs.append(_generate(_stylesheet_prompt(spec, brief), (STYLESHEET_FILE,)))

    try:
        results = await asyncio.gather(*jobs)
    except DailyLimitReached:
        raise  # resolves itself with time -- must not be reported as a generation fault
    except OpenRouterCallFailed as exc:
        raise GenerationFailed(f"Site generation failed: {exc}") from exc

    files: dict[str, str] = {}
    usage = {"model": "", "input_tokens": 0, "output_tokens": 0, "requests": len(jobs)}
    for extra in (brief_usage, research_usage, photo_usage):
        if extra:
            usage["input_tokens"] += extra["input_tokens"]
            usage["output_tokens"] += extra["output_tokens"]
            # Research is one call when it decides no lookup is needed and two when it
            # runs one, so it reports its own count rather than assuming.
            usage["requests"] += extra.get("requests", 1)
    for part_files, part_usage in results:
        for name, body in part_files.items():
            # Pages come back as fragments; the shared shell is added here rather than
            # written four times by the model.
            files[name] = (
                _assemble_page(spec, name, body, brief, stock_photo_credit=bool(photos))
                if name.endswith(".html") else body
            )
        usage["model"] = part_usage["model"]
        usage["input_tokens"] += part_usage["input_tokens"]
        usage["output_tokens"] += part_usage["output_tokens"]

    files[STYLESHEET_FILE] = _with_base_css(files.get(STYLESHEET_FILE, ""), brief)
    files = _sanitize_files(files)
    files = await _drop_dead_images(files)

    missing = [name for name in required_files_for(layout) if name not in files]
    if missing:
        raise GenerationFailed(f"Generated site is missing required file(s): {missing}")

    drift = _style_drift(files)
    if drift is not None:
        raise GenerationFailed(drift)

    if owner_telegram_id is not None:
        await record_usage(
            session, owner_telegram_id, business_id, usage["model"],
            usage["input_tokens"], usage["output_tokens"],
            kind=kind, requests=usage["requests"],
        )
    return files, usage
