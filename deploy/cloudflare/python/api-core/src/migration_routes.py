"""Data-protection migration inventory backed by the Cloudflare D1 projection."""

from __future__ import annotations

import hashlib
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context

router = APIRouter()

TARGET_LEVEL = "enhanced"
INVALID_TARGET_DETAIL = "Invalid target_level. Only migration to 'enhanced' is supported."
MAX_REQUEST_BYTES = 64_000
MAX_BATCH_REQUESTS = 500
MAX_ID_LENGTH = 256
MAX_IDEMPOTENCY_KEY_LENGTH = 128
MIGRATION_TYPES = frozenset({"conversation", "memory", "chat"})
MIGRATION_CUTOVER_STATE = "new"
MIGRATION_CUTOVER_PHASE = "completed"


def _error(error: str, status_code: int, **extra: object) -> JSONResponse:
    payload: dict[str, object] = {"error": error}
    payload.update(extra)
    return JSONResponse(payload, status_code=status_code)


def _context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _invalid_target() -> JSONResponse:
    return JSONResponse({"detail": INVALID_TARGET_DETAIL}, status_code=400)


async def _bounded_json(request: Request) -> object:
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("request body is too large")
    return json.loads(raw)


def _valid_text(value: object, *, max_length: int = MAX_ID_LENGTH) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= max_length
        and "\x00" not in value
        and "/" not in value
    )


def _validated_item(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    object_type = value.get("type")
    object_id = value.get("id")
    target_level = value.get("target_level")
    if (
        not isinstance(object_type, str)
        or object_type not in MIGRATION_TYPES
        or not _valid_text(object_id)
        or target_level != TARGET_LEVEL
    ):
        return None
    return {"type": object_type, "id": object_id, "target_level": TARGET_LEVEL}


def _validated_target(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict) or value.get("target_level") != TARGET_LEVEL:
        return None
    # The legacy Pydantic target model ignores unrelated fields, but accepting
    # an object identifier here would make a malformed single-object request
    # look like a global migration.  The boundary is intentionally stricter.
    if "type" in value or "id" in value or "requests" in value:
        return None
    return {"target_level": TARGET_LEVEL}


def _idempotency_inputs(request: Request, payload: dict[str, object]) -> tuple[str, int, str] | JSONResponse:
    key = request.headers.get("idempotency-key")
    raw_generation = request.headers.get("x-account-generation")
    if (
        not isinstance(key, str)
        or not key
        or len(key) > MAX_IDEMPOTENCY_KEY_LENGTH
        or not isinstance(raw_generation, str)
        or not raw_generation
    ):
        return _error("migration idempotency boundary requires Idempotency-Key and X-Account-Generation", 400)
    try:
        generation = int(raw_generation)
    except (TypeError, ValueError):
        return _error("invalid account generation", 400)
    if generation < 0:
        return _error("invalid account generation", 400)
    fingerprint = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return key, generation, fingerprint


async def _migration_authority(request: Request, uid: str) -> int | JSONResponse:
    """Require a completed D1 cutover and a policy-equivalent executor.

    The source projection currently has no encrypted conversation/chat payload
    columns and there is no Cloudflare encryption executor.  This check is
    therefore deliberately fail-closed: even a valid request never writes a
    receipt or returns ``status=ok`` until an operator-populated control row
    explicitly proves that both pieces exist.
    """

    env = request.scope["env"]
    now = int(time.time())
    try:
        fence = await env.APP_DB.prepare(
            "SELECT lifecycle FROM ("
            "SELECT 'deleting' AS lifecycle, 0 AS priority FROM cf_account_deletion_intents WHERE uid = ? "
            "UNION ALL SELECT 'deleted' AS lifecycle, 1 AS priority "
            "FROM cf_account_deletion_tombstones WHERE uid = ? AND expires_at > ?"
            ") ORDER BY priority LIMIT 1"
        ).bind(uid, uid, now).first()
        if fence is not None:
            if not isinstance(fence, dict) or fence.get("lifecycle") not in {"deleting", "deleted"}:
                return _error("migration unavailable", 503, reason="malformed_account_deletion_fence")
            return _error("account deletion in progress", 409)

        cutover = await env.APP_DB.prepare(
            "SELECT uid, schema_version, state, account_generation, checkpoint_phase, "
            "destination_backend_bound FROM cf_account_cutover WHERE uid = ?"
        ).bind(uid).first()
        if not isinstance(cutover, dict) or cutover.get("uid") != uid:
            return _error("migration unavailable", 503, reason="missing_completed_cutover")
        generation = cutover.get("account_generation")
        bound = cutover.get("destination_backend_bound")
        if (
            cutover.get("schema_version") != 1
            or cutover.get("state") != MIGRATION_CUTOVER_STATE
            or cutover.get("checkpoint_phase") != MIGRATION_CUTOVER_PHASE
            or bound not in (1, True)
            or type(generation) is not int
            or generation < 0
        ):
            return _error("migration unavailable", 503, reason="malformed_completed_cutover")

        control = await env.APP_DB.prepare(
            "SELECT uid, schema_version, source, enabled, executor_state, account_generation, source_revision "
            "FROM cf_data_protection_migration_control WHERE uid = ?"
        ).bind(uid).first()
        if not isinstance(control, dict) or control.get("uid") != uid:
            return _error("migration unavailable", 503, reason="missing_migration_capability")
        if (
            control.get("schema_version") != 1
            or control.get("source") != "cloudflare_data_protection_projection"
            or control.get("enabled") not in (1, True)
            or control.get("executor_state") != "ready"
            or control.get("account_generation") != generation
            or not isinstance(control.get("source_revision"), str)
            or not control.get("source_revision")
            or len(str(control.get("source_revision"))) > 256
        ):
            return _error("migration unavailable", 503, reason="encryption_authority_unavailable")
        # The schema intentionally stops at the authority boundary.  Returning
        # the generation lets the caller reject a stale client deterministically;
        # the caller still returns 503 below because a Queue consumer must
        # implement decrypt/encrypt and atomic source writes before admission.
        return generation
    except Exception:
        return _error("migration unavailable", 503, reason="authority_unavailable")


async def _reject_migration(request: Request, uid: str, operation: str, payload: dict[str, object]) -> JSONResponse:
    del operation  # Kept in the helper signature for the future Queue admission contract.
    idempotency = _idempotency_inputs(request, payload)
    if isinstance(idempotency, JSONResponse):
        return idempotency
    _key, requested_generation, _fingerprint = idempotency
    authority = await _migration_authority(request, uid)
    if isinstance(authority, JSONResponse):
        return authority
    if requested_generation != authority:
        return _error("account generation mismatch", 409)
    # ``_migration_authority`` currently always returns a response after
    # checking this invariant.  Retain a defensive failure for future edits
    # so this boundary can never accidentally acknowledge a no-op.
    return _error("migration unavailable", 503, reason="encryption_executor_unavailable")


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


async def _authenticated_uid(request: Request) -> str | JSONResponse:
    context = _context(request)
    if not context:
        return _error("unauthorized", 401)
    uid = context.get("uid")
    if not _valid_text(uid, max_length=256):
        return _error("unauthorized", 401)
    return uid


async def _parse_payload(request: Request) -> dict[str, object] | JSONResponse:
    try:
        raw = await _bounded_json(request)
    except (ValueError, TypeError, json.JSONDecodeError):
        return _error("invalid migration request", 400)
    if not isinstance(raw, dict):
        return _error("invalid migration request", 400)
    return raw


@router.post("/v1/users/migration/requests")
async def handle_migration_requests(request: Request):
    """Fail-closed shadow for the legacy single/global migration endpoint.

    This route deliberately does not call the legacy Firestore helpers.  The
    Cloudflare source projection cannot yet preserve encrypted payloads, so a
    request is acknowledged only after a future executor has been proven by
    the D1 control projection.  At present that executor is unavailable and
    every valid request returns a retryable 503 without a D1 receipt.
    """

    uid = await _authenticated_uid(request)
    if isinstance(uid, JSONResponse):
        return uid
    payload = await _parse_payload(request)
    if isinstance(payload, JSONResponse):
        return payload

    if "type" in payload or "id" in payload:
        normalized = _validated_item(payload)
        if normalized is None:
            return _invalid_target() if payload.get("target_level") != TARGET_LEVEL else _error(
                "invalid migration request", 400
            )
        operation = "single"
        canonical: dict[str, object] = normalized
    else:
        normalized_target = _validated_target(payload)
        if normalized_target is None:
            return _invalid_target() if payload.get("target_level") != TARGET_LEVEL else _error(
                "invalid migration request", 400
            )
        operation = "start"
        canonical = normalized_target
    return await _reject_migration(request, uid, operation, canonical)


@router.post("/v1/users/migration/batch-requests")
async def handle_batch_migration_requests(request: Request):
    """Fail-closed shadow for the legacy batch migration endpoint."""

    uid = await _authenticated_uid(request)
    if isinstance(uid, JSONResponse):
        return uid
    payload = await _parse_payload(request)
    if isinstance(payload, JSONResponse):
        return payload
    requests = payload.get("requests")
    if not isinstance(requests, list) or len(requests) > MAX_BATCH_REQUESTS:
        return _error("invalid migration request", 400)
    normalized: list[dict[str, str]] = []
    for item in requests:
        validated = _validated_item(item)
        if validated is None:
            if isinstance(item, dict) and item.get("target_level") != TARGET_LEVEL:
                return _invalid_target()
            return _error("invalid migration request", 400)
        normalized.append(validated)
    canonical = {"requests": normalized}
    return await _reject_migration(request, uid, "batch", canonical)


@router.post("/v1/users/migration/requests/data-protection-level/finalize")
async def finalize_migration_request(request: Request):
    """Fail-closed shadow for the legacy global-finalize endpoint."""

    uid = await _authenticated_uid(request)
    if isinstance(uid, JSONResponse):
        return uid
    payload = await _parse_payload(request)
    if isinstance(payload, JSONResponse):
        return payload
    normalized = _validated_target(payload)
    if normalized is None:
        return _invalid_target() if payload.get("target_level") != TARGET_LEVEL else _error(
            "invalid migration request", 400
        )
    return await _reject_migration(request, uid, "finalize", normalized)


__all__ = [
    "router",
    "get_migration_requests",
    "handle_migration_requests",
    "handle_batch_migration_requests",
    "finalize_migration_request",
]
