"""D1 daily-summary reads and mutations; generation has one day-scoped owner."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context
from daily_summary_generation import generate_summary
from pydantic import BaseModel

router = APIRouter()

MAX_ID_LENGTH = 256
MAX_LIST_LIMIT = 100
MAX_OFFSET = 100_000
MAX_JSON_BYTES = 1_000_000


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _json_list(value: object) -> list[object]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_JSON_BYTES:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _json_object(value: object) -> dict[str, object]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_JSON_BYTES:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _iso(value: object) -> str:
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.fromtimestamp(0, timezone.utc).isoformat()


def _response(row: dict[str, object]) -> dict[str, object]:
    return {
        "id": str(row.get("id") or ""),
        "date": str(row.get("date") or ""),
        "headline": str(row.get("headline") or "Your Day in Review"),
        "day_emoji": str(row.get("day_emoji") or "📅"),
        "overview": str(row.get("overview") or ""),
        "stats": _json_object(row.get("stats_json")),
        "highlights": _json_list(row.get("highlights_json")),
        "action_items": _json_list(row.get("action_items_json")),
        "unresolved_questions": _json_list(row.get("unresolved_questions_json")),
        "decisions_made": _json_list(row.get("decisions_made_json")),
        "knowledge_nuggets": _json_list(row.get("knowledge_nuggets_json")),
        "locations": _json_list(row.get("locations_json")),
        "memories_learned": _json_list(row.get("memories_learned_json")),
        **({"regenerated_at": _iso(row["regenerated_at"])} if row.get("regenerated_at") is not None else {}),
        "created_at": _iso(row.get("created_at")),
    }


def _public_response(row: dict[str, object]) -> dict[str, object]:
    response = _response(row)
    return {
        key: response[key]
        for key in (
            "id",
            "date",
            "headline",
            "overview",
            "day_emoji",
            "stats",
            "highlights",
            "action_items",
            "decisions_made",
            "knowledge_nuggets",
        )
    }


_SELECT = (
    "SELECT uid, id, date, headline, day_emoji, overview, stats_json, highlights_json, "
    "action_items_json, unresolved_questions_json, decisions_made_json, knowledge_nuggets_json, "
    "locations_json, memories_learned_json, regenerated_at, visibility, created_at, updated_at FROM cf_daily_summaries "
)


async def _first_summary(env: object, uid: str, summary_id: str) -> dict[str, object] | None:
    row = await env.APP_DB.prepare(_SELECT + "WHERE uid = ? AND id = ?").bind(uid, summary_id).first()
    return row if isinstance(row, dict) else None


def _pagination(request: Request) -> tuple[int, int] | JSONResponse:
    try:
        limit = int(request.query_params.get("limit", "30"))
        offset = int(request.query_params.get("offset", "0"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid pagination"}, status_code=400)
    if limit < 1 or limit > MAX_LIST_LIMIT or offset < 0 or offset > MAX_OFFSET:
        return JSONResponse({"error": "invalid pagination"}, status_code=400)
    return limit, offset


@router.get("/v1/daily-summaries/{summary_id}/shared")
async def get_shared_daily_summary(request: Request, summary_id: str):
    if not summary_id or len(summary_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid summary id"}, status_code=400)
    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(_SELECT + "WHERE id = ? AND visibility = 'shared' ORDER BY uid ASC LIMIT 2")
            .bind(summary_id)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "daily summaries unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    shared = [row for row in rows if isinstance(row, dict)]
    if len(shared) != 1:
        return JSONResponse({"detail": "Daily summary not found"}, status_code=404)
    return _public_response(shared[0])


@router.get("/v1/users/daily-summaries")
async def list_daily_summaries(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    pagination = _pagination(request)
    if isinstance(pagination, JSONResponse):
        return pagination
    limit, offset = pagination
    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(_SELECT + "WHERE uid = ? ORDER BY date DESC, id DESC LIMIT ? OFFSET ?")
            .bind(str(context["uid"]), limit, offset)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "daily summaries unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return {"summaries": [_response(row) for row in rows if isinstance(row, dict)]}


@router.get("/v1/users/daily-summaries/{summary_id}")
async def get_daily_summary(request: Request, summary_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not summary_id or len(summary_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid summary id"}, status_code=400)
    try:
        row = await _first_summary(request.scope["env"], str(context["uid"]), summary_id)
    except Exception:
        return JSONResponse({"error": "daily summaries unavailable"}, status_code=503)
    if row is None:
        return JSONResponse({"error": "daily summary not found"}, status_code=404)
    return _response(row)


@router.patch("/v1/users/daily-summaries/{summary_id}/visibility")
async def set_daily_summary_visibility(request: Request, summary_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    value = request.query_params.get("value")
    if value not in {"private", "shared"}:
        return JSONResponse({"error": "invalid visibility value"}, status_code=400)
    uid = str(context["uid"])
    try:
        if await _first_summary(request.scope["env"], uid, summary_id) is None:
            return JSONResponse({"error": "daily summary not found"}, status_code=404)
        await request.scope["env"].APP_DB.prepare(
            "UPDATE cf_daily_summaries SET visibility = ?, updated_at = ? WHERE uid = ? AND id = ?"
        ).bind(value, int(time.time()), uid, summary_id).run()
    except Exception:
        return JSONResponse({"error": "daily summaries unavailable"}, status_code=503)
    return {"status": "Ok"}


@router.delete("/v1/users/daily-summaries/{summary_id}")
async def delete_daily_summary(request: Request, summary_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    uid = str(context["uid"])
    try:
        if await _first_summary(request.scope["env"], uid, summary_id) is None:
            return JSONResponse({"error": "daily summary not found"}, status_code=404)
        await request.scope["env"].APP_DB.prepare("DELETE FROM cf_daily_summaries WHERE uid = ? AND id = ?").bind(
            uid, summary_id
        ).run()
    except Exception:
        return JSONResponse({"error": "daily summaries unavailable"}, status_code=503)
    return {"status": "ok"}


class CreateDailySummaryRequest(BaseModel):
    date: str


@router.post("/v1/users/daily-summaries")
async def create_daily_summary(request: Request, data: CreateDailySummaryRequest):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    result = await generate_summary(request, context, data.date)
    return result if isinstance(result, JSONResponse) else _response(result)


@router.post("/v1/users/daily-summary-settings/test")
async def test_daily_summary(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
    except (TypeError, ValueError):
        payload = {}
    date_text = payload.get("date") if isinstance(payload, dict) else None
    result = await generate_summary(request, context, date_text)
    if isinstance(result, JSONResponse):
        return result
    summary = _response(result)
    return {
        "status": "ok",
        "message": f"Daily summary generated for {summary['date']}",
        "summary_id": summary["id"],
        "conversations_count": summary["stats"].get("total_conversations", 0),
    }


@router.post("/v1/users/daily-summaries/{summary_id}/regenerate")
async def regenerate_daily_summary(request: Request, summary_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    result = await generate_summary(request, context, summary_id=summary_id)
    return result if isinstance(result, JSONResponse) else _response(result)
