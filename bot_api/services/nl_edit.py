import asyncio
import json

from google import genai
from google.genai import errors, types

from bot_api.config import get_settings
from db.models import Business
from worker.codegen.builder import spec_from_business

MODEL = "gemini-flash-latest"
MAX_ATTEMPTS = 3

OPERATIONS = (
    "update_business_info",
    "add_service",
    "update_service",
    "remove_service",
    "clarify",
    "not_an_edit",
)

PROMPT_TEMPLATE = """You manage edits to a small business's website via chat. The owner just sent you a message. Your job is to turn it into exactly one structured operation by calling one of the available functions.

Current site content:
{spec_json}

Owner's message:
{raw_message}

Rules:
- Only change fields the owner actually mentioned — never invent or guess unmentioned content.
- To update simple site info (name, tagline, about, phone, email, address, theme, or hours), call update_business_info with only the fields that changed.
- theme must be exactly one of: classic, modern, bold.
- To add/change/remove something in the services list, use add_service / update_service / remove_service. Refer to an existing service by its current name exactly as shown above.
- If the request is genuinely ambiguous, or asks for something not supported here (e.g. changing the business category, uploading photos, anything not covered by the functions above), call clarify with a short, specific question or explanation.
- If the message isn't a request to change the site at all (e.g. a greeting, thanks, or unrelated question), call not_an_edit.
"""

UPDATE_BUSINESS_INFO_FUNCTION = types.FunctionDeclaration(
    name="update_business_info",
    description="Update one or more simple site fields. Only include fields the owner actually mentioned.",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "name": types.Schema(type=types.Type.STRING),
            "tagline": types.Schema(type=types.Type.STRING),
            "about": types.Schema(type=types.Type.STRING),
            "phone": types.Schema(type=types.Type.STRING),
            "email": types.Schema(type=types.Type.STRING),
            "address": types.Schema(type=types.Type.STRING),
            "theme": types.Schema(type=types.Type.STRING, description="classic, modern, or bold"),
            "hours": types.Schema(type=types.Type.STRING, description="free-text hours, e.g. 'Mon-Fri 9-6'"),
        },
    ),
)

ADD_SERVICE_FUNCTION = types.FunctionDeclaration(
    name="add_service",
    description="Add a new service or product to the site.",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "name": types.Schema(type=types.Type.STRING),
            "price_label": types.Schema(type=types.Type.STRING),
        },
        required=["name"],
    ),
)

UPDATE_SERVICE_FUNCTION = types.FunctionDeclaration(
    name="update_service",
    description="Change an existing service's name and/or price.",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "current_name": types.Schema(type=types.Type.STRING, description="the service's current name"),
            "new_name": types.Schema(type=types.Type.STRING),
            "new_price_label": types.Schema(type=types.Type.STRING),
        },
        required=["current_name"],
    ),
)

REMOVE_SERVICE_FUNCTION = types.FunctionDeclaration(
    name="remove_service",
    description="Remove an existing service from the site.",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={"name": types.Schema(type=types.Type.STRING, description="the service's current name")},
        required=["name"],
    ),
)

CLARIFY_FUNCTION = types.FunctionDeclaration(
    name="clarify",
    description="The request is ambiguous or not supported by the other functions -- ask the owner a question instead of guessing.",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={"question": types.Schema(type=types.Type.STRING)},
        required=["question"],
    ),
)

NOT_AN_EDIT_FUNCTION = types.FunctionDeclaration(
    name="not_an_edit",
    description="The message is not a request to change the site (e.g. a greeting or thanks).",
    parameters=types.Schema(type=types.Type.OBJECT, properties={}),
)

GENERATE_CONFIG = types.GenerateContentConfig(
    tools=[
        types.Tool(
            function_declarations=[
                UPDATE_BUSINESS_INFO_FUNCTION,
                ADD_SERVICE_FUNCTION,
                UPDATE_SERVICE_FUNCTION,
                REMOVE_SERVICE_FUNCTION,
                CLARIFY_FUNCTION,
                NOT_AN_EDIT_FUNCTION,
            ]
        )
    ],
    tool_config=types.ToolConfig(
        function_calling_config=types.FunctionCallingConfig(
            mode=types.FunctionCallingConfigMode.ANY,
            allowed_function_names=list(OPERATIONS),
        )
    ),
)


class EditParseFailed(Exception):
    pass


async def parse_edit_message(raw_message: str, business: Business) -> dict:
    """Parse a free-text edit message into a structured operation.

    Returns {"operation": <name>, **args}. A successful "clarify" or "not_an_edit"
    call is a valid result, not a failure -- EditParseFailed is only raised on a
    technical failure (API errors, exhausted retries, malformed response).
    """
    spec_json = json.dumps(spec_from_business(business), indent=2, ensure_ascii=False)
    prompt = PROMPT_TEMPLATE.format(spec_json=spec_json, raw_message=raw_message)
    client = genai.Client(api_key=get_settings().gemini_api_key)

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await client.aio.models.generate_content(
                model=MODEL, contents=prompt, config=GENERATE_CONFIG
            )
        except errors.APIError as exc:
            if exc.code < 500:
                raise EditParseFailed(f"Edit parsing failed: {exc}") from exc
            last_error = exc
        else:
            call = next(
                (
                    part.function_call
                    for cand in response.candidates or []
                    for part in cand.content.parts or []
                    if part.function_call is not None
                ),
                None,
            )
            if call is None or call.name not in OPERATIONS:
                last_error = EditParseFailed(f"Model did not return a recognized operation: {call}")
            else:
                return {"operation": call.name, **dict(call.args or {})}

        if attempt < MAX_ATTEMPTS:
            await asyncio.sleep(2 ** (attempt - 1))

    raise EditParseFailed(f"Edit parsing failed after {MAX_ATTEMPTS} attempts: {last_error}")
