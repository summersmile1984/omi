"""D1-backed fair-use status projection for isolated Workers accounts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
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
CASE_REFERENCE_PATTERN = re.compile(r"^FU-[A-F0-9]{12}$")
VALID_STAGES = frozenset({"none", "warning", "throttle", "restrict"})
MAX_ADMIN_ROWS = 200
MAX_UID_LENGTH = 256
MAX_EVENT_ID_LENGTH = 64
MAX_ADMIN_NOTES_LENGTH = 2_000
STATE_COLUMNS = (
    "uid, stage, last_case_ref, throttle_until, restrict_until, updated_at, "
    "violation_count_7d, violation_count_30d, last_violation_at, "
    "last_classifier_score, last_classifier_type, cleared_by, cleared_at"
)
EVENT_COLUMNS = (
    "event_id, uid, case_ref, created_at, session_id, trigger, daily_speech_ms, "
    "three_day_speech_ms, weekly_speech_ms, daily_threshold_ms, "
    "three_day_threshold_ms, weekly_threshold_ms, classifier_json, "
    "enforcement_action, previous_stage, new_stage, resolved, resolved_at, "
    "resolved_by, admin_notes"
)


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


def _admin_identity(request: Request) -> str | None:
    expected = getattr(request.scope["env"], "FAIR_USE_ADMIN_KEY", None)
    provided = request.headers.get("x-admin-key")
    if not (
        isinstance(expected, str) and expected and isinstance(provided, str) and hmac.compare_digest(provided, expected)
    ):
        return None
    return f"admin:{hashlib.sha256(provided.encode()).hexdigest()[:8]}"


def _require_admin(request: Request) -> tuple[str | None, JSONResponse | None]:
    identity = _admin_identity(request)
    if identity is None:
        return None, JSONResponse({"detail": "Invalid admin key"}, status_code=403)
    return identity, None


def _bounded_identifier(value: str, maximum: int) -> str | None:
    normalized = value.strip()
    return normalized if 0 < len(normalized) <= maximum else None


def _rows(result: object) -> list[dict[str, object]]:
    if not isinstance(result, dict):
        return []
    values = result.get("results")
    if not isinstance(values, list):
        return []
    return [row for row in values if isinstance(row, dict)]


def _state_payload(row: dict[str, object]) -> dict[str, object]:
    payload = dict(row)
    for key in ("throttle_until", "restrict_until", "updated_at", "last_violation_at", "cleared_at"):
        payload[key] = _timestamp(payload.get(key))
    payload["id"] = "current"
    return payload


def _event_payload(row: dict[str, object], *, detail: bool = False) -> dict[str, object]:
    classifier: object = None
    raw_classifier = row.get("classifier_json")
    if isinstance(raw_classifier, str) and len(raw_classifier.encode()) <= 32_000:
        try:
            parsed = json.loads(raw_classifier)
            classifier = parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            classifier = None
    payload: dict[str, object] = {
        "event_id" if detail else "id": str(row.get("event_id") or ""),
        "case_ref": str(row.get("case_ref") or ""),
        "created_at": _timestamp(row.get("created_at")),
        "session_id": str(row.get("session_id") or ""),
        "trigger": str(row.get("trigger") or "daily"),
        "window_speech_ms": {
            "daily": int(row.get("daily_speech_ms") or 0),
            "three_day": int(row.get("three_day_speech_ms") or 0),
            "weekly": int(row.get("weekly_speech_ms") or 0),
        },
        "thresholds_ms": {
            "daily": int(row.get("daily_threshold_ms") or 0),
            "three_day": int(row.get("three_day_threshold_ms") or 0),
            "weekly": int(row.get("weekly_threshold_ms") or 0),
        },
        "classifier": classifier,
        "enforcement_action": str(row.get("enforcement_action") or "none"),
        "previous_stage": str(row.get("previous_stage") or "none"),
        "new_stage": str(row.get("new_stage") or "none"),
        "resolved": bool(row.get("resolved")),
        "resolved_at": _timestamp(row.get("resolved_at")),
        "resolved_by": str(row.get("resolved_by") or ""),
        "admin_notes": str(row.get("admin_notes") or ""),
    }
    if detail:
        payload["uid"] = str(row.get("uid") or "")
    return payload


async def _rolling_usage(env: object, uid: str, now: int) -> dict[str, int]:
    row = (
        await env.APP_DB.prepare(
            "SELECT COALESCE(SUM(CASE WHEN occurred_at >= ? THEN speech_ms ELSE 0 END), 0) AS daily_ms, "
            "COALESCE(SUM(CASE WHEN occurred_at >= ? THEN speech_ms ELSE 0 END), 0) AS three_day_ms, "
            "COALESCE(SUM(speech_ms), 0) AS weekly_ms FROM cf_fair_use_usage_sources "
            "WHERE uid = ? AND source_kind IN (?, ?) AND occurred_at >= ?"
        )
        .bind(now - 86_400, now - 3 * 86_400, uid, *LIVE_SOURCE_KINDS, now - 7 * 86_400)
        .first()
    )
    row = row if isinstance(row, dict) else {}
    return {key: max(0, int(row.get(key) or 0)) for key in ("daily_ms", "three_day_ms", "weekly_ms")}


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


def _timestamp(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


@router.get("/v1/fair-use/case/{case_ref}/status")
async def get_public_case_status(case_ref: str, request: Request):
    normalized = case_ref.strip().upper()
    if not CASE_REFERENCE_PATTERN.fullmatch(normalized):
        return JSONResponse({"detail": "Case not found"}, status_code=404)
    env = request.scope["env"]
    now = int(time.time())
    try:
        row = (
            await env.APP_DB.prepare(
                "SELECT event.case_ref, event.created_at, event.resolved_at, "
                "COALESCE(state.stage, 'none') AS stage, state.restrict_until "
                "FROM cf_fair_use_events AS event "
                "LEFT JOIN cf_fair_use_states AS state ON state.uid = event.uid "
                "WHERE event.case_ref = ? LIMIT 1"
            )
            .bind(normalized)
            .first()
        )
    except Exception:
        return JSONResponse({"error": "fair use case lookup unavailable"}, status_code=503)
    if not isinstance(row, dict):
        return JSONResponse({"detail": "Case not found"}, status_code=404)
    stage = str(row.get("stage") or "none")
    restrict_until = row.get("restrict_until")
    if stage == "restrict" and (
        isinstance(restrict_until, bool) or not isinstance(restrict_until, (int, float)) or int(restrict_until) < now
    ):
        stage = "throttle"
    if stage not in {"none", "warning", "throttle", "restrict"}:
        stage = "none"
    created_at = _timestamp(row.get("created_at"))
    return {
        "case_ref": normalized,
        "stage": stage,
        "message": _message(stage, normalized),
        "created_at": created_at,
        "updated_at": _timestamp(row.get("resolved_at")) or created_at,
        "support_email": "team@basedhardware.com",
    }


@router.get("/v1/admin/fair-use/flagged")
async def get_flagged_users(request: Request, stage: str | None = None, limit: int = 50):
    _, denial = _require_admin(request)
    if denial:
        return denial
    bounded_limit = max(1, min(int(limit), MAX_ADMIN_ROWS))
    if stage is not None and (not stage or len(stage) > 20):
        return JSONResponse({"error": "invalid stage"}, status_code=400)
    clause = "stage = ?" if stage is not None else "stage IN ('warning', 'throttle', 'restrict')"
    args: list[object] = [stage] if stage is not None else []
    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(
                f"SELECT {STATE_COLUMNS} FROM cf_fair_use_states WHERE {clause} "
                "ORDER BY updated_at DESC, uid ASC LIMIT ?"
            )
            .bind(*args, bounded_limit)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "fair use admin unavailable"}, status_code=503)
    return {"users": [_state_payload(row) for row in _rows(result)], "fair_use_enabled": True}


@router.get("/v1/admin/fair-use/user/{uid}")
async def get_user_fair_use_detail(uid: str, request: Request):
    _, denial = _require_admin(request)
    if denial:
        return denial
    normalized_uid = _bounded_identifier(uid, MAX_UID_LENGTH)
    if normalized_uid is None:
        return JSONResponse({"error": "invalid uid"}, status_code=400)
    now = int(time.time())
    env = request.scope["env"]
    try:
        state = (
            await env.APP_DB.prepare(f"SELECT {STATE_COLUMNS} FROM cf_fair_use_states WHERE uid = ?")
            .bind(normalized_uid)
            .first()
        )
        events_result = (
            await env.APP_DB.prepare(
                f"SELECT {EVENT_COLUMNS} FROM cf_fair_use_events WHERE uid = ? "
                "ORDER BY created_at DESC, event_id DESC LIMIT 50"
            )
            .bind(normalized_uid)
            .all()
        )
        usage = await _rolling_usage(env, normalized_uid, now)
    except Exception:
        return JSONResponse({"error": "fair use admin unavailable"}, status_code=503)
    return {
        "uid": normalized_uid,
        "state": _state_payload(state) if isinstance(state, dict) else {},
        "events": [_event_payload(row) for row in _rows(events_result)],
        "current_speech_ms": usage,
    }


@router.post("/v1/admin/fair-use/user/{uid}/resolve-event/{event_id}")
async def resolve_fair_use_event(uid: str, event_id: str, request: Request, notes: str = ""):
    admin_id, denial = _require_admin(request)
    if denial:
        return denial
    normalized_uid = _bounded_identifier(uid, MAX_UID_LENGTH)
    normalized_event_id = _bounded_identifier(event_id, MAX_EVENT_ID_LENGTH)
    if normalized_uid is None or normalized_event_id is None or len(notes) > MAX_ADMIN_NOTES_LENGTH:
        return JSONResponse({"error": "invalid fair use event"}, status_code=400)
    now = int(time.time())
    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(
                "UPDATE cf_fair_use_events SET resolved = 1, resolved_at = ?, resolved_by = ?, admin_notes = ? "
                "WHERE uid = ? AND event_id = ?"
            )
            .bind(now, admin_id, notes, normalized_uid, normalized_event_id)
            .run()
        )
    except Exception:
        return JSONResponse({"error": "fair use admin unavailable"}, status_code=503)
    changes = result.get("meta", {}).get("changes", 0) if isinstance(result, dict) else 0
    if changes != 1:
        return JSONResponse({"detail": "Fair use event not found"}, status_code=404)
    return {"status": "resolved"}


@router.post("/v1/admin/fair-use/user/{uid}/reset")
async def reset_user_fair_use(uid: str, request: Request):
    admin_id, denial = _require_admin(request)
    if denial:
        return denial
    normalized_uid = _bounded_identifier(uid, MAX_UID_LENGTH)
    if normalized_uid is None:
        return JSONResponse({"error": "invalid uid"}, status_code=400)
    now = int(time.time())
    try:
        await request.scope["env"].APP_DB.prepare(
            "INSERT INTO cf_fair_use_states (uid, stage, updated_at, cleared_by, cleared_at) "
            "VALUES (?, 'none', ?, ?, ?) ON CONFLICT(uid) DO UPDATE SET stage = 'none', "
            "violation_count_7d = 0, violation_count_30d = 0, last_violation_at = NULL, "
            "throttle_until = NULL, restrict_until = NULL, last_classifier_score = 0.0, "
            "last_classifier_type = 'none', evaluation_lease_token = NULL, evaluation_lease_until = NULL, "
            "next_evaluation_at = NULL, cleared_by = excluded.cleared_by, cleared_at = excluded.cleared_at, "
            "updated_at = excluded.updated_at"
        ).bind(normalized_uid, now, admin_id, now).run()
    except Exception:
        return JSONResponse({"error": "fair use admin unavailable"}, status_code=503)
    return {"status": "reset"}


@router.post("/v1/admin/fair-use/user/{uid}/set-stage")
async def set_user_fair_use_stage(uid: str, request: Request, stage: str):
    admin_id, denial = _require_admin(request)
    if denial:
        return denial
    normalized_uid = _bounded_identifier(uid, MAX_UID_LENGTH)
    if normalized_uid is None:
        return JSONResponse({"error": "invalid uid"}, status_code=400)
    if stage not in VALID_STAGES:
        return JSONResponse(
            {"detail": f"Invalid stage. Must be one of: {VALID_STAGES}"},
            status_code=400,
        )
    now = int(time.time())
    clear = stage == "none"
    throttle_until = now + 7 * 86_400 if stage == "throttle" else None
    restrict_until = now + 30 * 86_400 if stage == "restrict" else None
    try:
        await request.scope["env"].APP_DB.prepare(
            "INSERT INTO cf_fair_use_states "
            "(uid, stage, throttle_until, restrict_until, updated_at, cleared_by, cleared_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(uid) DO UPDATE SET stage = excluded.stage, "
            "throttle_until = excluded.throttle_until, restrict_until = excluded.restrict_until, "
            "evaluation_lease_token = NULL, evaluation_lease_until = NULL, "
            "cleared_by = CASE WHEN excluded.stage = 'none' THEN excluded.cleared_by ELSE cf_fair_use_states.cleared_by END, "
            "cleared_at = CASE WHEN excluded.stage = 'none' THEN excluded.cleared_at ELSE cf_fair_use_states.cleared_at END, "
            "updated_at = excluded.updated_at"
        ).bind(
            normalized_uid,
            stage,
            throttle_until,
            restrict_until,
            now,
            admin_id if clear else None,
            now if clear else None,
        ).run()
    except Exception:
        return JSONResponse({"error": "fair use admin unavailable"}, status_code=503)
    return {"status": "updated", "stage": stage}


@router.get("/v1/admin/fair-use/case/{case_ref}")
async def lookup_fair_use_case(case_ref: str, request: Request):
    _, denial = _require_admin(request)
    if denial:
        return denial
    normalized = case_ref.strip().upper()
    if not CASE_REFERENCE_PATTERN.fullmatch(normalized):
        return JSONResponse({"detail": f"Case {normalized} not found"}, status_code=404)
    try:
        row = (
            await request.scope["env"]
            .APP_DB.prepare(f"SELECT {EVENT_COLUMNS} FROM cf_fair_use_events WHERE case_ref = ? LIMIT 1")
            .bind(normalized)
            .first()
        )
    except Exception:
        return JSONResponse({"error": "fair use admin unavailable"}, status_code=503)
    if not isinstance(row, dict):
        return JSONResponse({"detail": f"Case {normalized} not found"}, status_code=404)
    return _event_payload(row, detail=True)
