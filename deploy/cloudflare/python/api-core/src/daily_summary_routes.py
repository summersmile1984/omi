"""D1-backed daily-summary projections for the Cloudflare staging profile.

Summary generation is intentionally deterministic here.  It gives the web
client a real D1 owner without pretending that the legacy LLM/notification
pipeline has moved to Workers; richer generation can be added when that
provider contract is migrated.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context

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
    "locations_json, visibility, created_at, updated_at FROM cf_daily_summaries "
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


def _date_range(date_text: str) -> tuple[date, int, int] | None:
    try:
        target = date.fromisoformat(date_text)
    except (TypeError, ValueError):
        return None
    start = int(datetime(target.year, target.month, target.day, tzinfo=timezone.utc).timestamp())
    return target, start, start + 86_400


def _summary_id(date_text: str) -> str:
    return f"cf-daily-{date_text}"


async def _build_summary(request: Request, uid: str, date_text: str) -> tuple[dict[str, object], int] | JSONResponse:
    parsed = _date_range(date_text)
    if parsed is None:
        return JSONResponse({"error": "invalid date format. use YYYY-MM-DD"}, status_code=400)
    target, start, end = parsed
    env = request.scope["env"]
    try:
        result = (
            await env.APP_DB.prepare(
                "SELECT id, started_at, finished_at, structured_json FROM cf_conversations "
                "WHERE uid = ? AND discarded = 0 AND is_locked = 0 AND COALESCE(started_at, created_at) >= ? "
                "AND COALESCE(started_at, created_at) < ? ORDER BY created_at ASC, id ASC"
            )
            .bind(uid, start, end)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "daily summaries unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    conversations = [row for row in rows if isinstance(row, dict)]
    if not conversations:
        return JSONResponse({"error": f"no conversations found for {date_text}"}, status_code=400)
    action_items = 0
    duration_seconds = 0
    for row in conversations:
        structured = _json_object(row.get("structured_json"))
        action_items += len(_json_list(json.dumps(structured.get("action_items", []))))
        try:
            started = int(row.get("started_at") or 0)
            finished = int(row.get("finished_at") or started)
            duration_seconds += max(0, min(finished - started, 24 * 60 * 60))
        except (TypeError, ValueError):
            pass
    summary = {
        "id": _summary_id(date_text),
        "date": target.isoformat(),
        "headline": "Your Day in Review",
        "day_emoji": "📅",
        "overview": f"You had {len(conversations)} conversation{'s' if len(conversations) != 1 else ''} on {date_text}.",
        "stats": {
            "total_conversations": len(conversations),
            "total_duration_minutes": round(duration_seconds / 60),
            "action_items_count": action_items,
        },
        "highlights": [],
        "action_items": [],
        "unresolved_questions": [],
        "decisions_made": [],
        "knowledge_nuggets": [],
        "locations": [],
    }
    now = int(time.time())
    values = (
        uid,
        summary["id"],
        summary["date"],
        summary["headline"],
        summary["day_emoji"],
        summary["overview"],
        json.dumps(summary["stats"], ensure_ascii=False, separators=(",", ":")),
        "[]",
        "[]",
        "[]",
        "[]",
        "[]",
        "[]",
        "private",
        now,
        now,
    )
    try:
        existing = (
            await env.APP_DB.prepare("SELECT id FROM cf_daily_summaries WHERE uid = ? AND date = ?")
            .bind(uid, date_text)
            .first()
        )
        if isinstance(existing, dict):
            summary["id"] = str(existing.get("id") or summary["id"])
            values = (uid, summary["id"], summary["date"], *values[3:])
            await env.APP_DB.prepare(
                "UPDATE cf_daily_summaries SET headline = ?, day_emoji = ?, overview = ?, stats_json = ?, "
                "highlights_json = ?, action_items_json = ?, unresolved_questions_json = ?, decisions_made_json = ?, "
                "knowledge_nuggets_json = ?, locations_json = ?, updated_at = ? WHERE uid = ? AND id = ?"
            ).bind(
                summary["headline"],
                summary["day_emoji"],
                summary["overview"],
                values[6],
                "[]",
                "[]",
                "[]",
                "[]",
                "[]",
                "[]",
                now,
                uid,
                summary["id"],
            ).run()
        else:
            await env.APP_DB.prepare(
                "INSERT INTO cf_daily_summaries "
                "(uid, id, date, headline, day_emoji, overview, stats_json, highlights_json, action_items_json, "
                "unresolved_questions_json, decisions_made_json, knowledge_nuggets_json, locations_json, visibility, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ).bind(*values).run()
        row = await _first_summary(env, uid, str(summary["id"]))
    except Exception:
        return JSONResponse({"error": "daily summaries unavailable"}, status_code=503)
    return (_response(row) if isinstance(row, dict) else summary), len(conversations)


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
    if not isinstance(date_text, str):
        date_text = datetime.now(timezone.utc).date().isoformat()
    built = await _build_summary(request, str(context["uid"]), date_text)
    if isinstance(built, JSONResponse):
        return built
    summary, count = built
    return {
        "status": "ok",
        "message": f"Daily summary generated for {date_text}",
        "summary_id": summary["id"],
        "conversations_count": count,
    }


@router.post("/v1/users/daily-summaries/{summary_id}/regenerate")
async def regenerate_daily_summary(request: Request, summary_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    uid = str(context["uid"])
    try:
        existing = await _first_summary(request.scope["env"], uid, summary_id)
    except Exception:
        return JSONResponse({"error": "daily summaries unavailable"}, status_code=503)
    if existing is None:
        return JSONResponse({"error": "daily summary not found"}, status_code=404)
    built = await _build_summary(request, uid, str(existing.get("date") or ""))
    if isinstance(built, JSONResponse):
        return built
    return built[0]
