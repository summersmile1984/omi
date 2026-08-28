"""D1-backed desktop chat sessions and persistence-only messages."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from chat_routes import MAX_ID_LENGTH, MAX_MESSAGE_BYTES, _auth_context, _stored_message
from feedback_routes import chat_feedback_statements

router = APIRouter()

MAX_SESSION_TITLE_LENGTH = 500
MAX_APP_ID_LENGTH = 200
MAX_DESKTOP_LIST_LIMIT = 1_000
MAX_RECONCILE_LIMIT = 100
MAX_RECONCILE_SCAN = 1_000
MAX_OFFSET = 100_000
MAX_TEXT_LENGTH = 100_000
MAX_JOURNAL_REVISION = 9_007_199_254_740_991


class CreateChatSessionRequest(BaseModel):
    model_config = {"extra": "ignore"}

    title: str | None = Field(None, max_length=MAX_SESSION_TITLE_LENGTH)
    app_id: str | None = Field(None, max_length=MAX_APP_ID_LENGTH)


class UpdateChatSessionRequest(BaseModel):
    model_config = {"extra": "ignore"}

    title: str | None = Field(None, max_length=MAX_SESSION_TITLE_LENGTH)
    starred: bool | None = None


class SaveMessageRequest(BaseModel):
    model_config = {"extra": "ignore"}

    text: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)
    sender: str = Field(pattern=r"^(human|ai)$")
    app_id: str | None = Field(None, max_length=MAX_APP_ID_LENGTH)
    session_id: str | None = Field(None, max_length=MAX_APP_ID_LENGTH)
    metadata: str | None = None
    content_blocks: list[dict[str, object]] | None = None
    client_message_id: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{1,128}$")
    message_source: str = Field("desktop_chat", pattern=r"^(desktop_chat|realtime_voice)$")
    journal_revision: int | None = Field(None, ge=1, le=MAX_JOURNAL_REVISION)


class RateMessageRequest(BaseModel):
    model_config = {"extra": "ignore"}

    rating: int | None = Field(None, ge=-1, le=1)
    app_version: str | None = None


async def _bounded_json(request: Request) -> object:
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_MESSAGE_BYTES:
        raise ValueError("request body exceeds size limit")
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


def _valid_id(value: str) -> bool:
    return 0 < len(value) <= MAX_ID_LENGTH


def _iso_timestamp(value: object) -> str:
    timestamp = int(value)
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _session_projection(row: dict[str, object]) -> dict[str, object]:
    app_id = row.get("app_id") if isinstance(row.get("app_id"), str) else None
    preview = row.get("preview") if isinstance(row.get("preview"), str) else None
    return {
        "id": str(row["id"]),
        "title": str(row.get("title") or "New Chat"),
        "preview": preview,
        "created_at": _iso_timestamp(row["created_at"]),
        "updated_at": _iso_timestamp(row["updated_at"]),
        "app_id": app_id,
        "plugin_id": app_id,
        "message_count": max(0, int(row.get("message_count") or 0)),
        "starred": bool(row.get("starred")),
    }


async def _session_row(env: object, uid: str, session_id: str) -> dict[str, object] | None:
    row = (
        await env.APP_DB.prepare(
            "SELECT id, title, preview, created_at, updated_at, app_id, message_count, starred "
            "FROM cf_chat_sessions WHERE uid = ? AND id = ?"
        )
        .bind(uid, session_id)
        .first()
    )
    return row if isinstance(row, dict) else None


def _app_scope(app_id: str | None) -> tuple[str, list[object]]:
    return ("app_id IS NULL", []) if app_id is None else ("app_id = ?", [app_id])


def _requested_app_id(request: Request) -> str | None | JSONResponse:
    raw = request.query_params.get("app_id")
    if raw is None:
        return None
    if not isinstance(raw, str) or len(raw) > MAX_APP_ID_LENGTH:
        return JSONResponse({"error": "invalid app id"}, status_code=400)
    return raw


def _scope(request: Request) -> tuple[str, list[object]] | JSONResponse:
    session_id = request.query_params.get("session_id")
    if session_id is not None:
        if not isinstance(session_id, str) or not _valid_id(session_id):
            return JSONResponse({"error": "invalid session id"}, status_code=400)
        return (
            "COALESCE(NULLIF(json_extract(message_json, '$.chat_session_id'), ''), "
            "NULLIF(json_extract(message_json, '$.session_id'), '')) = ?",
            [session_id],
        )
    app_id = _requested_app_id(request)
    if isinstance(app_id, JSONResponse):
        return app_id
    return _app_scope(app_id)


def _pagination(request: Request, *, maximum: int) -> tuple[int, int] | JSONResponse:
    try:
        limit = int(request.query_params.get("limit", "100"))
        offset = int(request.query_params.get("offset", "0"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid pagination"}, status_code=400)
    if limit < 1 or limit > maximum or offset < 0 or offset > MAX_OFFSET:
        return JSONResponse({"error": "invalid pagination"}, status_code=400)
    return limit, offset


async def _acquire_session(env: object, uid: str, app_id: str | None, now: int) -> tuple[str, object | None]:
    app_clause, app_args = _app_scope(app_id)
    row = (
        await env.APP_DB.prepare(
            "SELECT id FROM cf_chat_sessions WHERE uid = ? AND "
            + app_clause
            + " ORDER BY updated_at DESC, id DESC LIMIT 1"
        )
        .bind(uid, *app_args)
        .first()
    )
    if isinstance(row, dict) and isinstance(row.get("id"), str):
        return str(row["id"]), None
    session_id = str(uuid.uuid4())
    statement = env.APP_DB.prepare(
        "INSERT INTO cf_chat_sessions "
        "(uid, id, title, preview, created_at, updated_at, app_id, message_count, starred) "
        "VALUES (?, ?, 'New Chat', NULL, ?, ?, ?, 0, 0)"
    ).bind(uid, session_id, now, now, app_id)
    return session_id, statement


def _payload_hash(payload: SaveMessageRequest, requested_session_id: str | None) -> str:
    value: dict[str, object] = {
        "app_id": payload.app_id,
        "message_source": payload.message_source,
        "metadata": payload.metadata,
        "sender": payload.sender,
        "session_id": requested_session_id,
        "text": payload.text,
    }
    if payload.content_blocks is not None:
        value["content_blocks"] = payload.content_blocks
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _message_identity_matches(
    message: dict[str, object], payload: SaveMessageRequest, requested_session_id: str | None
) -> bool:
    if message.get("sender") != payload.sender:
        return False
    if message.get("message_source", "desktop_chat") != payload.message_source:
        return False
    existing_app_ids = [message[field] for field in ("app_id", "plugin_id") if field in message] or [None]
    if any(app_id != payload.app_id for app_id in existing_app_ids):
        return False
    if requested_session_id is not None:
        existing_session_ids = [message[field] for field in ("chat_session_id", "session_id") if field in message] or [
            None
        ]
        if any(session_id != requested_session_id for session_id in existing_session_ids):
            return False
    return True


def _stored_payload_matches(
    message: dict[str, object], payload: SaveMessageRequest, requested_session_id: str | None, payload_hash: str
) -> bool:
    stored_hash = message.get("client_message_payload_hash")
    if stored_hash is not None:
        return stored_hash == payload_hash
    if not _message_identity_matches(message, payload, requested_session_id):
        return False
    if message.get("text") != payload.text or message.get("metadata") != payload.metadata:
        return False
    if payload.content_blocks is not None and message.get("content_blocks") != payload.content_blocks:
        return False
    return True


def _save_response(message_id: str, message: dict[str, object], *, updated: bool) -> dict[str, object]:
    created_at = message.get("created_at")
    if not isinstance(created_at, str):
        created_at = datetime.now(timezone.utc).isoformat()
    session_id = message.get("chat_session_id") or message.get("session_id")
    return {
        "id": message_id,
        "created_at": created_at,
        "session_id": session_id if isinstance(session_id, str) else None,
        "created": False,
        "updated": updated,
        "journal_revision": message.get("journal_revision"),
    }


async def _existing_message(env: object, uid: str, message_id: str) -> dict[str, object] | None:
    row = (
        await env.APP_DB.prepare("SELECT message_json FROM cf_chat_messages WHERE uid = ? AND id = ?")
        .bind(uid, message_id)
        .first()
    )
    return _stored_message(row) if isinstance(row, dict) else None


async def _existing_save(
    env: object,
    uid: str,
    message_id: str,
    payload: SaveMessageRequest,
    requested_session_id: str | None,
    payload_hash: str,
) -> dict[str, object] | JSONResponse | None:
    message = await _existing_message(env, uid, message_id)
    if message is None:
        return None
    if not _message_identity_matches(message, payload, requested_session_id):
        return JSONResponse({"error": "client_message_id payload conflict"}, status_code=409)
    stored_revision = int(message.get("journal_revision") or 1)
    requested_revision = payload.journal_revision
    if requested_revision is None or requested_revision == stored_revision:
        if not _stored_payload_matches(message, payload, requested_session_id, payload_hash):
            return JSONResponse({"error": "client_message_id payload conflict"}, status_code=409)
        return _save_response(message_id, message, updated=False)
    if requested_revision < stored_revision:
        return _save_response(message_id, message, updated=False)

    message.update(
        {
            "text": payload.text,
            "metadata": payload.metadata,
            "client_message_payload_hash": payload_hash,
            "journal_revision": requested_revision,
        }
    )
    if payload.content_blocks is not None:
        message["content_blocks"] = payload.content_blocks
    result = (
        await env.APP_DB.prepare(
            "UPDATE cf_chat_messages SET message_json = ? WHERE uid = ? AND id = ? "
            "AND COALESCE(CAST(json_extract(message_json, '$.journal_revision') AS INTEGER), 1) < ?"
        )
        .bind(
            json.dumps(message, separators=(",", ":"), ensure_ascii=False),
            uid,
            message_id,
            requested_revision,
        )
        .run()
    )
    meta = result.get("meta", {}) if isinstance(result, dict) else {}
    if int(meta.get("changes") or 0) == 0:
        latest = await _existing_message(env, uid, message_id)
        if latest is None or not _message_identity_matches(latest, payload, requested_session_id):
            return JSONResponse({"error": "client_message_id payload conflict"}, status_code=409)
        latest_revision = int(latest.get("journal_revision") or 1)
        if latest_revision == requested_revision and not _stored_payload_matches(
            latest, payload, requested_session_id, payload_hash
        ):
            return JSONResponse({"error": "client_message_id payload conflict"}, status_code=409)
        return _save_response(message_id, latest, updated=False)
    return _save_response(message_id, message, updated=True)


@router.post("/v2/chat-sessions")
async def create_chat_session(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = CreateChatSessionRequest.model_validate(await _bounded_json(request))
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid chat session"}, status_code=400)
    title = (payload.title or "New Chat").strip() or "New Chat"
    now = int(time.time())
    session_id = str(uuid.uuid4())
    env = request.scope["env"]
    try:
        await env.APP_DB.prepare(
            "INSERT INTO cf_chat_sessions "
            "(uid, id, title, preview, created_at, updated_at, app_id, message_count, starred) "
            "VALUES (?, ?, ?, NULL, ?, ?, ?, 0, 0)"
        ).bind(str(context["uid"]), session_id, title, now, now, payload.app_id).run()
        row = await _session_row(env, str(context["uid"]), session_id)
    except Exception:
        return JSONResponse({"error": "chat sessions unavailable"}, status_code=503)
    return _session_projection(row) if row else JSONResponse({"error": "chat sessions unavailable"}, status_code=503)


@router.get("/v2/chat-sessions")
async def list_chat_sessions(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    pagination = _pagination(request, maximum=500)
    if isinstance(pagination, JSONResponse):
        return pagination
    app_id = _requested_app_id(request)
    if isinstance(app_id, JSONResponse):
        return app_id
    starred_raw = request.query_params.get("starred")
    if starred_raw is None:
        starred = None
    elif starred_raw.lower() in {"true", "1"}:
        starred = 1
    elif starred_raw.lower() in {"false", "0"}:
        starred = 0
    else:
        return JSONResponse({"error": "invalid starred filter"}, status_code=400)
    limit, offset = pagination
    app_clause, app_args = _app_scope(app_id)
    starred_clause = "" if starred is None else " AND starred = ?"
    args = [str(context["uid"]), *app_args]
    if starred is not None:
        args.append(starred)
    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT id, title, preview, created_at, updated_at, app_id, message_count, starred "
                "FROM cf_chat_sessions WHERE uid = ? AND "
                + app_clause
                + starred_clause
                + " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
            )
            .bind(*args, limit, offset)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "chat sessions unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [_session_projection(row) for row in rows if isinstance(row, dict)]


@router.get("/v2/chat-sessions/{session_id}")
async def get_chat_session(request: Request, session_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _valid_id(session_id):
        return JSONResponse({"error": "chat session not found"}, status_code=404)
    try:
        row = await _session_row(request.scope["env"], str(context["uid"]), session_id)
    except Exception:
        return JSONResponse({"error": "chat sessions unavailable"}, status_code=503)
    return _session_projection(row) if row else JSONResponse({"error": "chat session not found"}, status_code=404)


@router.patch("/v2/chat-sessions/{session_id}")
async def update_chat_session(request: Request, session_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _valid_id(session_id):
        return JSONResponse({"error": "chat session not found"}, status_code=404)
    try:
        payload = UpdateChatSessionRequest.model_validate(await _bounded_json(request))
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid chat session"}, status_code=400)
    if payload.title is None and payload.starred is None:
        return JSONResponse({"error": "invalid chat session"}, status_code=400)
    values: list[object] = []
    assignments: list[str] = []
    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            return JSONResponse({"error": "invalid chat session"}, status_code=400)
        assignments.append("title = ?")
        values.append(title)
    if payload.starred is not None:
        assignments.append("starred = ?")
        values.append(1 if payload.starred else 0)
    assignments.append("updated_at = ?")
    values.append(int(time.time()))
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        await env.APP_DB.prepare(
            "UPDATE cf_chat_sessions SET " + ", ".join(assignments) + " WHERE uid = ? AND id = ?"
        ).bind(*values, uid, session_id).run()
        row = await _session_row(env, uid, session_id)
    except Exception:
        return JSONResponse({"error": "chat sessions unavailable"}, status_code=503)
    return _session_projection(row) if row else JSONResponse({"error": "chat session not found"}, status_code=404)


@router.delete("/v2/chat-sessions/{session_id}")
async def delete_chat_session(request: Request, session_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _valid_id(session_id):
        return JSONResponse({"error": "invalid session id"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        await env.APP_DB.batch(
            [
                env.APP_DB.prepare(
                    "DELETE FROM cf_chat_messages WHERE uid = ? AND "
                    "COALESCE(NULLIF(json_extract(message_json, '$.chat_session_id'), ''), "
                    "NULLIF(json_extract(message_json, '$.session_id'), '')) = ?"
                ).bind(uid, session_id),
                env.APP_DB.prepare("DELETE FROM cf_chat_sessions WHERE uid = ? AND id = ?").bind(uid, session_id),
            ]
        )
    except Exception:
        return JSONResponse({"error": "chat sessions unavailable"}, status_code=503)
    return {"status": "ok"}


@router.post("/v2/desktop/messages")
async def save_desktop_message(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = SaveMessageRequest.model_validate(await _bounded_json(request))
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid message"}, status_code=400)
    requested_session_id = payload.session_id
    message_id = payload.client_message_id or str(uuid.uuid4())
    payload_hash = _payload_hash(payload, requested_session_id)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        existing = await _existing_save(env, uid, message_id, payload, requested_session_id, payload_hash)
        if existing is not None:
            return existing

        now = int(time.time())
        session_id = requested_session_id
        session_insert = None
        if session_id is None:
            session_id, session_insert = await _acquire_session(env, uid, payload.app_id, now)
        created_at = datetime.now(timezone.utc).isoformat()
        message: dict[str, object] = {
            "id": message_id,
            "text": payload.text,
            "created_at": created_at,
            "sender": payload.sender,
            "type": "text",
            "app_id": payload.app_id,
            "plugin_id": payload.app_id,
            "session_id": session_id,
            "chat_session_id": session_id,
            "from_external_integration": False,
            "rating": None,
            "reported": False,
            "memories_id": [],
            "memories": [],
            "files_id": [],
            "files": [],
            "metadata": payload.metadata,
            "content_blocks": payload.content_blocks or [],
            "client_message_id": payload.client_message_id,
            "client_message_payload_hash": payload_hash if payload.client_message_id else None,
            "message_source": payload.message_source,
            "journal_revision": payload.journal_revision,
        }
        order_key = int(time.time() * 1_000_000) * 2
        statements = []
        if session_insert is not None:
            statements.append(session_insert)
        statements.extend(
            [
                env.APP_DB.prepare(
                    "INSERT INTO cf_chat_messages (uid, id, app_id, created_at, message_json) " "VALUES (?, ?, ?, ?, ?)"
                ).bind(
                    uid,
                    message_id,
                    payload.app_id,
                    order_key,
                    json.dumps(message, separators=(",", ":"), ensure_ascii=False),
                ),
                env.APP_DB.prepare(
                    "UPDATE cf_chat_sessions SET updated_at = ?, message_count = message_count + 1, preview = ? "
                    "WHERE uid = ? AND id = ?"
                ).bind(now, payload.text[:100], uid, session_id),
            ]
        )
        if payload.sender == "human" and payload.message_source == "desktop_chat":
            statements.append(
                env.APP_DB.prepare(
                    "INSERT OR IGNORE INTO cf_chat_quota_events "
                    "(uid, idempotency_key, source, message_id, chat_session_id, platform, occurred_at) "
                    "VALUES (?, ?, 'desktop_messages', ?, ?, ?, ?)"
                ).bind(
                    uid,
                    f"desktop_messages:{message_id}",
                    message_id,
                    session_id,
                    request.headers.get("x-app-platform"),
                    now,
                )
            )
        await env.APP_DB.batch(statements)
    except Exception:
        if payload.client_message_id:
            try:
                existing = await _existing_save(env, uid, message_id, payload, requested_session_id, payload_hash)
            except Exception:
                existing = None
            if existing is not None:
                return existing
        return JSONResponse({"error": "messages unavailable"}, status_code=503)
    return {
        "id": message_id,
        "created_at": created_at,
        "session_id": session_id,
        "created": True,
        "updated": False,
        "journal_revision": payload.journal_revision,
    }


async def _scoped_message_rows(
    env: object,
    uid: str,
    scope: tuple[str, list[object]],
    *,
    limit: int,
    offset: int,
) -> list[dict[str, object]]:
    clause, args = scope
    result = (
        await env.APP_DB.prepare(
            "SELECT message_json FROM cf_chat_messages WHERE uid = ? AND "
            + clause
            + " AND COALESCE(json_extract(message_json, '$.reported'), 0) != 1 "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        )
        .bind(uid, *args, limit, offset)
        .all()
    )
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [row for row in rows if isinstance(row, dict)]


@router.get("/v2/desktop/messages")
async def get_desktop_messages(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    scope = _scope(request)
    if isinstance(scope, JSONResponse):
        return scope
    pagination = _pagination(request, maximum=MAX_DESKTOP_LIST_LIMIT)
    if isinstance(pagination, JSONResponse):
        return pagination
    try:
        rows = await _scoped_message_rows(
            request.scope["env"], str(context["uid"]), scope, limit=pagination[0], offset=pagination[1]
        )
    except Exception:
        return JSONResponse({"error": "messages unavailable"}, status_code=503)
    messages = [_stored_message(row) for row in rows]
    return [message for message in messages if message is not None]


@router.get("/v2/desktop/messages/reconcile")
async def reconcile_desktop_messages(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    scope = _scope(request)
    if isinstance(scope, JSONResponse):
        return scope
    try:
        limit = int(request.query_params.get("limit", "100"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid pagination"}, status_code=400)
    if limit < 1 or limit > MAX_RECONCILE_LIMIT:
        return JSONResponse({"error": "invalid pagination"}, status_code=400)
    cursor = request.query_params.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or not _valid_id(cursor)):
        return JSONResponse({"error": "invalid message reconciliation cursor"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    clause, args = scope
    cursor_clause = ""
    cursor_args: list[object] = []
    try:
        if cursor is not None:
            cursor_row = (
                await env.APP_DB.prepare(
                    "SELECT id, created_at FROM cf_chat_messages WHERE uid = ? AND id = ? AND " + clause
                )
                .bind(uid, cursor, *args)
                .first()
            )
            if not isinstance(cursor_row, dict):
                return JSONResponse({"error": "invalid message reconciliation cursor"}, status_code=400)
            created_at = int(cursor_row["created_at"])
            cursor_clause = " AND (created_at < ? OR (created_at = ? AND id < ?))"
            cursor_args = [created_at, created_at, cursor]
        scan_limit = min(MAX_RECONCILE_SCAN, max(100, limit * 4))
        result = (
            await env.APP_DB.prepare(
                "SELECT id, message_json FROM cf_chat_messages WHERE uid = ? AND "
                + clause
                + cursor_clause
                + " ORDER BY created_at DESC, id DESC LIMIT ?"
            )
            .bind(uid, *args, *cursor_args, scan_limit + 1)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "messages unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    messages: list[dict[str, object]] = []
    next_cursor = cursor
    scanned = 0
    for row in rows[:scan_limit]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        scanned += 1
        next_cursor = str(row["id"])
        message = _stored_message(row)
        if message is None or message.get("reported") is True:
            continue
        messages.append(message)
        if len(messages) == limit:
            break
    has_more = scanned < len(rows) or len(rows) > scan_limit or len(messages) == limit
    if next_cursor == cursor and not messages:
        next_cursor = None
    return {"messages": messages, "next_cursor": next_cursor, "has_more": has_more}


@router.delete("/v2/desktop/messages")
async def delete_desktop_messages(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    scope = _scope(request)
    if isinstance(scope, JSONResponse):
        return scope
    uid = str(context["uid"])
    env = request.scope["env"]
    clause, args = scope
    try:
        now = int(time.time())
        session_expression = (
            "COALESCE(NULLIF(json_extract(message_json, '$.chat_session_id'), ''), "
            "NULLIF(json_extract(message_json, '$.session_id'), ''))"
        )
        matching_messages = "cf_chat_messages.uid = ? AND " + clause
        update_sessions = env.APP_DB.prepare(
            "UPDATE cf_chat_sessions SET message_count = MAX(0, message_count - ("
            "SELECT COUNT(*) FROM cf_chat_messages WHERE "
            + matching_messages
            + " AND "
            + session_expression
            + " = cf_chat_sessions.id)), preview = NULL, updated_at = ? "
            "WHERE cf_chat_sessions.uid = ? AND EXISTS (SELECT 1 FROM cf_chat_messages WHERE "
            + matching_messages
            + " AND "
            + session_expression
            + " = cf_chat_sessions.id)"
        ).bind(uid, *args, now, uid, uid, *args)
        delete_messages = env.APP_DB.prepare("DELETE FROM cf_chat_messages WHERE uid = ? AND " + clause).bind(
            uid, *args
        )
        results = await env.APP_DB.batch([update_sessions, delete_messages])
    except Exception:
        return JSONResponse({"error": "messages unavailable"}, status_code=503)
    deleted = results[1] if isinstance(results, list) and len(results) > 1 else {}
    meta = deleted.get("meta", {}) if isinstance(deleted, dict) else {}
    return {"status": "ok", "deleted_count": max(0, int(meta.get("changes") or 0))}


@router.patch("/v2/desktop/messages/{message_id}/rating")
async def rate_desktop_message(request: Request, message_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _valid_id(message_id):
        return JSONResponse({"error": "message not found"}, status_code=404)
    try:
        payload = RateMessageRequest.model_validate(await _bounded_json(request))
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid rating"}, status_code=400)
    if payload.rating == 0:
        return JSONResponse({"error": "invalid rating"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        if await _existing_message(env, uid, message_id) is None:
            return JSONResponse({"error": "message not found"}, status_code=404)
        value = payload.rating if payload.rating is not None else 0
        await env.APP_DB.batch(chat_feedback_statements(env, uid, message_id, value))
    except Exception:
        return JSONResponse({"error": "messages unavailable"}, status_code=503)
    return {"status": "ok"}
