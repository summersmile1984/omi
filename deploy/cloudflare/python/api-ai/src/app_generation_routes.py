"""Workers AI implementation of the app-generator sample prompt contract."""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from fallback import record_fallback
from internal_auth import decode_context

router = APIRouter()

DEFAULT_WORKERS_AI_APP_GENERATION_MODEL = "@cf/meta/llama-3.2-3b-instruct"
MAX_GENERATED_PROMPT_CHARS = 200
FALLBACK_PROMPTS = (
    "Mind map generator from conversations",
    "Jokes and funny moments extractor",
    "Key decisions and commitments tracker",
    "Elon Musk startup advisor clone",
    "Strict accountability coach",
)
SYSTEM_PROMPT = """Generate 5 creative and diverse ideas for apps that are either:
1. Conversation summary based apps - analyze user's recorded conversations and extract/organize information
2. Chat assistant based apps - AI personas or assistants users can chat with

Generate exactly 3 conversation-based and 2 chat-based app ideas.

Examples:
- Conversation based: "Mind map generator from my conversations", "Jokes and funny moments extractor", "Meeting action items tracker"
- Chat based: "Elon Musk personality clone", "Strict accountability mentor", "Socratic philosophy tutor"

Return ONLY a JSON array of 5 strings, each being a short app description (max 50 characters).
Format: ["idea 1", "idea 2", "idea 3", "idea 4", "idea 5"]

First 3 should be conversation-based, last 2 should be chat-based.
Be creative, fun, and varied. No generic ideas."""

MAX_APP_GENERATION_INPUT_CHARS = 2_000
MAX_DESCRIPTION_RESPONSE_CHARS = 8_000


class GenerateDescriptionRequest(BaseModel):
    name: str = Field(max_length=500)
    description: str = Field(max_length=MAX_APP_GENERATION_INPUT_CHARS)


class GenerateDescriptionEmojiRequest(BaseModel):
    name: str = Field(max_length=500)
    prompt: str = Field(max_length=MAX_APP_GENERATION_INPUT_CHARS)


DESCRIPTION_SYSTEM_PROMPT = """You are an AI assistant specializing in crafting detailed and engaging descriptions for apps.
You will be provided with an app's name and a brief description. Expand it into a captivating, concise,
professional description that highlights the app's features, functionality, and benefits. Use no more than
40 words and respond with only the description, tailored to the app's concept and purpose."""

DESCRIPTION_EMOJI_SYSTEM_PROMPT = """You are an AI assistant that creates app descriptions and selects representative emojis.
Given an app name and what it should do, respond with a JSON object containing:
1. "description": a concise, engaging description (max 40 words) highlighting what the app does
2. "emoji": a single emoji that best represents the app's purpose
Respond ONLY with the JSON object, no other text."""


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _fallback(*, reason: str) -> dict[str, list[str]]:
    record_fallback(
        from_mode="none",
        to_mode="none",
        reason=reason,
        outcome="degraded",
    )
    return {"prompts": list(FALLBACK_PROMPTS)}


def _response_text(value: object) -> str | None:
    if isinstance(value, dict):
        response = value.get("response")
    else:
        to_py = getattr(value, "to_py", None)
        converted = to_py() if callable(to_py) else None
        response = converted.get("response") if isinstance(converted, dict) else None
    if not isinstance(response, str):
        return None
    text = response.strip()
    return text if text else None


def _model_name(env: object) -> str | None:
    model = str(getattr(env, "WORKERS_AI_APP_GENERATION_MODEL", DEFAULT_WORKERS_AI_APP_GENERATION_MODEL) or "").strip()
    return model if 0 < len(model) <= 200 else None


async def _run_generation(
    env: object,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    temperature: float,
) -> tuple[str, object] | None:
    ai = getattr(env, "AI", None)
    model = _model_name(env)
    if ai is None or model is None:
        return None
    result = await ai.run(
        model,
        {
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
    )
    response = _response_text(result)
    return (response, result) if response is not None else None


def _record_generation_fallback(reason: str) -> None:
    record_fallback(
        from_mode="none",
        to_mode="none",
        reason=reason,
        outcome="degraded",
    )


def _parse_prompts(text: str) -> list[str] | None:
    content = text.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        content = "\n".join(lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:]).strip()
    try:
        payload = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, list) or len(payload) < 5:
        return None
    prompts = payload[:5]
    if not all(isinstance(prompt, str) and 0 < len(prompt.strip()) <= MAX_GENERATED_PROMPT_CHARS for prompt in prompts):
        return None
    return [prompt.strip() for prompt in prompts]


def _parse_description_emoji(text: str, fallback_description: str) -> dict[str, str]:
    content = text.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        content = "\n".join(lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:]).strip()
    try:
        payload = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        _record_generation_fallback("malformed_doc")
        return {"description": f"A custom app that {fallback_description}", "emoji": "✨"}
    if not isinstance(payload, dict):
        _record_generation_fallback("malformed_doc")
        return {"description": f"A custom app that {fallback_description}", "emoji": "✨"}
    description = payload.get("description")
    emoji = payload.get("emoji")
    if not isinstance(description, str) or not description.strip() or len(description) > MAX_DESCRIPTION_RESPONSE_CHARS:
        description = f"A custom app that {fallback_description}"
    if not isinstance(emoji, str) or not emoji.strip() or len(emoji) > 16:
        emoji = "✨"
    return {"description": description.strip(), "emoji": emoji.strip()}


async def _record_usage(env: object, uid: str, model: str, result: object) -> None:
    """Best-effort feature ledger entry; generation must not fail on accounting."""
    database = getattr(env, "APP_DB", None)
    if database is None:
        return
    usage = result.get("usage") if isinstance(result, dict) else None
    input_tokens = usage.get("prompt_tokens", 0) if isinstance(usage, dict) else 0
    output_tokens = usage.get("completion_tokens", 0) if isinstance(usage, dict) else 0
    if not isinstance(input_tokens, int) or isinstance(input_tokens, bool) or input_tokens < 0:
        input_tokens = 0
    if not isinstance(output_tokens, int) or isinstance(output_tokens, bool) or output_tokens < 0:
        output_tokens = 0
    now = int(time.time())
    usage_day = time.strftime("%Y-%m-%d", time.gmtime(now))
    try:
        await database.prepare(
            "INSERT INTO cf_llm_usage_daily "
            "(uid, usage_day, usage_kind, feature, model, account, input_tokens, output_tokens, "
            "total_tokens, call_count, updated_at) VALUES (?, ?, 'feature', 'app_generator', ?, 'omi', ?, ?, ?, 1, ?) "
            "ON CONFLICT(uid, usage_day, usage_kind, feature, model, account) DO UPDATE SET "
            "input_tokens = input_tokens + excluded.input_tokens, "
            "output_tokens = output_tokens + excluded.output_tokens, "
            "total_tokens = total_tokens + excluded.total_tokens, "
            "call_count = call_count + 1, updated_at = excluded.updated_at"
        ).bind(uid, usage_day, model, input_tokens, output_tokens, input_tokens + output_tokens, now).run()
    except Exception:
        return


@router.get("/v1/app/generate-prompts")
async def generate_sample_prompts(request: Request):
    """Generate app-builder ideas, retaining the legacy static fallback."""
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    env = request.scope["env"]
    ai = getattr(env, "AI", None)
    if ai is None:
        return _fallback(reason="dependency_unavailable")
    model = str(getattr(env, "WORKERS_AI_APP_GENERATION_MODEL", DEFAULT_WORKERS_AI_APP_GENERATION_MODEL) or "").strip()
    if not model or len(model) > 200:
        return _fallback(reason="dependency_unavailable")
    try:
        result = await ai.run(
            model,
            {
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": "Generate 5 creative app ideas now"},
                ],
                "stream": False,
                "max_tokens": 192,
                "temperature": 0.8,
            },
        )
        response = _response_text(result)
        prompts = _parse_prompts(response) if response is not None else None
        if prompts is None:
            return _fallback(reason="malformed_doc")
        await _record_usage(env, str(context["uid"]), model, result)
        return {"prompts": prompts}
    except Exception:
        return _fallback(reason="dependency_unavailable")


@router.post("/v1/app/generate-description")
async def generate_description(request: Request, payload: GenerateDescriptionRequest):
    """Expand a short app description using the native Workers AI binding."""
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    name = payload.name.strip()
    description = payload.description.strip()
    if not name:
        return JSONResponse({"detail": "App Name is required"}, status_code=422)
    if not description:
        return JSONResponse({"detail": "App Description is required"}, status_code=422)
    try:
        generated = await _run_generation(
            request.scope["env"],
            [
                {"role": "system", "content": DESCRIPTION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"App Name: {name}\nDescription: {description}",
                },
            ],
            max_tokens=160,
            temperature=0.4,
        )
    except Exception:
        generated = None
    if generated is None:
        return JSONResponse({"error": "app description generation unavailable"}, status_code=502)
    response, result = generated
    await _record_usage(
        request.scope["env"], str(context["uid"]), _model_name(request.scope["env"]) or "unknown", result
    )
    return {"description": response[:MAX_DESCRIPTION_RESPONSE_CHARS]}


@router.post("/v1/app/generate-description-emoji")
async def generate_description_and_emoji(request: Request, payload: GenerateDescriptionEmojiRequest):
    """Generate an app description and representative emoji."""
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    name = payload.name.strip()
    prompt = payload.prompt.strip()
    if not name:
        return JSONResponse({"detail": "App Name is required"}, status_code=422)
    if not prompt:
        return JSONResponse({"detail": "App Prompt is required"}, status_code=422)
    try:
        generated = await _run_generation(
            request.scope["env"],
            [
                {"role": "system", "content": DESCRIPTION_EMOJI_SYSTEM_PROMPT},
                {"role": "user", "content": f"App Name: {name}\nWhat it does: {prompt}"},
            ],
            max_tokens=160,
            temperature=0.5,
        )
    except Exception:
        generated = None
    if generated is None:
        return JSONResponse({"error": "app description generation unavailable"}, status_code=502)
    response, result = generated
    await _record_usage(
        request.scope["env"], str(context["uid"]), _model_name(request.scope["env"]) or "unknown", result
    )
    return _parse_description_emoji(response, prompt)


__all__ = [
    "generate_description",
    "generate_description_and_emoji",
    "generate_sample_prompts",
    "router",
]
