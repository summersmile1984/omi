"""D1-backed chat history projection for the Cloudflare web client."""

from __future__ import annotations

from datetime import datetime, timezone
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context

router = APIRouter()
MAX_ID_LENGTH = 256
MAX_LIST_LIMIT = 100
MAX_OFFSET = 100_000
MAX_MESSAGE_BYTES = 1_000_000


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _app_id(request: Request) -> str | None:
    value = request.query_params.get("app_id") or request.query_params.get("plugin_id")
    if value in {None, "", "null"}:
        return None
    return value[:MAX_ID_LENGTH] if len(value) <= MAX_ID_LENGTH else None


def _initial_message(app_id: str | None) -> dict[str, object]:
    return {
        "id": "cf-initial-chat" + (f"-{app_id}" if app_id else ""),
        "text": "Hi! I'm Omi. How can I help?",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sender": "ai",
        "type": "text",
        "app_id": app_id,
        "plugin_id": app_id,
        "from_external_integration": False,
        "memories_id": [],
        "memories": [],
        "files_id": [],
        "files": [],
        "reported": False,
    }


def _stored_message(row: dict[str, object]) -> dict[str, object] | None:
    raw = row.get("message_json")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
        return None
    try:
        message = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return message if isinstance(message, dict) else None


def _pagination(request: Request) -> tuple[int, int] | JSONResponse:
    try:
        limit = int(request.query_params.get("limit", "100"))
        offset = int(request.query_params.get("offset", "0"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid pagination"}, status_code=400)
    if limit < 1 or limit > MAX_LIST_LIMIT or offset < 0 or offset > MAX_OFFSET:
        return JSONResponse({"error": "invalid pagination"}, status_code=400)
    return limit, offset


@router.get("/v2/messages")
async def get_messages(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    app_id = _app_id(request)
    if (request.query_params.get("app_id") or request.query_params.get("plugin_id")) and app_id is None:
        return JSONResponse({"error": "invalid app id"}, status_code=400)
    pagination = _pagination(request)
    if isinstance(pagination, JSONResponse):
        return pagination
    limit, offset = pagination
    uid = str(context["uid"])
    app_clause = "app_id IS NULL" if app_id is None else "app_id = ?"
    args: tuple[object, ...] = (uid,) if app_id is None else (uid, app_id)
    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT message_json FROM cf_chat_messages WHERE uid = ? AND "
                + app_clause
                + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
            )
            .bind(*args, limit, offset)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "messages unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    messages = [_stored_message(row) for row in rows if isinstance(row, dict)]
    messages = [message for message in messages if message is not None]
    return messages or [_initial_message(app_id)]


@router.delete("/v2/messages")
async def clear_messages(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    app_id = _app_id(request)
    if (request.query_params.get("app_id") or request.query_params.get("plugin_id")) and app_id is None:
        return JSONResponse({"error": "invalid app id"}, status_code=400)
    uid = str(context["uid"])
    if app_id is None:
        statement = (
            request.scope["env"]
            .APP_DB.prepare("DELETE FROM cf_chat_messages WHERE uid = ? AND app_id IS NULL")
            .bind(uid)
        )
    else:
        statement = (
            request.scope["env"]
            .APP_DB.prepare("DELETE FROM cf_chat_messages WHERE uid = ? AND app_id = ?")
            .bind(uid, app_id)
        )
    try:
        await statement.run()
    except Exception:
        return JSONResponse({"error": "messages unavailable"}, status_code=503)
    return _initial_message(app_id)
