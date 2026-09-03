"""D1-backed fair-use restriction checks before Worker ASR inference."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

from fastapi.responses import JSONResponse

from fallback import record_fallback

DEFAULT_CAPS_MS = (7_200_000, 28_800_000, 36_000_000)
MAX_DAILY_AUDIO_MS = 108_000_000
DEFAULT_RETRY_AFTER_SECONDS = 60 * 60
MAX_RETRY_AFTER_SECONDS = 30 * 24 * 60 * 60
LIVE_SOURCE_KINDS = ("realtime", "sync_fresh")


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, parsed)


def _bounded_retry_after(value: object) -> int:
    parsed = _nonnegative_int(value)
    if parsed <= 0:
        return DEFAULT_RETRY_AFTER_SECONDS
    return min(parsed, MAX_RETRY_AFTER_SECONDS)


def _next_utc_day_retry(now: int) -> int:
    current = datetime.fromtimestamp(now, timezone.utc)
    next_day = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int(next_day.timestamp()) - now)


async def fair_use_restriction(env: object, uid: str, *, now: int | None = None) -> dict[str, object] | None:
    database = getattr(env, "APP_DB", None)
    if database is None or not uid:
        return None
    effective_now = int(time.time()) if now is None else int(now)
    try:
        statement = database.prepare(
            "SELECT COALESCE(state.stage, 'none') AS stage, state.restrict_until, "
            "COALESCE(usage.daily_ms, 0) AS daily_ms, "
            "COALESCE(usage.three_day_ms, 0) AS three_day_ms, "
            "COALESCE(usage.weekly_ms, 0) AS weekly_ms FROM (SELECT ? AS uid) AS subject "
            "LEFT JOIN cf_fair_use_states AS state ON state.uid = subject.uid "
            "LEFT JOIN (SELECT uid, "
            "SUM(CASE WHEN occurred_at >= ? THEN speech_ms ELSE 0 END) AS daily_ms, "
            "SUM(CASE WHEN occurred_at >= ? THEN speech_ms ELSE 0 END) AS three_day_ms, "
            "SUM(speech_ms) AS weekly_ms FROM cf_fair_use_usage_sources "
            "WHERE uid = ? AND source_kind IN (?, ?) AND occurred_at >= ? GROUP BY uid"
            ") AS usage ON usage.uid = subject.uid"
        )
        if not callable(getattr(statement, "first", None)):
            return None
        row = await statement.bind(
            uid,
            effective_now - 86_400,
            effective_now - 3 * 86_400,
            uid,
            *LIVE_SOURCE_KINDS,
            effective_now - 7 * 86_400,
        ).first()
    except Exception:
        record_fallback(from_mode="d1", to_mode="none", reason="dependency_unavailable", outcome="degraded")
        return None
    row = row if isinstance(row, dict) else {}
    raw_stage = str(row.get("stage") or "none")
    stage = raw_stage if raw_stage in {"none", "warning", "throttle", "restrict"} else "none"
    raw_restrict_until = row.get("restrict_until")
    restrict_until = (
        raw_restrict_until if isinstance(raw_restrict_until, int) and not isinstance(raw_restrict_until, bool) else None
    )
    if stage == "restrict" and (restrict_until is None or restrict_until < effective_now):
        try:
            await database.prepare(
                "UPDATE cf_fair_use_states SET stage = 'throttle', restrict_until = NULL, updated_at = ? "
                "WHERE uid = ? AND stage = 'restrict'"
            ).bind(effective_now, uid).run()
        except Exception:
            record_fallback(from_mode="d1", to_mode="none", reason="dependency_unavailable", outcome="degraded")
        if restrict_until is None:
            record_fallback(from_mode="restrict", to_mode="throttle", reason="malformed_doc", outcome="recovered")
        stage = "throttle"

    daily_ms = _nonnegative_int(row.get("daily_ms"))
    three_day_ms = _nonnegative_int(row.get("three_day_ms"))
    weekly_ms = _nonnegative_int(row.get("weekly_ms"))
    if daily_ms >= MAX_DAILY_AUDIO_MS:
        return {
            "reason": "daily_audio_ceiling",
            "retry_after": _bounded_retry_after(_next_utc_day_retry(effective_now)),
            "stage": stage,
        }
    if stage == "restrict" and (
        daily_ms > DEFAULT_CAPS_MS[0] or three_day_ms > DEFAULT_CAPS_MS[1] or weekly_ms > DEFAULT_CAPS_MS[2]
    ):
        retry_after = restrict_until - effective_now if restrict_until is not None else None
        return {
            "reason": "fair_use_restricted",
            "retry_after": _bounded_retry_after(retry_after),
            "stage": stage,
        }
    return None


def fair_use_restriction_response(restriction: dict[str, object]) -> JSONResponse:
    return JSONResponse(
        {
            "code": "fair_use_restricted",
            "detail": "Account temporarily restricted due to fair-use policy",
        },
        status_code=429,
        headers={
            "Retry-After": str(_bounded_retry_after(restriction.get("retry_after"))),
            "X-Omi-Rate-Limit-Reason": "fair_use",
        },
    )
