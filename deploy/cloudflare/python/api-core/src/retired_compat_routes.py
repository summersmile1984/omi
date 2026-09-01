"""Retired migration endpoints and narrow Cloudflare-owned compatibility routes.

These routes were released to clients before Candidate became the sole task
authority.  Most remain inert compatibility APIs; the Limitless delete route
is an explicit exception because the Cloudflare importer now owns a canonical
D1 conversation projection and can safely remove it without reviving Firestore.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context

router = APIRouter()

MAX_CURSOR_LENGTH = 256
MAX_PAGE_SIZE = 100
MAX_LIMITLESS_DELETE_BATCH = 30


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


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


@router.delete("/v1/import/limitless/conversations")
async def delete_limitless_conversations(request: Request):
    """Delete only the caller's Cloudflare-projected Limitless conversations.

    The Jobs importer writes a canonical ``source='limitless'`` marker, so the
    old no-op compatibility response is no longer safe: it leaves imported
    content visible after a user explicitly asks to remove it.  Keep the
    released response envelope while making the mutation uid-scoped and
    deletion-fence aware.  Vector rows are removed asynchronously
    through the canonical D1 outbox; the route never calls a provider.
    """
    if denial := _require_auth(request):
        return denial
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    database = getattr(env, "APP_DB", None)
    if database is None:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)
    uid = str(context["uid"])
    now = int(time.time())
    try:
        fence = (
            await database.prepare(
                "SELECT 1 AS fenced FROM cf_account_deletion_intents WHERE uid = ? "
                "UNION ALL SELECT 1 AS fenced FROM cf_account_deletion_tombstones "
                "WHERE uid = ? AND expires_at > ? LIMIT 1"
            )
            .bind(uid, uid, now)
            .first()
        )
        if isinstance(fence, dict):
            return JSONResponse({"error": "account deletion in progress"}, status_code=409)
        deleted_count = 0
        while True:
            rows = (
                await database.prepare(
                    "SELECT id, updated_at, created_at FROM cf_conversations "
                    "WHERE uid = ? AND source = 'limitless' ORDER BY created_at, id "
                    f"LIMIT {MAX_LIMITLESS_DELETE_BATCH}"
                )
                .bind(uid)
                .all()
            )
            conversations = [
                row
                for row in (rows.get("results", []) if isinstance(rows, dict) else [])
                if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
            ]
            if not conversations:
                break
            statements = []
            for row in conversations:
                conversation_id = str(row["id"])
                desired_version = max(
                    now,
                    _as_int(row.get("updated_at"), _as_int(row.get("created_at"), now)) + 1,
                )
                # Re-check the source marker in each mutation statement so a
                # concurrent source update cannot make this route delete an Omi
                # conversation with the same id.
                statements.extend(
                    [
                        database.prepare(
                            "DELETE FROM cf_shared_conversation_index WHERE uid = ? "
                            "AND conversation_id = ? AND EXISTS (SELECT 1 FROM cf_conversations "
                            "WHERE uid = ? AND id = ? AND source = 'limitless')"
                        ).bind(uid, conversation_id, uid, conversation_id),
                        database.prepare(
                            "INSERT INTO cf_vector_projection_outbox "
                            "(uid, source_kind, source_id, desired_version, operation, attempts, "
                            "next_attempt_at, last_error, created_at, updated_at) "
                            "SELECT ?, 'conversation', ?, ?, 'delete', 0, ?, NULL, ?, ? "
                            "WHERE EXISTS (SELECT 1 FROM cf_conversations "
                            "WHERE uid = ? AND id = ? AND source = 'limitless') "
                            "ON CONFLICT(uid, source_kind, source_id) DO UPDATE SET "
                            "desired_version = excluded.desired_version, operation = 'delete', "
                            "attempts = 0, next_attempt_at = excluded.next_attempt_at, "
                            "last_error = NULL, updated_at = excluded.updated_at "
                            "WHERE excluded.desired_version >= cf_vector_projection_outbox.desired_version"
                        ).bind(
                            uid,
                            conversation_id,
                            desired_version,
                            now,
                            now,
                            now,
                            uid,
                            conversation_id,
                        ),
                        database.prepare(
                            "DELETE FROM cf_conversations WHERE uid = ? AND id = ? AND source = 'limitless'"
                        ).bind(uid, conversation_id),
                    ]
                )
            await database.batch(statements)
            ids = [str(row["id"]) for row in conversations]
            placeholders = ", ".join("?" for _ in ids)
            remaining_selected = (
                await database.prepare(
                    "SELECT COUNT(*) AS count FROM cf_conversations " f"WHERE uid = ? AND id IN ({placeholders})"
                )
                .bind(uid, *ids)
                .first()
            )
            deleted_count += max(
                0,
                len(conversations)
                - (_as_int(remaining_selected.get("count")) if isinstance(remaining_selected, dict) else 0),
            )
        return {
            "deleted_count": deleted_count,
            "message": f"Successfully deleted {deleted_count} Limitless conversations",
        }
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)
