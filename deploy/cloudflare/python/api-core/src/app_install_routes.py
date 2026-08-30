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


def _installable(payload: object, *, allow_private: bool) -> tuple[bool, int, str | None]:
    if not isinstance(payload, dict):
        return False, 503, "app catalog unavailable"
    capabilities = payload.get("capabilities", [])
    if not isinstance(capabilities, (list, tuple, set)) or any(not isinstance(item, str) for item in capabilities):
        return False, 503, "app catalog unavailable"
    if "persona" in capabilities or (_flag(payload.get("private")) and not allow_private):
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
                "SELECT c.id, c.owner_uid, c.approved, c.disabled, c.data_json, "
                "CASE WHEN ta.app_id IS NULL THEN 0 ELSE 1 END AS tester_access, "
                "CASE WHEN t.uid IS NULL THEN 0 ELSE 1 END AS is_tester "
                "FROM cf_app_catalog c "
                "LEFT JOIN cf_app_tester_access ta ON ta.app_id = c.id AND ta.uid = ? "
                "LEFT JOIN cf_app_testers t ON t.uid = ? "
                "WHERE c.id = ?"
            )
            .bind(uid, uid, app_id)
            .first()
        )
    except Exception:
        return None, JSONResponse({"error": "app catalog unavailable"}, status_code=503)
    if not isinstance(row, dict):
        return None, JSONResponse({"error": "App not found"}, status_code=404)
    if _flag(row.get("disabled")):
        return None, JSONResponse(
            {
                "error": (
                    "This app is currently unavailable due to connectivity issues. " "The developer has been notified."
                )
            },
            status_code=400,
        )
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
    owner = isinstance(row.get("owner_uid"), str) and row.get("owner_uid") == uid
    tester_access = _flag(row.get("tester_access")) and not _flag(row.get("approved"))
    public = _flag(row.get("approved")) and not _flag(payload.get("private"))
    if not owner and not tester_access and not public:
        return None, JSONResponse({"error": "App not found"}, status_code=404)
    allowed, status, message = _installable(payload, allow_private=owner or tester_access)
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
    return {
        "id": str(row.get("id") or app_id),
        "payload": payload,
        "counts_install": public and not owner and not _flag(row.get("is_tester")),
    }, None


async def _preferred_app_is_visible(env: object, app_id: str, uid: str) -> tuple[bool, JSONResponse | None]:
    """Use the catalog's owner/public/assigned-tester visibility boundary.

    Preference selection intentionally does not require installation, payment
    entitlement, or external setup completion. The legacy setter admitted any
    app visible to the user and the downstream conversation processor resolved
    capability compatibility when it consumed the preference.
    """
    try:
        row = (
            await env.APP_DB.prepare(
                "SELECT c.id, c.owner_uid, c.approved, c.data_json, "
                "CASE WHEN ta.app_id IS NULL THEN 0 ELSE 1 END AS tester_access "
                "FROM cf_app_catalog c "
                "LEFT JOIN cf_app_tester_access ta ON ta.app_id = c.id AND ta.uid = ? "
                "WHERE c.id = ?"
            )
            .bind(uid, app_id)
            .first()
        )
    except Exception:
        return False, JSONResponse({"detail": "App catalog unavailable."}, status_code=503)
    if not isinstance(row, dict):
        return False, None
    raw = row.get("data_json")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_APP_PAYLOAD_BYTES:
        return False, JSONResponse({"detail": "App catalog unavailable."}, status_code=503)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return False, JSONResponse({"detail": "App catalog unavailable."}, status_code=503)
    if not isinstance(payload, dict) or payload.get("id") not in (None, app_id):
        return False, JSONResponse({"detail": "App catalog unavailable."}, status_code=503)
    owner = isinstance(row.get("owner_uid"), str) and row.get("owner_uid") == uid
    public = _flag(row.get("approved")) and not _flag(payload.get("private"))
    return owner or _flag(row.get("tester_access")) or public, None


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
        if int(changes or 0) > 0 and _flag(app.get("counts_install")):
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
            "UPDATE cf_app_catalog SET installs = MAX(0, installs - 1), updated_at = ? "
            "WHERE id = ? AND approved = 1 "
            "AND COALESCE(json_extract(data_json, '$.private'), 0) NOT IN (1, 'true') "
            "AND (owner_uid IS NULL OR owner_uid != ?) "
            "AND NOT EXISTS (SELECT 1 FROM cf_app_testers WHERE uid = ?)"
        ).bind(int(time.time()), app_id, uid, uid).run()
    except Exception:
        return JSONResponse({"error": "enabled apps unavailable"}, status_code=503)
    return {"status": "ok"}


@router.put("/v1/users/preferences/app")
async def set_preferred_app(request: Request, app_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    uid = str(context["uid"])
    visible, catalog_error = await _preferred_app_is_visible(env, app_id, uid)
    if catalog_error:
        return catalog_error
    if not visible:
        return JSONResponse(
            {"detail": f"App with ID '{app_id}' not found or not accessible."},
            status_code=410,
        )
    try:
        await env.APP_DB.prepare(
            "INSERT INTO cf_user_app_preferences (uid, preferred_app_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(uid) DO UPDATE SET preferred_app_id = excluded.preferred_app_id, "
            "updated_at = excluded.updated_at"
        ).bind(uid, app_id, int(time.time())).run()
    except Exception:
        return JSONResponse({"detail": "Failed to store app preference."}, status_code=500)
    return {
        "status": "ok",
        "message": f"App {app_id} set as preferred app for user {uid}.",
    }
