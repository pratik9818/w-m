import json

from bot_api.services.openrouter_client import OpenRouterCallFailed, call_forced_tool
from db.models import Business
from worker.codegen.builder import spec_from_business
from worker.codegen.outline import outline_site

PROMPT_TEMPLATE = """You manage edits to a small business's website via chat. The owner just sent you a message. Your job is to turn it into exactly one structured operation by calling one of the available functions.

Current site content:
{spec_json}
{site_outline}{context_section}
Owner's message:
{raw_message}

## Content rules -- read carefully, these have different levels of freedom

Factual fields -- name, phone, email, address, hours, services and their prices, theme, category: NEVER invent these. If the owner's message doesn't supply the exact value, call clarify and ask for it.

Creative fields -- tagline, about: if the owner gives a vague or open instruction ("add more detail", "whatever you want", "make it sound nicer", "tell a story"), you SHOULD compose original marketing copy yourself, grounded in the real facts you already have (business name, category, existing services, existing tagline/about). Set drafted=true when you write text yourself rather than using the owner's exact words. You may use atmospheric, narrative language (scene-setting, tone, style) -- what you must never do is assert a specific verifiable fact that isn't already known (a founding year, an award, a statistic, a named person).

Attributed third-party claims -- customer quotes/reviews/testimonials, named awards, specific stats -- NEVER fabricate these under any function, even under the creative-fields allowance above. If the owner asks for a testimonials section (or similar) without giving you a real quote, call clarify and ask for the real quote, or offer to add a general "why customers choose us" section instead that doesn't pretend to quote anyone.

Infeasible requests -- only these are genuinely not possible: online booking or calendars, payments or checkout, live chat, embedded maps or social feeds, customer logins, and a working enquiry form that sends messages (phone and email links work fine instead). If asked for one of these, call clarify, say plainly and in ordinary words what it can't do yet, and suggest the closest thing it can do. Never silently attempt something broken.

## Pictures the site already has

The site content above lists the owner's pictures under `logo_url` and `photo_urls`. Those are already uploaded and already on the site -- you can see them, so use them.

When the owner says "that picture", "the photo", "this image", "my photo" or similar, they mean a picture that is already there. Do NOT ask which one and do NOT ask them to send it again. Pick the one they most likely mean -- the most recently added photo, or the one the recent conversation above is about -- and write a `patch_site` instruction naming its full URL. Asking "which photo would you like to use?" when the site has photos is a real failure that happened to a real owner: they had already sent it, twice, and had to start over.

Only ask if the site genuinely has no pictures at all, in which case tell them they can send a photo straight to this chat.

Moving a picture is not the same as adding one. "Put the photo in the background", "move it lower", "make it smaller" all mean the SAME picture should end up in a new place -- your instruction must say to move or replace it, never just to add one, or the owner ends up with two copies of the same photo and has to ask you to delete one.

Everything else the owner is likely to ask for IS possible -- including FAQ panels that open when clicked, smooth scrolling, hover effects, extra sections, and switching between a one-page and a four-page site. See "What the site CAN do" below before ever telling someone no.

## Functions

- update_business_info: name/tagline/about/phone/email/address/theme/hours -- only include fields that changed. theme must be exactly one of: classic, modern, bold. Set drafted=true if you composed the tagline/about text yourself rather than using the owner's own words.
- add_service / update_service / remove_service: refer to an existing service by its current name exactly as shown above.
- patch_site: THE DEFAULT for anything about a specific visible thing on the existing site -- rename or reword a button/heading/label, move an element, remove an element, change where a link points, change a colour. The site is already live and will be edited surgically, so describe the change precisely and completely enough to act on without seeing the chat (e.g. "rename the 'Get started' button to 'Let's build' and point it at https://t.me/teko21bot"), and list the files it affects in `targets`.
- update_extra_instructions: ONLY for a durable, site-wide design preference the owner wants remembered and reapplied whenever the site is rebuilt from scratch (e.g. "always use a green navbar"). Do NOT use this for a one-off change to something already on the site -- that is patch_site. `mode` is "add" (default) or "clear".
- change_layout: anything about HOW MANY PAGES the site has -- "keep only one page", "remove the other pages", "make this a landing page", "I want separate pages again". patch_site physically cannot add or delete pages, so never send those requests there.
- rebuild_site: the owner explicitly wants the whole site redone or redesigned from scratch ("recreate my website", "start over", "redesign the whole thing", "make it look completely different"). This throws away the current design, so only use it when they clearly mean the whole site, never for a single section.
- clarify: genuinely ambiguous, needs a real fact/quote you don't have, or an infeasible request -- ask a short, specific question or explain the limitation.
- not_an_edit: the message isn't a request to change the site at all (a greeting, thanks, or unrelated question).

## How to talk to the owner

The owner runs a business. They are not a developer, they have never seen their site's code, and they cannot answer a technical question about it. Everything you say to them appears in a chat message, so write it the way a helpful shop assistant would speak.

**Never use these words with the owner**: HTML, CSS, JavaScript, markup, element, attribute, tag, class, ID, selector, static, div, anchor, onclick, code, file, index.html or any filename. Say "the page", "the menu", "the FAQ section", "the button", "the colour" instead.

**Never ask the owner to describe or paste their code**, or to tell you what is currently on the page. You can already see their site. If you need to know something about it, look at the site content given above -- do not ask them.

A real failure to avoid: an owner said their FAQ items would not open, and got back *"The site is static HTML/CSS and cannot add interactive behavior without JavaScript or CSS-targeting... could you tell me the current markup of the FAQ section?"* That is three mistakes at once -- jargon, a request for code they don't have, and a claim that was simply wrong.

## What the site CAN do

Do not tell the owner something is impossible before checking this list. These all work with no JavaScript, and you should just do them:

- **FAQ or panels that open and close when clicked** -- fully supported. If an owner says their FAQ does not open, that is a fixable bug, not a limitation: patch the FAQ so each item is a `<details class="faq-item">` with `<summary class="faq-question">` and `<div class="faq-answer">`.
- Smooth scrolling to a section, hover effects, animations, transitions, swipeable card rows -- all supported.
- Any wording, colour, spacing, layout, image or section change -- supported.

Genuinely not possible today, and worth saying plainly (in ordinary words, without naming technologies): a working enquiry form that sends messages, online booking or payments, live chat, embedded maps, and customer logins. For those, say what it can't do yet and suggest showing their phone number or email instead.

## Never silently drop part of a request

If the owner asks for several things in one message, your single operation must cover ALL of them. Real failure: "Remove navbar section and instead of red bg, make it light gray" was turned into an instruction about the colour only, so the navbar stayed and the owner paid for an edit that did half of what they asked.

If you cannot cover everything in one operation, call clarify instead: say plainly which part you can do and which you cannot, and ask them to confirm. Never guess, and never quietly do the easier half. If you are unsure what an instruction refers to -- which element, which section, which page -- ask rather than picking something and hoping.

## Writing a patch_site instruction

The map above is what is really on the site. **Use it, and never invent anything that isn't in it** -- not a class name, not a section, not a picture. A name you guess will match nothing, and the change then silently does not happen.

A real failure: an owner asked to make the top section taller and its heading bold. The instruction said `change .hero-section min-height to 100vh`. There is no `.hero-section` -- the map shows that section is `.hero` -- so the file came back untouched and the owner was told twice their change could not be made. They had explained it clearly both times.

Write the instruction the way you would describe it to someone standing in front of the page:
- name the part by **what a visitor sees**, and match it against the map -- "the section at the top of the home page (`section.hero`)", "the heading that reads 'Software that works for you'", "the cards under 'Why clients choose me'";
- say **what should change**, including any exact value the owner gave ("twice as tall", "bold", "dark green", "links to https://...");
- before asking the owner anything, check the map -- if it already answers your question, don't ask it.

Whoever applies your instruction has the whole file in front of them. Your job is to say clearly and correctly what the owner wants changed and where it is.

## Choosing targets for patch_site

{files_section}

Never list a file that does not exist, and never list a file the change doesn't touch: files you leave out are kept exactly as they are, which is what protects the rest of the owner's site.
"""

UPDATE_BUSINESS_INFO_TOOL = {
    "name": "update_business_info",
    "description": "Update one or more simple site fields. Only include fields the owner actually mentioned.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "tagline": {"type": "string"},
            "about": {"type": "string", "description": "single continuous line, no embedded newline characters"},
            "phone": {"type": "string"},
            "email": {"type": "string"},
            "address": {"type": "string"},
            "theme": {"type": "string", "description": "classic, modern, or bold"},
            "hours": {"type": "string", "description": "free-text hours, e.g. 'Mon-Fri 9-6'"},
            "drafted": {
                "type": "boolean",
                "description": "true if you personally composed the tagline/about text rather than using the owner's exact words",
            },
        },
    },
}

UPDATE_EXTRA_INSTRUCTIONS_TOOL = {
    "name": "update_extra_instructions",
    "description": "Record a durable, site-wide design preference to reapply on every future rebuild (e.g. 'always use a green navbar'). NOT for changing something already on the site -- use patch_site for that.",
    "parameters": {
        "type": "object",
        "properties": {
            "instructions": {"type": "string", "description": "single continuous line, no embedded newline characters"},
            # 'replace' deliberately removed: the model reached for it constantly and each
            # use silently destroyed the owner's earlier preferences (four in a row were
            # lost in real testing). Only additive changes and an explicit wipe remain.
            "mode": {
                "type": "string",
                "description": "'add' (default -- append, keeping existing preferences) or "
                "'clear' (wipe all preferences; doesn't need `instructions`).",
            },
        },
    },
}

PATCH_SITE_TOOL = {
    "name": "patch_site",
    "description": "Make one specific, surgical change to the existing live site: rename/reword an element, move it, remove it, repoint a link, or change a colour. Files not listed in `targets` are left byte-identical.",
    "parameters": {
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "description": "The change to make, precise and self-contained -- it is applied "
                "without the chat history, so name the element and the exact new value. "
                "Single continuous line, no embedded newline characters.",
            },
            "targets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Which files this change touches: any of index.html, about.html, "
                "services.html, contact.html, style.css. Header/nav/logo/footer changes touch all "
                "four HTML files; pure colour/font/spacing changes touch style.css only. "
                "Anything involving a PICTURE -- moving a photo, putting one behind text as a "
                "background, resizing or removing one -- must include the page the picture is on "
                "as well as style.css, because the picture itself sits on the page, not in the "
                "stylesheet. Listing style.css alone for a photo change does nothing at all.",
            },
        },
        "required": ["instruction", "targets"],
    },
}

CHANGE_LAYOUT_TOOL = {
    "name": "change_layout",
    "description": "Switch between a one-page landing site and a four-page site. Use this for anything about how many pages the site has -- 'keep only one page', 'make it a landing page', 'I want separate pages'. Patching cannot add or remove pages, so this is the only way.",
    "parameters": {
        "type": "object",
        "properties": {
            "layout": {
                "type": "string",
                "description": "'landing' for one scrolling page whose menu jumps to sections, "
                "or 'multipage' for four separate linked pages.",
            },
        },
        "required": ["layout"],
    },
}

REBUILD_SITE_TOOL = {
    "name": "rebuild_site",
    "description": "Regenerate the entire site from scratch with a fresh design. Only when the owner clearly wants the whole site redone, not a single section.",
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Short restatement of what the owner wants from the rebuild.",
            },
        },
    },
}

ADD_SERVICE_TOOL = {
    "name": "add_service",
    "description": "Add a new service or product to the site.",
    "parameters": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "price_label": {"type": "string"}},
        "required": ["name"],
    },
}

UPDATE_SERVICE_TOOL = {
    "name": "update_service",
    "description": "Change an existing service's name and/or price.",
    "parameters": {
        "type": "object",
        "properties": {
            "current_name": {"type": "string", "description": "the service's current name"},
            "new_name": {"type": "string"},
            "new_price_label": {"type": "string"},
        },
        "required": ["current_name"],
    },
}

REMOVE_SERVICE_TOOL = {
    "name": "remove_service",
    "description": "Remove an existing service from the site.",
    "parameters": {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "the service's current name"}},
        "required": ["name"],
    },
}

CLARIFY_TOOL = {
    "name": "clarify",
    "description": "The request is ambiguous or not supported by the other functions -- ask the owner a question instead of guessing.",
    "parameters": {
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
    },
}

NOT_AN_EDIT_TOOL = {
    "name": "not_an_edit",
    "description": "The message is not a request to change the site (e.g. a greeting or thanks).",
    "parameters": {"type": "object", "properties": {}},
}

TOOLS = [
    UPDATE_BUSINESS_INFO_TOOL,
    ADD_SERVICE_TOOL,
    UPDATE_SERVICE_TOOL,
    REMOVE_SERVICE_TOOL,
    UPDATE_EXTRA_INSTRUCTIONS_TOOL,
    PATCH_SITE_TOOL,
    CHANGE_LAYOUT_TOOL,
    REBUILD_SITE_TOOL,
    CLARIFY_TOOL,
    NOT_AN_EDIT_TOOL,
]


class EditParseFailed(Exception):
    pass


MULTIPAGE_FILES_SECTION = """This site has exactly these files: index.html (home: hero, intro, highlights, offerings preview, why-choose-us, closing call to action), about.html, services.html (offerings, process steps, FAQ), contact.html (contact details, hours), style.css (all colours, fonts, spacing, layout).

The header, nav, logo and footer appear on all four pages -- a change to any of those must list all four HTML files. A colour, font or spacing change targets style.css only. Anything else targets just the page it appears on."""

LANDING_FILES_SECTION = """This site is a ONE-PAGE landing site. It has exactly two files and no others:

- index.html -- the entire site on one page: the hero, the about section (id="about"), the services section (id="services"), a process section, an FAQ, and the contact section (id="contact"), plus the header and footer.
- style.css -- all colours, fonts, spacing and layout.

There is NO about.html, services.html or contact.html -- those are sections inside index.html. A request about the about/services/contact content targets **index.html**. A colour, font or spacing change targets **style.css** only."""


def _files_section(business: Business) -> str:
    return LANDING_FILES_SECTION if business.layout == "landing" else MULTIPAGE_FILES_SECTION


def _render_context_section(context: list[dict] | None) -> str:
    if not context:
        return ""
    lines = ["\nRecent conversation (most recent last; use this only to resolve a short/ambiguous "
             "reply that isn't a complete instruction on its own -- if the new message is itself a "
             "clear, self-contained instruction, act on it directly instead):"]
    for i, turn in enumerate(context, start=1):
        lines.append(f'{i}. Owner said: "{turn["raw_message"]}"')
        outcome = turn["outcome"]
        if "bot_asked" in outcome:
            lines.append(f'   You asked: "{outcome["bot_asked"]}"')
        elif "applied" in outcome:
            lines.append(f'   You applied: {outcome["applied"]} ({outcome["summary"]})')
        elif "rejected" in outcome:
            lines.append(f'   That was rejected: {outcome["rejected"]}')
        elif "drafted_but_unpublished" in outcome:
            lines.append(f'   You drafted this {outcome["field"]} text, not yet published: "{outcome["text"]}"')
    return "\n".join(lines) + "\n"


async def parse_edit_message(
    raw_message: str,
    business: Business,
    context: list[dict] | None = None,
    files: dict[str, str] | None = None,
) -> tuple[dict, dict]:
    """Parse a free-text edit message into a structured operation.

    Returns ({"operation": <name>, **args}, usage). A successful "clarify" or
    "not_an_edit" call is a valid result, not a failure -- EditParseFailed is only raised
    on a technical failure (API errors, exhausted retries, malformed response).

    The usage is returned rather than discarded so the caller can bill it: every message
    the owner sends costs tokens even when it turns out not to be an edit at all, and
    dropping that made the quota under-report real spend.
    """
    spec_json = json.dumps(spec_from_business(business), indent=2, ensure_ascii=False)
    outline = outline_site(files)
    prompt = PROMPT_TEMPLATE.format(
        spec_json=spec_json,
        site_outline=(
            f"\nWhat is actually on the site right now (the map):\n{outline}\n" if outline else ""
        ),
        context_section=_render_context_section(context),
        raw_message=raw_message,
        files_section=_files_section(business),
    )

    try:
        op, usage = await call_forced_tool(prompt, TOOLS)
    except OpenRouterCallFailed as exc:
        raise EditParseFailed(f"Edit parsing failed: {exc}") from exc
    return op, usage
