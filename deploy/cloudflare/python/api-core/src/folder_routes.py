"""D1-backed folder metadata routes for the isolated Cloudflare profile.

Folder metadata and ordering are independent from conversation storage. Moving
conversation documents, derived conversation counts, and folder conversation
queries remain on the legacy Firestore owner until the conversation authority
is migrated as one contract.
"""

from __future__ import annotations

import json
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError, model_validator

from internal_auth import decode_context

router = APIRouter()

MAX_REQUEST_BYTES = 64_000
MAX_ID_LENGTH = 256

SYSTEM_FOLDERS = (
    {
        "name": "Work",
        "category_mapping": "work",
        "icon": "💼",
        "color": "#3B82F6",
        "description": "Work, business, professional, and career-related conversations",
    },
    {
        "name": "Personal",
        "category_mapping": "personal",
        "icon": "👤",
        "color": "#10B981",
        "description": "Personal life, family, health, hobbies, and self-improvement",
    },
    {
        "name": "Social",
        "category_mapping": "social",
        "icon": "👥",
        "color": "#8B5CF6",
        "description": "Friends, social gatherings, entertainment, and casual conversations",
    },
)


class FolderCreate(BaseModel):
    model_config = {"extra": "ignore"}

    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    color: str | None = Field(default=None, max_length=32)
    icon: str | None = Field(default=None, max_length=64)


class FolderUpdate(BaseModel):
    model_config = {"extra": "ignore"}

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    color: str | None = Field(default=None, max_length=32)
    icon: str | None = Field(default=None, max_length=64)
    order: int | None = Field(default=None, ge=0, le=100_000)

    @model_validator(mode="after")
    def require_update(self) -> "FolderUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one folder field is required")
        return self


class FolderReorder(BaseModel):
    model_config = {"extra": "ignore"}

    folder_ids: list[str] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def reject_duplicates(self) -> "FolderReorder":
        if len(self.folder_ids) != len(set(self.folder_ids)):
            raise ValueError("folder_ids must not contain duplicates")
        if any(not item or len(item) > MAX_ID_LENGTH for item in self.folder_ids):
            raise ValueError("invalid folder id")
        return self


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


def _iso(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _bool(value: object) -> bool:
    return bool(value) and value not in ("0", "false", "False")


def _response(row: dict[str, object]) -> dict[str, object]:
    return {
        "id": str(row.get("id") or ""),
        "name": str(row.get("name") or ""),
        "description": row.get("description"),
        "color": str(row.get("color") or "#6B7280"),
        "icon": str(row.get("icon") or "folder"),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "order": int(row.get("display_order") or 0),
        "is_default": _bool(row.get("is_default")),
        "is_system": _bool(row.get("is_system")),
        "category_mapping": row.get("category_mapping"),
        "conversation_count": int(row.get("conversation_count") or 0),
    }


_SELECT = (
    "SELECT id, name, description, color, icon, created_at, updated_at, display_order, is_default, is_system, "
    "category_mapping, conversation_count FROM cf_folders "
)


async def _first_folder(env: object, uid: str, folder_id: str) -> dict[str, object] | None:
    row = await env.APP_DB.prepare(_SELECT + "WHERE uid = ? AND id = ?").bind(uid, folder_id).first()
    return row if isinstance(row, dict) else None


async def _ensure_system_folders(env: object, uid: str) -> None:
    existing = await env.APP_DB.prepare("SELECT id FROM cf_folders WHERE uid = ? LIMIT 1").bind(uid).first()
    if isinstance(existing, dict):
        return
    now = int(time.time())
    for index, folder in enumerate(SYSTEM_FOLDERS):
        folder_id = f"system_{folder['category_mapping']}"
        await env.APP_DB.prepare(
            "INSERT INTO cf_folders (uid, id, name, description, color, icon, created_at, updated_at, display_order, "
            "is_default, is_system, category_mapping, conversation_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, 0) "
            "ON CONFLICT(uid, id) DO NOTHING"
        ).bind(
            uid,
            folder_id,
            folder["name"],
            folder["description"],
            folder["color"],
            folder["icon"],
            now,
            now,
            index,
            folder["category_mapping"],
        ).run()


@router.get("/v1/folders")
async def list_folders(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        await _ensure_system_folders(env, uid)
        result = (
            await env.APP_DB.prepare(_SELECT + "WHERE uid = ? ORDER BY display_order ASC, created_at ASC")
            .bind(uid)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "folders unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [_response(row) for row in rows if isinstance(row, dict)]


@router.post("/v1/folders")
async def create_folder(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        folder = FolderCreate.model_validate(await _bounded_json(request))
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid folder"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    now = int(time.time())
    folder_id = uuid.uuid4().hex
    try:
        await _ensure_system_folders(env, uid)
        maximum = (
            await env.APP_DB.prepare(
                "SELECT COALESCE(MAX(display_order), -1) AS max_order FROM cf_folders WHERE uid = ?"
            )
            .bind(uid)
            .first()
        )
        max_order = int(maximum.get("max_order") or -1) if isinstance(maximum, dict) else -1
        await env.APP_DB.prepare(
            "INSERT INTO cf_folders (uid, id, name, description, color, icon, created_at, updated_at, display_order, "
            "is_default, is_system, category_mapping, conversation_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, NULL, 0)"
        ).bind(
            uid,
            folder_id,
            folder.name,
            folder.description,
            folder.color or "#6B7280",
            folder.icon or "📁",
            now,
            now,
            max_order + 1,
        ).run()
        row = await _first_folder(env, uid, folder_id)
    except Exception:
        return JSONResponse({"error": "folders unavailable"}, status_code=503)
    return _response(row) if row else JSONResponse({"error": "folder unavailable"}, status_code=503)


@router.get("/v1/folders/{folder_id}")
async def get_folder(request: Request, folder_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not folder_id or len(folder_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid folder id"}, status_code=400)
    try:
        row = await _first_folder(request.scope["env"], str(context["uid"]), folder_id)
    except Exception:
        return JSONResponse({"error": "folders unavailable"}, status_code=503)
    return _response(row) if row else JSONResponse({"error": "folder not found"}, status_code=404)


@router.patch("/v1/folders/{folder_id}")
async def update_folder(request: Request, folder_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        update = FolderUpdate.model_validate(await _bounded_json(request))
        env = request.scope["env"]
        uid = str(context["uid"])
        existing = await _first_folder(env, uid, folder_id)
        if existing is None:
            return JSONResponse({"error": "folder not found"}, status_code=404)
        values = update.model_dump(exclude_unset=True)
        if "order" in values:
            values["display_order"] = values.pop("order")
        values["updated_at"] = int(time.time())
        allowed = {"name", "description", "color", "icon", "display_order", "updated_at"}
        values = {key: value for key, value in values.items() if key in allowed}
        assignments = ", ".join(f"{key} = ?" for key in values)
        await env.APP_DB.prepare(f"UPDATE cf_folders SET {assignments} WHERE uid = ? AND id = ?").bind(
            *values.values(), uid, folder_id
        ).run()
        row = await _first_folder(env, uid, folder_id)
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid folder update"}, status_code=400)
    except Exception:
        return JSONResponse({"error": "folders unavailable"}, status_code=503)
    return _response(row) if row else JSONResponse({"error": "folder not found"}, status_code=404)


@router.delete("/v1/folders/{folder_id}")
async def delete_folder(request: Request, folder_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    uid = str(context["uid"])
    try:
        existing = await _first_folder(env, uid, folder_id)
        if existing is None:
            return JSONResponse({"error": "folder not found"}, status_code=404)
        if _bool(existing.get("is_system")):
            return JSONResponse({"error": "cannot delete system folder"}, status_code=400)
        await env.APP_DB.prepare("DELETE FROM cf_folders WHERE uid = ? AND id = ?").bind(uid, folder_id).run()
    except Exception:
        return JSONResponse({"error": "folders unavailable"}, status_code=503)
    return Response(status_code=204)


@router.post("/v1/folders/reorder")
async def reorder_folders(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        reorder = FolderReorder.model_validate(await _bounded_json(request))
        env = request.scope["env"]
        uid = str(context["uid"])
        rows = await env.APP_DB.prepare("SELECT id FROM cf_folders WHERE uid = ?").bind(uid).all()
        existing_ids = (
            {str(row["id"]) for row in rows.get("results", []) if isinstance(row, dict)}
            if isinstance(rows, dict)
            else set()
        )
        unknown = [folder_id for folder_id in reorder.folder_ids if folder_id not in existing_ids]
        if unknown:
            return JSONResponse({"error": "unknown folder ids", "folder_ids": unknown}, status_code=422)
        now = int(time.time())
        for index, folder_id in enumerate(reorder.folder_ids):
            await env.APP_DB.prepare(
                "UPDATE cf_folders SET display_order = ?, updated_at = ? WHERE uid = ? AND id = ?"
            ).bind(index, now, uid, folder_id).run()
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid folder order"}, status_code=400)
    except Exception:
        return JSONResponse({"error": "folders unavailable"}, status_code=503)
    return {"status": "ok"}
