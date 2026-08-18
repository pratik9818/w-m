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
import sys

import httpx

from bot_api.config import get_settings

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
GENERATION_MODELS = (
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
)
# Edit parsing (forced tool call) is latency-bound -- the owner is waiting in the chat --
# and only needs to pick one short operation, so the fast model leads. Both entries are
# verified live to support inference-enforced tool_choice; not every free model does
# (openai/gpt-oss-20b:free 400s on it).
TOOL_MODELS = ("nvidia/nemotron-3-nano-30b-a3b:free", "nvidia/nemotron-3-super-120b-a12b:free")

TOOL_MAX_ATTEMPTS = 3
# Only 2 for generation: a retry against the slow primary costs ~7 minutes, so burning
# three of them before falling back would leave the owner waiting far too long. Two
# attempts still absorbs a transient blip, then hands over to the fast fallback.
GENERATION_MAX_ATTEMPTS = 2

# No single call now produces a whole site -- the largest is one stylesheet or two pages --
# so this is back to a ceiling for a hung connection rather than a real expected duration.
REQUEST_TIMEOUT_SECONDS = 300
# A four-page site plus its stylesheet runs well past the default response budget; all
# the generation candidates advertise >=65k max completion tokens.
GENERATION_MAX_TOKENS = 32000


class OpenRouterCallFailed(Exception):
    pass


class DailyLimitReached(OpenRouterCallFailed):
    """The provider's per-day request cap is exhausted, not a fault in our request.

    Worth its own type because it is the one failure that resolves itself with time:
    reported as a generic "problem generating your website", it sends the owner chasing
    a bug that does not exist.
    """


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
    }
    data, model = await _request_with_retries(prompt, body_extra, TOOL_MODELS, TOOL_MAX_ATTEMPTS)
    return _parse_tool_response(data, model, valid_names)


async def call_plain_completion(prompt: str) -> tuple[str, dict]:
    """Call OpenRouter with `prompt`, no tools -- a plain text completion. Use this
    for long-form output (e.g. a full HTML document) that would otherwise get cut
    off by the free tier's ~1024-character cap on tool-call argument strings.

    Returns (response_text, usage).
    """
    data, model = await _request_with_retries(
        prompt, {"max_tokens": GENERATION_MAX_TOKENS}, GENERATION_MODELS, GENERATION_MAX_ATTEMPTS
    )
    choices = data.get("choices") or []
    content = choices[0]["message"].get("content") if choices else None
    if not content:
        raise OpenRouterCallFailed(f"Model returned no content: {data}")
    return content, _extract_usage(data, model)


async def _request_with_retries(
    prompt: str, body_extra: dict, models: tuple[str, ...], max_attempts: int
) -> tuple[dict, str]:
    """Shared retry/model-fallback loop. Returns (response_json, model_used) for a
    successful (HTTP 200, no body-level error) response.
    """
    headers = {"Authorization": f"Bearer {get_settings().openrouter_api_key}"}
    body_base = {"messages": [{"role": "user", "content": prompt}], **body_extra}

    last_error: Exception | None = None
    for model in models:
        for attempt in range(1, max_attempts + 1):
            print(f"[openrouter] {model} attempt {attempt}/{max_attempts}...", file=sys.stderr, flush=True)
            try:
                resp = await asyncio.to_thread(_post_sync_bounded, headers, {**body_base, "model": model})
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
                        return data, model
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

            if attempt < max_attempts:
                await asyncio.sleep(2 ** (attempt - 1))

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
