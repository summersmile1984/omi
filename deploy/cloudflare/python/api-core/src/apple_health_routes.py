"""D1-backed Apple Health projection for the isolated Cloudflare profile.

Apple Health is a device-pushed integration rather than an OAuth provider. The
client sends a bounded summary from HealthKit, while this Worker stores the
uid-scoped projection and exposes the same connection/status contract used by
the mobile integration provider.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from internal_auth import decode_context

router = APIRouter()

MAX_REQUEST_BYTES = 512_000
MAX_JSON_CHARS = 500_000
MAX_PERIOD_DAYS = 3650
MAX_DAILY_ROWS = 366
MAX_SLEEP_SESSIONS = 500
MAX_WORKOUTS = 500


class AppleHealthSyncData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    period_days: int = Field(default=7, ge=0, le=MAX_PERIOD_DAYS)

    total_steps: int | None = Field(default=None, ge=0, le=10_000_000_000)
    average_steps_per_day: float | None = Field(default=None, ge=0, le=10_000_000_000)
    daily_steps: list[dict[str, Any]] | None = Field(default=None, max_length=MAX_DAILY_ROWS)

    total_sleep_hours: float | None = Field(default=None, ge=0, le=100_000)
    total_in_bed_hours: float | None = Field(default=None, ge=0, le=100_000)
    sleep_sessions_count: int | None = Field(default=None, ge=0, le=100_000)
    sleep_sessions: list[dict[str, Any]] | None = Field(default=None, max_length=MAX_SLEEP_SESSIONS)
    daily_sleep: list[dict[str, Any]] | None = Field(default=None, max_length=MAX_DAILY_ROWS)

    heart_rate_average: float | None = Field(default=None, ge=0, le=500)
    heart_rate_min: float | None = Field(default=None, ge=0, le=500)
    heart_rate_max: float | None = Field(default=None, ge=0, le=500)

    total_active_energy: float | None = Field(default=None, ge=0, le=10_000_000)
    average_active_energy_per_day: float | None = Field(default=None, ge=0, le=10_000_000)
    daily_active_energy: list[dict[str, Any]] | None = Field(default=None, max_length=MAX_DAILY_ROWS)

    workouts: list[dict[str, Any]] | None = Field(default=None, max_length=MAX_WORKOUTS)


class AppleHealthConnectionUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    connected: bool = True


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


async def _bounded_json(request: Request) -> object:
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("request body exceeds size limit")
    return json.loads(raw)


def _health_data(data: AppleHealthSyncData) -> dict[str, object]:
    health_data: dict[str, object] = {"period_days": data.period_days}

    if data.total_steps is not None:
        health_data["steps"] = {
            "total": data.total_steps,
            "average_per_day": data.average_steps_per_day or (data.total_steps / max(data.period_days, 1)),
            "period_days": data.period_days,
            "daily": data.daily_steps or [],
        }

    if data.total_sleep_hours is not None or data.sleep_sessions:
        health_data["sleep"] = {
            "total_sleep_hours": data.total_sleep_hours or 0,
            "total_in_bed_hours": data.total_in_bed_hours or 0,
            "sessions_count": data.sleep_sessions_count or 0,
            "sessions": data.sleep_sessions or [],
            "daily": data.daily_sleep or [],
        }

    if data.heart_rate_average is not None:
        health_data["heart_rate"] = {
            "average": data.heart_rate_average,
            "minimum": data.heart_rate_min,
            "maximum": data.heart_rate_max,
        }

    if data.total_active_energy is not None:
        health_data["active_energy"] = {
            "total": data.total_active_energy,
            "average_per_day": data.average_active_energy_per_day
            or (data.total_active_energy / max(data.period_days, 1)),
            "daily": data.daily_active_energy or [],
        }

    if data.workouts:
        health_data["workouts"] = data.workouts
    return health_data


def _dump_health_data(health_data: dict[str, object]) -> str:
    encoded = json.dumps(health_data, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > MAX_JSON_CHARS:
        raise ValueError("health data exceeds storage limit")
    return encoded


@router.put("/v1/integrations/apple-health/sync")
async def sync_apple_health_data(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        data = AppleHealthSyncData.model_validate(await _bounded_json(request))
        health_data = _health_data(data)
        health_json = _dump_health_data(health_data)
    except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
        return JSONResponse({"error": "invalid Apple Health data"}, status_code=400)

    uid = str(context["uid"])
    now = int(time.time())
    synced_at = datetime.now(timezone.utc).isoformat()
    try:
        await request.scope["env"].APP_DB.prepare(
            "INSERT INTO cf_apple_health "
            "(uid, connected, health_data_json, last_synced, created_at, updated_at) "
            "VALUES (?, 1, ?, ?, ?, ?) "
            "ON CONFLICT(uid) DO UPDATE SET connected = 1, health_data_json = excluded.health_data_json, "
            "last_synced = excluded.last_synced, updated_at = excluded.updated_at"
        ).bind(uid, health_json, synced_at, now, now).run()
    except Exception:
        return JSONResponse({"error": "Apple Health unavailable"}, status_code=503)
    return {
        "status": "ok",
        "app_key": "apple_health",
        "synced_at": synced_at,
        "data_types_synced": list(health_data.keys()),
    }


@router.put("/v1/integrations/apple_health")
async def save_apple_health_connection(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        update = AppleHealthConnectionUpdate.model_validate(await _bounded_json(request))
    except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
        return JSONResponse({"error": "invalid Apple Health connection"}, status_code=400)

    uid = str(context["uid"])
    now = int(time.time())
    try:
        await request.scope["env"].APP_DB.prepare(
            "INSERT INTO cf_apple_health "
            "(uid, connected, health_data_json, last_synced, created_at, updated_at) "
            "VALUES (?, ?, '{}', NULL, ?, ?) "
            "ON CONFLICT(uid) DO UPDATE SET connected = excluded.connected, updated_at = excluded.updated_at"
        ).bind(uid, 1 if update.connected else 0, now, now).run()
    except Exception:
        return JSONResponse({"error": "Apple Health unavailable"}, status_code=503)
    return {"status": "ok", "app_key": "apple_health"}


@router.delete("/v1/integrations/apple_health")
async def delete_apple_health_connection(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare("DELETE FROM cf_apple_health WHERE uid = ?")
            .bind(str(context["uid"]))
            .run()
        )
    except Exception:
        return JSONResponse({"error": "Apple Health unavailable"}, status_code=503)
    changes = result.get("meta", {}).get("changes", 0) if isinstance(result, dict) else 0
    if changes != 1:
        return JSONResponse({"detail": "Integration not found"}, status_code=404)
    return Response(status_code=204)
