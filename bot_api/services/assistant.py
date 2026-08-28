"""The half of the conversation that isn't an edit.

Everything else in this bot assumes the owner is asking for a change. They are not, most of
the time. They are asking where their site is, whether it worked, what a domain is, why it
looks like that, what they have left, or what they should do next -- and until now every
one of those hit the same wall:

    "Not sure that's something I can help edit! If you want to change your site, just tell
     me what to update -- e.g. "change my hours to 9-6". Use /mysites to switch sites or
     /newsite to build another."

That reply is addressed to someone who already knows what this bot is, what a site is, and
what a slash command is. The people using it know none of those things. They know how to
ask a question, which is what they did, and they were told they had asked wrongly.

Two rules hold this together:

  1. **Facts come from the database, never from the model.** Everything the owner might ask
     about -- their sites, their links, whether a build is running, what they have spent --
     is knowable exactly. The model is given those facts and asked to put them into
     sentences. It is never asked to recall them, because a confidently wrong web address
     is worse than no answer.
  2. **Never end without a next step.** An owner who is told something true and then left
     staring at the screen has been helped no more than one who was bounced.

The cheapest questions never reach a model at all: "what's my link" is answered from a
column.
"""
import logging
import re

from bot_api.services.llm_client import LLMCallFailed, call_plain_completion
from bot_api.services.session import render_edit_context
from worker.codegen.quota import AVG_EDIT_COST, get_quota_summary

logger = logging.getLogger(__name__)

# Long enough to teach something, short enough to read on a phone in a chat window.
MAX_REPLY_CHARS = 900


# --------------------------------------------------------------- what is actually true

async def owner_facts(session, telegram_user_id: int, businesses: list, active_id=None) -> dict:
    """Everything true about this owner, gathered once.

    Passed to the model as facts and used directly by the deterministic answers below, so
    the two can never disagree with each other.
    """
    quota = await get_quota_summary(session, telegram_user_id)
    return {
        "sites": [
            {
                "name": business.name,
                "url": business.deployment_url,
                "status": business.generation_status,
                "layout": business.layout,
                "is_active": active_id is not None and business.id == active_id,
            }
            for business in businesses
        ],
        "tokens_used": quota["used"],
        "tokens_limit": quota["limit"],
        "tokens_remaining": quota["remaining"],
        "changes_left": quota["remaining"] // AVG_EDIT_COST,
    }


def _describe_site(site: dict) -> str:
    if site["url"]:
        return f"<b>{site['name']}</b> — {site['url']}"
    if site["status"] in ("queued", "generating", "building", "deploying"):
        return f"<b>{site['name']}</b> — still being built, I'll send the link when it's ready"
    return f"<b>{site['name']}</b> — not built yet"


def render_facts(facts: dict) -> str:
    """The facts, written out for the model. Plain and complete; it invents what it is not told."""
    if not facts["sites"]:
        lines = ["They have no websites yet."]
    else:
        lines = [f"They have {len(facts['sites'])} website(s):"]
        for site in facts["sites"]:
            active = " (this is the one they are currently editing)" if site["is_active"] else ""
            shape = "one-page landing site" if site["layout"] == "landing" else "four-page site"
            address = site["url"] or "no address yet -- it has not finished building"
            lines.append(
                f'- "{site["name"]}": a {shape}, status "{site["status"]}", '
                f"address: {address}{active}"
            )
    lines.append(
        f"\nTheir allowance: {facts['tokens_used']:,} of {facts['tokens_limit']:,} used, "
        f"{facts['tokens_remaining']:,} left -- room for roughly {facts['changes_left']} "
        "more changes."
    )
    return "\n".join(lines)


# --------------------------------------------------------------- answered for free

_LINK_RE = re.compile(
    r"\b(?:link|url|address|web ?address|domain|live|online|see (?:my|the) site|"
    r"where is (?:my|the) (?:site|website)|show me (?:my|the) (?:site|website))\b",
    re.IGNORECASE,
)
# "Is it ready yet?" is the single most-asked question during a build, and it has the same
# answer as "where is my link" -- the address if there is one, and how far along it is if
# there is not. Kept as its own pattern rather than folded into the one above because these
# words say nothing about a link.
_STATUS_RE = re.compile(
    r"\b(?:ready|done|finished|built|building|working|complete|completed|up yet|"
    r"how(?:'s| is) it going|any progress|status)\b",
    re.IGNORECASE,
)
_COUNT_RE = re.compile(
    r"\bhow many\b[^.?!]{0,25}\b(?:sites?|websites?|pages?)\b"
    r"|\b(?:list|show|what are)\b[^.?!]{0,20}\b(?:my )?(?:sites?|websites?)\b",
    re.IGNORECASE,
)
_QUOTA_RE = re.compile(
    r"\b(?:token|tokens|allowance|quota|credit|credits|limit|balance)\b"
    r"|\bhow (?:much|many)\b[^.?!]{0,20}\b(?:left|remaining|used)\b",
    re.IGNORECASE,
)
_CAPABILITY_RE = re.compile(
    r"\bwhat can you do\b|\bwhat do you do\b|\bhow (?:does this|do you) work\b"
    r"|\bwho are you\b|\bwhat is this\b|\bhelp\b(?!\s+me\s+(?:change|edit|add|remove))",
    re.IGNORECASE,
)

CAPABILITIES = (
    "I build and look after websites for small businesses — you talk, I do the work.\n\n"
    "Things you can say to me:\n"
    "• <i>\"I want a website for my bakery in Leeds\"</i> — I'll build one\n"
    "• <i>\"change my phone number to 0113 496 0000\"</i>\n"
    "• <i>\"make the heading bigger\"</i> or <i>\"use green buttons\"</i>\n"
    "• <i>\"add a picture at the top\"</i> — send me one, or I'll find one\n"
    "• <i>\"what's my link?\"</i> or <i>\"how much have I used?\"</i>\n"
    "• <i>\"how many people visited today?\"</i> — I count them for you\n"
    "• <i>\"where did they come from?\"</i> — Google, Facebook, or a link you sent\n\n"
    "You never need to know anything about websites. Just say it how you'd say it to a "
    "person, and ask me if you're not sure — that's what I'm here for."
)


# A question, by shape rather than by subject. Required before any of the patterns below
# counts, because the words overlap with real edit requests: "make the link bigger" is a
# change to a page and mentions a link, and answering it with "here's your web address"
# would be confidently unhelpful.
_QUESTION_SHAPE_RE = re.compile(
    r"\?\s*$"
    r"|^\s*(?:what'?s?|where'?s?|when|how|why|which|who|do|does|did|is|are|am|can|could|"
    r"would|will|should|have|has|tell me|show me|give me|list|i want to know|"
    r"any (?:idea|chance))\b",
    re.IGNORECASE,
)


def looks_like_a_question(text: str) -> bool:
    """Can this be answered from stored facts, without reading it with a model?

    Used to skip the two model calls the edit pipeline would otherwise spend working out
    that "what's my link?" is not an instruction.
    """
    body = text or ""
    if _CAPABILITY_RE.search(body):
        return True
    if not _QUESTION_SHAPE_RE.search(body.strip()):
        return False
    return bool(_LINK_RE.search(body) or _COUNT_RE.search(body)
                or _QUOTA_RE.search(body) or _STATUS_RE.search(body))


def answer_from_facts(question: str, facts: dict) -> str | None:
    """The common questions, answered from stored data with no model call.

    These are asked constantly and have exactly one right answer each, so paying to have
    one composed would be paying for a worse version of a column lookup.
    """
    sites = facts["sites"]

    if _CAPABILITY_RE.search(question):
        return CAPABILITIES

    if not _QUESTION_SHAPE_RE.search((question or "").strip()):
        # Mentions one of these subjects but is not asking about it. Let the model read it.
        return None

    if _QUOTA_RE.search(question):
        if facts["tokens_remaining"] <= 0:
            return (
                f"You've used your whole allowance — {facts['tokens_used']:,} of "
                f"{facts['tokens_limit']:,}. I can't make changes until it's topped up. "
                "Let me know and I'll sort it out."
            )
        return (
            f"You've used {facts['tokens_used']:,} of your {facts['tokens_limit']:,} "
            f"allowance, so you've got {facts['tokens_remaining']:,} left — that's room "
            f"for about <b>{facts['changes_left']}</b> more changes.\n\n"
            "Every message I read and every rebuild uses a little. Small tweaks like "
            "colours and wording cost the least."
        )

    if _LINK_RE.search(question) or _COUNT_RE.search(question) or _STATUS_RE.search(question):
        if not sites:
            return (
                "You haven't got a website yet — but that's about ten minutes away.\n\n"
                "Just tell me about your business in one message: what you do, where you "
                "are, and anything you'd like on the page. Something like <i>\"a page for "
                "my bakery Rise &amp; Crumb in Leeds, we do sourdough and celebration "
                "cakes\"</i>. I'll write the rest."
            )
        if len(sites) == 1:
            site = sites[0]
            if site["url"]:
                return (
                    f"Here it is — <b>{site['name']}</b>:\n{site['url']}\n\n"
                    "That link is live now; you can send it to anyone. Tell me anything "
                    "you'd like changed and I'll do it."
                )
            return (
                f"<b>{site['name']}</b> is {site['status']} — I'm still working on it. "
                "I'll message you here with the link the moment it's ready."
            )
        listed = "\n".join(f"• {_describe_site(site)}" for site in sites)
        return (
            f"You've got {len(sites)} sites:\n{listed}\n\n"
            "Tell me which one you'd like to work on, or just say what you want changed."
        )

    return None


# --------------------------------------------------------------- answered by the model

_PROMPT = """You are the assistant inside a Telegram bot that builds and edits websites for
small business owners. You are talking to the owner right now.

Who they are: someone who runs a shop, a salon, a plumbing round, a cafe. They do not know
what HTML is, what a domain is, what hosting is, or what a slash command is, and they should
never need to. They know how to ask a question. Treat every question as a reasonable one.

## What is true about this person right now

{facts}
{context}
## What you can do for them

- Build a website from a description of their business.
- Change anything on a site they already have: wording, colours, sizes, photos, phone
  number, opening hours, services, how many pages it has.
- Show them their link, tell them what they have spent, undo the last change.
- Tell them how many people visited their site and when, and where those visitors came
  from -- Google, Facebook, or a link they sent out themselves. If they ask about visitors
  and no numbers appear above, say you will check and ask them to put it as a question
  like "how many visits this week?" -- never guess a visitor count.
- Explain anything about their website in plain language.

## Their message

"{question}"

## How to answer

Write a short reply, as a knowledgeable friend would in a chat window. Two or three
sentences is usually right; a short list is fine when there are genuinely several things.

Rules, in order of importance:

1. **Only state facts from the section above.** Never invent a web address, a number, a
   status or a site name. If the answer is not there, say plainly that you do not know it
   and say what you would need.
2. **Never leave them stuck.** End with the specific thing they can type next, in their own
   words -- not a command name. "Tell me what you'd like the heading to say" beats "use
   /edit".
3. **No jargon.** Not deploy, repository, HTML, CSS, DNS, cache, token limit. If a technical
   thing genuinely needs explaining, explain it with something they already know.
4. **Answer what they asked**, not what would be easier to answer. If they asked something
   you cannot do, say so in one sentence and tell them what you can do instead.
5. Do not greet them, do not introduce yourself, and do not apologise for being an AI. You
   are mid-conversation.

Reply with the message only -- no preamble, no sign-off, no quotation marks around it.
Telegram HTML is allowed: <b>bold</b>, <i>italic</i>. Nothing else."""


async def answer_question(question: str, facts: dict, context: list[dict] | None = None):
    """A reply to something that is not an edit. Returns (reply, usage).

    Runs at reduced effort deliberately. This is conversation, not code: the expensive
    thinking in this bot belongs to writing a site, and an owner asking where their link is
    should not be charged for deliberation.
    """
    rendered_context = render_edit_context(context)
    prompt = _PROMPT.format(
        facts=render_facts(facts),
        context=f"\n## The conversation so far\n{rendered_context}\n" if rendered_context else "",
        question=question.strip()[:1000],
    )
    reply, usage = await call_plain_completion(prompt, reduced_reasoning=True)
    return reply.strip()[:MAX_REPLY_CHARS], usage


# What is said when even the model call fails. Still an answer, still a next step -- the
# whole point of this module is that there is no dead end, and a broken model call is not a
# reason to reintroduce one.
FALLBACK_REPLY = (
    "I'm having trouble thinking straight for a moment — give me a minute and ask again.\n\n"
    "In the meantime, if you want something changed on your site, just tell me what it is "
    "in your own words and I'll get on with it."
)


async def answer_or_fallback(question: str, facts: dict, context: list[dict] | None = None):
    """`answer_question`, but never raising. Returns (reply, usage-or-None)."""
    try:
        return await answer_question(question, facts, context)
    except LLMCallFailed as exc:
        logger.warning(
            "assistant.failed",
            extra={"event": "assistant.failed", "error": str(exc)[:200]},
        )
        return FALLBACK_REPLY, None
