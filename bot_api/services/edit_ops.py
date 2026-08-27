import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from bot_api.services.validation import EMAIL_RE, FIELD_LIMITS, THEMES
from db.models import Business, Service

BUSY_STATUSES = {"queued", "generating", "testing", "deploying"}
# Generous on purpose: the slowest legitimate build measured is ~121s, and the old
# architecture took 696s. Anything still "busy" after this long means the worker died
# mid-job, not that the build is slow.
STALE_BUILD_MINUTES = 20


def is_build_stale(business: Business) -> bool:
    if business.generation_status not in BUSY_STATUSES:
        return False
    updated = business.updated_at
    if updated is None:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - updated > timedelta(minutes=STALE_BUILD_MINUTES)


def is_business_busy(business: Business) -> bool:
    """Whether an edit should be rejected because a build is already running.

    A stale busy status counts as NOT busy. Without this, a worker that dies mid-job
    leaves the business frozen in e.g. `generating` forever, and every future edit is
    refused with "try again in a minute or two" that will never come true -- the owner's
    site becomes permanently uneditable. Observed live when a network blip killed the
    worker. The reaper in worker/worker.py cleans these up properly; this is the
    belt-and-braces so an owner is never locked out even if the reaper is down too.
    """
    return business.generation_status in BUSY_STATUSES and not is_build_stale(business)


class ValidationError(Exception):
    pass


PAGE_FILES = ("index.html", "about.html", "services.html", "contact.html")
STYLESHEET_FILE = "style.css"
PATCHABLE_FILES = (*PAGE_FILES, STYLESHEET_FILE)

# Which files display which spec field, so a field edit can be applied surgically instead
# of regenerating the site. Name/logo live in the header and footer of every page.
FIELD_TARGETS: dict[str, tuple[str, ...]] = {
    "name": PAGE_FILES,
    "tagline": ("index.html",),
    "about": ("about.html", "index.html"),
    "phone": ("contact.html",),
    "email": ("contact.html",),
    "address": ("contact.html",),
    "hours": ("contact.html",),
    "theme": (STYLESHEET_FILE,),
    "services": ("services.html", "index.html"),
}


# Asking a patch to add or delete a page is a contradiction: patching means "return this
# file with one change applied", so it can never remove the file it was handed. One real
# request -- "Keep only one page and remove all" -- was accepted anyway and burned 21,867
# tokens across three calls to return the same three pages very slightly reworded.
#
# Both halves of this were once looser than that, and both cost a real owner an edit:
#
# "page" on its own matched "remove the Services link from the page" -- a nav link, on the
# one page there is -- so an ordinary edit was turned away as if it restructured the site.
# A page is only named structurally as one of the site's actual pages, or in the plural.
#
# A bare "landing page" was worse: it matched the owner's *answer* to the question this
# guard asks. Told "landing page or four-page site?", they replied "Single landing page",
# that matched, and the same question came back -- three times, and it could never have
# stopped. A layout name only means restructuring when something asks for the change.
_PAGE_NOUN = r"(?:pages|(?:about|services|contact|home|index)\s+page|\w+\.html)"
_LAYOUT_NOUN = r"(?:single[- ]page|one[- ]page|landing page|four[- ]page|multi[- ]?page)"
STRUCTURAL_REQUEST_RE = re.compile(
    rf"\b(remove|delete|drop|get rid of)\b[^.]{{0,40}}\b{_PAGE_NOUN}\b"
    r"|\b(add|create|make)\b[^.]{0,30}\b(new page|another page|extra page)\b"
    r"|\bkeep only one page\b"
    rf"|\b(make|turn|convert|change|rebuild|split)\b[^.]{{0,30}}\b{_LAYOUT_NOUN}\b",
    re.IGNORECASE,
)

# The two ways an owner answers "landing page or four-page site?". Deliberately narrow:
# this only has to read a reply to that question, and anything it does not recognise falls
# through to being read as an ordinary message rather than guessed at.
_LANDING_ANSWER_RE = re.compile(
    r"\b(single|one)\b[^.]{0,20}\b(page|landing)\b|\blanding page\b|\bone[- ]pager\b",
    re.IGNORECASE,
)
_MULTIPAGE_ANSWER_RE = re.compile(
    r"\bfour[- ]?page\b|\b4[- ]?page\b|\bmulti[- ]?page\b|\bseparate pages\b",
    re.IGNORECASE,
)


def is_structural_request(instruction: str) -> bool:
    """True if this asks to change which pages exist, which patching cannot do."""
    return bool(STRUCTURAL_REQUEST_RE.search(instruction or ""))


def layout_answer(text: str) -> str | None:
    """Which layout this message asks for, or None if it is not an answer at all.

    Checked before the multipage pattern would ever see "single landing page", since the
    two vocabularies overlap ("one page" vs "four page") and the landing wording is the
    one an owner actually types.
    """
    if _LANDING_ANSWER_RE.search(text or ""):
        return "landing"
    if _MULTIPAGE_ANSWER_RE.search(text or ""):
        return "multipage"
    return None


# A picture is an element on a page, never a rule in the stylesheet. A real edit asking to
# put a photo behind the hero text was sent to style.css alone, twice, and changed nothing
# both times -- the <img> it needed to move was in index.html, which was never opened.
PICTURE_WORDS = ("image", "photo", "picture", "background", "banner", "logo")

_PICTURE_NOUN = r"(?:images?|photos?|photographs?|pictures?|pics?|logos?|banners?)"
# Asking for a picture the site does not have yet. Deliberately narrower than "mentions a
# picture": making one smaller, moving one, or deleting one are all about a picture that
# already exists, and offering to go and find one in reply to those would be answering a
# question nobody asked.
_WANTS_PICTURE_RE = re.compile(
    # The trailing `s?` throughout is not decoration: "the site needs photos" is how an
    # owner phrases this, and `\bneed\b` does not match "needs".
    rf"\b(?:adds?|puts?|inserts?|places?|includes?|shows?|needs?|wants?|uploads?|uses?|"
    rf"changes?|replaces?|swaps?|updates?|new|another|different|more|missing)"
    rf"\b[^.?!]{{0,40}}\b{_PICTURE_NOUN}\b"
    rf"|\b{_PICTURE_NOUN}\b[^.?!]{{0,30}}\b(?:missing|empty|blank|needed|required)\b"
    rf"|\bno\s+{_PICTURE_NOUN}\b",
    re.IGNORECASE,
)
# These are about a picture that is already on the page.
_EXISTING_PICTURE_RE = re.compile(
    rf"\b(?:remove|delete|drop|get rid of|hide|resize|shrink|enlarge|crop|move|"
    rf"bigger|smaller|larger|wider|taller|rounder|round(?:ed)?)\b[^.?!]{{0,40}}\b{_PICTURE_NOUN}\b"
    rf"|\b{_PICTURE_NOUN}\b[^.?!]{{0,30}}\b(?:bigger|smaller|larger|wider|taller|"
    rf"too big|too small)\b",
    re.IGNORECASE,
)


def wants_a_picture(text: str) -> bool:
    """Is this a request for a picture the site does not have yet?

    Worth telling apart because it is the one request the bot cannot simply carry out. It
    needs a photograph from somewhere, and there are two somewheres -- the owner's phone,
    or a stock library -- and owners do not know the second one exists. Asked plainly, a
    real owner wrote "there are no images whole website is empty" and then waited, because
    nothing had ever told them they could send one.
    """
    body = text or ""
    if _EXISTING_PICTURE_RE.search(body):
        return False
    return bool(_WANTS_PICTURE_RE.search(body))


_OWN_PICTURE_RE = re.compile(
    # `i'?ll` deliberately also matches the apostrophe-less "ill", which is how people
    # actually type it in a chat window.
    r"\b(?:i'?ll|i will|i'?m|i am|i)\b[^.?!]{0,15}"
    r"\b(?:send|sending|upload|uploading|attach|attaching|share|give|have|got|post)\b"
    r"|\b(?:sending|uploading|attaching) (?:it|one|them|a )\b"
    r"|\bmine\b|\bmy own\b|\bown (?:photos?|pictures?|images?)\b"
    r"|\bwait\b|\bhold on\b|\bone (?:sec|second|moment|minute)\b",
    re.IGNORECASE,
)
_FIND_PICTURE_RE = re.compile(
    r"\byou\b[^.?!]{0,20}\b(?:find|choose|pick|add|get|select|source|decide)\b"
    r"|\b(?:find|choose|pick|get|source)\b[^.?!]{0,20}\b(?:one|it|some|a picture|a photo)\b"
    r"|\bstock\b|\bany(?:thing)? (?:is )?fine\b|\bwhatever\b|\bup to you\b|\byour choice\b"
    r"|\bdon'?t have (?:one|any|a photo|a picture)\b|\bno (?:photos?|pictures?|images?)\b",
    re.IGNORECASE,
)


def picture_source_answer(text: str) -> str | None:
    """"own" if they are sending one, "find" if we should, None if neither.

    `find` is checked first: "I don't have one, you pick" says both things, and the half
    that decides what happens next is the second one.
    """
    body = text or ""
    if _FIND_PICTURE_RE.search(body):
        return "find"
    if _OWN_PICTURE_RE.search(body):
        return "own"
    return None


def widen_targets_for_pictures(instruction: str, targets: list[str]) -> list[str]:
    """Add a page file when a picture change was aimed at the stylesheet alone.

    Deterministic on purpose. The tool description says this too, but a rule that lives
    only in a prompt is a hope -- this one had already been ignored in production.
    """
    if not targets or any(t.endswith(".html") for t in targets):
        return targets
    if not any(word in instruction.lower() for word in PICTURE_WORDS):
        return targets
    return targets + ["index.html"]


def normalize_patch_targets(
    targets: list[str] | None, available: list[str] | None = None
) -> list[str]:
    """Keep only filenames that actually exist on THIS site.

    `available` matters for one-page sites: they have only index.html and style.css, so a
    request about "services" naturally names services.html, which isn't there. That used to
    reach the worker and fail the whole build with "Nothing to patch". Anything naming a
    page this site doesn't have is redirected to the page that does exist, because on a
    landing site those sections all live in index.html.
    """
    known = [n for n in PATCHABLE_FILES if available is None or n in available]
    requested = {str(t).strip().lstrip("./").lower() for t in (targets or [])}
    resolved = [name for name in known if name.lower() in requested]
    if resolved:
        return resolved

    # Nothing matched. If they named any HTML page, they meant page content -- send it to
    # the pages this site really has rather than failing.
    if any(r.endswith(".html") for r in requested):
        return [n for n in known if n.endswith(".html")]
    return [n for n in known if n.lower() in requested]


def patch_for_field_edit(op: dict, available: list[str] | None = None) -> dict | None:
    """Turn a spec-field edit into a surgical patch instruction for the live files.

    The spec stays the source of truth for a future rebuild; this keeps the deployed
    HTML in step without handing the model licence to redesign the site.
    """
    parts: list[str] = []
    targets: list[str] = []
    for field, files in FIELD_TARGETS.items():
        if field in op and field != "services":
            parts.append(f"the {field} is now: {str(op[field]).strip()}")
            targets += files
    if not parts:
        return None
    # Filtered against the site's real files: a one-page site has no contact.html, so a
    # phone change must land on index.html instead of a file that isn't there.
    ordered = normalize_patch_targets(sorted(set(targets)), available)
    if not ordered:
        return None
    return {
        "instruction": (
            "Update the site's existing content to reflect these new details, changing only "
            "the places they already appear: " + "; ".join(parts) + "."
        ),
        "targets": ordered,
    }


def patch_for_extra_instructions(instructions: str) -> dict:
    """Apply a saved design preference to the stylesheet instead of rebuilding the site.

    A durable preference is, by definition, about how the site looks -- so it belongs in
    style.css. Routing it to a full rebuild is what made "Layout, it should be consistent
    in all pages" cost ~24,000 tokens and return a site with different colours, which is
    the complaint this whole patching architecture exists to prevent. A stylesheet patch
    costs roughly a seventh of that and cannot touch the page content at all.
    """
    return {
        "instruction": (
            f"Apply this styling preference to the stylesheet: {instructions.strip()}. "
            "Change only the rules needed for it; leave every other rule exactly as it is."
        ),
        "targets": [STYLESHEET_FILE],
    }


def patch_for_service_edit(summary: str, available: list[str] | None = None) -> dict | None:
    """Service list changed -- refresh only the places services are listed."""
    targets = normalize_patch_targets(list(FIELD_TARGETS["services"]), available)
    if not targets:
        return None
    return {
        "instruction": (
            f"The business's service list changed ({summary}). Update the existing services "
            "content to match, changing only the service entries themselves."
        ),
        "targets": targets,
    }


async def apply_edit_operation(session: AsyncSession, business: Business, op: dict) -> str:
    """Apply a validated operation to `business`/its services on `session`.

    Mutates in place; caller commits. Returns a short human-readable summary for
    the confirmation reply. Raises ValidationError (with a user-facing message)
    on any invalid input -- never silently clamps or guesses.
    """
    operation = op["operation"]
    if operation == "update_business_info":
        return _apply_update_business_info(business, op)
    if operation == "add_service":
        return _apply_add_service(session, business, op)
    if operation == "update_service":
        return _apply_update_service(business, op)
    if operation == "remove_service":
        return _apply_remove_service(business, op)
    if operation == "update_extra_instructions":
        return _apply_update_extra_instructions(business, op)
    raise ValueError(f"apply_edit_operation called with a non-mutating operation: {operation}")


def _check_length(field_label: str, value: str, limit_key: str) -> None:
    limit = FIELD_LIMITS[limit_key]
    if len(value) > limit:
        raise ValidationError(
            f"That {field_label} is {len(value)} characters — I can only fit {limit}. Try something shorter?"
        )


def _apply_update_business_info(business: Business, op: dict) -> str:
    changed: list[str] = []

    if "name" in op:
        value = op["name"].strip()
        _check_length("name", value, "name")
        business.name = value
        changed.append("name")
    if "tagline" in op:
        value = op["tagline"].strip()
        _check_length("tagline", value, "tagline")
        business.tagline = value
        changed.append("tagline")
    if "about" in op:
        value = op["about"].strip()
        _check_length("about description", value, "about")
        business.about = value
        changed.append("about section")
    if "phone" in op:
        value = op["phone"].strip()
        _check_length("phone number", value, "phone")
        business.phone = value
        changed.append("phone number")
    if "email" in op:
        value = op["email"].strip()
        if not EMAIL_RE.match(value):
            raise ValidationError(f"\"{value}\" doesn't look like a valid email — what should it be?")
        _check_length("email", value, "email")
        business.email = value
        changed.append("email")
    if "address" in op:
        value = op["address"].strip()
        _check_length("address", value, "address")
        business.address = value
        changed.append("address")
    if "theme" in op:
        value = op["theme"].strip().lower()
        if value not in THEMES:
            raise ValidationError("I can only use classic, modern, or bold as a look — which one?")
        business.theme = value
        changed.append("look")
    if "hours" in op:
        value = op["hours"].strip()
        _check_length("hours", value, "hours")
        business.hours = {"display_text": value}
        changed.append("hours")

    if not changed:
        raise ValidationError("I didn't catch a specific change to make — could you tell me again?")

    return "changed your " + ", ".join(changed)


def _find_service(business: Business, name: str) -> Service:
    active = [s for s in business.services if s.is_active]
    needle = name.strip().lower()

    exact = [s for s in active if s.name.strip().lower() == needle]
    if len(exact) == 1:
        return exact[0]

    substring = [s for s in active if needle in s.name.strip().lower()]
    matches = exact or substring
    if len(matches) == 1:
        return matches[0]

    names = ", ".join(f'"{s.name}"' for s in active) or "(none)"
    if not matches:
        raise ValidationError(f'I couldn\'t find a service called "{name}". Your current services: {names}.')
    raise ValidationError(f'"{name}" matches more than one service ({names}) — which one did you mean?')


def _apply_add_service(session: AsyncSession, business: Business, op: dict) -> str:
    name = op["name"].strip()
    _check_length("service name", name, "service_name")
    price_label = op.get("price_label")
    if price_label:
        price_label = price_label.strip()
        _check_length("price", price_label, "service_price_label")

    existing = next(
        (s for s in business.services if s.is_active and s.name.strip().lower() == name.lower()), None
    )
    if existing is not None:
        existing.price_label = price_label or existing.price_label
        return f'updated "{existing.name}"' + (f" ({price_label})" if price_label else "")

    next_order = max((s.sort_order for s in business.services), default=-1) + 1
    session.add(
        Service(
            business_id=business.id,
            name=name,
            price_label=price_label,
            sort_order=next_order,
        )
    )
    return f'added "{name}"' + (f" ({price_label})" if price_label else "")


def _apply_update_service(business: Business, op: dict) -> str:
    service = _find_service(business, op["current_name"])
    changed: list[str] = []

    new_name = op.get("new_name")
    if new_name:
        new_name = new_name.strip()
        _check_length("service name", new_name, "service_name")
        service.name = new_name
        changed.append("name")

    new_price_label = op.get("new_price_label")
    if new_price_label:
        new_price_label = new_price_label.strip()
        _check_length("price", new_price_label, "service_price_label")
        service.price_label = new_price_label
        changed.append("price")

    if not changed:
        raise ValidationError("What should I change about that service — its name or its price?")

    return f'updated "{service.name}"\'s ' + " and ".join(changed)


def _apply_remove_service(business: Business, op: dict) -> str:
    service = _find_service(business, op["name"])
    service.is_active = False
    return f'removed "{service.name}"'


def _apply_update_extra_instructions(business: Business, op: dict) -> str:
    mode = op.get("mode", "add")

    if mode == "clear":
        business.extra_instructions = None
        return "removed your custom site instructions"

    instructions = (op.get("instructions") or "").strip()
    if not instructions:
        raise ValidationError("What would you like me to add?")

    # Always additive. 'replace' used to be honoured here and the model reached for it
    # constantly -- four consecutive real edits each silently deleted every preference the
    # owner had set before. Wiping preferences now requires an explicit mode="clear".
    if not business.extra_instructions:
        new_value = instructions
    elif instructions in business.extra_instructions:
        return "that preference was already saved"
    else:
        new_value = business.extra_instructions + "\n" + instructions

    _check_length("custom instructions", new_value, "extra_instructions")
    business.extra_instructions = new_value
    return "updated your site's custom instructions"
