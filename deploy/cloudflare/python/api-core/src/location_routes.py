import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from internal_auth import decode_context
from location_policy import (
    LOCATION_CONTEXT_CONSENT_TTL_SECONDS,
    LOCATION_CONTEXT_DISCLOSED_PROVIDERS,
    LOCATION_CONTEXT_PURPOSE,
    location_context_response,
)

router = APIRouter()


class LocationContextConsentUpdate(BaseModel):
    enabled: bool
    disclosure_accepted: bool = False


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


async def _load_consent(env: object, uid: str) -> dict[str, object]:
    row = await env.APP_DB.prepare(
        "SELECT status, purpose, disclosed_providers_json, granted_at, expires_at, revoked_at "
        "FROM cf_user_location_context_consent WHERE uid = ?"
    ).bind(uid).first()
    return row if isinstance(row, dict) else {}


async def _save_consent(env: object, uid: str, *, enabled: bool) -> None:
    now = int(time.time())
    expires_at = now + LOCATION_CONTEXT_CONSENT_TTL_SECONDS if enabled else now
    await env.APP_DB.prepare(
        "INSERT INTO cf_user_location_context_consent "
        "(uid, status, purpose, disclosed_providers_json, granted_at, expires_at, revoked_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(uid) DO UPDATE SET status = excluded.status, purpose = excluded.purpose, "
        "disclosed_providers_json = excluded.disclosed_providers_json, granted_at = excluded.granted_at, "
        "expires_at = excluded.expires_at, revoked_at = excluded.revoked_at, updated_at = excluded.updated_at"
    ).bind(
        uid,
        "granted" if enabled else "revoked",
        LOCATION_CONTEXT_PURPOSE,
        json.dumps(LOCATION_CONTEXT_DISCLOSED_PROVIDERS),
        now,
        expires_at,
        None if enabled else now,
        now,
        now,
    ).run()


@router.get("/v1/users/location-context-consent")
async def get_location_context_consent(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    row = await _load_consent(request.scope["env"], str(context["uid"]))
    return location_context_response(row, now=int(time.time()))


@router.put("/v1/users/location-context-consent")
async def set_location_context_consent(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        update = LocationContextConsentUpdate.model_validate(await request.json())
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid location context consent"}, status_code=400)
    if update.enabled and not update.disclosure_accepted:
        return JSONResponse(
            {"error": "location context requires accepting the provider disclosure"}, status_code=422
        )
    env = request.scope["env"]
    uid = str(context["uid"])
    await _save_consent(env, uid, enabled=update.enabled)
    row = await _load_consent(env, uid)
    return location_context_response(row, now=int(time.time()))
