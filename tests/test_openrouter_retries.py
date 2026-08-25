"""A 200 whose body is the wrong shape must retry, not fail the owner's request.

Both live failures had this one cause. A build died on "Model returned no content" when
the provider aborted mid-reasoning; an edit died on "Model did not return a tool call"
when the model deliberated past its token budget. Each was a single unlucky roll, checked
by the caller *after* the retry ladder had already returned, so neither ever got the
second attempt or the fallback model that existed for exactly this.
"""

import json

import pytest

from bot_api.services import openrouter_client as client


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _completion(content):
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }


ABORTED = {
    # Exactly what the provider returned on the build that failed: 200, no content, and
    # the reasoning cut off partway through.
    "choices": [{"finish_reason": "error", "message": {"content": None, "reasoning": "We need"}}],
    "usage": {"prompt_tokens": 3939, "completion_tokens": 98},
}


def _tool_reply(name="patch_site", arguments='{"instruction": "x", "targets": ["index.html"]}'):
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {"tool_calls": [{"function": {"name": name, "arguments": arguments}}]},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }


NO_TOOL_CALL = {
    # The model reasoned until its budget ran out and never called anything.
    "choices": [{"finish_reason": "length", "message": {"content": "Let me think..."}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 16000},
}

TOOLS = [{"name": "patch_site", "description": "d", "parameters": {"type": "object"}}]


@pytest.fixture
def responses(monkeypatch):
    """Serve canned responses in order; record the model each attempt used."""
    served = {"models": []}

    def install(payloads):
        queue = list(payloads)

        def fake_post(headers, body):
            served["models"].append(body["model"])
            served["max_tokens"] = body.get("max_tokens")
            served["reasoning"] = body.get("reasoning")
            item = queue.pop(0)
            # A queue entry is either a bare 200 payload or a ready-made FakeResponse,
            # for the tests that care about the status code.
            return item if isinstance(item, FakeResponse) else FakeResponse(item)

        monkeypatch.setattr(client, "_post_sync_bounded", fake_post)

    monkeypatch.setattr(client.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(
        client, "get_settings", lambda: type("S", (), {"openrouter_api_key": "k"})()
    )
    served["install"] = install
    return served


async def _no_sleep(_seconds):
    return None


# ------------------------------------------------------------------ plain completions

@pytest.mark.asyncio
async def test_an_aborted_response_is_retried_instead_of_failing(responses):
    responses["install"]([ABORTED, _completion("the real site")])
    content, usage = await client.call_plain_completion("build me a site")
    assert content == "the real site"
    assert len(responses["models"]) == 2


def _attempts(model):
    """How many attempts the ladder spends on one candidate, per model."""
    if model.startswith("stealth/"):
        return client.STEALTH_MAX_ATTEMPTS
    return client.GENERATION_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_a_model_that_keeps_aborting_falls_back_to_the_next_one(responses):
    # Every attempt on the primary aborts, then the second candidate answers.
    primary_attempts = _attempts(client.GENERATION_MODELS[0])
    responses["install"]([ABORTED] * primary_attempts + [_completion("from the fallback")])
    content, _ = await client.call_plain_completion("build me a site")
    assert content == "from the fallback"
    assert responses["models"][:primary_attempts] == [client.GENERATION_MODELS[0]] * primary_attempts
    assert responses["models"][-1] == client.GENERATION_MODELS[1]


@pytest.mark.asyncio
async def test_it_still_gives_up_when_every_candidate_aborts(responses):
    total = sum(_attempts(m) for m in client.GENERATION_MODELS)
    responses["install"]([ABORTED] * total)
    with pytest.raises(client.OpenRouterCallFailed):
        await client.call_plain_completion("build me a site")
    assert len(responses["models"]) == total  # every candidate's whole ladder, all spent


@pytest.mark.asyncio
async def test_the_stealth_pool_gets_its_longer_ladder_before_the_fallback(responses):
    """The stealth model answers only every few tries -- measured live, the generation
    prompt got through on attempt 3 and the tool call on attempt 6, each rejection
    arriving in seconds as a shared-pool 429. On the 2-attempt ladder the other models
    use, it would be abandoned before it ever answered."""
    rate_limited = FakeResponse({"error": "temporarily rate-limited upstream"}, status_code=429)
    responses["install"](
        [rate_limited] * client.STEALTH_MAX_ATTEMPTS + [_completion("from the fallback")]
    )
    content, _ = await client.call_plain_completion("build me a site")
    assert content == "from the fallback"
    stealth_tries = [m for m in responses["models"] if m.startswith("stealth/")]
    assert len(stealth_tries) == client.STEALTH_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_the_stealth_model_never_runs_at_its_own_default_effort(responses):
    """Left to itself it picks "max": 236s and 9,914 output tokens on one page prompt,
    against a 300s request ceiling. A generation call asks for no particular effort, so
    the ladder has to fill one in."""
    responses["install"]([_completion("done")])
    await client.call_plain_completion("build me a site")
    assert responses["models"][0].startswith("stealth/")
    assert responses["reasoning"] == {"effort": client.STEALTH_DEFAULT_EFFORT}


@pytest.mark.asyncio
async def test_an_explicit_reasoning_effort_is_left_alone(responses):
    responses["install"]([_completion("done")])
    await client.call_plain_completion("tweak this file", reduced_reasoning=True)
    assert responses["reasoning"] == client.REDUCED_REASONING["reasoning"]


@pytest.mark.asyncio
async def test_a_good_first_answer_costs_exactly_one_request(responses):
    responses["install"]([_completion("done")])
    await client.call_plain_completion("build me a site")
    assert len(responses["models"]) == 1


# ------------------------------------------------------------------ tool calls

@pytest.mark.asyncio
async def test_a_missing_tool_call_is_retried(responses):
    responses["install"]([NO_TOOL_CALL, _tool_reply()])
    op, _ = await client.call_forced_tool("edit my site", TOOLS)
    assert op["operation"] == "patch_site"
    assert len(responses["models"]) == 2


@pytest.mark.asyncio
async def test_malformed_tool_arguments_are_retried(responses):
    responses["install"]([_tool_reply(arguments="{not json"), _tool_reply()])
    op, _ = await client.call_forced_tool("edit my site", TOOLS)
    assert op["operation"] == "patch_site"


@pytest.mark.asyncio
async def test_an_unrecognized_tool_is_retried(responses):
    responses["install"]([_tool_reply(name="something_else"), _tool_reply()])
    op, _ = await client.call_forced_tool("edit my site", TOOLS)
    assert op["operation"] == "patch_site"


@pytest.mark.asyncio
async def test_tool_calls_are_given_room_to_finish_reasoning(responses):
    responses["install"]([_tool_reply()])
    await client.call_forced_tool("edit my site", TOOLS)
    # Sent with no budget at all, the provider's default ceiling cut the model off
    # mid-deliberation and no tool call was ever emitted.
    assert responses["max_tokens"] == client.TOOL_MAX_TOKENS
