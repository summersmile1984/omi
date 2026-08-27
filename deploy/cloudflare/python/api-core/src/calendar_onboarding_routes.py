"""D1-backed calendar onboarding state for the isolated Cloudflare profile.

Only onboarding flags are projected here. Google OAuth tokens, event reads,
refresh, and calendar writes remain owned by the legacy integration service.
The projection is staging-only until existing integration rows are backfilled.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context

router = APIRouter()


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _state(row: dict[str, object] | None) -> dict[str, object]:
    row = row or {}
    connected = bool(row.get("connected"))
    skipped = bool(row.get("onboarding_skipped"))
    reauth_required = bool(row.get("reauth_required"))
    has_token = bool(row.get("has_access_token"))
    needs_reconnect = reauth_required or (connected and not has_token)
    reauth_reason = row.get("reauth_reason") if reauth_required else None
    if needs_reconnect:
        state = "needs_reconnect"
    elif connected:
        state = "connected"
    elif skipped:
        state = "skipped"
    else:
        state = "not_started"
    return {
        "connected": connected,
        "onboarding_completed": connected or skipped,
        "needs_reconnect": needs_reconnect,
        "reauth_reason": reauth_reason,
        "state": state,
    }


async def _load(env: object, uid: str) -> dict[str, object] | None:
    row = (
        await env.APP_DB.prepare(
            "SELECT connected, onboarding_skipped, reauth_required, has_access_token, reauth_reason "
            "FROM cf_user_calendar_onboarding WHERE uid = ?"
        )
        .bind(uid)
        .first()
    )
    return row if isinstance(row, dict) else None


async def _upsert_flags(env: object, uid: str, *, skipped: int, reauth_required: int, reauth_reason: object) -> None:
    now = int(time.time())
    await env.APP_DB.prepare(
        "INSERT INTO cf_user_calendar_onboarding "
        "(uid, connected, onboarding_skipped, reauth_required, has_access_token, reauth_reason, created_at, updated_at) "
        "VALUES (?, 0, ?, ?, 0, ?, ?, ?) "
        "ON CONFLICT(uid) DO UPDATE SET onboarding_skipped = excluded.onboarding_skipped, "
        "reauth_required = excluded.reauth_required, reauth_reason = excluded.reauth_reason, updated_at = excluded.updated_at"
    ).bind(uid, skipped, reauth_required, reauth_reason, now, now).run()


@router.get("/v1/calendar/onboarding/status")
async def get_calendar_onboarding_status(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        row = await _load(request.scope["env"], str(context["uid"]))
    except Exception:
        return JSONResponse({"error": "calendar onboarding unavailable"}, status_code=503)
    return _state(row)


@router.post("/v1/calendar/onboarding/skip")
async def skip_calendar_onboarding(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    uid = str(context["uid"])
    try:
        current = await _load(request.scope["env"], uid) or {}
        await _upsert_flags(
            request.scope["env"],
            uid,
            skipped=1,
            reauth_required=1 if bool(current.get("reauth_required")) else 0,
            reauth_reason=current.get("reauth_reason"),
        )
    except Exception:
        return JSONResponse({"error": "calendar onboarding unavailable"}, status_code=503)
    return {"skipped": True}


@router.post("/v1/calendar/onboarding/reset")
async def reset_calendar_onboarding(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    uid = str(context["uid"])
    try:
        await _upsert_flags(request.scope["env"], uid, skipped=0, reauth_required=0, reauth_reason=None)
    except Exception:
        return JSONResponse({"error": "calendar onboarding unavailable"}, status_code=503)
    return {"reset": True}
