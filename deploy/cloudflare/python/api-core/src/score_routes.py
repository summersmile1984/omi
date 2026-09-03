"""D1-backed productivity score projections for the Cloudflare profile.

Scores are a read-only projection over the migrated ``cf_action_items`` table.
They intentionally do not claim legacy analytics or conversation-derived scores:
the contract is the same daily/weekly/overall completion calculation used by
the existing action-item score endpoints.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context

router = APIRouter()

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


async def _count(env: object, uid: str, clause: str, bounds: tuple[int, int]) -> tuple[int, int]:
    row = (
        await env.APP_DB.prepare(
            "SELECT COUNT(*) AS total, COALESCE(SUM(CASE WHEN completed = 1 THEN 1 ELSE 0 END), 0) AS completed "
            f"FROM cf_action_items WHERE uid = ? AND deleted = 0 AND {clause}"
        )
        .bind(uid, *bounds)
        .first()
    )
    if not isinstance(row, dict):
        return 0, 0
    try:
        total = max(0, int(row.get("total") or 0))
        completed = min(total, max(0, int(row.get("completed") or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0, 0
    return completed, total


def _score(completed: int, total: int, decimals: int) -> float | int:
    value = (completed / total * 100) if total else 0
    return round(value, decimals)


@router.get("/v1/daily-score")
async def get_daily_score(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    day = _requested_day(request)
    if isinstance(day, JSONResponse):
        return day
    next_day = day + timedelta(days=1)
    try:
        completed, total = await _count(
            request.scope["env"],
            str(context["uid"]),
            "due_at >= ? AND due_at < ?",
            (int(day.timestamp()), int(next_day.timestamp())),
        )
    except Exception:
        return JSONResponse({"error": "scores unavailable"}, status_code=503)
    return {
        "date": day.strftime("%Y-%m-%d"),
        "score": int(_score(completed, total, 0)),
        "completed_tasks": completed,
        "total_tasks": total,
    }


@router.get("/v1/scores")
async def get_scores(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    day = _requested_day(request)
    if isinstance(day, JSONResponse):
        return day
    next_day = day + timedelta(days=1)
    week_start = day - timedelta(days=6)
    env = request.scope["env"]
    uid = str(context["uid"])
    try:
        daily_completed, daily_total = await _count(
            env,
            uid,
            "due_at >= ? AND due_at < ?",
            (int(day.timestamp()), int(next_day.timestamp())),
        )
        weekly_completed, weekly_total = await _count(
            env,
            uid,
            "created_at >= ? AND created_at < ?",
            (int(week_start.timestamp()), int(next_day.timestamp())),
        )
        overall_row = (
            await env.APP_DB.prepare(
                "SELECT COUNT(*) AS total, COALESCE(SUM(CASE WHEN completed = 1 THEN 1 ELSE 0 END), 0) AS completed "
                "FROM cf_action_items WHERE uid = ? AND deleted = 0"
            )
            .bind(uid)
            .first()
        )
        if not isinstance(overall_row, dict):
            overall_completed = overall_total = 0
        else:
            overall_total = max(0, int(overall_row.get("total") or 0))
            overall_completed = min(overall_total, max(0, int(overall_row.get("completed") or 0)))
    except (TypeError, ValueError, OverflowError):
        return JSONResponse({"error": "scores unavailable"}, status_code=503)
    except Exception:
        return JSONResponse({"error": "scores unavailable"}, status_code=503)

    daily_score = _score(daily_completed, daily_total, 1)
    weekly_score = _score(weekly_completed, weekly_total, 1)
    overall_score = _score(overall_completed, overall_total, 1)
    if daily_total > 0 and daily_score >= weekly_score and daily_score >= overall_score:
        default_tab = "daily"
    elif weekly_score >= overall_score:
        default_tab = "weekly"
    else:
        default_tab = "overall"
    return {
        "daily": {
            "score": daily_score,
            "completed_tasks": daily_completed,
            "total_tasks": daily_total,
        },
        "weekly": {
            "score": weekly_score,
            "completed_tasks": weekly_completed,
            "total_tasks": weekly_total,
        },
        "overall": {
            "score": overall_score,
            "completed_tasks": overall_completed,
            "total_tasks": overall_total,
        },
        "default_tab": default_tab,
        "date": day.strftime("%Y-%m-%d"),
    }
