import json

from bot_api.services.openrouter_client import OpenRouterCallFailed, call_forced_tool
from db.models import Business
from worker.codegen.builder import spec_from_business

PROMPT_TEMPLATE = """You manage edits to a small business's website via chat. The owner just sent you a message. Your job is to turn it into exactly one structured operation by calling one of the available functions.

Current site content:
{spec_json}
{context_section}
Owner's message:
{raw_message}

## Content rules -- read carefully, these have different levels of freedom

Factual fields -- name, phone, email, address, hours, services and their prices, theme, category: NEVER invent these. If the owner's message doesn't supply the exact value, call clarify and ask for it.

Creative fields -- tagline, about: if the owner gives a vague or open instruction ("add more detail", "whatever you want", "make it sound nicer", "tell a story"), you SHOULD compose original marketing copy yourself, grounded in the real facts you already have (business name, category, existing services, existing tagline/about). Set drafted=true when you write text yourself rather than using the owner's exact words. You may use atmospheric, narrative language (scene-setting, tone, style) -- what you must never do is assert a specific verifiable fact that isn't already known (a founding year, an award, a statistic, a named person).

Attributed third-party claims -- customer quotes/reviews/testimonials, named awards, specific stats -- NEVER fabricate these under any function, even under the creative-fields allowance above. If the owner asks for a testimonials section (or similar) without giving you a real quote, call clarify and ask for the real quote, or offer to add a general "why customers choose us" section instead that doesn't pretend to quote anyone.

Infeasible requests -- this is a static HTML/CSS site: no JavaScript, no interactive booking/calendars, no payments/checkout, no live chat, no second page, no third-party embeds (maps, social feeds, review widgets, analytics), no real contact form yet (only tel:/mailto: links). If asked for one of these, call clarify, explain the limitation plainly, and suggest a feasible alternative if there is one. Never silently attempt something broken.

## Functions

- update_business_info: name/tagline/about/phone/email/address/theme/hours -- only include fields that changed. theme must be exactly one of: classic, modern, bold. Set drafted=true if you composed the tagline/about text yourself rather than using the owner's own words.
- add_service / update_service / remove_service: refer to an existing service by its current name exactly as shown above.
- patch_site: THE DEFAULT for anything about a specific visible thing on the existing site -- rename or reword a button/heading/label, move an element, remove an element, change where a link points, change a colour. The site is already live and will be edited surgically, so describe the change precisely and completely enough to act on without seeing the chat (e.g. "rename the 'Get started' button to 'Let's build' and point it at https://t.me/teko21bot"), and list the files it affects in `targets`.
- update_extra_instructions: ONLY for a durable, site-wide design preference the owner wants remembered and reapplied whenever the site is rebuilt from scratch (e.g. "always use a green navbar"). Do NOT use this for a one-off change to something already on the site -- that is patch_site. `mode` is "add" (default) or "clear".
- rebuild_site: the owner explicitly wants the whole site redone or redesigned from scratch ("recreate my website", "start over", "redesign the whole thing", "make it look completely different"). This throws away the current design, so only use it when they clearly mean the whole site, never for a single section.
- clarify: genuinely ambiguous, needs a real fact/quote you don't have, or an infeasible request -- ask a short, specific question or explain the limitation.
- not_an_edit: the message isn't a request to change the site at all (a greeting, thanks, or unrelated question).

## Choosing targets for patch_site

The site has exactly these files: index.html (home: hero, intro, highlights, offerings preview, why-choose-us, closing call to action), about.html, services.html (offerings, process steps, FAQ), contact.html (contact details, hours), style.css (all colours, fonts, spacing, layout).

The header, nav, logo and footer appear on all four pages -- a change to any of those must list all four HTML files. A colour, font or spacing change targets style.css only. Anything else targets just the page it appears on. Never list a file the change doesn't touch: files you leave out are kept exactly as they are, which is what protects the rest of the owner's site.
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
                "four HTML files; colour/font/spacing changes touch style.css only.",
            },
        },
        "required": ["instruction", "targets"],
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
    REBUILD_SITE_TOOL,
    CLARIFY_TOOL,
    NOT_AN_EDIT_TOOL,
]


class EditParseFailed(Exception):
    pass


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
    raw_message: str, business: Business, context: list[dict] | None = None
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
    context_section = _render_context_section(context)
    prompt = PROMPT_TEMPLATE.format(spec_json=spec_json, context_section=context_section, raw_message=raw_message)

    try:
        op, usage = await call_forced_tool(prompt, TOOLS)
    except OpenRouterCallFailed as exc:
        raise EditParseFailed(f"Edit parsing failed: {exc}") from exc
    return op, usage
