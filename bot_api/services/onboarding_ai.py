"""Turn one free-text description into a complete site spec.

Replaces a ~12-question interrogation. That flow was accurate but exhausting -- an owner
literally typed "So not include this" into the hours field just to escape a question, and
that answer then got rendered on their live site as opening hours.

Same content rules as everywhere else: descriptive copy may be composed, but a phone
number, price, address or opening time is only ever recorded if the owner actually said it.
"""
import json

from bot_api.services.llm_client import (
    DailyLimitReached,
    LLMCallFailed,
    call_forced_tool,
)

PROMPT_TEMPLATE = """A business owner wants a website. They described it in their own words below. Turn that into a structured site brief by calling exactly one of the available functions.

{context_section}{current_section}Owner's message:
{raw_message}

## Rules

Never invent a phone number, email address, postal address, price, or opening hours. Record those ONLY if the owner actually stated them. Leave them out otherwise -- a made-up contact detail on a live business site is the worst possible failure here.

You SHOULD write the tagline and the about text yourself, grounded in what they told you. That is your job as their copywriter -- do not ask them to write it.

Pick a sensible `category` from what they describe (e.g. "Restaurant / Cafe", "Beauty", "Tech", "Professional Services", "Retail", "Fitness"). Never ask for it.

Pick a `theme` that fits their business and tone: classic (warm, traditional, serif), modern (minimal, clean, lots of whitespace), bold (dark, high contrast, vivid accent). Never ask for it.

Pick a `layout`: "landing" if they asked for a landing page, a one-page site, or anything single-scroll; "multipage" otherwise. Default to "multipage" when they didn't say.

Record `services` only if they named specific things they sell or offer. Include a price label only where they gave one.

## When to ask instead

Call need_more_info ONLY when you genuinely cannot tell what the business is or what it should be called -- for example "make me a website" or "build something nice" with nothing else. Ask one short, specific question. Do not ask about design, colours, layout, categories, taglines or wording: decide those yourself.

If you can identify the business and a usable name, always call create_site, even if lots of details are missing. Missing detail is normal and fine; the owner can refine it afterwards by messaging you."""

CREATE_SITE_TOOL = {
    "name": "create_site",
    "description": "Build the website from the owner's description. Use this whenever you can identify what the business is and what to call it.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The business name"},
            "category": {"type": "string", "description": "Short category you chose yourself"},
            "tagline": {"type": "string", "description": "One short line you write for them"},
            "about": {"type": "string", "description": "A couple of sentences you write for them, single continuous line"},
            "services": {
                "type": "array",
                "description": "Only things they actually named. Omit entirely if they named none.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "price_label": {"type": "string", "description": "Only if they gave a price"},
                    },
                    "required": ["name"],
                },
            },
            "phone": {"type": "string", "description": "ONLY if stated by the owner"},
            "email": {"type": "string", "description": "ONLY if stated by the owner"},
            "address": {"type": "string", "description": "ONLY if stated by the owner"},
            "hours": {"type": "string", "description": "ONLY if stated by the owner"},
            "theme": {"type": "string", "description": "classic, modern, or bold"},
            "layout": {"type": "string", "description": "landing or multipage"},
        },
        "required": ["name", "category"],
    },
}

NEED_MORE_INFO_TOOL = {
    "name": "need_more_info",
    "description": "Only when you cannot tell what the business is or what to call it.",
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "One short, specific question"},
        },
        "required": ["question"],
    },
}

TOOLS = [CREATE_SITE_TOOL, NEED_MORE_INFO_TOOL]


class BriefParseFailed(Exception):
    pass


def _render_context(previous: list[str] | None) -> str:
    if not previous:
        return ""
    lines = "\n".join(f"- {m}" for m in previous)
    return (
        "Earlier messages from the same owner about this same site "
        "(treat all of it together as one brief):\n" + lines + "\n\n"
    )


# Fields the owner has already read back and approved by not objecting to them. A
# correction about one thing must not quietly reword the rest.
_STABLE_FIELDS = ("name", "category", "tagline", "about", "theme", "layout")


def _render_current(current: dict | None) -> str:
    """The brief already shown to the owner, when this message is a correction to it.

    Without this the correction path was a *fresh parse*: the owner replied "add price
    section" to the read-back summary, the whole brief was regenerated from history, and
    they got back a different tagline, a different about and a different category with no
    explanation. They had asked for one addition and received a new description of their
    business -- which reads exactly like the bot having forgotten the conversation.
    """
    if not current:
        return ""
    shown = {k: v for k, v in current.items() if k != "operation" and v}
    return (
        "## The brief you already gave them\n\n"
        "You produced this and they have read it. Their message below is a CORRECTION to "
        "it, not a new description:\n\n"
        f"```json\n{json.dumps(shown, indent=2, ensure_ascii=False)}\n```\n\n"
        "Call create_site again with this same brief, changing only what their message "
        f"actually asks you to change. Do not reword {', '.join(_STABLE_FIELDS)} unless "
        "the correction is about that field -- copy those through exactly as they are "
        "above. Keep every service they did not mention.\n\n"
    )


async def parse_business_brief(
    raw_message: str,
    previous: list[str] | None = None,
    current: dict | None = None,
) -> tuple[dict, dict]:
    """Return ({"operation": name, **args}, usage) for one described business.

    `current` is the brief already read back to the owner. Passing it turns this from a
    fresh parse into a correction applied to that brief -- see `_render_current`.
    """
    prompt = PROMPT_TEMPLATE.format(
        raw_message=raw_message,
        context_section=_render_context(previous),
        current_section=_render_current(current),
    )
    try:
        return await call_forced_tool(prompt, TOOLS)
    except DailyLimitReached:
        # Subclasses LLMCallFailed, so without this it would be wrapped into a
        # generic parse failure and reported as "try again in a moment" -- advice that
        # cannot work, because the cap resets on the day, not in a moment.
        raise
    except LLMCallFailed as exc:
        raise BriefParseFailed(f"Could not read that description: {exc}") from exc
