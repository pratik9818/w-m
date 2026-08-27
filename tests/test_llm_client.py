"""The one place the bot talks to a model.

The client this replaced carried a ladder of free candidate models and fell between them
when one failed. That machinery is gone: the SDK retries, and there is one model. What is
left worth testing is the shape of the two calls, and that the three failures the bot
treats differently are still told apart -- rate limiting resolves itself with time, an
exhausted account does not, and everything else is a fault.
"""

import anthropic
import pytest

from bot_api.services import llm_client


class FakeUsage:
    def __init__(self, input_tokens=100, output_tokens=50):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeBlock:
    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class FakeMessage:
    def __init__(self, content, stop_reason="end_turn", model=llm_client.MODEL):
        self.content = content
        self.stop_reason = stop_reason
        self.model = model
        self.usage = FakeUsage()


def _text(body):
    return FakeMessage([FakeBlock("text", text=body)])


def _tool(name="patch_site", data=None):
    return FakeMessage(
        [FakeBlock("tool_use", name=name, input=data if data is not None else {"instruction": "x"})],
        stop_reason="tool_use",
    )


@pytest.fixture
def api(monkeypatch):
    """Stand in for the Anthropic client, recording the request it was given."""
    sent = {}

    class FakeStream:
        def __init__(self, message):
            self._message = message

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get_final_message(self):
            if isinstance(self._message, Exception):
                raise self._message
            return self._message

    class FakeMessages:
        def __init__(self, outcome):
            self._outcome = outcome

        async def create(self, **kwargs):
            sent.update(kwargs)
            if isinstance(self._outcome, Exception):
                raise self._outcome
            return self._outcome

        def stream(self, **kwargs):
            sent.update(kwargs)
            return FakeStream(self._outcome)

    class FakeClient:
        def __init__(self, outcome):
            self.messages = FakeMessages(outcome)

    def install(outcome):
        monkeypatch.setattr(llm_client, "_client", lambda: FakeClient(outcome))

    monkeypatch.setattr(
        llm_client, "get_settings", lambda: type("S", (), {"anthropic_api_key": "k"})()
    )
    sent["install"] = install
    return sent


def _make(cls, message):
    exc = cls.__new__(cls)
    Exception.__init__(exc, message)
    return exc


# ------------------------------------------------------------------ forced tool calls

@pytest.mark.asyncio
async def test_a_tool_call_comes_back_as_an_operation(api):
    api["install"](_tool("patch_site", {"instruction": "make it bigger", "targets": ["index.html"]}))

    op, usage = await llm_client.call_forced_tool(
        "make the heading bigger", [{"name": "patch_site", "description": "d", "parameters": {}}]
    )

    assert op == {"operation": "patch_site", "instruction": "make it bigger",
                  "targets": ["index.html"]}
    assert usage["model"] == llm_client.MODEL
    assert usage["input_tokens"] == 100


@pytest.mark.asyncio
async def test_exactly_one_tool_call_is_demanded(api):
    """Every caller here wants a single decision. Left to itself the model may answer with
    several calls at once, and the code reads only the first."""
    api["install"](_tool())
    await llm_client.call_forced_tool("x", [{"name": "patch_site", "parameters": {}}])

    assert api["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}


@pytest.mark.asyncio
async def test_the_callers_tool_shape_is_translated(api):
    """Callers describe tools with `parameters`, which is what they were written against.
    Anthropic wants `input_schema`; translating here keeps that out of six modules."""
    api["install"](_tool())
    schema = {"type": "object", "properties": {"instruction": {"type": "string"}}}

    await llm_client.call_forced_tool(
        "x", [{"name": "patch_site", "description": "d", "parameters": schema}]
    )

    assert api["tools"] == [{"name": "patch_site", "description": "d", "input_schema": schema}]


@pytest.mark.asyncio
async def test_an_unknown_tool_name_is_refused(api):
    api["install"](_tool("something_else"))
    with pytest.raises(llm_client.LLMCallFailed, match="unrecognized"):
        await llm_client.call_forced_tool("x", [{"name": "patch_site", "parameters": {}}])


@pytest.mark.asyncio
async def test_a_turn_with_no_tool_call_is_a_failure_not_a_silent_pass(api):
    api["install"](FakeMessage([FakeBlock("text", text="I think...")], stop_reason="max_tokens"))
    with pytest.raises(llm_client.LLMCallFailed, match="no tool call"):
        await llm_client.call_forced_tool("x", [{"name": "patch_site", "parameters": {}}])


# ------------------------------------------------------------------ plain completions

@pytest.mark.asyncio
async def test_a_page_comes_back_as_text(api):
    api["install"](_text("<!DOCTYPE html><html></html>"))

    text, usage = await llm_client.call_plain_completion("write me a page")

    assert text == "<!DOCTYPE html><html></html>"
    assert usage["output_tokens"] == 50


@pytest.mark.asyncio
async def test_generation_is_streamed(api):
    """A 32k-token response is past the point where one plain HTTP request is a sensible
    way to wait for it."""
    api["install"](_text("<html></html>"))
    await llm_client.call_plain_completion("write me a page")
    assert api["max_tokens"] == llm_client.GENERATION_MAX_TOKENS


@pytest.mark.asyncio
async def test_mechanical_edits_do_not_pay_for_deliberation(api):
    """Writing a site is the work; applying a stated change to a file is not."""
    api["install"](_text("<html></html>"))
    await llm_client.call_plain_completion("apply this change", reduced_reasoning=True)
    assert api["output_config"] == {"effort": llm_client.REDUCED_EFFORT}

    api["install"](_text("<html></html>"))
    await llm_client.call_plain_completion("write a whole site")
    assert api["output_config"] == {"effort": llm_client.GENERATION_EFFORT}


@pytest.mark.asyncio
async def test_a_lookup_gets_web_search_and_a_build_does_not(api):
    api["install"](_text("- a fact"))
    await llm_client.call_plain_completion("look this up", online=True)
    assert api["tools"] == [{"type": "web_search_20260209", "name": "web_search"}]

    api.pop("tools")
    api["install"](_text("<html></html>"))
    await llm_client.call_plain_completion("write a page")
    assert "tools" not in api, "a build must not pay for a search it never asked for"


@pytest.mark.asyncio
async def test_an_empty_response_is_a_failure(api):
    """Seen on the old provider: a 200 with the reasoning cut off and no content at all.
    Returning "" would put an empty page in front of an owner."""
    api["install"](FakeMessage([], stop_reason="max_tokens"))
    with pytest.raises(llm_client.LLMCallFailed, match="no text"):
        await llm_client.call_plain_completion("write me a page")


# ------------------------------------------------------------------ failures worth telling apart

@pytest.mark.asyncio
async def test_rate_limiting_is_reported_as_the_kind_that_clears(api):
    """Reported as a generic fault it sends the owner chasing a bug that does not exist."""
    api["install"](_make(anthropic.RateLimitError, "429 rate limited"))
    with pytest.raises(llm_client.DailyLimitReached):
        await llm_client.call_forced_tool("x", [{"name": "t", "parameters": {}}])


@pytest.mark.asyncio
async def test_an_empty_account_is_its_own_failure(api):
    """Nothing in the code or the request can fix this one -- it waits on a person topping
    up the account -- so it must be legible in the log rather than one more 400."""
    api["install"](_make(
        anthropic.BadRequestError,
        "Error code: 400 - Your credit balance is too low to access the Anthropic API.",
    ))
    with pytest.raises(llm_client.CreditExhausted):
        await llm_client.call_plain_completion("write me a page")


@pytest.mark.asyncio
async def test_credit_exhaustion_is_still_caught_by_existing_handlers(api):
    """Every caller already catches LLMCallFailed; the new type must not slip past them."""
    assert issubclass(llm_client.CreditExhausted, llm_client.LLMCallFailed)
    assert issubclass(llm_client.DailyLimitReached, llm_client.LLMCallFailed)


@pytest.mark.asyncio
async def test_an_ordinary_api_error_is_a_plain_failure(api):
    api["install"](_make(anthropic.BadRequestError, "Error code: 400 - malformed tool schema"))
    with pytest.raises(llm_client.LLMCallFailed) as caught:
        await llm_client.call_plain_completion("write me a page")
    assert not isinstance(caught.value, (llm_client.CreditExhausted, llm_client.DailyLimitReached))


def test_the_model_is_the_one_that_was_asked_for():
    assert llm_client.MODEL == "claude-sonnet-5"
