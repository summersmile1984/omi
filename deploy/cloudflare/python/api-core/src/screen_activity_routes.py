"""D1-backed text-only screen activity routes for the Cloudflare profile.

The migrated surface stores bounded app/window/OCR events and serves the
first-party list and aggregate APIs. It intentionally drops client embeddings:
semantic screen search, vector lifecycle, and paid entitlement checks remain
legacy-owned until their own data and billing contracts move.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from internal_auth import decode_context

router = APIRouter()

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$")
MAX_REQUEST_BYTES = 1_000_000
MAX_BATCH_ROWS = 100
MAX_LIST_LIMIT = 5_000


class ScreenActivityRow(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: int
    timestamp: str
    app_name: str = Field(default="", alias="appName", max_length=512)
    window_title: str = Field(default="", alias="windowTitle", max_length=2048)
    ocr_text: str = Field(default="", alias="ocrText", max_length=8_192)
    device_name: str | None = Field(default=None, alias="deviceName", max_length=256)
    client_device_id: str | None = Field(default=None, alias="clientDeviceId", max_length=256)
    # The desktop sends embeddings for the legacy vector path. Accept but do not
    # persist them so this D1 route remains text-only and bounded.
    embedding: list[float] | None = None

    @field_validator("timestamp")
    @classmethod
    def canonicalize_timestamp(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("timestamp must be ISO-8601 compatible") from exc
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return f"{parsed.strftime('%Y-%m-%d %H:%M:%S')}.{parsed.microsecond // 1000:03d}"

    def storage_id(self) -> str:
        return f"{self.client_device_id}-{self.id}" if self.client_device_id else str(self.id)


class ScreenActivitySyncRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rows: list[ScreenActivityRow] = Field(max_length=MAX_BATCH_ROWS)


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


def _day_bounds(request: Request) -> tuple[str, str] | JSONResponse:
    raw = getattr(request, "query_params", {}).get("date")
    if raw in (None, ""):
        return "", ""
    if not isinstance(raw, str) or not DATE_PATTERN.fullmatch(raw):
        return JSONResponse({"error": "invalid date"}, status_code=422)
    try:
        day = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return JSONResponse({"error": "invalid date"}, status_code=422)
    next_day = day + timedelta(days=1)
    return (
        day.strftime("%Y-%m-%d 00:00:00.000"),
        next_day.strftime("%Y-%m-%d 00:00:00.000"),
    )


def _query_int(request: Request, name: str, default: int, minimum: int, maximum: int) -> int | None:
    raw = getattr(request, "query_params", {}).get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if minimum <= value <= maximum else None


def _response(row: dict[str, object]) -> dict[str, object]:
    return {
        "id": str(row.get("id") or ""),
        "timestamp": str(row.get("timestamp") or ""),
        "appName": str(row.get("app_name") or ""),
        "windowTitle": str(row.get("window_title") or ""),
        "ocrText": str(row.get("ocr_text") or ""),
        "deviceName": row.get("device_name"),
        "clientDeviceId": row.get("client_device_id"),
    }


def _where(request: Request, uid: str) -> tuple[str, list[object]] | JSONResponse:
    clauses = ["uid = ?"]
    params: list[object] = [uid]
    bounds = _day_bounds(request)
    if isinstance(bounds, JSONResponse):
        return bounds
    start, end = bounds
    if start:
        clauses.extend(["timestamp >= ?", "timestamp < ?"])
        params.extend([start, end])
    app_filter = getattr(request, "query_params", {}).get("app_filter")
    if app_filter:
        if not isinstance(app_filter, str) or len(app_filter) > 512:
            return JSONResponse({"error": "invalid app_filter"}, status_code=400)
        clauses.append("app_name = ?")
        params.append(app_filter)
    return " AND ".join(clauses), params


@router.post("/v1/screen-activity/sync")
async def sync_screen_activity(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = ScreenActivitySyncRequest.model_validate(await _bounded_json(request))
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid screen activity batch"}, status_code=400)
    if not payload.rows:
        return {"synced": 0, "last_id": 0}
    uid = str(context["uid"])
    try:
        for row in payload.rows:
            await request.scope["env"].APP_DB.prepare(
                "INSERT INTO cf_screen_activity "
                "(uid, id, timestamp, app_name, window_title, ocr_text, device_name, client_device_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(uid, id) DO UPDATE SET timestamp = excluded.timestamp, "
                "app_name = excluded.app_name, window_title = excluded.window_title, "
                "ocr_text = excluded.ocr_text, device_name = excluded.device_name, "
                "client_device_id = excluded.client_device_id"
            ).bind(
                uid,
                row.storage_id(),
                row.timestamp,
                row.app_name,
                row.window_title,
                row.ocr_text[:1000],
                row.device_name,
                row.client_device_id,
            ).run()
    except Exception:
        return JSONResponse({"error": "screen activity unavailable"}, status_code=503)
    return {"synced": len(payload.rows), "last_id": max(row.id for row in payload.rows)}


async def _list_rows(request: Request, uid: str, *, limit: int, offset: int = 0) -> list[dict[str, object]]:
    query = _where(request, uid)
    if isinstance(query, JSONResponse):
        raise ValueError("invalid query")
    where, params = query
    result = (
        await request.scope["env"]
        .APP_DB.prepare(
            "SELECT id, timestamp, app_name, window_title, ocr_text, device_name, client_device_id "
            f"FROM cf_screen_activity WHERE {where} ORDER BY timestamp ASC, id ASC LIMIT ? OFFSET ?"
        )
        .bind(*params, limit, offset)
        .all()
    )
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [row for row in rows if isinstance(row, dict)]


@router.get("/v1/screen-activity")
async def list_screen_activity(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    limit = _query_int(request, "limit", 500, 1, MAX_LIST_LIMIT)
    if limit is None:
        return JSONResponse({"error": "invalid limit"}, status_code=400)
    offset = _query_int(request, "offset", 0, 0, 1_000_000)
    if offset is None:
        return JSONResponse({"error": "invalid offset"}, status_code=400)
    if isinstance(bounds := _day_bounds(request), JSONResponse):
        return bounds
    if isinstance(query := _where(request, str(context["uid"])), JSONResponse):
        return query
    try:
        rows = await _list_rows(request, str(context["uid"]), limit=limit, offset=offset)
    except Exception:
        return JSONResponse({"error": "screen activity unavailable"}, status_code=503)
    return [_response(row) for row in rows]


@router.get("/v1/screen-activity/summary")
async def screen_activity_summary(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if isinstance(bounds := _day_bounds(request), JSONResponse):
        return bounds
    if isinstance(query := _where(request, str(context["uid"])), JSONResponse):
        return query
    try:
        rows = await _list_rows(request, str(context["uid"]), limit=MAX_LIST_LIMIT)
    except Exception:
        return JSONResponse({"error": "screen activity unavailable"}, status_code=503)
    apps: dict[str, dict[str, object]] = {}
    for row in rows:
        app_name = str(row.get("app_name") or "Unknown")
        entry = apps.setdefault(
            app_name,
            {
                "count": 0,
                "first_seen": row.get("timestamp"),
                "last_seen": row.get("timestamp"),
                "window_titles": set(),
            },
        )
        entry["count"] = int(entry["count"]) + 1
        entry["last_seen"] = row.get("timestamp")
        title = str(row.get("window_title") or "")
        if title:
            titles = entry["window_titles"]
            if isinstance(titles, set):
                titles.add(title)
    for entry in apps.values():
        titles = entry["window_titles"]
        entry["window_titles"] = list(titles)[:10] if isinstance(titles, set) else []
    return {"apps": apps, "total_screenshots": len(rows)}
