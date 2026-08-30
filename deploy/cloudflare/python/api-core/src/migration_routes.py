"""Data-protection migration inventory backed by the Cloudflare D1 projection."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context

router = APIRouter()

TARGET_LEVEL = "enhanced"
INVALID_TARGET_DETAIL = "Invalid target_level. Only migration to 'enhanced' is supported."


def _context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _invalid_target() -> JSONResponse:
    return JSONResponse({"detail": INVALID_TARGET_DETAIL}, status_code=400)


@router.get("/v1/users/migration/requests")
async def get_migration_requests(request: Request):
    """List D1 objects that still have the standard protection level.

    The legacy route defaults missing protection metadata to ``standard`` and
    skips only public/shared conversations.  D1 conversations and chat
    messages currently have no separate protection column, so those rows are
    intentionally treated as standard until their projection gains one.
    """

    context = _context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.query_params.get("target_level") != TARGET_LEVEL:
        return _invalid_target()
    uid = context.get("uid")
    if not isinstance(uid, str) or not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    env = request.scope["env"]
    try:
        conversations = (
            await env.APP_DB.prepare(
                "SELECT id FROM cf_conversations "
                "WHERE uid = ? AND COALESCE(visibility, 'private') NOT IN ('public', 'shared') "
                "ORDER BY id"
            )
            .bind(uid)
            .all()
        )
        memories = (
            await env.APP_DB.prepare(
                "SELECT id FROM cf_memories "
                "WHERE uid = ? AND (data_protection_level IS NULL OR data_protection_level != ?) "
                "ORDER BY id"
            )
            .bind(uid, TARGET_LEVEL)
            .all()
        )
        chats = await env.APP_DB.prepare("SELECT id FROM cf_chat_messages WHERE uid = ? ORDER BY id").bind(uid).all()
    except Exception:
        return JSONResponse({"error": "migration inventory unavailable"}, status_code=503)

    needs_migration: list[dict[str, str]] = []
    for row in conversations.get("results", []) if isinstance(conversations, dict) else []:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            needs_migration.append({"id": row["id"], "type": "conversation"})
    for row in memories.get("results", []) if isinstance(memories, dict) else []:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            needs_migration.append({"id": row["id"], "type": "memory"})
    for row in chats.get("results", []) if isinstance(chats, dict) else []:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            needs_migration.append({"id": row["id"], "type": "chat"})
    return {"needs_migration": needs_migration}
