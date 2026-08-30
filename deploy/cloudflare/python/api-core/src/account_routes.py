"""D1-backed account usage, subscription, and price-catalog reads."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from chat_quota import (
    chat_quota_snapshot,
    is_trial_paywalled,
    plan_policy,
    request_has_valid_byok_keys,
    subscription_plan,
    trial_metadata,
)
from internal_auth import decode_context

router = APIRouter()

BASIC_TRANSCRIPTION_SECONDS_LIMIT = 72_000
PLUS_TRANSCRIPTION_SECONDS_LIMIT = 90_000
MAX_SOURCE_ID_LENGTH = 256
_PERIODS = frozenset({"today", "monthly", "yearly", "all_time"})
_PAID_PLANS = frozenset({"unlimited", "plus", "unlimited_v2", "operator", "architect"})
_WEB_PLANS = frozenset({"plus", "unlimited_v2", "operator", "architect"})
_MOBILE_PLANS = frozenset({"plus", "unlimited_v2"})
_DESKTOP_PLANS = frozenset({"operator", "architect"})

_PLAN_FEATURES = {
    "basic": [
        "1,200 minutes of listening per month",
        "Unlimited words transcribed",
        "Unlimited insights",
        "Unlimited memories",
    ],
    "plus": [
        "1,500 minutes of transcription per month",
        "Unlimited memories and insights",
    ],
    "unlimited_v2": ["Unlimited transcription", "Unlimited memories and insights"],
    "unlimited": ["Unlimited listening and transcription", "Unlimited memories and insights"],
    "operator": ["Unlimited listening and transcription", "Unlimited memories and insights"],
    "architect": ["Unlimited listening, memories, and insights", "Priority desktop AI features"],
}


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _account_created_at(context: dict[str, object]) -> int | None:
    value = context.get("accountCreatedAt")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def usage_source_statement(
    env: object,
    *,
    uid: str,
    source_kind: str,
    source_id: str,
    occurred_at: int,
    transcription_seconds: int = 0,
    words_transcribed: int = 0,
    insights_gained: int = 0,
    memories_created: int = 0,
    updated_at: int | None = None,
):
    """Build an idempotent usage-source upsert for an enclosing D1 batch."""
    if source_kind not in {"conversation", "memory"}:
        raise ValueError("invalid usage source kind")
    if not source_id or len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError("invalid usage source id")
    metrics = (transcription_seconds, words_transcribed, insights_gained, memories_created)
    if any(isinstance(value, bool) or value < 0 for value in metrics):
        raise ValueError("invalid usage metrics")
    return env.APP_DB.prepare(
        "INSERT INTO cf_usage_sources "
        "(uid, source_kind, source_id, occurred_at, transcription_seconds, words_transcribed, "
        "insights_gained, memories_created, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(uid, source_kind, source_id) DO UPDATE SET "
        "occurred_at = excluded.occurred_at, transcription_seconds = excluded.transcription_seconds, "
        "words_transcribed = excluded.words_transcribed, insights_gained = excluded.insights_gained, "
        "memories_created = excluded.memories_created, updated_at = excluded.updated_at"
    ).bind(
        uid,
        source_kind,
        source_id,
        int(occurred_at),
        int(transcription_seconds),
        int(words_transcribed),
        int(insights_gained),
        int(memories_created),
        int(updated_at if updated_at is not None else time.time()),
    )


def _period_bounds(period: str, now: datetime) -> tuple[int | None, int | None]:
    if period == "all_time":
        return None, None
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = datetime.fromtimestamp(start.timestamp() + 86_400, timezone.utc)
    elif period == "monthly":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
    else:
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1)
    return int(start.timestamp()), int(end.timestamp())


def _where(uid: str, bounds: tuple[int | None, int | None]) -> tuple[str, list[object]]:
    start, end = bounds
    if start is None or end is None:
        return "uid = ?", [uid]
    return "uid = ? AND occurred_at >= ? AND occurred_at < ?", [uid, start, end]


def _stats(row: dict[str, object] | None) -> dict[str, int]:
    row = row or {}
    return {
        "transcription_seconds": int(row.get("transcription_seconds") or 0),
        "words_transcribed": int(row.get("words_transcribed") or 0),
        "insights_gained": int(row.get("insights_gained") or 0),
        "memories_created": int(row.get("memories_created") or 0),
        "speech_seconds": 0,
    }


async def _usage_projection(env: object, uid: str, period: str, *, now: datetime | None = None):
    current = now or datetime.now(timezone.utc)
    where, args = _where(uid, _period_bounds(period, current))
    aggregate = (
        await env.APP_DB.prepare(
            "SELECT COALESCE(SUM(transcription_seconds), 0) AS transcription_seconds, "
            "COALESCE(SUM(words_transcribed), 0) AS words_transcribed, "
            "COALESCE(SUM(insights_gained), 0) AS insights_gained, "
            "COALESCE(SUM(memories_created), 0) AS memories_created "
            f"FROM cf_usage_sources WHERE {where}"
        )
        .bind(*args)
        .first()
    )
    bucket_format = {
        "today": "%Y-%m-%dT%H:00:00Z",
        "monthly": "%Y-%m-%d",
        "yearly": "%Y-%m-01",
        "all_time": "%Y-01-01",
    }[period]
    history_result = (
        await env.APP_DB.prepare(
            "SELECT strftime(?, occurred_at, 'unixepoch') AS date, "
            "SUM(transcription_seconds) AS transcription_seconds, "
            "SUM(words_transcribed) AS words_transcribed, SUM(insights_gained) AS insights_gained, "
            "SUM(memories_created) AS memories_created "
            f"FROM cf_usage_sources WHERE {where} GROUP BY date ORDER BY date ASC LIMIT 100"
        )
        .bind(bucket_format, *args)
        .all()
    )
    raw_history = history_result.get("results", []) if isinstance(history_result, dict) else []
    history = [
        {
            "date": str(row.get("date") or ""),
            **{key: value for key, value in _stats(row).items() if key != "speech_seconds"},
        }
        for row in raw_history
        if isinstance(row, dict) and row.get("date")
    ]
    return _stats(aggregate if isinstance(aggregate, dict) else None), history


def _json_list(value: object) -> list[str]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 32_000:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []


async def _subscription_row(env: object, uid: str) -> dict[str, object] | None:
    row = (
        await env.APP_DB.prepare(
            "SELECT plan, status, current_period_start, current_period_end, stripe_subscription_id, "
            "current_price_id, features_json, cancel_at_period_end, show_subscription_ui "
            "FROM cf_user_subscriptions WHERE uid = ?"
        )
        .bind(uid)
        .first()
    )
    return row if isinstance(row, dict) else None


def _plan_limits(plan: str) -> tuple[int, int, int]:
    if plan == "basic":
        return BASIC_TRANSCRIPTION_SECONDS_LIMIT, 0, 0
    if plan == "plus":
        return PLUS_TRANSCRIPTION_SECONDS_LIMIT, 0, 0
    return 0, 0, 0


def _allowed_plans(platform: str, current_plan: str) -> frozenset[str]:
    if platform in {"ios", "android"}:
        if current_plan in _DESKTOP_PLANS:
            return frozenset({current_plan})
        allowed = set(_MOBILE_PLANS)
    elif platform in {"macos", "windows"}:
        allowed = set(_DESKTOP_PLANS)
    else:
        allowed = set(_WEB_PLANS)
    if current_plan == "unlimited":
        allowed.add("unlimited")
    return frozenset(allowed)


@router.get("/v1/users/me/usage")
async def get_user_usage(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    period = request.query_params.get("period", "today")
    if period not in _PERIODS:
        return JSONResponse({"error": "invalid usage period"}, status_code=400)
    try:
        stats, history = await _usage_projection(request.scope["env"], str(context["uid"]), period)
    except Exception:
        return JSONResponse({"error": "usage unavailable"}, status_code=503)
    return {period: stats, "history": history}


@router.get("/v1/users/me/subscription")
async def get_user_subscription(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    uid = str(context["uid"])
    env = request.scope["env"]
    has_byok_keys = request_has_valid_byok_keys(context, request.headers)
    if has_byok_keys:
        chat_quota = await chat_quota_snapshot(env, uid, has_byok_keys=True)
        return {
            "subscription": {
                "plan": "unlimited",
                "status": "active",
                "current_period_start": None,
                "current_period_end": None,
                "stripe_subscription_id": None,
                "current_price_id": None,
                "features": ["byok"],
                "cancel_at_period_end": False,
                "limits": {
                    "transcription_seconds": None,
                    "words_transcribed": None,
                    "insights_gained": None,
                    "chat_questions_per_month": None,
                    "chat_cost_usd_per_month": None,
                },
            },
            "transcription_seconds_used": 0,
            "transcription_seconds_limit": 0,
            "words_transcribed_used": 0,
            "words_transcribed_limit": 0,
            "insights_gained_used": 0,
            "insights_gained_limit": 0,
            "memories_created_used": 0,
            "memories_created_limit": 0,
            "available_plans": [],
            "show_subscription_ui": False,
            "chat_quota_used": chat_quota["used"],
            "chat_quota_unit": chat_quota["unit"],
            "chat_quota_percent": chat_quota["percent"],
            "chat_quota_allowed": chat_quota["allowed"],
            "chat_quota_reset_at": chat_quota["reset_at"],
        }
    try:
        row = await _subscription_row(env, uid)
        plan = str((row or {}).get("plan") or "basic") if (row or {}).get("status") == "active" else "basic"
        if plan not in _PAID_PLANS and plan != "basic":
            plan = "basic"
        monthly, _ = await _usage_projection(env, uid, "monthly")
        chat_quota = await chat_quota_snapshot(
            env,
            uid,
            platform=request.headers.get("x-app-platform"),
            account_created_at=_account_created_at(context),
            has_byok_keys=False,
        )
        catalog = await env.APP_DB.prepare(
            "SELECT COUNT(*) AS count FROM cf_subscription_prices WHERE active = 1"
        ).first()
    except Exception:
        return JSONResponse({"error": "subscription unavailable"}, status_code=503)
    transcription_limit, words_limit, insights_limit = _plan_limits(plan)
    chat_policy = plan_policy(env, plan)
    features = _json_list((row or {}).get("features_json")) or list(_PLAN_FEATURES[plan])
    catalog_available = int(catalog.get("count") or 0) > 0 if isinstance(catalog, dict) else False
    return {
        "subscription": {
            "plan": plan,
            "status": str((row or {}).get("status") or "active"),
            "current_period_start": (row or {}).get("current_period_start"),
            "current_period_end": (row or {}).get("current_period_end"),
            "stripe_subscription_id": (row or {}).get("stripe_subscription_id"),
            "current_price_id": (row or {}).get("current_price_id"),
            "features": features,
            "cancel_at_period_end": bool((row or {}).get("cancel_at_period_end")),
            "limits": {
                "transcription_seconds": transcription_limit or None,
                "words_transcribed": words_limit or None,
                "insights_gained": insights_limit or None,
                "chat_questions_per_month": (int(chat_policy["limit"]) if chat_policy["unit"] == "questions" else None),
                "chat_cost_usd_per_month": (float(chat_policy["limit"]) if chat_policy["unit"] == "cost_usd" else None),
            },
        },
        "transcription_seconds_used": monthly["transcription_seconds"],
        "transcription_seconds_limit": transcription_limit,
        "words_transcribed_used": monthly["words_transcribed"],
        "words_transcribed_limit": words_limit,
        "insights_gained_used": monthly["insights_gained"],
        "insights_gained_limit": insights_limit,
        "memories_created_used": monthly["memories_created"],
        "memories_created_limit": 0,
        "available_plans": [],
        "show_subscription_ui": catalog_available
        and (bool(row.get("show_subscription_ui")) if row is not None else True),
        "chat_quota_used": chat_quota["used"],
        "chat_quota_unit": chat_quota["unit"],
        "chat_quota_percent": chat_quota["percent"],
        "chat_quota_allowed": chat_quota["allowed"],
        "chat_quota_reset_at": chat_quota["reset_at"],
    }


@router.get("/v1/users/me/usage-quota")
async def get_user_chat_usage_quota(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        return await chat_quota_snapshot(
            request.scope["env"],
            str(context["uid"]),
            platform=request.headers.get("x-app-platform"),
            account_created_at=_account_created_at(context),
            has_byok_keys=request_has_valid_byok_keys(context, request.headers),
        )
    except Exception:
        return JSONResponse({"error": "chat quota unavailable"}, status_code=503)


@router.get("/v1/users/me/paywall")
async def get_user_paywall_status(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    try:
        plan = await subscription_plan(env, str(context["uid"]))
    except Exception:
        return {"paywalled": False}
    platform = request.headers.get("x-app-platform") or request.query_params.get("platform")
    return {
        "paywalled": is_trial_paywalled(
            env,
            plan=plan,
            platform=platform,
            account_created_at=_account_created_at(context),
            has_byok_keys=request_has_valid_byok_keys(context, request.headers),
        )
    }


@router.get("/v1/users/me/trial")
async def get_user_trial_status(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    try:
        plan = await subscription_plan(env, str(context["uid"]))
    except Exception:
        # The legacy helper fails open when its entitlement lookup fails.
        plan = "architect"
    return trial_metadata(
        env,
        plan=plan,
        account_created_at=_account_created_at(context),
        has_byok_keys=request_has_valid_byok_keys(context, request.headers),
    )


@router.get("/v1/payments/available-plans")
async def get_available_plans(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    uid = str(context["uid"])
    env = request.scope["env"]
    platform = request.headers.get("x-app-platform", "web").strip().lower()
    try:
        subscription = await _subscription_row(env, uid)
        current_plan = str((subscription or {}).get("plan") or "basic")
        allowed = _allowed_plans(platform, current_plan)
        placeholders = ",".join("?" for _ in allowed)
        result = (
            await env.APP_DB.prepare(
                "SELECT id, plan_id, title, description, subtitle, eyebrow, price_string, interval, unit_amount "
                f"FROM cf_subscription_prices WHERE active = 1 AND plan_id IN ({placeholders}) "
                "ORDER BY CASE interval WHEN 'month' THEN 0 ELSE 1 END, plan_id, id"
            )
            .bind(*sorted(allowed))
            .all()
        )
    except Exception:
        return JSONResponse({"error": "plan catalog unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    current_price_id = (subscription or {}).get("current_price_id")
    return {
        "plans": [
            {
                "id": str(row["id"]),
                "plan_id": str(row["plan_id"]),
                "title": str(row["title"]),
                "description": row.get("description"),
                "subtitle": row.get("subtitle"),
                "eyebrow": row.get("eyebrow"),
                "price_string": str(row["price_string"]),
                "interval": str(row["interval"]),
                "unit_amount": int(row["unit_amount"]),
                "is_active": row.get("id") == current_price_id,
            }
            for row in rows
            if isinstance(row, dict)
        ]
    }
