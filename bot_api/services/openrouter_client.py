"""Shared OpenRouter chat-completions client, used by both worker/codegen/builder.py
(Part 2) and bot_api/services/nl_edit.py (Part 4b).

Free-model rotation is a real, observed risk (see the plan doc's provider-swap note --
two plausible candidate models had already rotated to paid-only mid-session), so this
tries a primary model and falls back to a secondary on a "model unavailable" 404
specifically, rather than hardcoding a single model name everywhere.

Uses a SYNCHRONOUS httpx.Client, bounded by concurrent.futures.Future.result(timeout=...)
rather than asyncio.wait_for, run off the event loop via asyncio.to_thread. Observed
live on this exact setup (Python 3.14.5 / Windows): asyncio.wait_for does NOT reliably
enforce its timeout against a to_thread-wrapped blocking call -- confirmed with a
trivial, network-free repro (wait_for(to_thread(time.sleep(300)), timeout=5) never
fired). concurrent.futures' own timeout mechanism is independent of that asyncio
cancellation path and is what actually bounds execution here.

Two call shapes, deliberately NOT interchangeable:
- call_forced_tool(): forced tool-calling (tool_choice="required"). Verified live that
  OpenRouter's free tier caps individual tool-call ARGUMENT strings at ~1024 characters
  REGARDLESS of model or max_tokens (confirmed with max_tokens=16000, only 521 tokens
  actually used, still cut off at 1024 chars) -- fine for Part 4b's short field values,
  but useless for a multi-KB HTML document.
- call_plain_completion(): plain text completion, no tools. Verified live this does NOT
  hit the same cap (8839 chars, finish_reason="stop", nothing truncated) -- this is what
  Part 2's actual site generation must use instead.
"""
import asyncio
import concurrent.futures
import json
import logging
from collections.abc import Callable

import httpx

from bot_api.config import get_settings

logger = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Two separate candidate lists, because the two call shapes want opposite things.
#
# Generation (plain completion) is quality-bound: it writes a whole multi-page website in
# one response, and the small/fast models produce thin, sparse pages no matter how the
# prompt is written. Latency is acceptable here -- the pipeline is already async and the
# owner gets per-stage progress messages in Telegram while it runs.
#
# Measured live against the real four-page site prompt (wall time / body words across the
# four pages / CSS custom properties, as a proxy for how designed the stylesheet is):
#   ultra-550b   408s  2599 words  37 vars, 16 hover states, 5 gradients, 5 media queries
#   lightning    134s  2084 words  14 vars, but only 1 media query and 6 empty sections
#   super-120b    57s  1869 words   9 vars,  0 gradients,     5 media queries
# ultra leads because output quality is the entire product here and the pipeline is async
# with per-stage progress messages; super-120b is the fallback because it is 7x faster and
# still returns a complete, clean five-file site. lightning is deliberately not a candidate
# (weak responsive coverage and it left empty sections behind).
# super-120b leads on throughput (157 tok/s vs ultra's 39), and generation is now split
# into concurrent per-artifact calls, so each call is small enough that the faster model's
# lower single-shot word count no longer costs content -- every page gets its own full
# response budget instead of competing with four other files in one reply. ultra stays as
# the fallback for its higher ceiling.
#
# stealth/ox-alpha now leads both lists: a free, currently-cloaked reasoning model aimed at
# coding, with a 1M context and a 131k completion ceiling -- a strictly higher ceiling than
# anything else here at no cost. Verified live on this key: a full home-page prompt came
# back complete in 26s (finish_reason "stop"), and it honours inference-enforced
# tool_choice, returning a correct one-line edit in 7s. It is NOT a safe sole candidate --
# see STEALTH_MAX_ATTEMPTS -- so the nemotrons stay behind it as proven fallbacks.
STEALTH_MODEL = "stealth/ox-alpha"
GENERATION_MODELS = (
    STEALTH_MODEL,
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
)
# Edit parsing (forced tool call) is latency-bound -- the owner is waiting in the chat --
# and only needs to pick one short operation, so the fast model leads among the fallbacks.
# Every entry is verified live to support inference-enforced tool_choice; not every free
# model does (openai/gpt-oss-20b:free 400s on it).
TOOL_MODELS = (
    STEALTH_MODEL,
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
)

TOOL_MAX_ATTEMPTS = 3
# Tool calls had no budget at all, so the model got whatever the provider defaulted to.
# Both tool models reason before answering and the reasoning is billed as completion
# tokens: a single successful edit parse spent 4,961 output tokens to produce a one-line
# answer. A deliberation slightly longer than that reached the provider's default ceiling
# before the tool call was ever emitted, and the owner was told the bot could not process
# their message. This is deliberately far above what the answer itself needs.
TOOL_MAX_TOKENS = 16000
# Only 2 for generation: a retry against the slow primary costs ~7 minutes, so burning
# three of them before falling back would leave the owner waiting far too long. Two
# attempts still absorbs a transient blip, then hands over to the fast fallback.
GENERATION_MAX_ATTEMPTS = 2

# The stealth model is served from a shared upstream pool that rejects far more often than
# it answers, with HTTP 429 "temporarily rate-limited upstream" (distinct from a per-day
# cap -- it clears in seconds). Measured live on the real call shapes: the generation
# prompt got through on attempt 3, the tool call on attempt 6. On the normal 2-3 attempt
# ladders it would therefore almost never actually be reached, and the run would silently
# be a nemotron run every time. These rejections are cheap -- each fails in 1-7s without
# spending tokens -- so it gets a much longer ladder than the models whose retries cost
# real minutes. Worst case here is ~1 minute of fast rejections before the fallback.
STEALTH_MAX_ATTEMPTS = 8
# The stealth model always reasons, and left to itself it picks its "max" effort. Measured
# live on one page prompt: max took 236s (9,914 output tokens) against this module's 300s
# ceiling, while high took 24s and low 18s -- both finishing cleanly, high returning the
# fuller page of the two. Max is not worth a call that spends four minutes and can time out
# outright on a longer prompt, so anything that hasn't asked for a specific effort gets
# high. Callers that pass REDUCED_REASONING still get theirs; this only fills the gap.
STEALTH_DEFAULT_EFFORT = "high"
# ...and a cap on the backoff to match: 2**7 would sit out 128s waiting for a limit that
# clears in seconds.
MAX_BACKOFF_SECONDS = 8

# No single call now produces a whole site -- the largest is one stylesheet or two pages --
# so this is back to a ceiling for a hung connection rather than a real expected duration.
REQUEST_TIMEOUT_SECONDS = 300
# A four-page site plus its stylesheet runs well past the default response budget; all
# the generation candidates advertise >=65k max completion tokens.
GENERATION_MAX_TOKENS = 32000


class OpenRouterCallFailed(Exception):
    pass


def _plain_problem(data: dict) -> str | None:
    """Why this completion is unusable, or None if it is fine."""
    choices = data.get("choices") or []
    if not choices:
        return "response contained no choices"
    choice = choices[0]
    if not (choice.get("message") or {}).get("content"):
        # Seen live: the provider aborted 98 tokens into its reasoning and returned
        # HTTP 200, finish_reason "error", and no content at all.
        return f"empty content (finish_reason={choice.get('finish_reason')!r})"
    return None


def _tool_problem(data: dict, valid_names: set[str]) -> str | None:
    """Why this tool response is unusable, or None if it is fine."""
    choices = data.get("choices") or []
    if not choices:
        return "response contained no choices"
    choice = choices[0]
    tool_calls = (choice.get("message") or {}).get("tool_calls")
    if not tool_calls:
        # The usual cause is a model that reasoned until its budget ran out and never
        # got as far as calling anything.
        return f"no tool call returned (finish_reason={choice.get('finish_reason')!r})"
    call = tool_calls[0].get("function") or {}
    if call.get("name") not in valid_names:
        return f"called an unrecognized tool: {call.get('name')!r}"
    try:
        json.loads(call.get("arguments") or "")
    except (json.JSONDecodeError, TypeError):
        return f"returned malformed tool arguments: {str(call.get('arguments'))[:120]!r}"
    return None


class DailyLimitReached(OpenRouterCallFailed):
    """The provider's per-day request cap is exhausted, not a fault in our request.

    Worth its own type because it is the one failure that resolves itself with time:
    reported as a generic "problem generating your website", it sends the owner chasing
    a bug that does not exist.
    """


# Both chosen models reason before answering, and OpenRouter bills that thinking as
# completion tokens. Measured on a real edit: 28,610 output tokens billed while the two
# files being returned were only ~5,595 tokens -- roughly 80% was invisible deliberation.
#
# `medium` rather than `low`: deliberately conservative. On a trivial probe the three
# effort levels came out non-monotonic (low 48, medium 104, high 46 tokens), so that probe
# proves the parameter is accepted but says nothing reliable about the real saving -- and
# nothing at all about quality. Medium keeps a safety margin on edit quality until the
# effect is measured on an actual patch.
#
# Note `exclude: true` is NOT the same thing: it hides reasoning from the response while
# still generating and billing it.
REDUCED_REASONING = {"reasoning": {"effort": "medium"}}


async def call_forced_tool(prompt: str, tools: list[dict]) -> tuple[dict, dict]:
    """Call OpenRouter with `prompt`, forcing a call to exactly one of `tools`
    (each a plain {"name", "description", "parameters"} schema dict). With a single
    tool this forces that one; with several, the model picks which -- verified live
    that `tool_choice: "required"` works the same way in both cases.

    Only suitable for short argument values (see module docstring) -- not for full
    site generation, use call_plain_completion() for that.

    Returns ({"operation": <name>, **parsed_arguments}, usage) where usage is
    {"model": str, "input_tokens": int, "output_tokens": int}.
    """
    valid_names = {t["name"] for t in tools}
    body_extra = {
        "tools": [{"type": "function", "function": t} for t in tools],
        "tool_choice": "required",
        "max_tokens": TOOL_MAX_TOKENS,
        # Picking one operation from a fixed list is a classification, not an essay --
        # one such call spent 5,744 output tokens deliberating over a small JSON answer.
        **REDUCED_REASONING,
    }
    data, model = await _request_with_retries(
        prompt, body_extra, TOOL_MODELS, TOOL_MAX_ATTEMPTS,
        problem_with=lambda d: _tool_problem(d, valid_names),
    )
    return _parse_tool_response(data, model, valid_names)


async def call_plain_completion(
    prompt: str,
    *,
    reduced_reasoning: bool = False,
    models: tuple[str, ...] | None = None,
    online: bool = False,
) -> tuple[str, dict]:
    """Call OpenRouter with `prompt`, no tools -- a plain text completion. Use this
    for long-form output (e.g. a full HTML document) that would otherwise get cut
    off by the free tier's ~1024-character cap on tool-call argument strings.

    `online=True` appends OpenRouter's `:online` suffix, which runs a web search on the
    prompt and injects the results before the model ever sees it. That search runs
    unconditionally and is billed per result, so it must only be set once something has
    already decided a lookup is genuinely needed -- see worker/codegen/research.py, which
    spends one free non-online call making that decision first.

    Returns (response_text, usage).
    """
    # Writing a site from scratch is creative work where deliberation earns its cost;
    # applying a stated change to an existing file is mechanical, so callers doing that
    # pass low_reasoning and stop paying for thinking they don't need.
    body = {"max_tokens": GENERATION_MAX_TOKENS, **(REDUCED_REASONING if reduced_reasoning else {})}
    candidates = models or GENERATION_MODELS
    if online:
        candidates = tuple(f"{m}:online" for m in candidates)
    data, model = await _request_with_retries(
        prompt, body, candidates, GENERATION_MAX_ATTEMPTS, problem_with=_plain_problem
    )
    # Guaranteed non-empty by _plain_problem, which the retry ladder enforced above.
    content = data["choices"][0]["message"]["content"]
    return content, _extract_usage(data, model)


async def _request_with_retries(
    prompt: str,
    body_extra: dict,
    models: tuple[str, ...],
    max_attempts: int,
    problem_with: Callable[[dict], str | None] | None = None,
) -> tuple[dict, str]:
    """Shared retry/model-fallback loop. Returns (response_json, model_used) for a
    response that is HTTP 200, carries no body-level error, and is actually usable.

    `problem_with` is what makes the third of those true. A 200 whose body is the wrong
    shape -- no content, no tool call, unparseable arguments -- used to be detected by the
    caller, *after* this function had already returned, so it escaped the retry ladder
    entirely: no second attempt, no fall back to the other model, straight to a failure the
    owner saw. Both real symptoms had that one cause. A build died on "Model returned no
    content" when the provider aborted mid-reasoning, and an edit died on "Model did not
    return a tool call" when the model deliberated past its token budget -- each a single
    unlucky roll that a plain retry would have absorbed. Checking here makes a
    wrong-shaped body exactly as retryable as a 502.
    """
    headers = {"Authorization": f"Bearer {get_settings().openrouter_api_key}"}
    body_base = {"messages": [{"role": "user", "content": prompt}], **body_extra}

    last_error: Exception | None = None
    for model in models:
        # Per-model, because the stealth pool needs a long ladder of cheap retries while
        # the nemotrons need a short one of expensive ones -- see STEALTH_MAX_ATTEMPTS.
        attempts_here = STEALTH_MAX_ATTEMPTS if model.startswith("stealth/") else max_attempts
        for attempt in range(1, attempts_here + 1):
            logger.info(
                "llm.attempt",
                extra={"event": "llm.attempt", "model": model, "attempt": attempt, "max_attempts": attempts_here},
            )
            body = {**body_base, "model": model}
            if model.startswith("stealth/") and "reasoning" not in body:
                body["reasoning"] = {"effort": STEALTH_DEFAULT_EFFORT}
            try:
                resp = await asyncio.to_thread(_post_sync_bounded, headers, body)
            except (httpx.HTTPError, concurrent.futures.TimeoutError) as exc:
                last_error = exc
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    # OpenRouter sometimes returns HTTP 200 with the failure embedded
                    # in the body instead of a proper error status (observed live: a
                    # transient 504 "Upstream idle timeout exceeded" arrived this way).
                    body_error = data.get("error")
                    if body_error is None:
                        problem = problem_with(data) if problem_with else None
                        if problem is None:
                            return data, model
                        logger.warning(
                            "llm.unusable",
                            extra={
                                "event": "llm.unusable", "model": model,
                                "attempt": attempt, "problem": problem,
                            },
                        )
                        last_error = OpenRouterCallFailed(f"{model}: {problem}")
                    else:
                        last_error = OpenRouterCallFailed(f"{model}: {body_error}")
                elif resp.status_code == 404:
                    # This model is gone from the free tier -- no point retrying it,
                    # move straight to the next candidate.
                    last_error = OpenRouterCallFailed(f"{model}: {resp.text}")
                    break
                elif resp.status_code == 429 and "per-day" in resp.text:
                    # A daily cap will not clear on a retry or a different model, so stop
                    # immediately rather than burning the retry ladder against a wall.
                    raise DailyLimitReached(f"OpenRouter daily request limit reached: {resp.text[:200]}")
                elif resp.status_code not in (429, 500, 502, 503, 504):
                    # A real request problem (bad schema, auth, etc.) -- retrying or
                    # falling back to another model won't help.
                    raise OpenRouterCallFailed(f"OpenRouter call failed ({resp.status_code}): {resp.text}")
                else:
                    last_error = OpenRouterCallFailed(f"{model} {resp.status_code}: {resp.text}")

            if attempt < attempts_here:
                await asyncio.sleep(min(2 ** (attempt - 1), MAX_BACKOFF_SECONDS))

    raise OpenRouterCallFailed(f"OpenRouter call failed after trying all model candidates: {last_error}")


def _post_sync(headers: dict, body: dict) -> httpx.Response:
    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        return client.post(API_URL, headers=headers, json=body)


def _post_sync_bounded(headers: dict, body: dict) -> httpx.Response:
    """Runs _post_sync in its own thread, bounded by Future.result(timeout=...) --
    independent of asyncio's cancellation machinery, which doesn't reliably fire here.

    Deliberately NOT a `with ThreadPoolExecutor(...)` block: its __exit__ calls
    shutdown(wait=True), which would block on the still-running orphaned thread for
    the same reason the timeout was needed in the first place. shutdown(wait=False)
    lets this function actually return promptly on timeout; the underlying thread
    can't be force-killed either way and finishes on its own in the background.
    """
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_post_sync, headers, body)
    try:
        return future.result(timeout=REQUEST_TIMEOUT_SECONDS + 10)
    finally:
        pool.shutdown(wait=False)


def _extract_usage(data: dict, model: str) -> dict:
    usage_raw = data.get("usage") or {}
    return {
        "model": model,
        "input_tokens": usage_raw.get("prompt_tokens", 0),
        "output_tokens": usage_raw.get("completion_tokens", 0),
    }


def _parse_tool_response(data: dict, model: str, valid_names: set[str]) -> tuple[dict, dict]:
    choices = data.get("choices") or []
    tool_calls = choices[0]["message"].get("tool_calls") if choices else None
    if not tool_calls:
        raise OpenRouterCallFailed(f"Model did not return a tool call: {data}")

    call = tool_calls[0]["function"]
    name = call["name"]
    if name not in valid_names:
        raise OpenRouterCallFailed(f"Model called an unrecognized tool: {name}")

    try:
        args = json.loads(call["arguments"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise OpenRouterCallFailed(f"Model returned malformed tool arguments: {call['arguments']!r}") from exc

    return {"operation": name, **args}, _extract_usage(data, model)
