"""D1-backed chat quota projections shared by account read routes."""

from __future__ import annotations

from datetime import datetime, timezone

PLAN_DISPLAY_NAMES = {
    "basic": "Free",
    "unlimited": "Neo",
    "plus": "Plus",
    "unlimited_v2": "Unlimited",
    "operator": "Operator",
    "architect": "Architect",
}
PAID_PLANS = frozenset(plan for plan in PLAN_DISPLAY_NAMES if plan != "basic")
DESKTOP_PLATFORMS = frozenset({"macos", "windows", "desktop"})
TRIAL_LENGTH_SECONDS = 3 * 24 * 60 * 60


def _positive_number(env: object, name: str, default: float) -> float:
    raw = getattr(env, name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def plan_policy(env: object, plan: str) -> dict[str, object]:
    if plan == "architect":
        return {
            "unit": "cost_usd",
            "limit": _positive_number(env, "ARCHITECT_CHAT_COST_USD_PER_MONTH", 400.0),
        }
    defaults = {
        "basic": ("FREE_CHAT_QUESTIONS_PER_MONTH", 30.0),
        "unlimited": ("NEO_CHAT_QUESTIONS_PER_MONTH", 200.0),
        "plus": ("PLUS_CHAT_QUESTIONS_PER_MONTH", 200.0),
        "unlimited_v2": ("UNLIMITED_V2_CHAT_QUESTIONS_PER_MONTH", 1_000.0),
        "operator": ("OPERATOR_CHAT_QUESTIONS_PER_MONTH", 500.0),
    }
    variable, default = defaults.get(plan, defaults["basic"])
    return {"unit": "questions", "limit": _positive_number(env, variable, default)}


def _trial_paywall_enabled(env: object) -> bool:
    return str(getattr(env, "TRIAL_PAYWALL_ENABLED", "false")).strip().lower() == "true"


def _trial_length(env: object) -> int:
    return int(_positive_number(env, "TRIAL_LENGTH_SECONDS", float(TRIAL_LENGTH_SECONDS)))


def request_has_all_byok_keys(headers: object) -> bool:
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return False
    return all(
        bool(str(getter(f"x-byok-{provider}") or "").strip())
        for provider in ("openai", "anthropic", "gemini", "deepgram")
    )


def is_trial_paywalled(
    env: object,
    *,
    plan: str,
    platform: str | None,
    account_created_at: int | None,
    has_byok_keys: bool = False,
    now: int | None = None,
) -> bool:
    if not _trial_paywall_enabled(env) or plan in PAID_PLANS or has_byok_keys:
        return False
    if not platform or platform.strip().lower() not in DESKTOP_PLATFORMS:
        return False
    if not isinstance(account_created_at, int) or isinstance(account_created_at, bool) or account_created_at <= 0:
        return False
    current = int(datetime.now(timezone.utc).timestamp()) if now is None else now
    return current - account_created_at > _trial_length(env)


def trial_metadata(
    env: object,
    *,
    plan: str,
    account_created_at: int | None,
    has_byok_keys: bool = False,
    now: int | None = None,
) -> dict[str, object]:
    duration = _trial_length(env)
    base: dict[str, object] = {
        "trial_started_at": None,
        "trial_ends_at": None,
        "trial_remaining_seconds": 0,
        "trial_expired": False,
        "trial_duration_seconds": duration,
        "trial_features": [
            "unlimited_listening",
            "unlimited_transcription",
            "unlimited_memories",
            "unlimited_insights",
            f"{int(_positive_number(env, 'FREE_CHAT_QUESTIONS_PER_MONTH', 30.0))}_chat_questions_per_month",
        ],
        "plan_after_trial": "Free",
    }
    if (
        not _trial_paywall_enabled(env)
        or plan in PAID_PLANS
        or has_byok_keys
        or not isinstance(account_created_at, int)
        or isinstance(account_created_at, bool)
        or account_created_at <= 0
    ):
        return base
    current = int(datetime.now(timezone.utc).timestamp()) if now is None else now
    end = account_created_at + duration
    remaining = max(0, end - current)
    return {
        **base,
        "trial_started_at": account_created_at,
        "trial_ends_at": end,
        "trial_remaining_seconds": remaining,
        "trial_expired": remaining == 0,
    }


def month_bounds(now: datetime | None = None) -> tuple[int, int]:
    start = (
        (now or datetime.now(timezone.utc))
        .astimezone(timezone.utc)
        .replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    )
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return int(start.timestamp()), int(end.timestamp())


async def chat_quota_snapshot(
    env: object,
    uid: str,
    *,
    now: datetime | None = None,
    platform: str | None = None,
    account_created_at: int | None = None,
    has_byok_keys: bool = False,
) -> dict[str, object]:
    start, end = month_bounds(now)
    plan = await subscription_plan(env, uid)
    start_day = datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%d")
    end_day = datetime.fromtimestamp(end, timezone.utc).strftime("%Y-%m-%d")
    usage = (
        await env.APP_DB.prepare(
            "SELECT COUNT(*) AS questions, "
            "COALESCE(SUM(CASE WHEN source = 'v2_messages' AND settled_at IS NULL THEN 1 ELSE 0 END), 0) "
            "AS unsettled, COALESCE((SELECT SUM(cost_usd) FROM cf_llm_usage_daily "
            "WHERE uid = ? AND usage_day >= ? AND usage_day < ? AND "
            "((usage_kind = 'feature' AND feature = 'chat') OR "
            "(usage_kind = 'bucket' AND feature = 'desktop_chat'))), 0) AS cost_usd "
            "FROM cf_chat_quota_events WHERE uid = ? AND occurred_at >= ? AND occurred_at < ?"
        )
        .bind(uid, start_day, end_day, uid, start, end)
        .first()
    )
    if not isinstance(usage, dict):
        raise RuntimeError("chat quota projection unavailable")
    policy = plan_policy(env, plan)
    unit = str(policy["unit"])
    if unit == "cost_usd" and int(usage.get("unsettled") or 0) > 0:
        # Never tell an Architect user they have spent $0 while an in-Worker
        # provider call is still unaccounted for. Desktop persistence events use
        # the separate daily bucket report and therefore have no per-event cost
        # settlement to wait for.
        raise RuntimeError("chat cost projection has unsettled events")
    used = float(usage.get("cost_usd") or 0) if unit == "cost_usd" else float(usage.get("questions") or 0)
    limit = float(policy["limit"])
    percent = min(100.0, round(100.0 * used / limit, 2)) if limit > 0 else 0.0
    if is_trial_paywalled(
        env,
        plan=plan,
        platform=platform,
        account_created_at=account_created_at,
        has_byok_keys=has_byok_keys,
        now=int((now or datetime.now(timezone.utc)).timestamp()),
    ):
        used = limit
        percent = 100.0
    return {
        "plan": PLAN_DISPLAY_NAMES[plan],
        "plan_type": plan,
        "unit": unit,
        "used": round(used, 4),
        "limit": limit,
        "percent": percent,
        "allowed": used < limit,
        "reset_at": end,
    }


async def subscription_plan(env: object, uid: str) -> str:
    row = await env.APP_DB.prepare("SELECT plan, status FROM cf_user_subscriptions WHERE uid = ?").bind(uid).first()
    if isinstance(row, dict) and row.get("status") == "active":
        plan = str(row.get("plan") or "basic")
        if plan in PLAN_DISPLAY_NAMES:
            return plan
    return "basic"


__all__ = [
    "PAID_PLANS",
    "chat_quota_snapshot",
    "is_trial_paywalled",
    "month_bounds",
    "plan_policy",
    "request_has_all_byok_keys",
    "subscription_plan",
    "trial_metadata",
]
