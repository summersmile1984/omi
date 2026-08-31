"""D1-backed chat-session attachment projection for the Cloudflare boundary.

The file upload authority lives in the Jobs Worker.  This router only links
already-ready canonical file rows to an owner-scoped D1 chat session and reads
that link back.  It intentionally does not create OpenAI threads/runs or
pretend that a provider file id is a replacement for the legacy Assistants
session contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from chat_session_routes import _auth_context, _bounded_json

router = APIRouter()

MAX_SESSION_FILE_IDS = 20
MAX_SESSION_ID_LENGTH = 256
MAX_FILE_ID_LENGTH = 128


class AttachSessionFilesRequest(BaseModel):
    model_config = {"extra": "forbid"}

    file_ids: list[str] = Field(min_length=1, max_length=MAX_SESSION_FILE_IDS)


def _valid_identifier(value: str, maximum: int) -> bool:
    return 0 < len(value) <= maximum and "\x00" not in value and "/" not in value


def _file_projection(row: dict[str, object]) -> dict[str, object]:
    created_at = int(row.get("created_at") or 0)
    return {
        "id": str(row["file_id"]),
        "name": str(row.get("name") or ""),
        "mime_type": str(row.get("mime_type") or "application/octet-stream"),
        "size": max(0, int(row.get("size") or 0)),
        "openai_file_id": row.get("provider_file_id"),
        "created_at": datetime.fromtimestamp(created_at, timezone.utc).isoformat(),
        # API Core has no CHAT_FILES bucket binding.  The Jobs Worker remains
        # the owner of signed thumbnail URLs, so do not expose a storage key.
        "thumbnail": None,
        "thumb_name": None,
        "attached_at": int(row.get("attached_at") or 0),
    }


async def _session_exists(env: object, uid: str, session_id: str) -> bool:
    row = (
        await env.APP_DB.prepare("SELECT id FROM cf_chat_sessions WHERE uid = ? AND id = ? LIMIT 1")
        .bind(uid, session_id)
        .first()
    )
    return isinstance(row, dict)


async def read_session_chat_files(
    env: object,
    uid: str,
    session_id: str,
    *,
    file_ids: list[str] | None = None,
) -> list[dict[str, object]]:
    """Read ready canonical files attached to one owner-scoped session.

    This helper is the only reader contract future chat providers should use;
    it never trusts a provider id supplied by the client and never returns
    failed/deleted file rows.
    """

    if not _valid_identifier(uid, MAX_SESSION_ID_LENGTH) or not _valid_identifier(session_id, MAX_SESSION_ID_LENGTH):
        return []
    query = (
        "SELECT sf.file_id, sf.attached_at, f.name, f.mime_type, f.size, "
        "f.provider_file_id, f.created_at "
        "FROM cf_chat_session_files sf "
        "JOIN cf_chat_files f ON f.uid = sf.uid AND f.file_id = sf.file_id "
        "WHERE sf.uid = ? AND sf.session_id = ? AND f.status = 'ready'"
    )
    args: list[object] = [uid, session_id]
    if file_ids is not None:
        if not file_ids or len(file_ids) > MAX_SESSION_FILE_IDS:
            return []
        placeholders = ", ".join("?" for _ in file_ids)
        query += f" AND sf.file_id IN ({placeholders})"
        args.extend(file_ids)
    query += " ORDER BY sf.attached_at ASC, sf.file_id ASC LIMIT ?"
    args.append(MAX_SESSION_FILE_IDS)
    result = await env.APP_DB.prepare(query).bind(*args).all()
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [_file_projection(row) for row in rows if isinstance(row, dict)]


async def read_ready_chat_files(
    env: object,
    uid: str,
    file_ids: list[str],
) -> list[dict[str, object]]:
    """Read canonical ready files in the caller's requested order.

    This reader is used by the persistence-only message projection before it
    links a message's file ids to its D1 session.  It deliberately reads the
    canonical row rather than trusting a provider id supplied by a client.
    """

    if not _valid_identifier(uid, MAX_SESSION_ID_LENGTH) or not file_ids:
        return []
    if len(file_ids) > MAX_SESSION_FILE_IDS or any(
        not _valid_identifier(file_id, MAX_FILE_ID_LENGTH) for file_id in file_ids
    ):
        return []
    placeholders = ", ".join("?" for _ in file_ids)
    result = await env.APP_DB.prepare(
        "SELECT file_id, name, mime_type, size, provider_file_id, created_at "
        "FROM cf_chat_files WHERE uid = ? AND status = 'ready' "
        f"AND file_id IN ({placeholders})"
    ).bind(uid, *file_ids).all()
    rows = result.get("results", []) if isinstance(result, dict) else []
    by_id = {
        str(row["file_id"]): _file_projection(row)
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("file_id"), str)
    }
    return [by_id[file_id] for file_id in file_ids if file_id in by_id]


async def _ready_file_rows(env: object, uid: str, file_ids: list[str]) -> list[dict[str, object]]:
    placeholders = ", ".join("?" for _ in file_ids)
    result = await env.APP_DB.prepare(
        "SELECT file_id FROM cf_chat_files WHERE uid = ? AND status = 'ready' "
        f"AND file_id IN ({placeholders})"
    ).bind(uid, *file_ids).all()
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [row for row in rows if isinstance(row, dict)]


@router.get("/v2/cf/chat-sessions/{session_id}/files")
async def list_session_chat_files(request: Request, session_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _valid_identifier(session_id, MAX_SESSION_ID_LENGTH):
        return JSONResponse({"error": "chat session not found"}, status_code=404)
    uid = str(context["uid"])
    try:
        if not await _session_exists(request.scope["env"], uid, session_id):
            return JSONResponse({"error": "chat session not found"}, status_code=404)
        return await read_session_chat_files(request.scope["env"], uid, session_id)
    except Exception:
        return JSONResponse({"error": "chat session files unavailable"}, status_code=503)


@router.post("/v2/cf/chat-sessions/{session_id}/files")
async def attach_session_chat_files(request: Request, session_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _valid_identifier(session_id, MAX_SESSION_ID_LENGTH):
        return JSONResponse({"error": "chat session not found"}, status_code=404)
    try:
        payload = AttachSessionFilesRequest.model_validate(await _bounded_json(request))
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid chat session files"}, status_code=400)

    # Duplicate ids cannot add information and would make the response order
    # ambiguous.  Reject them before any D1 mutation.
    if len(set(payload.file_ids)) != len(payload.file_ids) or any(
        not _valid_identifier(file_id, MAX_FILE_ID_LENGTH) for file_id in payload.file_ids
    ):
        return JSONResponse({"error": "invalid chat session files"}, status_code=400)

    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        if not await _session_exists(env, uid, session_id):
            return JSONResponse({"error": "chat session not found"}, status_code=404)
        ready_rows = await _ready_file_rows(env, uid, payload.file_ids)
        ready_ids = {str(row.get("file_id")) for row in ready_rows}
        if ready_ids != set(payload.file_ids):
            # Do not reveal whether a missing id belongs to another account or
            # is a failed/deleted canonical row.
            return JSONResponse({"error": "chat file not found"}, status_code=404)
        now = int(time.time())
        await env.APP_DB.batch(
            [
                env.APP_DB.prepare(
                    "INSERT OR IGNORE INTO cf_chat_session_files "
                    "(uid, session_id, file_id, source_message_id, attached_at) "
                    "VALUES (?, ?, ?, NULL, ?)"
                ).bind(uid, session_id, file_id, now)
                for file_id in payload.file_ids
            ]
            + [
                env.APP_DB.prepare(
                    "UPDATE cf_chat_sessions SET updated_at = ? WHERE uid = ? AND id = ?"
                ).bind(now, uid, session_id)
            ]
        )
        rows = await read_session_chat_files(env, uid, session_id)
        by_id = {str(row["id"]): row for row in rows}
        return [by_id[file_id] for file_id in payload.file_ids if file_id in by_id]
    except Exception:
        return JSONResponse({"error": "chat session files unavailable"}, status_code=503)


@router.delete("/v2/cf/chat-sessions/{session_id}/files/{file_id}")
async def detach_session_chat_file(request: Request, session_id: str, file_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _valid_identifier(session_id, MAX_SESSION_ID_LENGTH) or not _valid_identifier(file_id, MAX_FILE_ID_LENGTH):
        return JSONResponse({"error": "chat file not found"}, status_code=404)
    uid = str(context["uid"])
    try:
        result = await request.scope["env"].APP_DB.prepare(
            "DELETE FROM cf_chat_session_files WHERE uid = ? AND session_id = ? AND file_id = ?"
        ).bind(uid, session_id, file_id).run()
    except Exception:
        return JSONResponse({"error": "chat session files unavailable"}, status_code=503)
    changes = int((result.get("meta", {}) if isinstance(result, dict) else {}).get("changes") or 0)
    if changes != 1:
        return JSONResponse({"error": "chat file not found"}, status_code=404)
    return {"status": "ok", "id": file_id}
