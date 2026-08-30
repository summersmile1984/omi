"""Retired migration endpoints kept as authenticated, side-effect-free shims.

These routes were released to clients before Candidate became the sole task
authority.  The legacy service now treats them as inert compatibility APIs;
keeping the same response envelopes in API Core lets the edge move them off
the legacy runtime without reviving the old Firestore migration behavior.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context

router = APIRouter()

MAX_CURSOR_LENGTH = 256
MAX_PAGE_SIZE = 100


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _valid_pagination(request: Request) -> bool:
    raw_limit = request.query_params.get("limit")
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return False
        if limit < 1 or limit > MAX_PAGE_SIZE:
            return False
    cursor = request.query_params.get("cursor")
    return cursor is None or 0 < len(cursor) <= MAX_CURSOR_LENGTH


def _require_auth(request: Request) -> JSONResponse | None:
    if not _auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _valid_pagination(request):
        return JSONResponse({"detail": "invalid pagination"}, status_code=422)
    return None


@router.post("/v1/staged-tasks/migrate")
async def migrate_ai_tasks(request: Request):
    denial = _require_auth(request)
    if denial:
        return denial
    return {"status": "legacy task migration retired; no action taken"}


@router.post("/v1/staged-tasks/migrate-conversation-items")
async def migrate_conversation_items(request: Request):
    denial = _require_auth(request)
    if denial:
        return denial
    return {
        "status": "ok",
        "migrated": 0,
        "deleted": 0,
        "restored": 0,
        "skipped_existing": 0,
        "has_more": False,
        "next_cursor": None,
    }


@router.post("/v1/action-items/restore-legacy-conversation-items")
async def restore_legacy_conversation_items(request: Request):
    denial = _require_auth(request)
    if denial:
        return denial
    return {
        "status": "ok",
        "restored": 0,
        "skipped_existing": 0,
        "has_more": False,
        "next_cursor": None,
    }
