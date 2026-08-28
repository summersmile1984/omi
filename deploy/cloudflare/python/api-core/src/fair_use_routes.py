"""D1-backed fair-use status projection for isolated Workers accounts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context

router = APIRouter()

DEFAULT_LIMITS_MS = (7_200_000, 28_800_000, 36_000_000)
UNLIMITED_LIMITS_MS = (14_400_000, 57_600_000, 72_000_000)
UNLIMITED_PLANS = frozenset({"unlimited", "unlimited_v2", "operator", "architect"})
RESTRICT_DAILY_DG_MS = 1_800_000
LIVE_SOURCE_KINDS = ("realtime", "sync_fresh")


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _hours(milliseconds: int) -> float:
    return round(milliseconds / 3_600_000, 2)


def _percentage(milliseconds: int, limit: int) -> float:
    return round(milliseconds / limit * 100, 1) if limit > 0 else 0.0


def _message(stage: str, case_ref: str = "") -> str:
    ref_note = f" Your case reference is {case_ref}." if case_ref else ""
    messages = {
        "none": "Your usage is within normal limits.",
        "warning": (
            "Your usage is higher than typical. Omi is designed for personal conversations. "
            f"If non-personal content transcription continues, your service may be adjusted.{ref_note}"
        ),
        "throttle": (
            "Your transcription quality has been temporarily reduced due to high non-personal usage. "
            "This will reset automatically. Contact support at team@basedhardware.com if you believe this is an error. "
            f"Please quote your case reference when contacting support.{ref_note}"
        ),
        "restrict": (
            "Your cloud transcription is temporarily limited. On-device transcription continues normally. "
            "Contact support at team@basedhardware.com to discuss your usage and resolve this. "
            f"Please quote your case reference when contacting support.{ref_note}"
        ),
    }
    return messages.get(stage, messages["none"])


async def _projection(env: object, uid: str, now: int) -> dict[str, object]:
    state = (
        await env.APP_DB.prepare(
            "SELECT stage, last_case_ref, throttle_until, restrict_until " "FROM cf_fair_use_states WHERE uid = ?"
        )
        .bind(uid)
        .first()
    )
    subscription = await env.APP_DB.prepare("SELECT plan FROM cf_user_subscriptions WHERE uid = ?").bind(uid).first()
    cutoffs = (now - 86_400, now - 3 * 86_400, now - 7 * 86_400)
    live_usage = (
        await env.APP_DB.prepare(
            "SELECT "
            "COALESCE(SUM(CASE WHEN occurred_at >= ? THEN speech_ms ELSE 0 END), 0) AS daily_ms, "
            "COALESCE(SUM(CASE WHEN occurred_at >= ? THEN speech_ms ELSE 0 END), 0) AS three_day_ms, "
            "COALESCE(SUM(CASE WHEN occurred_at >= ? THEN speech_ms ELSE 0 END), 0) AS weekly_ms "
            "FROM cf_fair_use_usage_sources "
            "WHERE uid = ? AND source_kind IN (?, ?) AND occurred_at >= ?"
        )
        .bind(cutoffs[0], cutoffs[1], cutoffs[2], uid, *LIVE_SOURCE_KINDS, cutoffs[2])
        .first()
    )
    day_start = int(
        datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )
    dg_usage = (
        await env.APP_DB.prepare(
            "SELECT COALESCE(SUM(dg_ms), 0) AS used_ms FROM cf_fair_use_usage_sources "
            "WHERE uid = ? AND source_kind IN (?, ?) AND occurred_at >= ?"
        )
        .bind(uid, *LIVE_SOURCE_KINDS, day_start)
        .first()
    )
    state = state if isinstance(state, dict) else {}
    subscription = subscription if isinstance(subscription, dict) else {}
    live_usage = live_usage if isinstance(live_usage, dict) else {}
    dg_usage = dg_usage if isinstance(dg_usage, dict) else {}
    stage = str(state.get("stage") or "none")
    restrict_until = state.get("restrict_until")
    if stage == "restrict" and (
        isinstance(restrict_until, bool) or not isinstance(restrict_until, (int, float)) or int(restrict_until) < now
    ):
        stage = "throttle"
    case_ref = str(state.get("last_case_ref") or "")[:64]
    plan = str(subscription.get("plan") or "basic")
    limits = UNLIMITED_LIMITS_MS if plan in UNLIMITED_PLANS else DEFAULT_LIMITS_MS
    usage = tuple(max(0, int(live_usage.get(key) or 0)) for key in ("daily_ms", "three_day_ms", "weekly_ms"))
    used_dg_ms = max(0, int(dg_usage.get("used_ms") or 0))
    next_midnight = datetime.fromtimestamp(now, timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    return {
        "stage": stage,
        "case_ref": case_ref,
        "speech_hours_today": _hours(usage[0]),
        "speech_hours_3day": _hours(usage[1]),
        "speech_hours_weekly": _hours(usage[2]),
        "limits": {
            "daily_hours": _hours(limits[0]),
            "three_day_hours": _hours(limits[1]),
            "weekly_hours": _hours(limits[2]),
        },
        "usage_pct": {
            "daily": _percentage(usage[0], limits[0]),
            "three_day": _percentage(usage[1], limits[1]),
            "weekly": _percentage(usage[2], limits[2]),
        },
        "dg_budget": {
            "daily_limit_ms": RESTRICT_DAILY_DG_MS,
            "used_ms": used_dg_ms,
            "remaining_ms": max(0, RESTRICT_DAILY_DG_MS - used_dg_ms),
            "exhausted": used_dg_ms >= RESTRICT_DAILY_DG_MS,
            "resets_at": next_midnight.isoformat().replace("+00:00", "Z"),
        },
        "message": _message(stage, case_ref),
    }


@router.get("/v1/fair-use/status")
async def get_fair_use_status(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        return await _projection(request.scope["env"], str(context["uid"]), int(time.time()))
    except Exception:
        return JSONResponse({"error": "fair use status unavailable"}, status_code=503)
