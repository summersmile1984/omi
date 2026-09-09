"""Per-device running daily counters, aligned with backend/routers/users.py."""

from datetime import datetime
from typing import Annotated
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
import pytz

from internal_auth import decode_context

router = APIRouter()
UsageSeconds = Annotated[int, Field(strict=True, ge=0, le=86400)]
UsageCount = Annotated[int, Field(strict=True, ge=0, le=10000)]
COUNTERS = ("watching_seconds", "listening_seconds", "proactive_cards_shown", "proactive_cards_acted", "ptt_turns")


class DesktopDailyUsageRequest(BaseModel):
    date: str
    timezone: str
    client_device_id: str = Field(min_length=1, max_length=200)
    watching_seconds: UsageSeconds
    listening_seconds: UsageSeconds
    proactive_cards_shown: UsageCount
    proactive_cards_acted: UsageCount
    ptt_turns: UsageCount

    @field_validator("date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
        if parsed.strftime("%Y-%m-%d") != value:
            raise ValueError("date must be a real date in YYYY-MM-DD format")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            pytz.timezone(value)
        except pytz.UnknownTimeZoneError as error:
            raise ValueError("timezone must be a valid IANA timezone") from error
        return value

    @field_validator("client_device_id")
    @classmethod
    def valid_device(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("client_device_id cannot be blank")
        return value

    @model_validator(mode="after")
    def date_window(self):
        target = datetime.strptime(self.date, "%Y-%m-%d").date()
        today = datetime.now(pytz.timezone(self.timezone)).date()
        if abs((target - today).days) > 2:
            raise ValueError("date must be within 2 days of today in the supplied timezone")
        return self


@router.post("/v1/users/desktop-usage/daily")
async def record_desktop_daily_usage(data: DesktopDailyUsageRequest, request: Request):
    env = request.scope["env"]
    context = decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        await env.APP_DB.prepare(
            "INSERT INTO cf_desktop_daily_usage "
            "(uid, date, timezone, client_device_id, watching_seconds, listening_seconds, "
            "proactive_cards_shown, proactive_cards_acted, ptt_turns, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(uid, date, client_device_id) DO UPDATE SET "
            "timezone = excluded.timezone, updated_at = excluded.updated_at, "
            + ", ".join(f"{field} = MAX(cf_desktop_daily_usage.{field}, excluded.{field})" for field in COUNTERS)
        ).bind(
            str(context["uid"]),
            data.date,
            data.timezone,
            data.client_device_id,
            *(getattr(data, field) for field in COUNTERS),
            int(time.time()),
        ).run()
    except Exception:
        return JSONResponse({"error": "desktop daily usage unavailable"}, status_code=503)
    return {"ok": True}
