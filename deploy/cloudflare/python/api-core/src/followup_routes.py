"""Workers AI-backed Joan follow-up question generation.

The legacy endpoint is intentionally retained as a ``DELETE`` route for client
compatibility, but it is a read-only operation over the uid-scoped D1
conversation projection.  Transcript text is bounded before it enters the
Workers AI prompt and no legacy provider fallback is used.
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from fallback import record_fallback
from internal_auth import decode_context

router = APIRouter()

MAX_ID_LENGTH = 256
MAX_SEGMENTS = 2_000
MAX_TRANSCRIPT_JSON_BYTES = 1_000_000
MAX_RESPONSE_LENGTH = 2_000
DEFAULT_WORKERS_AI_MODEL = "@cf/meta/llama-3.2-3b-instruct"

_CONVERSATION_SELECT = "SELECT id, status, is_locked, transcript_segments_json, updated_at " "FROM cf_conversations "


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _truthy(value: object) -> bool:
    return bool(value) and value not in ("0", "false", "False", "no")


def _speaker_id(segment: dict[str, object]) -> int:
    value = segment.get("speaker_id")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    speaker = segment.get("speaker")
    if isinstance(speaker, str):
        suffix = speaker.split("_", 1)[1] if "_" in speaker else speaker
        if suffix.isdigit():
            return int(suffix)
    return 0


def _transcript_text(raw: object) -> str:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_TRANSCRIPT_JSON_BYTES:
        return ""
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(decoded, list):
        return ""

    lines: list[str] = []
    for segment in decoded[:MAX_SEGMENTS]:
        if not isinstance(segment, dict):
            continue
        text = segment.get("text")
        if not isinstance(text, str):
            continue
        text = text.strip()
        if not text:
            continue
        speaker = "User" if _truthy(segment.get("is_user")) else f"Speaker {_speaker_id(segment)}"
        lines.append(f"{speaker}: {text}")
    return "\n\n".join(lines).strip()


def _prompt(transcript: str) -> str:
    words = transcript.split()
    if len(words) > 100:
        transcript = " ".join(words[-100:])
    return (
        "You will be given the transcript of an in-progress conversation.\n"
        "Your task as an engaging, fun, and curious conversationalist, is to suggest the next follow-up question to keep the conversation engaging.\n\n"
        "Conversation Transcript:\n"
        f"{transcript}\n\n"
        "Output your response in plain text, without markdown.\n"
        "Output only the question, without context, be concise and straight to the point."
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
        parts: list[str] = []
        for item in result:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
        return "".join(parts)
    return ""


async def _conversation(env: object, uid: str, memory_id: str) -> dict[str, object] | None:
    if memory_id == "0":
        statement = env.APP_DB.prepare(
            _CONVERSATION_SELECT + "WHERE uid = ? AND status = 'in_progress' ORDER BY updated_at DESC, id DESC LIMIT 1"
        ).bind(uid)
    else:
        statement = env.APP_DB.prepare(_CONVERSATION_SELECT + "WHERE uid = ? AND id = ?").bind(uid, memory_id)
    row = await statement.first()
    return row if isinstance(row, dict) else None


@router.delete("/v1/joan/{memory_id}/followup-question")
async def get_followup_question(request: Request, memory_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not memory_id or len(memory_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid memory id"}, status_code=400)

    uid = str(context["uid"])
    try:
        row = await _conversation(request.scope["env"], uid, memory_id)
    except Exception:
        return JSONResponse({"error": "followup question unavailable"}, status_code=503)
    if row is None:
        if memory_id == "0":
            return JSONResponse({"detail": "No memory in progres"}, status_code=400)
        return JSONResponse({"detail": "Conversation not found"}, status_code=404)
    if _truthy(row.get("is_locked")):
        return JSONResponse(
            {"detail": "A paid plan is required to access this conversation."},
            status_code=402,
        )

    transcript = _transcript_text(row.get("transcript_segments_json"))
    if len(transcript.split()) < 10:
        return {"result": ""}

    env = request.scope["env"]
    ai = getattr(env, "AI", None)
    if ai is None:
        record_fallback(
            component="llm",
            from_mode="workers_ai",
            to_mode="none",
            reason="dependency_unavailable",
            outcome="exhausted",
        )
        return JSONResponse({"error": "followup question unavailable"}, status_code=503)

    try:
        result = await ai.run(
            getattr(env, "WORKERS_AI_INTEGRATION_MODEL", DEFAULT_WORKERS_AI_MODEL),
            {
                "messages": [{"role": "user", "content": _prompt(transcript)}],
                "max_tokens": 128,
                "temperature": 0,
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
        return JSONResponse({"error": "followup question unavailable"}, status_code=503)

    answer = re.sub(r"\s+", " ", _response_text(result)).strip()[:MAX_RESPONSE_LENGTH]
    if not answer:
        record_fallback(
            component="llm",
            from_mode="workers_ai",
            to_mode="none",
            reason="malformed_doc",
            outcome="exhausted",
        )
        return JSONResponse({"error": "followup question unavailable"}, status_code=503)
    return {"result": answer}


__all__ = ["get_followup_question", "router"]
