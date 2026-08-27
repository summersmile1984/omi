"""D1-backed focus-session routes for the isolated Cloudflare profile.

Focus sessions are a self-contained event log. The route group migrates the
session CRUD and deterministic daily aggregation only; screen-activity capture,
focus assistant inference, and notification side effects remain legacy-owned.
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from internal_auth import decode_context

router = APIRouter()

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_LIST_LIMIT = 1000
MAX_OFFSET = 1_000_000


class FocusSessionCreate(BaseModel):
    status: str = Field(pattern=r"^(focused|distracted)$")
    app_or_site: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=5000)
    message: str | None = Field(default=None, max_length=5000)
    duration_seconds: int | None = Field(default=None, ge=0, le=86400)


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _requested_day(request: Request) -> datetime | JSONResponse:
    raw = getattr(request, "query_params", {}).get("date")
    if raw in (None, ""):
        return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if not isinstance(raw, str) or not DATE_PATTERN.fullmatch(raw):
        return JSONResponse({"error": "invalid date"}, status_code=422)
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return JSONResponse({"error": "invalid date"}, status_code=422)


def _iso(epoch: object) -> str | None:
    if epoch is None or isinstance(epoch, bool):
        return None
    try:
        return datetime.fromtimestamp(int(epoch), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _response(row: dict[str, object]) -> dict[str, object]:
    duration = row.get("duration_seconds")
    return {
        "id": str(row.get("id") or ""),
        "status": str(row.get("status") or ""),
        "app_or_site": str(row.get("app_or_site") or ""),
        "description": str(row.get("description") or ""),
        "message": row.get("message"),
        "created_at": _iso(row.get("created_at")),
        "duration_seconds": int(duration) if duration is not None else None,
    }


def _query_int(request: Request, name: str, default: int, minimum: int, maximum: int) -> int | None:
    raw = getattr(request, "query_params", {}).get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if minimum <= value <= maximum else None


async def _rows_for_day(
    env: object, uid: str, day: datetime, *, limit: int, offset: int = 0
) -> list[dict[str, object]]:
    next_day = day + timedelta(days=1)
    result = (
        await env.APP_DB.prepare(
            "SELECT id, status, app_or_site, description, message, created_at, duration_seconds "
            "FROM cf_focus_sessions WHERE uid = ? AND created_at >= ? AND created_at < ? "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?"
        )
        .bind(uid, int(day.timestamp()), int(next_day.timestamp()), limit, offset)
        .all()
    )
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [row for row in rows if isinstance(row, dict)]


@router.post("/v1/focus-sessions")
async def create_focus_session(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        session = FocusSessionCreate.model_validate(body)
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid focus session"}, status_code=400)
    session_id = str(uuid.uuid4())
    now = int(time.time())
    try:
        await request.scope["env"].APP_DB.prepare(
            "INSERT INTO cf_focus_sessions "
            "(uid, id, status, app_or_site, description, message, created_at, duration_seconds) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        ).bind(
            str(context["uid"]),
            session_id,
            session.status,
            session.app_or_site,
            session.description,
            session.message,
            now,
            session.duration_seconds,
        ).run()
    except Exception:
        return JSONResponse({"error": "focus sessions unavailable"}, status_code=503)
    return _response(
        {
            "id": session_id,
            "status": session.status,
            "app_or_site": session.app_or_site,
            "description": session.description,
            "message": session.message,
            "created_at": now,
            "duration_seconds": session.duration_seconds,
        }
    )


@router.get("/v1/focus-sessions")
async def list_focus_sessions(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    limit = _query_int(request, "limit", 100, 1, MAX_LIST_LIMIT)
    offset = _query_int(request, "offset", 0, 0, MAX_OFFSET)
    if limit is None or offset is None:
        return JSONResponse({"error": "invalid pagination"}, status_code=400)
    day = _requested_day(request)
    if isinstance(day, JSONResponse):
        return day
    try:
        rows = await _rows_for_day(request.scope["env"], str(context["uid"]), day, limit=limit, offset=offset)
    except Exception:
        return JSONResponse({"error": "focus sessions unavailable"}, status_code=503)
    return [_response(row) for row in rows]


@router.delete("/v1/focus-sessions/{session_id}")
async def delete_focus_session(request: Request, session_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not session_id or len(session_id) > 256:
        return JSONResponse({"error": "invalid session id"}, status_code=400)
    try:
        await request.scope["env"].APP_DB.prepare("DELETE FROM cf_focus_sessions WHERE uid = ? AND id = ?").bind(
            str(context["uid"]), session_id
        ).run()
    except Exception:
        return JSONResponse({"error": "focus sessions unavailable"}, status_code=503)
    return {"status": "ok"}


@router.get("/v1/focus-stats")
async def get_focus_stats(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    day = _requested_day(request)
    if isinstance(day, JSONResponse):
        return day
    try:
        rows = await _rows_for_day(request.scope["env"], str(context["uid"]), day, limit=5000)
    except Exception:
        return JSONResponse({"error": "focus sessions unavailable"}, status_code=503)

    focused_count = 0
    distracted_count = 0
    total_focus_seconds = 0
    total_distracted_seconds = 0
    distractions: dict[str, dict[str, int]] = {}
    for row in rows:
        duration = row.get("duration_seconds") or 0
        if row.get("status") == "focused":
            focused_count += 1
            total_focus_seconds += int(duration)
        elif row.get("status") == "distracted":
            distracted_count += 1
            distracted_duration = int(row.get("duration_seconds") or 60)
            total_distracted_seconds += distracted_duration
            app = str(row.get("app_or_site") or "Unknown")
            entry = distractions.setdefault(app, {"total_seconds": 0, "count": 0})
            entry["total_seconds"] += distracted_duration
            entry["count"] += 1
    top = sorted(distractions.items(), key=lambda item: item[1]["total_seconds"], reverse=True)[:5]
    return {
        "date": day.strftime("%Y-%m-%d"),
        "focused_minutes": total_focus_seconds // 60,
        "distracted_minutes": total_distracted_seconds // 60,
        "session_count": focused_count + distracted_count,
        "focused_count": focused_count,
        "distracted_count": distracted_count,
        "top_distractions": [
            {"app_or_site": app, "total_seconds": values["total_seconds"], "count": values["count"]}
            for app, values in top
        ],
    }
