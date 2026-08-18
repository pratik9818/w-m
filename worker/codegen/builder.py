import asyncio
import datetime
import difflib
import json
import logging
import re
import uuid
from pathlib import Path
from string import Template

from sqlalchemy.ext.asyncio import AsyncSession

from bot_api.services.openrouter_client import (
    DailyLimitReached,
    OpenRouterCallFailed,
    call_plain_completion,
)
from db.models import Business
from worker.codegen.quota import check_quota, record_usage

PROMPTS_DIR = Path(__file__).parent / "prompts"

THEME_GUIDANCE = {
    "classic": (
        "Warm, traditional feel: neutral/cream background, a warm accent color "
        "(e.g. deep red, burgundy, or forest green), serif headings (e.g. Georgia, "
        "'Times New Roman'), sans-serif body text, generous but conventional spacing, "
        "rounded-corner cards."
    ),
    "modern": (
        "Minimal and clean: white or very light neutral background, one confident accent "
        "color, sans-serif throughout (e.g. system-ui, Helvetica), generous whitespace, "
        "understated borders/shadows, grid-based layout."
    ),
    "bold": (
        "High-contrast and energetic: dark background with a single vivid accent color "
        "(e.g. orange, electric blue, hot pink), large oversized headings, strong visual "
        "hierarchy, punchy call-to-action styling."
    ),
}

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
CSS_CLASS_RE = re.compile(r"\.(-?[_a-zA-Z][\w-]*)")
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
    "hero", "page-hero", "hero-inner", "hero-title", "hero-subtitle",
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
PAGE_FILES = ("index.html", "about.html", "services.html", "contact.html")
REQUIRED_FILES = (*PAGE_FILES, STYLESHEET_FILE)

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
        "  - An FAQ using `faq-list`, with 4-6 `faq-item` blocks. Write questions a real "
        "customer in this category would ask, and answer them honestly without inventing "
        "prices, times, or guarantees.\n"
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
        # Passed in as data so the footer never reaches for `new Date()` -- a real
        # failure: every generated page carried a `document.write` copyright year, which
        # tripped the sandbox's no_script_tags check and correctly blocked the deploy.
        "current_year": datetime.date.today().year,
    }


def _shared_block(spec: dict) -> str:
    theme = spec.get("theme") or "classic"
    guidance = THEME_GUIDANCE.get(theme, THEME_GUIDANCE["classic"])
    spec_json = json.dumps(spec, indent=2, ensure_ascii=False)
    return Template(_read_prompt("_shared.md")).substitute(
        theme_guidance=guidance, spec_json=spec_json
    )


def _stylesheet_prompt(shared: str) -> str:
    return Template(_read_prompt("stylesheet.md")).substitute(shared=shared)


def _pages_prompt(shared: str, page_names: tuple[str, ...]) -> str:
    output_format = "\n".join(
        f"===FILE: {name}===\n<the complete {name} document>" for name in page_names
    ) + "\n===END==="
    requirements = "\n\n".join(PAGE_REQUIREMENTS[name] for name in page_names)
    return Template(_read_prompt("pages.md")).substitute(
        shared=shared,
        page_names=" and ".join(page_names),
        output_format=output_format,
        page_requirements=requirements,
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


def _strip_broken_links(html: str) -> str:
    """Unwrap anchors whose href is empty or scheme-only, keeping their visible text.

    The prompt forbids these in two separate places and the models still emit them
    whenever a business has no phone or email -- it cost one real deploy tonight
    (`find-dog`, an empty `mailto:` failing contact_links_valid) and another earlier in
    the session. A prompt rule that has now been ignored twice is not a control; doing it
    deterministically here is. Keeps the text so no content is lost, just the dead link.
    """
    return BROKEN_LINK_RE.sub(lambda m: m.group("text"), html)


def _with_base_css(css: str) -> str:
    """Prepend the fallback stylesheet so a contract class can never end up unstyled.

    Model rules come after and win via the normal cascade. Without this, the stylesheet
    call omitting a single contract class failed the entire build -- which really happened
    to a real business (`find-dog`, missing `btn-primary`).
    """
    base = (Path(__file__).parent / "base.css").read_text(encoding="utf-8")
    return f"{base}\n/* ---- generated ---- */\n{css}"


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
    missing_contract = sorted(undefined & CONTRACT_CLASSES)
    if missing_contract:
        # Deliberately NOT a failure any more: base.css guarantees a usable rule for every
        # contract class, so this is cosmetic (the model's own styling for that class is
        # missing, the element still renders fine). Raising here killed a real build.
        logger.warning("stylesheet omitted contract class rules, base.css covering: %s", missing_contract)
    if len(undefined) > MAX_UNSTYLED_CLASSES:
        return (
            f"{len(undefined)} classes have no stylesheet rules, which suggests the pages "
            f"and stylesheet don't match: {sorted(undefined)[:15]}"
        )
    return None


async def _generate(prompt: str, expected: tuple[str, ...]) -> tuple[dict[str, str], dict]:
    content, usage = await call_plain_completion(prompt)
    files = _parse_files(content)
    missing = [name for name in expected if name not in files]
    if missing:
        raise GenerationFailed(
            f"Model response missing required file(s) {missing}: {content[:400]!r}"
        )
    # Truncation guard: the free tier silently cuts long responses off, and Part 3's
    # sandbox checks don't catch malformed HTML (Chromium renders it permissively).
    for name in expected:
        if name.endswith(".html") and not files[name].rstrip().endswith("</html>"):
            raise GenerationFailed(f"Model response looks truncated: {name} lacks a closing </html>")
    return {name: files[name] for name in expected}, usage


def _patch_prompt(filename: str, content: str, instruction: str) -> str:
    return Template(_read_prompt("patch.md")).safe_substitute(
        filename=filename, file_content=content, instruction=instruction
    )


async def _patch_one(filename: str, content: str, instruction: str) -> tuple[str, dict]:
    """Patch a single file, rejecting a response that restructured too much of it."""
    last_error = ""
    for attempt in (1, 2):
        text, usage = await call_plain_completion(_patch_prompt(filename, content, instruction))
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


async def patch_site_files(
    files: dict[str, str],
    instruction: str,
    targets: list[str],
    *,
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

    results = await asyncio.gather(
        *(_patch_one(name, files[name], instruction) for name in wanted)
    )

    patched = dict(files)
    usage = {"model": "", "input_tokens": 0, "output_tokens": 0, "requests": len(wanted)}
    for name, (content, part_usage) in zip(wanted, results):
        patched[name] = _strip_broken_links(content) if name.endswith(".html") else content
        usage["model"] = part_usage["model"]
        usage["input_tokens"] += part_usage["input_tokens"]
        usage["output_tokens"] += part_usage["output_tokens"]

    drift = _style_drift(patched)
    if drift is not None:
        raise GenerationFailed(drift)

    if owner_telegram_id is not None:
        await record_usage(
            session, owner_telegram_id, business_id, usage["model"],
            usage["input_tokens"], usage["output_tokens"],
            kind="edit", requests=usage["requests"],
        )
    return patched, usage


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

    shared = _shared_block(spec)
    jobs = [_generate(_pages_prompt(shared, group), group) for group in PAGE_GROUPS]
    jobs.append(_generate(_stylesheet_prompt(shared), (STYLESHEET_FILE,)))

    try:
        results = await asyncio.gather(*jobs)
    except DailyLimitReached:
        raise  # resolves itself with time -- must not be reported as a generation fault
    except OpenRouterCallFailed as exc:
        raise GenerationFailed(f"Site generation failed: {exc}") from exc

    files: dict[str, str] = {}
    usage = {"model": "", "input_tokens": 0, "output_tokens": 0, "requests": len(jobs)}
    for part_files, part_usage in results:
        files.update(part_files)
        usage["model"] = part_usage["model"]
        usage["input_tokens"] += part_usage["input_tokens"]
        usage["output_tokens"] += part_usage["output_tokens"]

    files[STYLESHEET_FILE] = _with_base_css(files.get(STYLESHEET_FILE, ""))
    files = {
        name: _strip_broken_links(body) if name.endswith(".html") else body
        for name, body in files.items()
    }

    missing = [name for name in REQUIRED_FILES if name not in files]
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
