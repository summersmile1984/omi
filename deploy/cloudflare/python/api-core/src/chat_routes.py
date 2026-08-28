"""D1-backed chat history projection for the Cloudflare web client."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from feedback_routes import chat_feedback_statements
from internal_auth import decode_context

router = APIRouter()
MAX_ID_LENGTH = 256
MAX_LIST_LIMIT = 100
MAX_OFFSET = 100_000
MAX_MESSAGE_BYTES = 1_000_000
MAX_SHARE_TOKEN_LENGTH = 128
MAX_SHARE_MESSAGES = 100
CHAT_SHARE_TTL_SECONDS = 60 * 60 * 24 * 30
CHAT_SHARE_BASE_URL = "https://h.omi.me/chat"


class ShareChatMessagesRequest(BaseModel):
    model_config = {"extra": "ignore"}

    message_ids: list[str] = Field(min_length=1, max_length=MAX_SHARE_MESSAGES)


class RateMessageRequest(BaseModel):
    model_config = {"extra": "ignore"}

    rating: int | None = Field(None, ge=-1, le=1)
    app_version: str | None = None


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


async def _bounded_json(request: Request) -> object:
    body_reader = getattr(request, "body", None)
    if callable(body_reader):
        raw = await body_reader()
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ValueError("request body exceeds size limit")
        return json.loads(raw)
    body = await request.json()
    if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("request body exceeds size limit")
    return body


@router.get("/v2/messages")
async def get_messages(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    raw_app_id = request.query_params.get("app_id") or request.query_params.get("plugin_id")
    app_id = _app_id(request)
    if raw_app_id not in {None, "", "null"} and app_id is None:
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
                + " AND COALESCE(json_extract(message_json, '$.reported'), 0) != 1"
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


@router.delete("/v1/messages")
@router.delete("/v2/messages")
async def clear_messages(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    raw_app_id = request.query_params.get("app_id") or request.query_params.get("plugin_id")
    app_id = _app_id(request)
    if raw_app_id not in {None, "", "null"} and app_id is None:
        return JSONResponse({"error": "invalid app id"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    app_clause = "app_id IS NULL" if app_id is None else "app_id = ?"
    app_args: tuple[object, ...] = () if app_id is None else (app_id,)
    try:
        session = (
            await env.APP_DB.prepare(
                "SELECT id FROM cf_chat_sessions WHERE uid = ? AND "
                + app_clause
                + " ORDER BY updated_at DESC, id DESC LIMIT 1"
            )
            .bind(uid, *app_args)
            .first()
        )
        if isinstance(session, dict) and isinstance(session.get("id"), str):
            session_id = str(session["id"])
            statements = [
                env.APP_DB.prepare(
                    "DELETE FROM cf_chat_messages WHERE uid = ? AND "
                    "COALESCE(NULLIF(json_extract(message_json, '$.chat_session_id'), ''), "
                    "NULLIF(json_extract(message_json, '$.session_id'), '')) = ?"
                ).bind(uid, session_id),
                env.APP_DB.prepare("DELETE FROM cf_chat_sessions WHERE uid = ? AND id = ?").bind(uid, session_id),
            ]
        else:
            statements = [
                env.APP_DB.prepare("DELETE FROM cf_chat_messages WHERE uid = ? AND " + app_clause).bind(uid, *app_args)
            ]
        await env.APP_DB.batch(statements)
    except Exception:
        return JSONResponse({"error": "messages unavailable"}, status_code=503)
    return _initial_message(app_id)


async def _message_row(env: object, uid: str, message_id: str) -> dict[str, object] | None:
    row = (
        await env.APP_DB.prepare("SELECT message_json FROM cf_chat_messages WHERE uid = ? AND id = ?")
        .bind(uid, message_id)
        .first()
    )
    return row if isinstance(row, dict) else None


@router.post("/v1/messages/{message_id}/report")
@router.post("/v2/messages/{message_id}/report")
async def report_message(request: Request, message_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not message_id or len(message_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "message not found"}, status_code=404)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        row = await _message_row(env, uid, message_id)
        if row is None:
            return JSONResponse({"error": "message not found"}, status_code=404)
        message = _stored_message(row)
        if message is None:
            return JSONResponse({"error": "messages unavailable"}, status_code=503)
        if message.get("sender") != "ai":
            return JSONResponse({"error": "only AI messages can be reported"}, status_code=400)
        if message.get("reported") is True:
            return JSONResponse({"error": "message already reported"}, status_code=400)
        await env.APP_DB.prepare(
            "UPDATE cf_chat_messages SET message_json = json_set(message_json, '$.reported', json('true')) "
            "WHERE uid = ? AND id = ?"
        ).bind(uid, message_id).run()
    except Exception:
        return JSONResponse({"error": "messages unavailable"}, status_code=503)
    return {"message": "Message reported"}


@router.patch("/v2/messages/{message_id}/rating")
async def rate_message(request: Request, message_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not message_id or len(message_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid rating"}, status_code=400)
    try:
        payload = RateMessageRequest.model_validate(await _bounded_json(request))
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid rating"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    value = payload.rating if payload.rating is not None else 0
    try:
        await env.APP_DB.batch(chat_feedback_statements(env, uid, message_id, value))
    except Exception:
        return JSONResponse({"error": "messages unavailable"}, status_code=503)
    return {"status": "ok"}


@router.get("/v1/users/stats/chat-messages")
async def get_chat_message_count(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        row = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT COUNT(*) AS count FROM cf_chat_messages WHERE uid = ? "
                "AND COALESCE(json_extract(message_json, '$.reported'), 0) != 1"
            )
            .bind(str(context["uid"]))
            .first()
        )
    except Exception:
        return JSONResponse({"error": "messages unavailable"}, status_code=503)
    return {"count": max(0, int((row or {}).get("count") or 0)) if isinstance(row, dict) else 0}


async def _chat_share(env: object, token: str, now: int) -> dict[str, object] | None:
    row = (
        await env.APP_DB.prepare(
            "SELECT token, sender_uid, sender_name, expires_at FROM cf_chat_shares "
            "WHERE token = ? AND expires_at > ?"
        )
        .bind(token, now)
        .first()
    )
    return row if isinstance(row, dict) else None


async def _shared_message_rows(env: object, token: str, now: int) -> list[dict[str, object]]:
    result = (
        await env.APP_DB.prepare(
            "SELECT message.message_json FROM cf_chat_shares AS share "
            "JOIN cf_chat_share_messages AS shared_message ON shared_message.token = share.token "
            "JOIN cf_chat_messages AS message "
            "ON message.uid = share.sender_uid AND message.id = shared_message.message_id "
            "WHERE share.token = ? AND share.expires_at > ? ORDER BY shared_message.ordinal ASC"
        )
        .bind(token, now)
        .all()
    )
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [row for row in rows if isinstance(row, dict)]


@router.post("/v2/messages/share")
async def share_chat_messages(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = ShareChatMessagesRequest.model_validate(await _bounded_json(request))
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid share request"}, status_code=400)
    if len(set(payload.message_ids)) != len(payload.message_ids) or any(
        not message_id or len(message_id) > MAX_ID_LENGTH for message_id in payload.message_ids
    ):
        return JSONResponse({"error": "invalid message ids"}, status_code=400)

    env = request.scope["env"]
    uid = str(context["uid"])
    placeholders = ", ".join("?" for _ in payload.message_ids)
    try:
        result = (
            await env.APP_DB.prepare(f"SELECT id FROM cf_chat_messages WHERE uid = ? AND id IN ({placeholders})")
            .bind(uid, *payload.message_ids)
            .all()
        )
        rows = result.get("results", []) if isinstance(result, dict) else []
        existing = {str(row["id"]) for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}
        missing = next((message_id for message_id in payload.message_ids if message_id not in existing), None)
        if missing is not None:
            return JSONResponse({"error": f"message {missing} not found"}, status_code=404)

        token = uuid.uuid4().hex
        now = int(time.time())
        sender_name = str(context.get("displayName") or "Omi user").strip()[:120] or "Omi user"
        statements = [
            env.APP_DB.prepare(
                "DELETE FROM cf_chat_share_messages WHERE token IN "
                "(SELECT token FROM cf_chat_shares WHERE expires_at <= ?)"
            ).bind(now),
            env.APP_DB.prepare("DELETE FROM cf_chat_shares WHERE expires_at <= ?").bind(now),
            env.APP_DB.prepare(
                "INSERT INTO cf_chat_shares (token, sender_uid, sender_name, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?)"
            ).bind(token, uid, sender_name, now + CHAT_SHARE_TTL_SECONDS, now),
        ]
        statements.extend(
            env.APP_DB.prepare("INSERT INTO cf_chat_share_messages (token, ordinal, message_id) VALUES (?, ?, ?)").bind(
                token, ordinal, message_id
            )
            for ordinal, message_id in enumerate(payload.message_ids)
        )
        await env.APP_DB.batch(statements)
    except Exception:
        return JSONResponse({"error": "chat sharing unavailable"}, status_code=503)
    return {"url": f"{CHAT_SHARE_BASE_URL}/{token}", "token": token}


@router.get("/v2/messages/shared/{token}")
async def get_shared_chat_messages(request: Request, token: str):
    if not token or len(token) > MAX_SHARE_TOKEN_LENGTH:
        return JSONResponse({"error": "share link expired or not found"}, status_code=404)
    try:
        now = int(time.time())
        share = await _chat_share(request.scope["env"], token, now)
        if share is None:
            return JSONResponse({"error": "share link expired or not found"}, status_code=404)
        rows = await _shared_message_rows(request.scope["env"], token, now)
    except Exception:
        return JSONResponse({"error": "chat sharing unavailable"}, status_code=503)

    messages: list[dict[str, object]] = []
    for row in rows:
        message = _stored_message(row)
        if message is None:
            continue
        message_id = message.get("id")
        text = message.get("text")
        sender = message.get("sender")
        created_at = message.get("created_at")
        if not isinstance(message_id, str) or not isinstance(text, str) or not isinstance(sender, str):
            continue
        messages.append(
            {
                "id": message_id,
                "text": text,
                "sender": sender,
                "created_at": created_at if isinstance(created_at, str) else None,
            }
        )
    return {
        "sender_name": str(share.get("sender_name") or "Omi user"),
        "messages": messages,
        "count": len(messages),
    }
