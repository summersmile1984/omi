"""Workers AI-backed persona greeting reads."""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from fallback import record_fallback
from internal_auth import decode_context

router = APIRouter()

MAX_USERNAME_LENGTH = 256
MAX_PROMPT_BYTES = 500_000
MAX_MESSAGE_LENGTH = 512
DEFAULT_WORKERS_AI_MODEL = "@cf/meta/llama-3.2-3b-instruct"


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _response_text(result: object) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("response", "text", "content"):
            if key in result:
                return _response_text(result[key])
        return ""
    if isinstance(result, list):
        return "".join(
            item if isinstance(item, str) else str(item.get("text") or item.get("content") or "")
            for item in result
            if isinstance(item, (str, dict))
        )
    return ""


def _persona(payload: object) -> tuple[str, str] | None:
    if not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_PROMPT_BYTES:
        return None
    try:
        value = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    capabilities = value.get("capabilities")
    name = value.get("name")
    prompt = value.get("persona_prompt")
    if (
        not isinstance(capabilities, list)
        or "persona" not in capabilities
        or not isinstance(name, str)
        or not name.strip()
        or not isinstance(prompt, str)
        or not prompt.strip()
    ):
        return None
    return name.strip()[:128], prompt.strip()


@router.get("/v1/personas/twitter/initial-message")
async def get_persona_initial_message(request: Request, username: str):
    if not _auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    username = username.removeprefix("@").strip()
    if not username or len(username) > MAX_USERNAME_LENGTH:
        return JSONResponse({"error": "invalid username"}, status_code=400)

    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT data_json FROM cf_app_catalog "
                "WHERE json_extract(data_json, '$.username') = ? AND disabled = 0 LIMIT 1"
            )
            .bind(username)
            .first()
        )
    except Exception:
        return JSONResponse({"error": "persona unavailable"}, status_code=503)
    persona = _persona(result.get("data_json")) if isinstance(result, dict) else None
    if persona is None:
        return {"message": ""}

    name, system_prompt = persona
    ai = getattr(request.scope["env"], "AI", None)
    if ai is None:
        record_fallback(
            component="llm",
            from_mode="workers_ai",
            to_mode="none",
            reason="dependency_unavailable",
            outcome="exhausted",
        )
        return JSONResponse({"error": "persona unavailable"}, status_code=503)
    try:
        result = await ai.run(
            getattr(request.scope["env"], "WORKERS_AI_INTEGRATION_MODEL", DEFAULT_WORKERS_AI_MODEL),
            {
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": (
                            f"Generate a short, funny 5-8 word message that would make someone want to chat with you. "
                            f"Be casual and witty, but don't mention being AI or a clone. Just be {name}. "
                            "The message should feel natural and make people curious to chat with you."
                        ),
                    },
                ],
                "max_tokens": 64,
                "temperature": 0.7,
            },
        )
    except Exception:
        record_fallback(
            component="llm",
            from_mode="workers_ai",
            to_mode="none",
            reason="dependency_unavailable",
            outcome="exhausted",
        )
        return JSONResponse({"error": "persona unavailable"}, status_code=503)

    message = re.sub(r"\s+", " ", _response_text(result)).strip().strip('"')[:MAX_MESSAGE_LENGTH]
    if not message:
        record_fallback(
            component="llm",
            from_mode="workers_ai",
            to_mode="none",
            reason="malformed_doc",
            outcome="exhausted",
        )
        return JSONResponse({"error": "persona unavailable"}, status_code=503)
    return {"message": message}


__all__ = ["get_persona_initial_message", "router"]
