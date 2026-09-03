"""Workers AI implementation of the app-generator sample prompt contract."""

from __future__ import annotations

import base64
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
ICON_MODEL = "@cf/black-forest-labs/flux-1-schnell"
MAX_ICON_PROMPT_CHARS = 2_048


class GenerateDescriptionRequest(BaseModel):
    name: str = Field(max_length=500)
    description: str = Field(max_length=MAX_APP_GENERATION_INPUT_CHARS)


class GenerateDescriptionEmojiRequest(BaseModel):
    name: str = Field(max_length=500)
    prompt: str = Field(max_length=MAX_APP_GENERATION_INPUT_CHARS)


class GenerateAppRequest(BaseModel):
    prompt: str = Field(max_length=MAX_APP_GENERATION_INPUT_CHARS)


class GenerateAppIconRequest(BaseModel):
    name: str = Field(default="", max_length=500)
    description: str = Field(default="", max_length=2_000)
    category: str = Field(default="other", max_length=200)


DESCRIPTION_SYSTEM_PROMPT = """You are an AI assistant specializing in crafting detailed and engaging descriptions for apps.
You will be provided with an app's name and a brief description. Expand it into a captivating, concise,
professional description that highlights the app's features, functionality, and benefits. Use no more than
40 words and respond with only the description, tailored to the app's concept and purpose."""

DESCRIPTION_EMOJI_SYSTEM_PROMPT = """You are an AI assistant that creates app descriptions and selects representative emojis.
Given an app name and what it should do, respond with a JSON object containing:
1. "description": a concise, engaging description (max 40 words) highlighting what the app does
2. "emoji": a single emoji that best represents the app's purpose
Respond ONLY with the JSON object, no other text."""

APP_CATEGORIES = (
    ("Conversation Analysis", "conversation-analysis"),
    ("Personality Clone", "personality-emulation"),
    ("Health", "health-and-wellness"),
    ("Education", "education-and-learning"),
    ("Communication", "communication-improvement"),
    ("Emotional Support", "emotional-and-mental-support"),
    ("Productivity", "productivity-and-organization"),
    ("Entertainment", "entertainment-and-fun"),
    ("Financial", "financial"),
    ("Travel", "travel-and-exploration"),
    ("Safety", "safety-and-security"),
    ("Shopping", "shopping-and-commerce"),
    ("Social", "social-and-relationships"),
    ("News", "news-and-information"),
    ("Utilities", "utilities-and-tools"),
    ("Other", "other"),
)
APP_GENERATOR_SYSTEM_PROMPT = """You are an expert app designer for Omi, an AI-powered wearable device that records conversations and provides intelligent insights.

Design an app based on the user's description. Omi apps can have two capabilities:
1. Chat apps (capability: "chat"): a persona or assistant with a detailed chat_prompt.
2. Conversation/memory apps (capability: "memories"): analysis with a detailed memory_prompt.
An app can have both capabilities when appropriate.

Available categories (use the id exactly):
{categories}

Return only valid JSON with this structure:
{{"name":"short catchy name (max 30 chars)","description":"compelling description (50-150 words)","category":"category-id","capabilities":["chat"],"chat_prompt":"...","memory_prompt":"..."}}
Only include chat_prompt when chat is present and memory_prompt when memories is present."""


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


def _json_candidates(text: str):
    """Yield the complete model response and bounded JSON-looking slices."""
    content = text.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        content = "\n".join(lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:]).strip()
    if content:
        yield content
    for opener, closer in (("[", "]"), ("{", "}")):
        start = content.find(opener)
        end = content.rfind(closer)
        if start >= 0 and end > start:
            candidate = content[start : end + 1].strip()
            if candidate != content:
                yield candidate


def _load_json(text: str, expected_type: type | None = None) -> object | None:
    for candidate in _json_candidates(text):
        try:
            payload = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if expected_type is None or isinstance(payload, expected_type):
            return payload
    return None


def _parse_prompts(text: str) -> list[str] | None:
    payload = _load_json(text, list)
    if not isinstance(payload, list) or len(payload) < 5:
        return None
    prompts = payload[:5]
    if not all(isinstance(prompt, str) and 0 < len(prompt.strip()) <= MAX_GENERATED_PROMPT_CHARS for prompt in prompts):
        return None
    return [prompt.strip() for prompt in prompts]


def _parse_description_emoji(text: str, fallback_description: str) -> dict[str, str]:
    payload = _load_json(text, dict)
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


def _parse_generated_app(text: str) -> dict[str, object] | None:
    payload = _load_json(text, dict)
    if not isinstance(payload, dict):
        return None
    name = payload.get("name")
    description = payload.get("description")
    category = payload.get("category")
    capabilities = payload.get("capabilities")
    if not isinstance(name, str) or not name.strip():
        name = "My App"
    if not isinstance(description, str) or not description.strip():
        description = "An AI-powered app"
    if not isinstance(category, str) or not category.strip():
        category = "other"
    if not isinstance(capabilities, list):
        capabilities = ["chat"]
    capabilities = [value for value in capabilities if isinstance(value, str) and value in {"chat", "memories"}]
    if not capabilities:
        capabilities = ["chat"]
    result: dict[str, object] = {
        "name": name.strip()[:50],
        "description": description.strip()[:MAX_DESCRIPTION_RESPONSE_CHARS],
        "category": category.strip()[:100],
        "capabilities": capabilities[:2],
    }
    if "chat" in capabilities:
        chat_prompt = payload.get("chat_prompt")
        result["chat_prompt"] = (
            chat_prompt.strip()[:MAX_DESCRIPTION_RESPONSE_CHARS] if isinstance(chat_prompt, str) else None
        )
    else:
        result["chat_prompt"] = None
    if "memories" in capabilities:
        memory_prompt = payload.get("memory_prompt")
        result["memory_prompt"] = (
            memory_prompt.strip()[:MAX_DESCRIPTION_RESPONSE_CHARS] if isinstance(memory_prompt, str) else None
        )
    else:
        result["memory_prompt"] = None
    return result


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


def _image_base64(value: object) -> str | None:
    """Normalize the FLUX binding response to the released base64 wire field."""
    image: object = None
    if isinstance(value, dict):
        image = value.get("image")
    else:
        to_py = getattr(value, "to_py", None)
        converted = to_py() if callable(to_py) else None
        if isinstance(converted, dict):
            image = converted.get("image")
        if image is None:
            image = getattr(value, "image", None)
    if isinstance(image, str):
        try:
            decoded = base64.b64decode(image, validate=True)
        except (ValueError, TypeError):
            return None
        return base64.b64encode(decoded).decode("ascii") if decoded else None
    if isinstance(image, (bytes, bytearray, memoryview)):
        encoded = base64.b64encode(bytes(image)).decode("ascii")
        return encoded or None
    return None


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


@router.post("/v1/app/generate")
async def generate_app(request: Request, payload: GenerateAppRequest):
    """Generate an app draft from a natural-language request."""
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    prompt = payload.prompt.strip()
    if not prompt:
        return JSONResponse({"detail": "Prompt is required"}, status_code=422)
    if len(prompt) < 10:
        return JSONResponse({"detail": "Prompt is too short. Please provide more details."}, status_code=422)
    categories = "\n".join(f"- {title} (id: {category})" for title, category in APP_CATEGORIES)
    system_prompt = APP_GENERATOR_SYSTEM_PROMPT.format(categories=categories)
    try:
        generated = await _run_generation(
            request.scope["env"],
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Create an app based on this description:\n\n{prompt}"},
            ],
            max_tokens=768,
            temperature=0.6,
        )
    except Exception:
        generated = None
    if generated is None:
        return JSONResponse({"error": "app generation unavailable"}, status_code=502)
    response, result = generated
    app = _parse_generated_app(response)
    if app is None:
        return JSONResponse({"error": "app generation returned invalid JSON"}, status_code=502)
    await _record_usage(
        request.scope["env"], str(context["uid"]), _model_name(request.scope["env"]) or "unknown", result
    )
    return {"status": "ok", "app": app}


@router.post("/v1/app/generate-icon")
async def generate_app_icon(request: Request, payload: GenerateAppIconRequest):
    """Generate an app icon with the Cloudflare-hosted FLUX image model."""
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    name = payload.name.strip()
    description = payload.description.strip()
    category = payload.category.strip() or "other"
    if not name:
        return JSONResponse({"detail": "App name is required"}, status_code=422)
    if not description:
        return JSONResponse({"detail": "App description is required"}, status_code=422)

    prompt_prefix = "Create a polished square app icon for an AI companion app. "
    prompt_suffix = " Use a single clear symbol, strong contrast, clean modern design, no words, no letters, no watermark."
    prompt_details = f"App name: {name[:500]}. Category: {category[:200]}. Description: "
    description_limit = max(0, MAX_ICON_PROMPT_CHARS - len(prompt_prefix) - len(prompt_details) - len(prompt_suffix))
    prompt = prompt_prefix + prompt_details + description[:description_limit] + prompt_suffix
    ai = getattr(request.scope["env"], "AI", None)
    if ai is None:
        return JSONResponse({"error": "app icon generation unavailable"}, status_code=502)
    try:
        result = await ai.run(ICON_MODEL, {"prompt": prompt, "steps": 4})
    except Exception:
        return JSONResponse({"error": "app icon generation unavailable"}, status_code=502)
    icon_base64 = _image_base64(result)
    if icon_base64 is None:
        return JSONResponse({"error": "app icon generation unavailable"}, status_code=502)
    await _record_usage(request.scope["env"], str(context["uid"]), ICON_MODEL, result)
    return {"status": "ok", "icon_base64": icon_base64, "mime_type": "image/jpeg"}


__all__ = [
    "generate_app",
    "generate_app_icon",
    "generate_description",
    "generate_description_and_emoji",
    "generate_sample_prompts",
    "router",
]
