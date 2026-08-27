"""D1-backed announcement and release-note routes for the Cloudflare profile.

The publishing surface is secret-gated and staging-only. This module owns the
public release-note reads, admin CRUD, and authenticated pending/dismissal
contract, so clients can use the Worker without Firestore or Redis calls.
"""

from __future__ import annotations

import json
import hmac
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from internal_auth import decode_context

router = APIRouter()

MAX_REQUEST_BYTES = 16_000
MAX_QUERY_LENGTH = 256
MAX_ANNOUNCEMENT_ID_LENGTH = 256
MAX_DEVICE_MODEL_LENGTH = 256
MAX_ROWS = 500
MAX_CHANGELOG_LIMIT = 50
MAX_CONTENT_BYTES = 12_000
_ANNOUNCEMENT_TYPES = frozenset({"changelog", "feature", "announcement"})

_COLUMNS = (
    "id, type, created_at, active, app_version, firmware_version, device_models_json, expires_at, "
    "targeting_json, display_json, content_json"
)
_TRIGGER_MAP = {
    "app_launch": "immediate",
    "version_upgrade": "version_upgrade",
    "firmware_upgrade": "firmware_upgrade",
}


class DismissAnnouncementRequest(BaseModel):
    model_config = {"extra": "ignore"}

    cta_clicked: bool = False


class AnnouncementCreateRequest(BaseModel):
    model_config = {"extra": "ignore"}

    id: str = Field(min_length=1, max_length=MAX_ANNOUNCEMENT_ID_LENGTH)
    type: str = Field(min_length=1, max_length=32)
    active: bool = True
    app_version: str | None = Field(default=None, max_length=MAX_QUERY_LENGTH)
    firmware_version: str | None = Field(default=None, max_length=MAX_QUERY_LENGTH)
    device_models: list[str] | None = Field(default=None, max_length=32)
    expires_at: datetime | None = None
    targeting: dict[str, object] | None = None
    display: dict[str, object] | None = None
    content: dict[str, object] = Field(default_factory=dict)


class AnnouncementUpdateRequest(BaseModel):
    model_config = {"extra": "ignore"}

    active: bool | None = None
    app_version: str | None = Field(default=None, max_length=MAX_QUERY_LENGTH)
    firmware_version: str | None = Field(default=None, max_length=MAX_QUERY_LENGTH)
    device_models: list[str] | None = Field(default=None, max_length=32)
    expires_at: datetime | None = None
    targeting: dict[str, object] | None = None
    display: dict[str, object] | None = None
    content: dict[str, object] | None = None


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


async def _bounded_json(request: Request) -> object:
    body_reader = getattr(request, "body", None)
    if callable(body_reader):
        raw = await body_reader()
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("request body exceeds size limit")
        return json.loads(raw)
    body = await request.json()
    if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError("request body exceeds size limit")
    return body


def _bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _iso(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _epoch_value(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return int(normalized.astimezone(timezone.utc).timestamp())
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp())


def _json_object(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _json_strings(value: object) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    if not isinstance(value, str) or not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [item for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []


def _record(row: dict[str, object]) -> dict[str, object]:
    targeting = _json_object(row.get("targeting_json"))
    display = _json_object(row.get("display_json"))
    return {
        "id": str(row.get("id") or ""),
        "type": str(row.get("type") or "announcement"),
        "created_at": _iso(row.get("created_at")) or datetime.fromtimestamp(0, timezone.utc).isoformat(),
        "active": _bool(row.get("active"), default=True),
        "app_version": row.get("app_version"),
        "firmware_version": row.get("firmware_version"),
        "device_models": _json_strings(row.get("device_models_json")) or None,
        "expires_at": _iso(row.get("expires_at")),
        "targeting": targeting,
        "display": display,
        "content": _json_object(row.get("content_json")) or {},
    }


def _version_parts(version: str) -> tuple[tuple[int, int, int], int, bool]:
    candidate = (version or "").strip().lstrip("v")
    if not candidate:
        return (0, 0, 0), 0, False
    has_build = "+" in candidate
    semantic, _, raw_build = candidate.partition("+")
    try:
        build = int(raw_build) if has_build else 0
    except ValueError:
        build = 0
    pieces = semantic.split(".")
    try:
        numbers = [int(piece) for piece in pieces[:3]]
    except ValueError:
        return (0, 0, 0), 0, has_build
    numbers.extend([0] * (3 - len(numbers)))
    return (numbers[0], numbers[1], numbers[2]), build, has_build


def _compare_versions(left: str, right: str) -> int:
    left_semantic, left_build, left_has_build = _version_parts(left)
    right_semantic, right_build, right_has_build = _version_parts(right)
    if left_semantic != right_semantic:
        return -1 if left_semantic < right_semantic else 1
    if not left_has_build or not right_has_build:
        return 0
    if left_build == right_build:
        return 0
    return -1 if left_build < right_build else 1


def _limit(value: int, maximum: int) -> int | None:
    return value if 1 <= value <= maximum else None


def _integer(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _legacy_targeting(row: dict[str, object]) -> dict[str, object]:
    return {
        "app_version_min": row.get("app_version"),
        "app_version_max": row.get("app_version"),
        "firmware_version_min": row.get("firmware_version"),
        "firmware_version_max": row.get("firmware_version"),
        "device_models": _json_strings(row.get("device_models_json")) or None,
        "trigger": "version_upgrade",
    }


def _targeting(row: dict[str, object]) -> dict[str, object]:
    return _json_object(row.get("targeting_json")) or _legacy_targeting(row)


def _display(row: dict[str, object]) -> dict[str, object]:
    return _json_object(row.get("display_json")) or {"expires_at": _iso(row.get("expires_at"))}


def _version_matches(candidate: str | None, minimum: object, maximum: object) -> bool:
    if minimum and (not candidate or _compare_versions(candidate, str(minimum)) < 0):
        return False
    if maximum and (not candidate or _compare_versions(candidate, str(maximum)) > 0):
        return False
    return True


def _validate_type(type_value: str | None) -> bool:
    return type_value is None or type_value in _ANNOUNCEMENT_TYPES


def _json_dump(value: object, default: object) -> str:
    return json.dumps(default if value is None else value, ensure_ascii=False, separators=(",", ":"))


def _admin_key_valid(request: Request) -> bool:
    expected = getattr(request.scope["env"], "ANNOUNCEMENTS_ADMIN_KEY", None)
    provided = request.headers.get("x-announcements-admin-key") or request.headers.get("secret-key")
    return (
        isinstance(expected, str)
        and bool(expected)
        and isinstance(provided, str)
        and hmac.compare_digest(provided, expected)
    )


def _require_admin(request: Request) -> JSONResponse | None:
    if not _admin_key_valid(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return None


async def _admin_rows(env: object, *, type_value: str | None, active_only: bool) -> list[dict[str, object]]:
    clauses = []
    params: list[object] = []
    if type_value is not None:
        clauses.append("type = ?")
        params.append(type_value)
    if active_only:
        clauses.append("active = 1")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    result = (
        await env.APP_DB.prepare(f"SELECT {_COLUMNS} FROM cf_announcements{where} ORDER BY created_at DESC LIMIT ?")
        .bind(*params, MAX_ROWS)
        .all()
    )
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [row for row in rows if isinstance(row, dict)]


async def _admin_row(env: object, announcement_id: str) -> dict[str, object] | None:
    row = (
        await env.APP_DB.prepare(f"SELECT {_COLUMNS} FROM cf_announcements WHERE id = ?").bind(announcement_id).first()
    )
    return row if isinstance(row, dict) else None


def _pending_match(
    row: dict[str, object],
    *,
    uid: str,
    app_version: str,
    platform: str,
    trigger: str,
    firmware_version: str | None,
    device_model: str | None,
    dismissed_ids: set[str],
    now: int,
) -> bool:
    targeting = _targeting(row)
    display = _display(row)
    announcement_id = str(row.get("id") or "")
    if _bool(display.get("show_once"), default=True) and announcement_id in dismissed_ids:
        return False
    start_at = _epoch_value(display.get("start_at"))
    expires_at = _epoch_value(display.get("expires_at"))
    if expires_at is None:
        expires_at = _integer(row.get("expires_at"), default=-1) if row.get("expires_at") is not None else None
    if start_at is not None and now < start_at:
        return False
    if expires_at is not None and now > expires_at:
        return False

    requested_trigger = _TRIGGER_MAP[trigger]
    configured_trigger = str(targeting.get("trigger") or "version_upgrade")
    if configured_trigger != requested_trigger and configured_trigger != "immediate":
        return False
    platforms = _json_strings(targeting.get("platforms"))
    if platforms and platform not in platforms:
        return False
    if not _version_matches(app_version, targeting.get("app_version_min"), targeting.get("app_version_max")):
        return False
    if targeting.get("firmware_version_min") or targeting.get("firmware_version_max"):
        if not _version_matches(
            firmware_version,
            targeting.get("firmware_version_min"),
            targeting.get("firmware_version_max"),
        ):
            return False
    device_models = _json_strings(targeting.get("device_models"))
    if device_models and (not device_model or device_model not in device_models):
        return False
    test_uids = _json_strings(targeting.get("test_uids"))
    return not test_uids or uid in test_uids


async def _all_rows(env: object, *, type_value: str | None = None) -> list[dict[str, object]]:
    if type_value is None:
        sql = f"SELECT {_COLUMNS} FROM cf_announcements WHERE active = 1 ORDER BY created_at DESC LIMIT ?"
        params: list[object] = [MAX_ROWS]
    else:
        sql = (
            f"SELECT {_COLUMNS} FROM cf_announcements WHERE active = 1 AND type = ? " "ORDER BY created_at DESC LIMIT ?"
        )
        params = [type_value, MAX_ROWS]
    result = await env.APP_DB.prepare(sql).bind(*params).all()
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [row for row in rows if isinstance(row, dict)]


@router.get("/v1/announcements/changelogs")
async def get_changelogs(
    request: Request,
    from_version: str | None = None,
    to_version: str | None = None,
    max_version: str | None = None,
    limit: int = 5,
):
    if _limit(limit, MAX_CHANGELOG_LIMIT) is None:
        return JSONResponse({"error": "invalid limit"}, status_code=400)
    for value in (from_version, to_version, max_version):
        if value is not None and (not value or len(value) > MAX_QUERY_LENGTH):
            return JSONResponse({"error": "invalid version"}, status_code=400)
    try:
        rows = await _all_rows(request.scope["env"], type_value="changelog")
    except Exception:
        return JSONResponse({"error": "announcements unavailable"}, status_code=503)
    if from_version and to_version:
        rows = [
            row
            for row in rows
            if row.get("app_version")
            and _compare_versions(from_version, str(row["app_version"])) < 0
            and _compare_versions(str(row["app_version"]), to_version) <= 0
        ]
    elif max_version:
        rows = [
            row
            for row in rows
            if row.get("app_version") and _compare_versions(str(row["app_version"]), max_version) <= 0
        ]
    rows.sort(key=lambda row: _version_parts(str(row.get("app_version") or "0")), reverse=True)
    return [_record(row) for row in rows[:limit]]


@router.get("/v1/announcements/features")
async def get_features(
    request: Request,
    version: str,
    version_type: str,
    device_model: str | None = None,
):
    if not version or len(version) > MAX_QUERY_LENGTH or version_type not in {"app", "firmware"}:
        return JSONResponse({"error": "invalid feature version"}, status_code=400)
    if device_model is not None and len(device_model) > MAX_DEVICE_MODEL_LENGTH:
        return JSONResponse({"error": "invalid device model"}, status_code=400)
    field = "app_version" if version_type == "app" else "firmware_version"
    try:
        rows = await _all_rows(request.scope["env"], type_value="feature")
    except Exception:
        return JSONResponse({"error": "announcements unavailable"}, status_code=503)
    result = []
    for row in rows:
        if str(row.get(field) or "") != version:
            continue
        models = _json_strings(row.get("device_models_json"))
        if device_model and models and device_model not in models:
            continue
        result.append(_record(row))
    return result


@router.get("/v1/announcements/general")
async def get_general_announcements(request: Request, last_checked_at: str | None = None):
    if last_checked_at is not None and len(last_checked_at) > MAX_QUERY_LENGTH:
        return JSONResponse({"error": "invalid last_checked_at"}, status_code=400)
    checked_at = _epoch_value(last_checked_at) if last_checked_at else None
    now = int(time.time())
    try:
        rows = await _all_rows(request.scope["env"], type_value="announcement")
    except Exception:
        return JSONResponse({"error": "announcements unavailable"}, status_code=503)
    result = []
    for row in rows:
        created_at = _integer(row.get("created_at"))
        expires_at = _epoch_value(row.get("expires_at"))
        if expires_at is None and row.get("expires_at") is not None:
            expires_at = _integer(row.get("expires_at"), default=-1)
        if checked_at is not None and created_at <= checked_at:
            continue
        if expires_at is not None and now > expires_at:
            continue
        result.append(_record(row))
    result.sort(key=lambda item: item["created_at"], reverse=True)
    return result


@router.get("/v1/announcements/pending")
async def get_pending_announcements(
    request: Request,
    app_version: str,
    platform: str,
    trigger: str,
    firmware_version: str | None = None,
    device_model: str | None = None,
):
    if not app_version or len(app_version) > MAX_QUERY_LENGTH:
        return JSONResponse({"error": "invalid app_version"}, status_code=400)
    if platform not in {"ios", "android"}:
        return JSONResponse({"error": "Platform must be 'ios' or 'android'"}, status_code=400)
    if trigger not in _TRIGGER_MAP:
        return JSONResponse(
            {"error": "Trigger must be 'app_launch', 'version_upgrade', or 'firmware_upgrade'"}, status_code=400
        )
    if firmware_version is not None and len(firmware_version) > MAX_QUERY_LENGTH:
        return JSONResponse({"error": "invalid firmware_version"}, status_code=400)
    if device_model is not None and len(device_model) > MAX_DEVICE_MODEL_LENGTH:
        return JSONResponse({"error": "invalid device_model"}, status_code=400)
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        rows = await _all_rows(env)
        dismissal_result = (
            await env.APP_DB.prepare("SELECT announcement_id FROM cf_announcement_dismissals WHERE uid = ?")
            .bind(uid)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "announcements unavailable"}, status_code=503)
    dismissal_rows = dismissal_result.get("results", []) if isinstance(dismissal_result, dict) else []
    dismissed_ids = {
        str(row.get("announcement_id"))
        for row in dismissal_rows
        if isinstance(row, dict) and row.get("announcement_id")
    }
    matching = [
        row
        for row in rows
        if _pending_match(
            row,
            uid=uid,
            app_version=app_version,
            platform=platform,
            trigger=trigger,
            firmware_version=firmware_version,
            device_model=device_model,
            dismissed_ids=dismissed_ids,
            now=int(time.time()),
        )
    ]
    matching.sort(
        key=lambda row: (
            _integer(_display(row).get("priority")),
            _integer(row.get("created_at")),
        ),
        reverse=True,
    )
    return [_record(row) for row in matching]


@router.get("/v1/announcements/all")
async def list_all_announcements(request: Request, announcement_type: str | None = None, active_only: bool = False):
    forbidden = _require_admin(request)
    if forbidden:
        return forbidden
    if not _validate_type(announcement_type):
        return JSONResponse({"error": "invalid announcement type"}, status_code=400)
    try:
        rows = await _admin_rows(request.scope["env"], type_value=announcement_type, active_only=active_only)
    except Exception:
        return JSONResponse({"error": "announcements unavailable"}, status_code=503)
    return [_record(row) for row in rows]


@router.get("/v1/announcements/{announcement_id}")
async def get_announcement(request: Request, announcement_id: str):
    forbidden = _require_admin(request)
    if forbidden:
        return forbidden
    if not announcement_id or len(announcement_id) > MAX_ANNOUNCEMENT_ID_LENGTH:
        return JSONResponse({"error": "invalid announcement id"}, status_code=400)
    try:
        row = await _admin_row(request.scope["env"], announcement_id)
    except Exception:
        return JSONResponse({"error": "announcements unavailable"}, status_code=503)
    return _record(row) if row else JSONResponse({"error": "announcement not found"}, status_code=404)


@router.post("/v1/announcements")
async def create_announcement(request: Request):
    forbidden = _require_admin(request)
    if forbidden:
        return forbidden
    try:
        payload = AnnouncementCreateRequest.model_validate(await _bounded_json(request))
        if payload.type not in _ANNOUNCEMENT_TYPES:
            raise ValueError("invalid announcement type")
        if len(json.dumps(payload.content, ensure_ascii=False).encode("utf-8")) > MAX_CONTENT_BYTES:
            raise ValueError("content too large")
        env = request.scope["env"]
        if await _admin_row(env, payload.id):
            return JSONResponse({"error": "announcement already exists"}, status_code=409)
        now = int(time.time())
        await env.APP_DB.prepare(
            "INSERT INTO cf_announcements "
            "(id, type, created_at, active, app_version, firmware_version, device_models_json, expires_at, "
            "targeting_json, display_json, content_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        ).bind(
            payload.id,
            payload.type,
            now,
            int(payload.active),
            payload.app_version,
            payload.firmware_version,
            _json_dump(payload.device_models, []),
            _epoch_value(payload.expires_at),
            _json_dump(payload.targeting, None),
            _json_dump(payload.display, None),
            _json_dump(payload.content, {}),
        ).run()
        row = await _admin_row(env, payload.id)
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid announcement"}, status_code=400)
    except Exception:
        return JSONResponse({"error": "announcements unavailable"}, status_code=503)
    return _record(row) if row else JSONResponse({"error": "announcement unavailable"}, status_code=503)


@router.put("/v1/announcements/{announcement_id}")
async def update_announcement(request: Request, announcement_id: str):
    forbidden = _require_admin(request)
    if forbidden:
        return forbidden
    if not announcement_id or len(announcement_id) > MAX_ANNOUNCEMENT_ID_LENGTH:
        return JSONResponse({"error": "invalid announcement id"}, status_code=400)
    try:
        payload = AnnouncementUpdateRequest.model_validate(await _bounded_json(request))
        values = payload.model_dump(exclude_unset=True)
        if not values:
            return JSONResponse({"error": "no fields to update"}, status_code=400)
        if "content" in values and values["content"] is not None:
            if len(json.dumps(values["content"], ensure_ascii=False).encode("utf-8")) > MAX_CONTENT_BYTES:
                raise ValueError("content too large")
        env = request.scope["env"]
        if await _admin_row(env, announcement_id) is None:
            return JSONResponse({"error": "announcement not found"}, status_code=404)
        updates: dict[str, object] = {}
        for field in ("active", "app_version", "firmware_version"):
            if field in values and values[field] is not None:
                updates[field] = int(values[field]) if field == "active" else values[field]
        if "device_models" in values and values["device_models"] is not None:
            updates["device_models_json"] = _json_dump(values["device_models"], [])
        if "expires_at" in values and values["expires_at"] is not None:
            updates["expires_at"] = _epoch_value(values["expires_at"])
        for field in ("targeting", "display", "content"):
            if field in values and values[field] is not None:
                updates[f"{field}_json"] = _json_dump(values[field], {})
        if not updates:
            return JSONResponse({"error": "no fields to update"}, status_code=400)
        assignments = ", ".join(f"{field} = ?" for field in updates)
        await env.APP_DB.prepare(f"UPDATE cf_announcements SET {assignments} WHERE id = ?").bind(
            *updates.values(), announcement_id
        ).run()
        row = await _admin_row(env, announcement_id)
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid announcement update"}, status_code=400)
    except Exception:
        return JSONResponse({"error": "announcements unavailable"}, status_code=503)
    return _record(row) if row else JSONResponse({"error": "announcement not found"}, status_code=404)


@router.delete("/v1/announcements/{announcement_id}")
async def delete_announcement(request: Request, announcement_id: str, soft_delete: bool = True):
    forbidden = _require_admin(request)
    if forbidden:
        return forbidden
    if not announcement_id or len(announcement_id) > MAX_ANNOUNCEMENT_ID_LENGTH:
        return JSONResponse({"error": "invalid announcement id"}, status_code=400)
    try:
        env = request.scope["env"]
        if await _admin_row(env, announcement_id) is None:
            return JSONResponse({"error": "announcement not found"}, status_code=404)
        if soft_delete:
            await env.APP_DB.prepare("UPDATE cf_announcements SET active = 0 WHERE id = ?").bind(announcement_id).run()
            message = "Announcement deactivated"
        else:
            await env.APP_DB.prepare("DELETE FROM cf_announcements WHERE id = ?").bind(announcement_id).run()
            message = "Announcement permanently deleted"
    except Exception:
        return JSONResponse({"error": "announcements unavailable"}, status_code=503)
    return {"success": True, "message": message}


@router.post("/v1/announcements/{announcement_id}/dismiss")
async def dismiss_announcement(request: Request, announcement_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not announcement_id or len(announcement_id) > MAX_ANNOUNCEMENT_ID_LENGTH:
        return JSONResponse({"error": "invalid announcement id"}, status_code=400)
    try:
        payload = DismissAnnouncementRequest.model_validate(await _bounded_json(request))
        row = (
            await request.scope["env"]
            .APP_DB.prepare("SELECT id FROM cf_announcements WHERE id = ? AND active = 1")
            .bind(announcement_id)
            .first()
        )
        if not isinstance(row, dict):
            return JSONResponse({"error": "announcement not found"}, status_code=404)
        await request.scope["env"].APP_DB.prepare(
            "INSERT INTO cf_announcement_dismissals "
            "(uid, announcement_id, dismissed_at, cta_clicked) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(uid, announcement_id) DO UPDATE SET dismissed_at = excluded.dismissed_at, "
            "cta_clicked = excluded.cta_clicked"
        ).bind(str(context["uid"]), announcement_id, int(time.time()), int(payload.cta_clicked)).run()
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid dismissal"}, status_code=400)
    except Exception:
        return JSONResponse({"error": "announcements unavailable"}, status_code=503)
    return {"success": True, "message": "Announcement dismissed"}


__all__ = [
    "router",
    "_compare_versions",
    "_pending_match",
]
