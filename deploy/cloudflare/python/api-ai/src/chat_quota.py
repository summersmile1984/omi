"""Atomic D1 reservation and Workers AI cost settlement for chat turns."""

from __future__ import annotations

from datetime import datetime, timezone
import time

DEFAULT_FREE_CHAT_QUESTIONS_PER_MONTH = 30
DEFAULT_INPUT_USD_PER_MILLION = 0.051
DEFAULT_OUTPUT_USD_PER_MILLION = 0.335
DEFAULT_TRIAL_LENGTH_SECONDS = 3 * 24 * 60 * 60
DESKTOP_PLATFORMS = frozenset({"macos", "windows", "desktop"})


def _positive_number(env: object, name: str, default: float) -> float:
    raw = getattr(env, name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _month_bounds(now: datetime | None = None) -> tuple[int, int]:
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


def trial_paywall_applies(
    env: object,
    *,
    platform: str | None,
    account_created_at: int | None,
    has_byok_keys: bool,
    now: int | None = None,
) -> bool:
    enabled = str(getattr(env, "TRIAL_PAYWALL_ENABLED", "false")).strip().lower() == "true"
    if not enabled or has_byok_keys or not platform or platform.strip().lower() not in DESKTOP_PLATFORMS:
        return False
    if not isinstance(account_created_at, int) or isinstance(account_created_at, bool) or account_created_at <= 0:
        return False
    current = int(time.time()) if now is None else now
    duration = int(_positive_number(env, "TRIAL_LENGTH_SECONDS", float(DEFAULT_TRIAL_LENGTH_SECONDS)))
    return current - account_created_at > duration


async def reserve_chat_question(
    env: object,
    *,
    uid: str,
    idempotency_key: str,
    message_id: str,
    chat_session_id: str | None,
    platform: str | None,
    account_created_at: int | None = None,
    has_byok_keys: bool = False,
    occurred_at: int | None = None,
    source: str = "v2_messages",
) -> bool:
    """Reserve one question atomically; only Free is hard-capped.

    The conditional INSERT is one D1 statement so concurrent request handlers
    cannot both observe the final free slot and overshoot the monthly cap.
    Paid plans still record usage but enter the legacy overage path.
    """
    database = getattr(env, "APP_DB", None)
    if database is None:
        raise RuntimeError("chat accounting is not configured")
    now = int(occurred_at if occurred_at is not None else time.time())
    start, end = _month_bounds(datetime.fromtimestamp(now, timezone.utc))
    limit = int(_positive_number(env, "FREE_CHAT_QUESTIONS_PER_MONTH", DEFAULT_FREE_CHAT_QUESTIONS_PER_MONTH))
    trial_blocked = trial_paywall_applies(
        env,
        platform=platform,
        account_created_at=account_created_at,
        has_byok_keys=has_byok_keys,
        now=now,
    )
    await database.prepare(
        "INSERT OR IGNORE INTO cf_chat_quota_events "
        "(uid, idempotency_key, source, message_id, chat_session_id, platform, occurred_at) "
        "SELECT ?, ?, ?, ?, ?, ?, ? WHERE "
        "COALESCE((SELECT CASE WHEN status = 'active' THEN plan ELSE 'basic' END "
        "FROM cf_user_subscriptions WHERE uid = ?), 'basic') != 'basic' OR "
        "(? = 0 AND (SELECT COUNT(*) FROM cf_chat_quota_events "
        "WHERE uid = ? AND occurred_at >= ? AND occurred_at < ?) < ?"
        ")"
    ).bind(
        uid,
        idempotency_key,
        source,
        message_id,
        chat_session_id,
        platform,
        now,
        uid,
        int(trial_blocked),
        uid,
        start,
        end,
        limit,
    ).run()
    row = (
        await database.prepare("SELECT 1 AS reserved FROM cf_chat_quota_events WHERE uid = ? AND idempotency_key = ?")
        .bind(uid, idempotency_key)
        .first()
    )
    return isinstance(row, dict) and int(row.get("reserved") or 0) == 1


async def reserve_stateless_chat_question(
    env: object,
    *,
    uid: str,
    idempotency_key: str,
    message_id: str,
    platform: str | None,
    account_created_at: int | None = None,
    has_byok_keys: bool = False,
    occurred_at: int | None = None,
) -> bool:
    """Reserve one question for generation that intentionally has no session.

    Stateless drafts still consume the same monthly entitlement as a normal
    chat turn, but their accounting row must not invent a chat session or
    write either side of the generated exchange to chat history.
    """
    return await reserve_chat_question(
        env,
        uid=uid,
        idempotency_key=idempotency_key,
        message_id=message_id,
        chat_session_id=None,
        platform=platform,
        account_created_at=account_created_at,
        has_byok_keys=has_byok_keys,
        occurred_at=occurred_at,
        source="v2_chat_generate_reply",
    )


async def free_quota_detail(
    env: object,
    uid: str,
    *,
    now: datetime | None = None,
    force_exhausted: bool = False,
) -> dict[str, object]:
    start, end = _month_bounds(now)
    row = (
        await env.APP_DB.prepare(
            "SELECT COUNT(*) AS used FROM cf_chat_quota_events "
            "WHERE uid = ? AND occurred_at >= ? AND occurred_at < ?"
        )
        .bind(uid, start, end)
        .first()
    )
    if not isinstance(row, dict):
        raise RuntimeError("chat quota projection unavailable")
    limit = float(_positive_number(env, "FREE_CHAT_QUESTIONS_PER_MONTH", DEFAULT_FREE_CHAT_QUESTIONS_PER_MONTH))
    used = limit if force_exhausted else float(row.get("used") or 0)
    return {
        "error": "quota_exceeded",
        "plan": "Free",
        "plan_type": "basic",
        "unit": "questions",
        "used": used,
        "limit": limit,
        "reset_at": end,
    }


def provider_usage(result: object) -> tuple[int, int] | None:
    if not isinstance(result, dict):
        return None
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if (
        isinstance(prompt, bool)
        or isinstance(completion, bool)
        or not isinstance(prompt, (int, float))
        or not isinstance(completion, (int, float))
        or prompt < 0
        or completion < 0
    ):
        return None
    return int(prompt), int(completion)


def provider_cost_usd(env: object, prompt_tokens: int, completion_tokens: int) -> float:
    input_rate = _positive_number(env, "WORKERS_AI_CHAT_INPUT_USD_PER_MILLION", DEFAULT_INPUT_USD_PER_MILLION)
    output_rate = _positive_number(env, "WORKERS_AI_CHAT_OUTPUT_USD_PER_MILLION", DEFAULT_OUTPUT_USD_PER_MILLION)
    return round((prompt_tokens * input_rate + completion_tokens * output_rate) / 1_000_000, 10)


def settlement_statement(
    env: object,
    *,
    uid: str,
    idempotency_key: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
):
    return env.APP_DB.prepare(
        "UPDATE cf_chat_quota_events SET cost_usd = ?, prompt_tokens = ?, completion_tokens = ?, "
        "model = ?, settled_at = ? WHERE uid = ? AND idempotency_key = ? AND settled_at IS NULL"
    ).bind(
        cost_usd,
        prompt_tokens,
        completion_tokens,
        model,
        int(time.time()),
        uid,
        idempotency_key,
    )


async def settle_failed_question(env: object, *, uid: str, idempotency_key: str, model: str) -> None:
    """Close a reservation that produced no usable provider result.

    The question remains counted, matching the legacy pre-provider event. A
    zero cost is only written for a request that did not yield a billable usage
    object or a user-visible answer.
    """
    await settlement_statement(
        env,
        uid=uid,
        idempotency_key=idempotency_key,
        model=model,
        prompt_tokens=0,
        completion_tokens=0,
        cost_usd=0.0,
    ).run()


__all__ = [
    "free_quota_detail",
    "provider_cost_usd",
    "provider_usage",
    "reserve_chat_question",
    "reserve_stateless_chat_question",
    "settle_failed_question",
    "settlement_statement",
    "trial_paywall_applies",
]
