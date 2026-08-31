"""D1-owned status and lifecycle routes for Limitless ZIP imports.

The upload/admission path lives in the Jobs Worker because it owns the staged
R2 object and Queue.  This module owns only the uid-scoped D1 job lifecycle;
the Queue consumer is the sole writer of imported conversations.  Keeping the
two surfaces separate prevents a status or cancel request from accidentally
becoming a second parser/producer.
"""

from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context

router = APIRouter()

MAX_JOB_ID_LENGTH = 128
MAX_LIMIT = 1000
ACTIVE_STATUSES = frozenset({"pending", "processing"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _as_int(value: object, default: int = 0) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed


def _created_at(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return datetime.fromtimestamp(_as_int(value), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _job_response(row: dict[str, Any]) -> dict[str, object]:
    return {
        "job_id": str(row.get("job_id") or ""),
        "status": str(row.get("status") or "failed"),
        "total_files": _as_int(row.get("total_files")),
        "processed_files": _as_int(row.get("processed_files")),
        "conversations_created": _as_int(row.get("conversations_created")),
        "created_at": _created_at(row.get("created_at")),
        "error": row.get("last_error"),
    }


async def _uid_and_generation(env: object, uid: str) -> tuple[int, str | None]:
    cutover = await env.APP_DB.prepare(
        "SELECT account_generation FROM cf_account_cutover WHERE uid = ?"
    ).bind(uid).first()
    generation = _as_int(cutover.get("account_generation")) if isinstance(cutover, dict) else 0
    now = int(time.time())
    fence = await env.APP_DB.prepare(
        "SELECT lifecycle FROM ("
        "SELECT 'deleting' AS lifecycle, 0 AS priority FROM cf_account_deletion_intents WHERE uid = ? "
        "UNION ALL SELECT 'deleted' AS lifecycle, 1 AS priority "
        "FROM cf_account_deletion_tombstones WHERE uid = ? AND expires_at > ?"
        ") ORDER BY priority LIMIT 1"
    ).bind(uid, uid, now).first()
    lifecycle = fence.get("lifecycle") if isinstance(fence, dict) else None
    if lifecycle not in (None, "deleting", "deleted"):
        raise ValueError("invalid account deletion fence")
    return generation, lifecycle


async def _job(request: Request, uid: str, job_id: str) -> dict[str, Any] | None:
    row = await request.scope["env"].APP_DB.prepare(
        "SELECT uid, job_id, status, total_files, processed_files, conversations_created, "
        "created_at, last_error, account_generation, source_object_key "
        "FROM cf_import_jobs WHERE uid = ? AND job_id = ?"
    ).bind(uid, job_id).first()
    return row if isinstance(row, dict) else None


@router.get("/v1/import/jobs")
async def list_import_jobs(request: Request, limit: int = 50):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    uid = str(context["uid"])
    bounded_limit = max(1, min(int(limit), MAX_LIMIT))
    try:
        _generation, lifecycle = await _uid_and_generation(request.scope["env"], uid)
        if lifecycle is not None:
            return JSONResponse({"error": "account deletion in progress"}, status_code=409)
        result = await request.scope["env"].APP_DB.prepare(
            "SELECT job_id, status, total_files, processed_files, conversations_created, created_at, last_error "
            "FROM cf_import_jobs WHERE uid = ? ORDER BY created_at DESC, job_id DESC LIMIT ?"
        ).bind(uid, bounded_limit).all()
        rows = result.get("results", []) if isinstance(result, dict) else []
        return [_job_response(row) for row in rows if isinstance(row, dict)]
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid import job state"}, status_code=503)
    except Exception:
        return JSONResponse({"error": "import jobs unavailable"}, status_code=503)


@router.get("/v1/import/jobs/{job_id}")
async def get_import_job_status(job_id: str, request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not job_id or len(job_id) > MAX_JOB_ID_LENGTH:
        return JSONResponse({"error": "import job not found"}, status_code=404)
    uid = str(context["uid"])
    try:
        _generation, lifecycle = await _uid_and_generation(request.scope["env"], uid)
        if lifecycle is not None:
            return JSONResponse({"error": "account deletion in progress"}, status_code=409)
        row = await _job(request, uid, job_id)
        if row is None:
            return JSONResponse({"error": "Import job not found"}, status_code=404)
        return _job_response(row)
    except Exception:
        return JSONResponse({"error": "import job unavailable"}, status_code=503)


@router.post("/v1/import/jobs/{job_id}/cancel")
async def cancel_import_job(job_id: str, request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not job_id or len(job_id) > MAX_JOB_ID_LENGTH:
        return JSONResponse({"error": "Import job not found"}, status_code=404)
    uid = str(context["uid"])
    now = int(time.time())
    try:
        _generation, lifecycle = await _uid_and_generation(request.scope["env"], uid)
        if lifecycle is not None:
            return JSONResponse({"error": "account deletion in progress"}, status_code=409)
        updated = await request.scope["env"].APP_DB.prepare(
            "UPDATE cf_import_jobs SET status = 'cancelled', last_error = 'Cancelled by user', "
            "lease_token = NULL, lease_until = NULL, completed_at = ?, updated_at = ? "
            "WHERE uid = ? AND job_id = ? AND status IN ('pending', 'processing') "
            "RETURNING job_id, status, total_files, processed_files, conversations_created, created_at, last_error"
        ).bind(now, now, uid, job_id).first()
        if isinstance(updated, dict):
            return _job_response(updated)
        row = await _job(request, uid, job_id)
        if row is None:
            return JSONResponse({"error": "Import job not found"}, status_code=404)
        if str(row.get("status")) in ACTIVE_STATUSES:
            return JSONResponse({"error": "import job unavailable"}, status_code=503)
        return JSONResponse({"error": "Only a pending or processing import can be cancelled"}, status_code=409)
    except Exception:
        return JSONResponse({"error": "import job unavailable"}, status_code=503)


def _safe_import_object(uid: str, value: object) -> str | None:
    if not isinstance(value, str) or not value.startswith(f"imports/{uid}/"):
        return None
    if len(value) > 512 or ".." in value or "\\" in value or "\x00" in value:
        return None
    return value


@router.delete("/v1/import/jobs/{job_id}")
async def delete_import_job(job_id: str, request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not job_id or len(job_id) > MAX_JOB_ID_LENGTH:
        return JSONResponse({"error": "Import job not found"}, status_code=404)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        _generation, lifecycle = await _uid_and_generation(env, uid)
        if lifecycle is not None:
            return JSONResponse({"error": "account deletion in progress"}, status_code=409)
        row = await _job(request, uid, job_id)
        if row is None:
            return JSONResponse({"error": "Import job not found"}, status_code=404)
        status = str(row.get("status") or "")
        if status in ACTIVE_STATUSES:
            return JSONResponse({"error": "Cancel the in-progress import before deleting it"}, status_code=409)
        object_key = _safe_import_object(uid, row.get("source_object_key"))
        if object_key is None:
            return JSONResponse({"error": "import job storage is unavailable"}, status_code=503)
        assets = getattr(env, "ASSETS", None)
        if assets is not None:
            try:
                await assets.delete(object_key)
            except Exception:
                return JSONResponse({"error": "import job storage is unavailable"}, status_code=503)
        deleted = await env.APP_DB.prepare(
            "DELETE FROM cf_import_jobs WHERE uid = ? AND job_id = ? AND status IN "
            "('completed', 'failed', 'cancelled')"
        ).bind(uid, job_id).run()
        if getattr(deleted, "meta", {}).get("changes") != 1:
            return JSONResponse({"error": "import job unavailable"}, status_code=503)
        return {"status": "ok", "job_id": job_id}
    except Exception:
        return JSONResponse({"error": "import job unavailable"}, status_code=503)


__all__ = ["router"]
