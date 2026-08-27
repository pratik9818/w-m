"""Look up facts the model does not reliably know, before it writes anything.

The generation models are small, free and frozen at their training cut-off. Asked to build
a site whose subject matter sits outside what they know -- a token project, a niche
regulation, a product catalogue, anything current -- they do the worst possible thing:
they invent it, fluently. `_content_rules.md` forbids inventing checkable facts, so until
now the alternative was a page that simply said nothing.

This module gives them the third option: go and look it up.

Two calls, deliberately split, because OpenRouter's `:online` suffix searches the web on
EVERY request carrying it and bills per result. Searching unconditionally would put a
per-build charge on the plumbers and cafes that never needed a lookup.

  1. Decide (free, no search). "Would building this need facts you don't reliably know?
     If so, what is the one search worth running?" A model answering NONE costs nothing
     beyond the free-tier call.
  2. Look up (billed, searched). Only runs when step 1 produced a query.

So the common case is one extra free call, and search is paid for only by the builds that
actually need one.
"""
import logging

from bot_api.services.llm_client import call_plain_completion

logger = logging.getLogger(__name__)

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

Examples:

Task: Build a website for "Raj Plumbing". Category: Plumber.
NONE

Task: Make the hero heading bigger and centre it.
NONE

Task: Build a website for "NovaToken". Category: Crypto project.
SEARCH: NovaToken cryptocurrency project

Task: Add a section listing the other major cryptocurrencies alongside ours.
SEARCH: largest cryptocurrencies by market capitalisation

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


async def _decide_query(task: str) -> tuple[str | None, dict | None]:
    """The query to run, or None. Defaults to None on anything it cannot read cleanly.

    Only this side of the decision is safe to get wrong cheaply: a missed search costs a
    slightly vaguer page, an imagined one costs money on every build that never needed it.
    """
    reply, usage = await call_plain_completion(
        _DECIDE_PROMPT.format(task=task), reduced_reasoning=True
    )
    for line in reply.strip().splitlines():
        stripped = line.strip().strip("*`\"'")
        if not stripped.lower().startswith(SEARCH_PREFIX):
            continue
        query = stripped[len(SEARCH_PREFIX):].strip().strip("\"'`")
        if query and not _is_none(query) and len(query) <= MAX_QUERY_CHARS:
            return query, usage
        return None, usage
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


async def gather_facts(task: str) -> tuple[str, dict | None]:
    """Return (facts_block, usage). The block is "" whenever no lookup was needed.

    Never raises. Research is an enhancement: a build that cannot reach the search must
    still produce a site, exactly as it did before this existed. Same reasoning as the
    design brief's fallback -- an optional call must not be able to fail a build.
    """
    try:
        query, decide_usage = await _decide_query(task)
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
    return await gather_facts(task)


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
