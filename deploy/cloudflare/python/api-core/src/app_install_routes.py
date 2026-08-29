"""D1-backed installation state for approved public apps.

Free apps can be installed directly. Paid apps require a current entitlement
projected from Stripe's signed webhook before the same mutation is allowed.
Private apps and external-integration setup callbacks remain on their legacy
owners, so this route cannot skip a provider-side authorization step.
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app_projection_routes import _flag
from internal_auth import decode_context

router = APIRouter()

MAX_APP_ID_LENGTH = 256
MAX_APP_PAYLOAD_BYTES = 500_000


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _app_id(request: Request) -> str | None:
    params = getattr(request, "query_params", None)
    value = params.get("app_id") if params is not None else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= MAX_APP_ID_LENGTH else None


def _installable(payload: object) -> tuple[bool, int, str | None]:
    if not isinstance(payload, dict):
        return False, 503, "app catalog unavailable"
    capabilities = payload.get("capabilities", [])
    if not isinstance(capabilities, (list, tuple, set)) or any(not isinstance(item, str) for item in capabilities):
        return False, 503, "app catalog unavailable"
    if "persona" in capabilities or _flag(payload.get("private")):
        return False, 403, "app is not publicly installable"
    external = payload.get("external_integration")
    if isinstance(external, dict) and external.get("setup_completed_url"):
        return False, 400, "app setup is not completed"
    return True, 200, None


def _paid_app(payload: dict[str, object]) -> bool:
    return _flag(payload.get("is_paid")) or bool(payload.get("payment_link") or payload.get("payment_link_id"))


def _entitled(row: object, now: int | None = None) -> bool:
    if not isinstance(row, dict):
        return False
    current_period_end = row.get("current_period_end")
    return (
        row.get("status") in {"active", "trialing"}
        and isinstance(current_period_end, int)
        and current_period_end > (int(time.time()) if now is None else now)
    )


async def _load_installable_app(
    env: object, app_id: str, uid: str
) -> tuple[dict[str, object] | None, JSONResponse | None]:
    try:
        row = (
            await env.APP_DB.prepare(
                "SELECT id, approved, disabled, data_json FROM cf_app_catalog "
                "WHERE id = ? AND approved = 1 AND disabled = 0"
            )
            .bind(app_id)
            .first()
        )
    except Exception:
        return None, JSONResponse({"error": "app catalog unavailable"}, status_code=503)
    if not isinstance(row, dict):
        return None, JSONResponse({"error": "App not found"}, status_code=404)
    raw = row.get("data_json")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_APP_PAYLOAD_BYTES:
        return None, JSONResponse({"error": "app catalog unavailable"}, status_code=503)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None, JSONResponse({"error": "app catalog unavailable"}, status_code=503)
    if not isinstance(payload, dict):
        return None, JSONResponse({"error": "app catalog unavailable"}, status_code=503)
    if payload.get("id") not in (None, app_id):
        return None, JSONResponse({"error": "app catalog unavailable"}, status_code=503)
    allowed, status, message = _installable(payload)
    if not allowed:
        return None, JSONResponse({"error": message}, status_code=status)
    if _paid_app(payload):
        try:
            entitlement = (
                await env.APP_DB.prepare(
                    "SELECT status, current_period_end FROM cf_app_subscriptions WHERE uid = ? AND app_id = ?"
                )
                .bind(uid, app_id)
                .first()
            )
        except Exception:
            return None, JSONResponse({"error": "app entitlement unavailable"}, status_code=503)
        if not _entitled(entitlement):
            return None, JSONResponse({"error": "You are not authorized to perform this action"}, status_code=403)
    return {"id": str(row.get("id") or app_id), "payload": payload}, None


@router.get("/v1/apps/enabled")
async def get_enabled_apps(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    uid = str(context["uid"])
    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT u.app_id, c.data_json, s.status, s.current_period_end "
                "FROM cf_user_enabled_apps u "
                "JOIN cf_app_catalog c ON c.id = u.app_id "
                "LEFT JOIN cf_app_subscriptions s ON s.uid = u.uid AND s.app_id = u.app_id "
                "WHERE u.uid = ? ORDER BY u.created_at ASC, u.app_id ASC"
            )
            .bind(uid)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "enabled apps unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    app_ids = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("app_id"), str):
            continue
        try:
            payload = json.loads(str(row.get("data_json") or ""))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if _paid_app(payload) and not _entitled(row):
            continue
        app_ids.append(str(row["app_id"]))
    return app_ids


@router.post("/v1/apps/enable")
async def enable_app(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    app_id = _app_id(request)
    if app_id is None:
        return JSONResponse({"error": "invalid app_id"}, status_code=400)
    env = request.scope["env"]
    uid = str(context["uid"])
    app, error = await _load_installable_app(env, app_id, uid)
    if error:
        return error
    try:
        result = (
            await env.APP_DB.prepare(
                "INSERT OR IGNORE INTO cf_user_enabled_apps (uid, app_id, created_at) VALUES (?, ?, ?)"
            )
            .bind(uid, app["id"], int(time.time()))
            .run()
        )
        changes = result.get("meta", {}).get("changes", 0) if isinstance(result, dict) else 0
        if int(changes or 0) > 0:
            await env.APP_DB.prepare(
                "UPDATE cf_app_catalog SET installs = MAX(0, installs + 1), updated_at = ? WHERE id = ?"
            ).bind(int(time.time()), app["id"]).run()
    except Exception:
        return JSONResponse({"error": "enabled apps unavailable"}, status_code=503)
    return {"status": "ok"}


@router.post("/v1/apps/disable")
async def disable_app(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    app_id = _app_id(request)
    if app_id is None:
        return JSONResponse({"error": "invalid app_id"}, status_code=400)
    env = request.scope["env"]
    uid = str(context["uid"])
    try:
        result = (
            await env.APP_DB.prepare("DELETE FROM cf_user_enabled_apps WHERE uid = ? AND app_id = ?")
            .bind(uid, app_id)
            .run()
        )
        changes = result.get("meta", {}).get("changes", 0) if isinstance(result, dict) else 0
        if int(changes or 0) <= 0:
            return JSONResponse({"error": "App not found"}, status_code=404)
        await env.APP_DB.prepare(
            "UPDATE cf_app_catalog SET installs = MAX(0, installs - 1), updated_at = ? WHERE id = ?"
        ).bind(int(time.time()), app_id).run()
    except Exception:
        return JSONResponse({"error": "enabled apps unavailable"}, status_code=503)
    return {"status": "ok"}
