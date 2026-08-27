"""The bot's one connection to a model: Claude Sonnet 5, through the Anthropic SDK.

This replaces an OpenRouter client that carried a list of free candidate models and fell
between them when one failed. That design existed because free models are perishable, and
it stopped being enough the week three of them decayed at once -- one preview ended, one
withdrew its free tier, and a third kept answering while quietly losing the ability to emit
a tool call. Every tool-calling path in the bot went down together, and an owner describing
their business was told "sorry, I couldn't process that".

One paid model needs none of that machinery. What the old client hand-rolled -- retry with
backoff, rate-limit handling, request timeouts -- the SDK already does, so the model
ladder, the two-tier attempt budgets, the 404-means-retired branch, and the thread-bounded
sync client are all gone rather than ported.

Two call shapes, deliberately not interchangeable:
- call_forced_tool(): the model must answer by calling exactly one of the tools given.
  Used wherever the answer is a decision -- which edit operation, what to photograph.
- call_plain_completion(): long-form text, for writing a page or a stylesheet. Streamed,
  because a 32k-token response is well past the point where a single HTTP request is a
  sensible way to wait.
"""
import json
import logging

import anthropic

from bot_api.config import get_settings

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"

# Sonnet 5 accepts up to 128k output tokens. A whole page plus its front matter fits in far
# less, and the ceiling is here to stop a runaway response, not to be reached.
GENERATION_MAX_TOKENS = 32000
# A decision, not an essay: which operation, which files, what to photograph. The budget
# only has to cover the thinking that precedes it.
TOOL_MAX_TOKENS = 8000

# `effort` is how much deliberation to spend, and it replaces the token-budget parameter
# older models used (Sonnet 5 rejects `budget_tokens` outright). Writing a site from
# scratch is the work; applying a stated change to an existing file is mechanical, and
# callers that say so get `low`.
GENERATION_EFFORT = "high"
REDUCED_EFFORT = "low"
# Picking one operation from a fixed list is a classification. It still gets adaptive
# thinking -- the model decides how much it needs -- but not a deep budget for it.
TOOL_EFFORT = "low"

# Generous, and not the real bound on a long generation: that is streamed, so the ceiling
# that matters is the model's own. This is for a connection that has stopped responding.
REQUEST_TIMEOUT_SECONDS = 600
# The SDK retries connection errors, 429 and 5xx on its own, with backoff.
MAX_RETRIES = 3


class LLMCallFailed(Exception):
    pass


class DailyLimitReached(LLMCallFailed):
    """Rate-limited by the API rather than at fault in our request.

    Worth its own type because it is the one failure that resolves itself with time:
    reported as a generic "problem generating your website", it sends the owner chasing a
    bug that does not exist.
    """


class CreditExhausted(LLMCallFailed):
    """The account is out of credit.

    A subclass of LLMCallFailed so every existing handler still catches it, but distinct in
    the log because nothing about the code or the request can fix it -- unlike every other
    failure here, this one is waiting on a person to top up the account.
    """


def _client() -> anthropic.AsyncAnthropic:
    """The key comes from settings, not the ambient environment: it lives in .env, which
    the process does not export, so a zero-argument client cannot find it."""
    return anthropic.AsyncAnthropic(
        api_key=get_settings().anthropic_api_key,
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=MAX_RETRIES,
    )


def _as_anthropic_tools(tools: list[dict]) -> list[dict]:
    """Convert the tool dicts the callers already write into Anthropic's shape.

    Every caller in this codebase describes a tool as {"name", "description",
    "parameters"}. Anthropic calls that last key `input_schema`. Translating here rather
    than editing six modules keeps the change to the one place that talks to the API --
    and those dicts are the callers' own documentation of their operations.
    """
    converted = []
    for tool in tools:
        schema = tool.get("input_schema") or tool.get("parameters") or {"type": "object"}
        converted.append({
            "name": tool["name"],
            "description": tool.get("description", ""),
            "input_schema": schema,
        })
    return converted


def _usage(response) -> dict:
    return {
        "model": response.model,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }


def _translate(exc: Exception) -> LLMCallFailed:
    """Map an SDK error onto the three cases the bot actually treats differently."""
    if isinstance(exc, anthropic.RateLimitError):
        return DailyLimitReached(f"rate limited: {exc}")
    if isinstance(exc, anthropic.BadRequestError) and "credit balance" in str(exc).lower():
        return CreditExhausted(f"the Anthropic account is out of credit: {exc}")
    return LLMCallFailed(str(exc))


async def call_forced_tool(prompt: str, tools: list[dict]) -> tuple[dict, dict]:
    """Call the model with `prompt`, requiring it to answer by calling one of `tools`.

    Returns ({"operation": <tool name>, **its arguments}, usage), where usage is
    {"model", "input_tokens", "output_tokens"}.

    `disable_parallel_tool_use` is what makes "one of" true: without it the model may
    answer with several calls at once, and every caller here is asking for a single
    decision -- which operation, which layout, what to photograph.
    """
    valid_names = {t["name"] for t in tools}
    try:
        response = await _client().messages.create(
            model=MODEL,
            max_tokens=TOOL_MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"effort": TOOL_EFFORT},
            tools=_as_anthropic_tools(tools),
            tool_choice={"type": "any", "disable_parallel_tool_use": True},
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as exc:
        raise _translate(exc) from exc

    calls = [block for block in response.content if block.type == "tool_use"]
    if not calls:
        # Forced tool use makes this close to impossible, but a refusal or an exhausted
        # token budget can still end a turn with no call in it.
        raise LLMCallFailed(
            f"model returned no tool call (stop_reason={response.stop_reason!r})"
        )

    call = calls[0]
    if call.name not in valid_names:
        raise LLMCallFailed(f"model called an unrecognized tool: {call.name!r}")

    # `input` arrives already parsed by the SDK; a string here would mean a shape we do
    # not understand, so it is a failure rather than something to coerce.
    if not isinstance(call.input, dict):
        raise LLMCallFailed(f"tool arguments were not an object: {str(call.input)[:120]!r}")

    logger.info(
        "llm.tool_call",
        extra={"event": "llm.tool_call", "model": response.model, "tool": call.name,
               "input_tokens": response.usage.input_tokens,
               "output_tokens": response.usage.output_tokens},
    )
    return {"operation": call.name, **call.input}, _usage(response)


async def call_plain_completion(
    prompt: str,
    *,
    reduced_reasoning: bool = False,
    online: bool = False,
) -> tuple[str, dict]:
    """Call the model with `prompt` and return (text, usage).

    Streamed rather than requested in one shot: these responses run to tens of thousands
    of tokens, and a plain request that size is a long silence that can end in a timeout
    instead of a page.

    `online=True` gives the model Anthropic's server-side web search, for the one step that
    genuinely needs a fact about a real business. It runs on Anthropic's servers, and the
    model decides whether to search at all, so it costs nothing extra when the answer was
    already in the prompt.
    """
    request = {
        "model": MODEL,
        "max_tokens": GENERATION_MAX_TOKENS,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": REDUCED_EFFORT if reduced_reasoning else GENERATION_EFFORT},
        "messages": [{"role": "user", "content": prompt}],
    }
    if online:
        request["tools"] = [{"type": "web_search_20260209", "name": "web_search"}]

    try:
        async with _client().messages.stream(**request) as stream:
            response = await stream.get_final_message()
    except anthropic.APIError as exc:
        raise _translate(exc) from exc

    text = "".join(block.text for block in response.content if block.type == "text")
    if not text.strip():
        raise LLMCallFailed(
            f"model returned no text (stop_reason={response.stop_reason!r})"
        )

    logger.info(
        "llm.completion",
        extra={"event": "llm.completion", "model": response.model,
               "chars": len(text), "stop_reason": response.stop_reason,
               "input_tokens": response.usage.input_tokens,
               "output_tokens": response.usage.output_tokens},
    )
    return text, _usage(response)
