import asyncio
import json
import uuid
from pathlib import Path
from string import Template

from google import genai
from google.genai import errors, types
from sqlalchemy.ext.asyncio import AsyncSession

from bot_api.config import get_settings
from db.models import Business
from worker.codegen.quota import check_quota, record_usage

# Alias, not a pinned snapshot: always resolves to Google's current recommended Flash
# model (verified live against the API; gemini-2.5-flash itself is retired for new
# accounts as of this session). The actual resolved model name is recorded per-call
# via response.model_version, not this constant.
MODEL = "gemini-flash-latest"
MAX_ATTEMPTS = 3

PROMPT_PATH = Path(__file__).parent / "prompts" / "site_builder.md"

THEME_GUIDANCE = {
    "classic": (
        "Warm, traditional feel: neutral/cream background, a warm accent color "
        "(e.g. deep red, burgundy, or forest green), serif headings (e.g. Georgia, "
        "'Times New Roman'), sans-serif body text, generous but conventional spacing, "
        "rounded-corner cards."
    ),
    "modern": (
        "Minimal and clean: white or very light neutral background, one confident accent "
        "color, sans-serif throughout (e.g. system-ui, Helvetica), generous whitespace, "
        "understated borders/shadows, grid-based layout."
    ),
    "bold": (
        "High-contrast and energetic: dark background with a single vivid accent color "
        "(e.g. orange, electric blue, hot pink), large oversized headings, strong visual "
        "hierarchy, punchy call-to-action styling."
    ),
}

# Gemini function-parameter names must be safe identifiers (no dots) — map to real filenames after.
PARAM_TO_FILENAME = {"index_html": "index.html", "style_css": "style.css"}

WRITE_SITE_FUNCTION = types.FunctionDeclaration(
    name="write_site",
    description="Write the generated website files.",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "index_html": types.Schema(type=types.Type.STRING, description="The full HTML document."),
            "style_css": types.Schema(type=types.Type.STRING, description="The full CSS stylesheet."),
        },
        required=list(PARAM_TO_FILENAME),
    ),
)

GENERATE_CONFIG = types.GenerateContentConfig(
    tools=[types.Tool(function_declarations=[WRITE_SITE_FUNCTION])],
    tool_config=types.ToolConfig(
        function_calling_config=types.FunctionCallingConfig(
            mode=types.FunctionCallingConfigMode.ANY,
            allowed_function_names=["write_site"],
        )
    ),
)


class GenerationFailed(Exception):
    pass


def spec_from_business(business: Business) -> dict:
    logo_url = next((m.url for m in business.media if m.kind == "logo"), None)
    photo_urls = [m.url for m in business.media if m.kind == "photo"]
    return {
        "name": business.name,
        "category": business.category,
        "tagline": business.tagline,
        "about": business.about,
        "theme": business.theme,
        "phone": business.phone,
        "email": business.email,
        "address": business.address,
        "hours": business.hours.get("display_text") if business.hours else None,
        "services": [
            {"name": s.name, "price_label": s.price_label} for s in business.services if s.is_active
        ],
        "logo_url": logo_url,
        "photo_urls": photo_urls,
    }


def _build_prompt(spec: dict) -> str:
    theme = spec.get("theme") or "classic"
    guidance = THEME_GUIDANCE.get(theme, THEME_GUIDANCE["classic"])
    spec_json = json.dumps(spec, indent=2, ensure_ascii=False)
    template = Template(PROMPT_PATH.read_text(encoding="utf-8"))
    return template.substitute(theme_guidance=guidance, spec_json=spec_json)


async def build_site(
    spec: dict,
    *,
    session: AsyncSession | None = None,
    owner_telegram_id: int | None = None,
    business_id: uuid.UUID | None = None,
) -> tuple[dict[str, str], dict]:
    """Generate a two-file static site from a business spec.

    If owner_telegram_id is given, the caller's token quota is checked before
    calling the API and the actual usage is recorded against it afterward.
    Returns (files, usage) where usage is {"model", "input_tokens", "output_tokens"}.
    """
    if owner_telegram_id is not None:
        assert session is not None, "session is required when owner_telegram_id is given"
        await check_quota(session, owner_telegram_id)

    prompt = _build_prompt(spec)
    client = genai.Client(api_key=get_settings().gemini_api_key)

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await client.aio.models.generate_content(
                model=MODEL, contents=prompt, config=GENERATE_CONFIG
            )
        except errors.APIError as exc:
            if exc.code < 500:
                raise GenerationFailed(f"Site generation failed: {exc}") from exc
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
            if call is None:
                last_error = GenerationFailed("Model did not return a write_site function call.")
            else:
                args = dict(call.args or {})
                files = {
                    filename: args.get(param) for param, filename in PARAM_TO_FILENAME.items()
                }
                missing = [k for k, v in files.items() if not v]
                if missing:
                    last_error = GenerationFailed(f"Model response missing required file(s): {missing}")
                else:
                    usage_meta = response.usage_metadata
                    input_tokens = usage_meta.prompt_token_count or 0
                    output_tokens = usage_meta.candidates_token_count or 0
                    resolved_model = response.model_version or MODEL
                    if owner_telegram_id is not None:
                        await record_usage(
                            session,
                            owner_telegram_id,
                            business_id,
                            resolved_model,
                            input_tokens,
                            output_tokens,
                        )
                    usage = {
                        "model": resolved_model,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    }
                    return files, usage

        if attempt < MAX_ATTEMPTS:
            await asyncio.sleep(2 ** (attempt - 1))

    raise GenerationFailed(f"Site generation failed after {MAX_ATTEMPTS} attempts: {last_error}")
