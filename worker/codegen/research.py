"""Look up facts the model does not reliably know, before it writes anything.

The generation models are small, free and frozen at their training cut-off. Asked to build
a site whose subject matter sits outside what they know -- a token project, a niche
regulation, a product catalogue, anything current -- they do the worst possible thing:
they invent it, fluently. `_content_rules.md` forbids inventing checkable facts, so until
now the alternative was a page that simply said nothing.

This module gives them the third option: go and look it up.

Two calls, deliberately split, so an ordinary plumber or cafe never pays for a search it
had no use for:

  1. Decide (cheap, no search). "Would building this need facts you don't reliably know?
     If so, what is the one search worth running?" Measured at ~630 tokens.
  2. Look up (expensive, searched). Only runs when step 1 produced a query.

Step 2 is far more expensive than it looks. The search runs on Anthropic's servers and the
results are fed back into the request as *input*, so one lookup was measured at **25,117
input tokens** for 330 tokens of output -- 32% of an entire 78,656-token build. (An earlier
version of this comment described OpenRouter's `:online` suffix billing per result; the bot
moved to Anthropic's server-side web search and the cost model changed completely, which is
why a "cheap" lookup went unnoticed as it became the single largest call in the build.)

The one search that can never succeed is a search for the business itself: a brand-new
small business has no web presence, so the results come back empty. That happened live --
query "Xtravu digital signage SaaS platform" returned `facts_chars: 0` after spending those
25k tokens. Both the prompt below and `_decide_query`'s deterministic guard refuse it.
"""
import logging
import re

from bot_api.services.llm_client import call_plain_completion

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-z0-9]+")
# Words a business name can be made of that say nothing distinctive about it. Without this,
# a business called "Bristol Cleaning Services" would block any query containing
# "services" -- including the useful ones.
_GENERIC_NAME_WORDS = frozenset({
    "the", "a", "an", "and", "of", "for", "at", "by", "my", "our",
    "co", "ltd", "limited", "inc", "llc", "plc", "gmbh", "group", "holdings",
    "company", "business", "services", "service", "solutions", "systems",
    "studio", "studios", "shop", "store", "agency", "works", "collective",
})

# The decide step must answer in one of exactly two shapes. Keying on a prefix rather than
# "the last line that wasn't NONE" is what stops a model's stray closing remark from
# becoming a query -- and a search only we pay for. Anything unrecognised means no search.
SEARCH_PREFIX = "search:"
NO_SEARCH = "none"
MAX_QUERY_CHARS = 200
# Facts ride in every page prompt of the build, so this is a budget rather than a limit on
# what is knowable. Ten or so bullets is what a page can actually use.
MAX_FACTS_CHARS = 2000

_DECIDE_PROMPT = """You are about to help build or edit a small business website. Before
any writing happens, decide one thing: does it need a fact you do not reliably know?

Reply in exactly one of these two forms, on a single line, with nothing else at all:

SEARCH: <the one web search query that would settle it>
NONE

Choose SEARCH when the writing depends on something current or specialist you would
otherwise have to guess -- a named product or project, a real organisation, a live market,
a specific place, anything that changes over time.

Choose NONE when this is ordinary writing about an ordinary business, or a change to how
the page looks. Most tasks are NONE.

**Never search for the business itself.** However specialist its category sounds, this is a
small business having its first website built -- there is nothing about it on the web to
find, and the search comes back empty every time. Search only for things that exist
independently of it: a market, a regulation, a named third-party product, a place. If the
query you were about to write is essentially the business's own name, answer NONE.

Examples:

Task: Build a website for "Raj Plumbing". Category: Plumber.
NONE

Task: Make the hero heading bigger and centre it.
NONE

Task: Build a website for "Xtravu". Category: digital signage SaaS platform.
NONE

Task: Build a website for "NovaToken". Category: Crypto project.
NONE

Task: Add a section listing the other major cryptocurrencies alongside ours.
SEARCH: largest cryptocurrencies by market capitalisation

Task: Add a section on the deposit protection rules we have to follow as letting agents.
SEARCH: UK tenancy deposit protection scheme rules for letting agents

Now the real one:

{task}"""


_LOOKUP_PROMPT = """Search results for this question are included above. Use them to
answer it:

{query}

Reply with between three and ten short factual bullet points, one per line, each starting
with "- ". No preamble, no heading, no closing remark.

Rules:
- Only state what the search results actually support. This is going onto a real
  business's public website, so a confident guess is worse than a missing bullet.
- Prefer specific, checkable facts (names, categories, how something works) over vague
  description.
- If the results are thin, contradictory, or do not answer the question, reply with
  exactly: NONE"""


def _is_none(reply: str) -> bool:
    return reply.strip().strip("\"'`.*").lower() == NO_SEARCH


def _is_self_search(query: str, subject: str | None) -> bool:
    """True when the query names the business it is supposed to be researching.

    Backs up the prompt rule instead of trusting it -- the same reasoning this codebase
    applies everywhere else: a rule that lives only in a prompt is a hope, and this one is
    worth a third of a build.

    Deliberately blunt: any distinctive word of the business's own name appearing in the
    query is enough to refuse it. A search naming the business is a search for a company
    that had no website until this moment, and it comes back empty. The genuinely useful
    lookups -- a market, a regulation, a third-party product -- never name the business, so
    they are unaffected. Erring this way is cheap by design: a missed search costs a
    slightly vaguer page, while the search it prevents cost 25,117 tokens and returned
    nothing.
    """
    if not subject or not query:
        return False
    distinctive = {
        word for word in _WORD_RE.findall(subject.lower())
        if word and word not in _GENERIC_NAME_WORDS
    }
    if not distinctive:
        # A name made entirely of filler ("The Studio") tells us nothing, so let the
        # model's own judgement stand rather than blocking on a coincidence.
        return False
    return bool(distinctive & set(_WORD_RE.findall(query.lower())))


async def _decide_query(
    task: str, subject: str | None = None
) -> tuple[str | None, dict | None]:
    """The query to run, or None. Defaults to None on anything it cannot read cleanly.

    Only this side of the decision is safe to get wrong cheaply: a missed search costs a
    slightly vaguer page, an imagined one costs money on every build that never needed it.

    `subject` is the business's own name, when there is one. A query naming it is refused
    outright -- see `_is_self_search`.
    """
    reply, usage = await call_plain_completion(
        _DECIDE_PROMPT.format(task=task), reduced_reasoning=True
    )
    for line in reply.strip().splitlines():
        stripped = line.strip().strip("*`\"'")
        if not stripped.lower().startswith(SEARCH_PREFIX):
            continue
        query = stripped[len(SEARCH_PREFIX):].strip().strip("\"'`")
        if not query or _is_none(query) or len(query) > MAX_QUERY_CHARS:
            return None, usage
        if _is_self_search(query, subject):
            logger.info(
                "research.self_search_refused",
                extra={"event": "research.self_search_refused", "query": query[:120]},
            )
            return None, usage
        return query, usage
    return None, usage


async def _look_up(query: str) -> tuple[str, dict | None]:
    reply, usage = await call_plain_completion(
        _LOOKUP_PROMPT.format(query=query), online=True
    )
    if _is_none(reply):
        return "", usage
    bullets = [ln.strip() for ln in reply.strip().splitlines() if ln.strip().startswith("-")]
    return "\n".join(bullets)[:MAX_FACTS_CHARS], usage


def _merge_usage(*parts: dict | None) -> dict | None:
    """Fold the research calls into one usage record the caller can bill as a unit."""
    real = [p for p in parts if p]
    if not real:
        return None
    return {
        "model": real[-1]["model"],
        "input_tokens": sum(p["input_tokens"] for p in real),
        "output_tokens": sum(p["output_tokens"] for p in real),
        "requests": len(real),
    }


def _format_block(query: str, facts: str) -> str:
    """Wrap the bullets in the framing the writing prompts need.

    The content rules forbid inventing checkable claims, and these facts arrive from
    outside the business data, so they need an explicit licence -- and an explicit limit.
    They are facts about the WORLD, never about this business: a search result saying a
    coin exists does not mean this business supports it.
    """
    return (
        "## Researched facts (from a live web search, run just now)\n\n"
        f"These answer: *{query}*\n\n"
        f"{facts}\n\n"
        "You may use these on the page. They are the one exception to writing only from "
        "the business data above -- everything here was looked up rather than remembered, "
        "so it is safe to state.\n\n"
        "Two limits, both absolute:\n"
        "- These are facts about the world, **not claims about this business.** That a "
        "thing exists does not mean this business offers, supports, stocks, partners with "
        "or is certified in it. Never write it as though it does.\n"
        "- Use only what is actually here. Do not extend the list from memory, do not add "
        "figures that are not written above, and leave out anything you were not given."
    )


async def gather_facts(task: str, subject: str | None = None) -> tuple[str, dict | None]:
    """Return (facts_block, usage). The block is "" whenever no lookup was needed.

    Never raises. Research is an enhancement: a build that cannot reach the search must
    still produce a site, exactly as it did before this existed. Same reasoning as the
    design brief's fallback -- an optional call must not be able to fail a build.

    `subject` is the business's own name where the caller knows it, so a query that merely
    names the business can be refused before the expensive call runs.
    """
    try:
        query, decide_usage = await _decide_query(task, subject)
        if not query:
            logger.info("research.skipped", extra={"event": "research.skipped"})
            return "", _merge_usage(decide_usage)

        facts, lookup_usage = await _look_up(query)
        logger.info(
            "research.done",
            extra={"event": "research.done", "query": query, "facts_chars": len(facts)},
        )
        usage = _merge_usage(decide_usage, lookup_usage)
        return (_format_block(query, facts) if facts else ""), usage
    except Exception:
        logger.warning("research raised, continuing without facts", exc_info=True)
        return "", None


async def facts_for_build(spec: dict) -> tuple[str, dict | None]:
    """Research for a whole site, described by its business spec."""
    services = ", ".join(s["name"] for s in spec.get("services") or [] if s.get("name"))
    task = "\n".join(
        part for part in (
            f"Build a website for a business called {spec.get('name')!r}.",
            f"Category: {spec.get('category')}." if spec.get("category") else "",
            f"They describe themselves as: {spec.get('tagline')}" if spec.get("tagline") else "",
            f"About: {spec.get('about')}" if spec.get("about") else "",
            f"Services listed: {services}" if services else "",
        ) if part
    )
    # The name goes in as the subject, not just inside the task text: a build is the one
    # case where the model is most tempted to search for the business itself, and it is
    # exactly the case that cannot work.
    return await gather_facts(task, subject=spec.get("name"))


async def facts_for_edit(
    instruction: str, user_message: str | None = None
) -> tuple[str, dict | None]:
    """Research for one edit to a live site.

    The owner's own words ride along with the parsed instruction because the instruction
    is written by a parser that has never seen the page -- "add the other coins too"
    survives in the owner's phrasing even when the parsed instruction flattens it.
    """
    task = f"Make this change to an existing small business website:\n\n{instruction}"
    if user_message and user_message.strip():
        task += f'\n\nThe owner asked for it in their own words: "{user_message.strip()}"'
    return await gather_facts(task)
