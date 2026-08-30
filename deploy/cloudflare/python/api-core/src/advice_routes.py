"""Canonical D1 advice routes for the isolated Cloudflare profile."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from internal_auth import decode_context

router = APIRouter()

MAX_REQUEST_BYTES = 32_000
MAX_ID_LENGTH = 256
MAX_LIST_LIMIT = 1_000
MAX_OFFSET = 9_223_372_036_854_775_807


class AdviceCreate(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)
    category: str | None = Field(default=None, max_length=100)
    reasoning: str | None = Field(default=None, max_length=5_000)
    source_app: str | None = Field(default=None, max_length=200)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    context_summary: str | None = Field(default=None, max_length=5_000)
    current_activity: str | None = Field(default=None, max_length=500)


class AdviceUpdate(BaseModel):
    is_read: bool | None = None
    is_dismissed: bool | None = None


_COLUMNS = (
    "id, content, category, reasoning, source_app, confidence, context_summary, current_activity, "
    "created_at, updated_at, is_read, is_dismissed"
)


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


async def _bounded_json(request: Request) -> object:
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("request body exceeds size limit")
    return json.loads(raw)


def _iso(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _bool(value: object) -> bool:
    return bool(value) and value not in ("0", "false", "False", "no")


def _response(row: dict[str, object]) -> dict[str, object]:
    confidence = row.get("confidence")
    return {
        "id": str(row.get("id") or ""),
        "content": str(row.get("content") or ""),
        "category": str(row.get("category") or "other"),
        "reasoning": row.get("reasoning"),
        "source_app": row.get("source_app"),
        "confidence": float(confidence) if confidence is not None else 0.5,
        "context_summary": row.get("context_summary"),
        "current_activity": row.get("current_activity"),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "is_read": _bool(row.get("is_read")),
        "is_dismissed": _bool(row.get("is_dismissed")),
    }


def _query_int(request: Request, name: str, default: int, minimum: int, maximum: int) -> int | None:
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if minimum <= value <= maximum else None


def _query_bool(request: Request, name: str, default: bool) -> bool | None:
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return default
    if not isinstance(raw, str):
        return None
    normalized = raw.lower()
    if normalized in {"1", "true", "on", "yes"}:
        return True
    if normalized in {"0", "false", "off", "no"}:
        return False
    return None


@router.post("/v1/advice")
async def create_advice(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        advice = AdviceCreate.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid advice"}, status_code=422)

    advice_id = str(uuid.uuid4())
    uid = str(context["uid"])
    category = advice.category or "other"
    now = int(time.time())
    row: dict[str, object] = {
        "id": advice_id,
        "content": advice.content,
        "category": category,
        "reasoning": advice.reasoning,
        "source_app": advice.source_app,
        "confidence": advice.confidence,
        "context_summary": advice.context_summary,
        "current_activity": advice.current_activity,
        "created_at": now,
        "updated_at": now,
        "is_read": 0,
        "is_dismissed": 0,
    }
    try:
        await request.scope["env"].APP_DB.prepare(
            "INSERT INTO cf_advice "
            "(uid, id, content, category, reasoning, source_app, confidence, context_summary, current_activity, "
            "created_at, updated_at, is_read, is_dismissed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)"
        ).bind(
            uid,
            advice_id,
            advice.content,
            category,
            advice.reasoning,
            advice.source_app,
            advice.confidence,
            advice.context_summary,
            advice.current_activity,
            now,
            now,
        ).run()
    except Exception:
        return JSONResponse({"error": "advice unavailable"}, status_code=503)
    return _response(row)


@router.get("/v1/advice")
async def list_advice(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    limit = _query_int(request, "limit", 100, 1, MAX_LIST_LIMIT)
    offset = _query_int(request, "offset", 0, 0, MAX_OFFSET)
    include_dismissed = _query_bool(request, "include_dismissed", False)
    if limit is None or offset is None or include_dismissed is None:
        return JSONResponse({"error": "invalid advice query"}, status_code=422)

    uid = str(context["uid"])
    category = request.query_params.get("category")
    query = f"SELECT {_COLUMNS} FROM cf_advice WHERE uid = ?"
    args: list[object] = [uid]
    if isinstance(category, str) and category:
        query += " AND category = ?"
        args.append(category)
    if not include_dismissed:
        query += " AND is_dismissed = 0"
    query += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    args.extend((limit, offset))
    try:
        result = await request.scope["env"].APP_DB.prepare(query).bind(*args).all()
    except Exception:
        return JSONResponse({"error": "advice unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [_response(row) for row in rows if isinstance(row, dict)]


@router.patch("/v1/advice/{advice_id}")
async def update_advice(request: Request, advice_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not advice_id or len(advice_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "advice not found"}, status_code=404)
    try:
        update = AdviceUpdate.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid advice"}, status_code=422)

    assignments: list[str] = []
    values: list[object] = []
    if update.is_read is not None:
        assignments.append("is_read = ?")
        values.append(int(update.is_read))
    if update.is_dismissed is not None:
        assignments.append("is_dismissed = ?")
        values.append(int(update.is_dismissed))
    assignments.append("updated_at = ?")
    values.extend((int(time.time()), str(context["uid"]), advice_id))
    try:
        row = (
            await request.scope["env"]
            .APP_DB.prepare(
                "UPDATE cf_advice SET " + ", ".join(assignments) + f" WHERE uid = ? AND id = ? RETURNING {_COLUMNS}"
            )
            .bind(*values)
            .first()
        )
    except Exception:
        return JSONResponse({"error": "advice unavailable"}, status_code=503)
    if not isinstance(row, dict):
        return JSONResponse({"error": "advice not found"}, status_code=404)
    return _response(row)


@router.delete("/v1/advice/{advice_id}")
async def delete_advice(request: Request, advice_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not advice_id or len(advice_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid advice id"}, status_code=400)
    try:
        await request.scope["env"].APP_DB.prepare("DELETE FROM cf_advice WHERE uid = ? AND id = ?").bind(
            str(context["uid"]), advice_id
        ).run()
    except Exception:
        return JSONResponse({"error": "advice unavailable"}, status_code=503)
    return {"status": "ok"}


@router.post("/v1/advice/mark-all-read")
async def mark_all_advice_read(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(
                "UPDATE cf_advice SET is_read = 1, updated_at = ? WHERE uid = ? AND is_read = 0 RETURNING id"
            )
            .bind(int(time.time()), str(context["uid"]))
            .all()
        )
    except Exception:
        return JSONResponse({"error": "advice unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return {"status": f"marked {len(rows)} as read"}
