"""Uid-scoped user feedback projections for the Cloudflare data plane."""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context

router = APIRouter()

MAX_SUBJECT_ID_LENGTH = 256
MAX_REASON_LENGTH = 256
RATING_VALUES = frozenset({-1, 0, 1})


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _subject_id(request: Request, name: str) -> str | None:
    value = request.query_params.get(name)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= MAX_SUBJECT_ID_LENGTH else None


def _rating(request: Request) -> int | None:
    try:
        value = int(request.query_params.get("value", ""))
    except (TypeError, ValueError):
        return None
    return value if value in RATING_VALUES else None


def _reason(request: Request) -> str | None:
    value = request.query_params.get("reason")
    if value is None or value == "":
        return None
    return value if isinstance(value, str) and len(value) <= MAX_REASON_LENGTH else None


def _feedback_upsert(env: object, uid: str, feedback_type: str, subject_id: str, value: int, reason: str | None):
    now = int(time.time())
    return env.APP_DB.prepare(
        "INSERT INTO cf_user_feedback "
        "(uid, feedback_type, subject_id, value, reason, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(uid, feedback_type, subject_id) DO UPDATE SET "
        "value = excluded.value, reason = excluded.reason, updated_at = excluded.updated_at"
    ).bind(uid, feedback_type, subject_id, value, reason, now, now)


@router.post("/v1/users/analytics/memory_summary")
async def set_memory_summary_rating(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    memory_id = _subject_id(request, "memory_id")
    value = _rating(request)
    if memory_id is None or value is None:
        return JSONResponse({"error": "invalid feedback"}, status_code=400)
    try:
        await _feedback_upsert(
            request.scope["env"], str(context["uid"]), "memory_summary", memory_id, value, None
        ).run()
    except Exception:
        return JSONResponse({"error": "feedback unavailable"}, status_code=503)
    return {"status": "ok"}


@router.get("/v1/users/analytics/memory_summary")
async def get_memory_summary_rating(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    memory_id = _subject_id(request, "memory_id")
    if memory_id is None:
        return JSONResponse({"error": "invalid feedback"}, status_code=400)
    try:
        row = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT value FROM cf_user_feedback "
                "WHERE uid = ? AND feedback_type = 'memory_summary' AND subject_id = ?"
            )
            .bind(str(context["uid"]), memory_id)
            .first()
        )
    except Exception:
        return JSONResponse({"error": "feedback unavailable"}, status_code=503)
    if not isinstance(row, dict):
        return {"has_rating": False, "rating": None}
    value = int(row.get("value", -1))
    return {"has_rating": value != -1, "rating": value}


@router.post("/v1/users/analytics/chat_message")
async def set_chat_message_rating(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    message_id = _subject_id(request, "message_id")
    value = _rating(request)
    reason_raw = request.query_params.get("reason")
    reason = _reason(request)
    if message_id is None or value is None or (reason_raw not in (None, "") and reason is None):
        return JSONResponse({"error": "invalid feedback"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    feedback = _feedback_upsert(env, uid, "chat_message", message_id, value, reason)
    message_rating = env.APP_DB.prepare(
        "UPDATE cf_chat_messages SET message_json = json_set(message_json, '$.rating', ?) " "WHERE uid = ? AND id = ?"
    ).bind(None if value == 0 else value, uid, message_id)
    try:
        await env.APP_DB.batch([feedback, message_rating])
    except Exception:
        return JSONResponse({"error": "feedback unavailable"}, status_code=503)
    return {"status": "ok"}
