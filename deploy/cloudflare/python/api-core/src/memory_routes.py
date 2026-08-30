"""Canonical D1 memory routes for Better Auth accounts in the isolated staging profile.

The Cloudflare profile cannot read a Better Auth principal's historical
Firestore subtree. These routes therefore own one uid-scoped D1 authority for
accounts created inside the isolated staging environment. There is no legacy
fallback or dual write. Production promotion remains gated on the repository's
account-cutover importer and verification contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import time
from typing import Literal
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from internal_auth import decode_context
from account_routes import usage_source_statement

router = APIRouter()

MAX_REQUEST_BYTES = 256_000
MAX_CONTENT_LENGTH = 50_000
MAX_ID_LENGTH = 256
MAX_LIST_LIMIT = 500
MAX_LIST_OFFSET = 100_000
MAX_TAGS = 100
MAX_TAG_LENGTH = 256
MAX_BATCH_DELETE = 100
MEMORY_CATEGORIES = frozenset({"interesting", "system", "manual", "workflow"})
LEGACY_CATEGORY_MAP = {
    "core": "system",
    "hobbies": "system",
    "lifestyle": "system",
    "interests": "system",
    "habits": "system",
    "work": "system",
    "skills": "system",
    "learnings": "system",
    "other": "system",
    "auto": "system",
}


class MemoryCreate(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    content: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)
    category: str = "interesting"
    visibility: Literal["public", "private"] = "private"
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    headline: str | None = Field(default=None, max_length=512)
    predicate: str | None = Field(default=None, max_length=256)
    arguments: dict[str, object] = Field(default_factory=dict)
    subject_entity_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    subject_attribution: Literal["user", "third_party", "unknown", "legacy_assumed"] = "unknown"
    object_entity_ids: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    qualifiers: dict[str, object] = Field(default_factory=dict)
    capture_confidence: float | None = Field(default=None, ge=0, le=1)
    veracity: float | None = Field(default=None, ge=0, le=1)
    uncertainty_reasons: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    durability: str | None = Field(default=None, max_length=128)

    @field_validator("category", mode="before")
    @classmethod
    def normalize_category(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("invalid memory category")
        normalized = value.strip().lower()
        if normalized in MEMORY_CATEGORIES:
            return normalized
        if normalized in LEGACY_CATEGORY_MAP:
            return LEGACY_CATEGORY_MAP[normalized]
        raise ValueError("invalid memory category")

    @model_validator(mode="after")
    def validate_json_fields(self) -> "MemoryCreate":
        if any(not tag or len(tag) > MAX_TAG_LENGTH for tag in self.tags):
            raise ValueError("invalid memory tag")
        if any(not item or len(item) > MAX_ID_LENGTH for item in self.object_entity_ids):
            raise ValueError("invalid object entity id")
        if any(not item or len(item) > MAX_TAG_LENGTH for item in self.uncertainty_reasons):
            raise ValueError("invalid uncertainty reason")
        for value in (self.tags, self.arguments, self.object_entity_ids, self.qualifiers, self.uncertainty_reasons):
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > MAX_REQUEST_BYTES:
                raise ValueError("memory metadata exceeds the size limit")
        return self


class MemoryValueUpdate(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    value: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)


class MemoryBatchDelete(BaseModel):
    model_config = {"extra": "ignore"}

    memory_ids: list[str] = Field(default_factory=list, max_length=MAX_BATCH_DELETE)

    @model_validator(mode="after")
    def validate_ids(self) -> "MemoryBatchDelete":
        if len(self.memory_ids) != len(set(self.memory_ids)):
            raise ValueError("memory_ids must not contain duplicates")
        if any(not memory_id or len(memory_id) > MAX_ID_LENGTH for memory_id in self.memory_ids):
            raise ValueError("invalid memory id")
        return self


class MemoryReadStatusUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    is_read: bool | None = None
    is_dismissed: bool | None = None

    @model_validator(mode="after")
    def validate_mutation(self) -> "MemoryReadStatusUpdate":
        if self.is_read is None and self.is_dismissed is None:
            raise ValueError("missing memory read mutation value")
        return self


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


def _iso(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _bool(value: object) -> bool:
    return bool(value) and value not in ("0", "false", "False", "no")


def _json(value: object, fallback: object) -> object:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_REQUEST_BYTES:
        return fallback
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    return parsed


def _response(row: dict[str, object]) -> dict[str, object]:
    memory_id = str(row.get("id") or "")
    tier = str(row.get("memory_tier") or "short_term")
    return {
        "id": memory_id,
        "memory_id": memory_id,
        "uid": str(row.get("uid") or ""),
        "content": str(row.get("content") or ""),
        "category": str(row.get("category") or "interesting"),
        "visibility": str(row.get("visibility") or "private"),
        "tags": _json(row.get("tags_json"), []),
        "headline": row.get("headline"),
        "predicate": row.get("predicate"),
        "arguments": _json(row.get("arguments_json"), {}),
        "subject_entity_id": row.get("subject_entity_id"),
        "subject_attribution": str(row.get("subject_attribution") or "unknown"),
        "object_entity_ids": _json(row.get("object_entity_ids_json"), []),
        "qualifiers": _json(row.get("qualifiers_json"), {}),
        "capture_confidence": row.get("capture_confidence"),
        "veracity": row.get("veracity"),
        "uncertainty_reasons": _json(row.get("uncertainty_reasons_json"), []),
        "durability": row.get("durability"),
        "conversation_id": row.get("conversation_id"),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "reviewed": _bool(row.get("reviewed")),
        "user_review": None if row.get("user_review") is None else _bool(row.get("user_review")),
        "manually_added": _bool(row.get("manually_added")),
        "edited": _bool(row.get("edited")),
        "scoring": row.get("scoring"),
        "app_id": row.get("app_id"),
        "data_protection_level": row.get("data_protection_level"),
        "is_locked": _bool(row.get("is_locked")),
        "is_read": _bool(row.get("is_read")),
        "is_dismissed": _bool(row.get("is_dismissed")),
        "kg_extracted": _bool(row.get("kg_extracted")),
        "is_baseline": _bool(row.get("is_baseline")),
        "evidence": [],
        "memory_tier": tier,
        "layer": tier,
        "valid_at": _iso(row.get("valid_at")),
        "invalid_at": _iso(row.get("invalid_at")),
        "superseded_by": row.get("superseded_by"),
        "primary_capture_device": row.get("primary_capture_device"),
        "capture_device_ids": _json(row.get("capture_device_ids_json"), []),
    }


_SELECT = (
    "SELECT uid, id, content, category, visibility, tags_json, headline, predicate, arguments_json, "
    "subject_entity_id, subject_attribution, object_entity_ids_json, qualifiers_json, capture_confidence, veracity, "
    "uncertainty_reasons_json, durability, conversation_id, reviewed, user_review, manually_added, edited, scoring, "
    "app_id, data_protection_level, is_locked, is_read, is_dismissed, kg_extracted, is_baseline, memory_tier, "
    "valid_at, invalid_at, superseded_by, primary_capture_device, capture_device_ids_json, created_at, updated_at "
    "FROM cf_memories "
)


async def _first_active(env: object, uid: str, memory_id: str) -> dict[str, object] | None:
    row = (
        await env.APP_DB.prepare(_SELECT + "WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL")
        .bind(uid, memory_id)
        .first()
    )
    return row if isinstance(row, dict) else None


async def _mutable_memory(env: object, uid: str, memory_id: str) -> dict[str, object] | JSONResponse:
    row = await _first_active(env, uid, memory_id)
    if row is None:
        return JSONResponse({"error": "memory not found"}, status_code=404)
    if _bool(row.get("is_locked")):
        return JSONResponse(
            {"error": "A paid plan is required to access this memory."},
            status_code=402,
        )
    return row


def _query_value(request: Request, name: str) -> str | None:
    value = request.query_params.get(name)
    return value if isinstance(value, str) else None


def _query_bool(request: Request, name: str) -> bool | None:
    value = _query_value(request, name)
    if value is None:
        return None
    normalized = value.lower()
    if normalized in {"1", "true", "on", "yes"}:
        return True
    if normalized in {"0", "false", "off", "no"}:
        return False
    return None


@router.get("/v3/memories")
async def list_memories(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        limit = int(_query_value(request, "limit") or "100")
        offset = int(_query_value(request, "offset") or "0")
    except ValueError:
        return JSONResponse({"error": "invalid pagination"}, status_code=400)
    if limit < 1 or limit > MAX_LIST_LIMIT or offset < 0 or offset > MAX_LIST_OFFSET:
        return JSONResponse({"error": "invalid pagination"}, status_code=400)

    categories_raw = _query_value(request, "categories")
    categories: list[str] = []
    if categories_raw:
        categories = list(dict.fromkeys(item.strip() for item in categories_raw.split(",") if item.strip()))
        if not categories or any(item not in MEMORY_CATEGORIES for item in categories):
            return JSONResponse({"error": "invalid memory categories"}, status_code=400)
    uid = str(context["uid"])
    query = _SELECT + "WHERE uid = ? AND deleted_at IS NULL AND invalid_at IS NULL"
    args: list[object] = [uid]
    if categories:
        query += " AND category IN (" + ",".join("?" for _ in categories) + ")"
        args.extend(categories)
    query += " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
    args.extend((limit, offset))
    try:
        result = await request.scope["env"].APP_DB.prepare(query).bind(*args).all()
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [_response(row) for row in rows if isinstance(row, dict)]


@router.post("/v3/memories")
async def create_memory(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        memory = MemoryCreate.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid memory"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    now = int(time.time())
    memory_id = uuid.uuid4().hex
    manually_added = memory.category == "manual"
    tier = "long_term" if manually_added or (memory.durability or "").lower() == "long_term" else "short_term"
    try:
        memory_statement = env.APP_DB.prepare(
            "INSERT INTO cf_memories "
            "(uid, id, content, category, visibility, tags_json, headline, predicate, arguments_json, "
            "subject_entity_id, subject_attribution, object_entity_ids_json, qualifiers_json, capture_confidence, "
            "veracity, uncertainty_reasons_json, durability, manually_added, memory_tier, valid_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        ).bind(
            uid,
            memory_id,
            memory.content,
            memory.category,
            memory.visibility,
            json.dumps(memory.tags, ensure_ascii=False, separators=(",", ":")),
            memory.headline,
            memory.predicate,
            json.dumps(memory.arguments, ensure_ascii=False, separators=(",", ":")),
            memory.subject_entity_id,
            memory.subject_attribution,
            json.dumps(memory.object_entity_ids, ensure_ascii=False, separators=(",", ":")),
            json.dumps(memory.qualifiers, ensure_ascii=False, separators=(",", ":")),
            memory.capture_confidence,
            memory.veracity,
            json.dumps(memory.uncertainty_reasons, ensure_ascii=False, separators=(",", ":")),
            memory.durability,
            int(manually_added),
            tier,
            now,
            now,
            now,
        )
        usage_statement = usage_source_statement(
            env,
            uid=uid,
            source_kind="memory",
            source_id=memory_id,
            occurred_at=now,
            memories_created=1,
            updated_at=now,
        )
        await env.APP_DB.batch([memory_statement, usage_statement])
        row = await _first_active(env, uid, memory_id)
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    if row is None:
        return JSONResponse({"error": "memory unavailable"}, status_code=503)
    return _response(row)


@router.delete("/v3/memories/batch")
async def delete_memories_batch(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        deletion = MemoryBatchDelete.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid memory deletion"}, status_code=400)
    if not deletion.memory_ids:
        return {"status": "ok"}
    uid = str(context["uid"])
    env = request.scope["env"]
    placeholders = ",".join("?" for _ in deletion.memory_ids)
    try:
        rows = (
            await env.APP_DB.prepare(
                "SELECT id FROM cf_memories WHERE uid = ? AND deleted_at IS NULL AND invalid_at IS NULL "
                f"AND id IN ({placeholders})"
            )
            .bind(uid, *deletion.memory_ids)
            .all()
        )
        found = {
            str(row["id"])
            for row in (rows.get("results", []) if isinstance(rows, dict) else [])
            if isinstance(row, dict) and row.get("id")
        }
        if found != set(deletion.memory_ids):
            return JSONResponse({"error": "memory not found"}, status_code=404)
        now = int(time.time())
        statements = [
            env.APP_DB.prepare(
                "UPDATE cf_memories SET deleted_at = ?, updated_at = ? "
                "WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
            ).bind(now, now, uid, memory_id)
            for memory_id in deletion.memory_ids
        ]
        await env.APP_DB.batch(statements)
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    return {"status": "ok"}


@router.delete("/v3/memories/{memory_id}")
async def delete_memory(request: Request, memory_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not memory_id or len(memory_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "memory not found"}, status_code=404)
    uid = str(context["uid"])
    env = request.scope["env"]
    now = int(time.time())
    try:
        if await _first_active(env, uid, memory_id) is None:
            return JSONResponse({"error": "memory not found"}, status_code=404)
        await env.APP_DB.prepare(
            "UPDATE cf_memories SET deleted_at = ?, updated_at = ? "
            "WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
        ).bind(now, now, uid, memory_id).run()
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    return {"status": "ok"}


@router.delete("/v3/memories")
async def delete_all_memories(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    uid = str(context["uid"])
    now = int(time.time())
    scope = _query_value(request, "scope") or "all"
    if scope not in {"all", "default"}:
        return JSONResponse({"error": "invalid memory deletion scope"}, status_code=400)
    tier_filter = " AND memory_tier != 'archive'" if scope == "default" else ""
    try:
        await request.scope["env"].APP_DB.prepare(
            "UPDATE cf_memories SET deleted_at = ?, updated_at = ? "
            "WHERE uid = ? AND deleted_at IS NULL AND invalid_at IS NULL" + tier_filter
        ).bind(now, now, uid).run()
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    return {"status": "ok"}


@router.patch("/v3/memories/{memory_id}")
async def update_memory_content(request: Request, memory_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    raw_value = _query_value(request, "value")
    try:
        update = (
            MemoryValueUpdate(value=raw_value)
            if raw_value is not None
            else MemoryValueUpdate.model_validate(await _bounded_json(request))
        )
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid memory content"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        if await _first_active(env, uid, memory_id) is None:
            return JSONResponse({"error": "memory not found"}, status_code=404)
        await env.APP_DB.prepare(
            "UPDATE cf_memories SET content = ?, edited = 1, updated_at = ? "
            "WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
        ).bind(update.value, int(time.time()), uid, memory_id).run()
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    return {"status": "ok"}


@router.patch("/v3/memories/{memory_id}/visibility")
async def update_memory_visibility(request: Request, memory_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    raw_value = _query_value(request, "value")
    if raw_value is None:
        try:
            raw_value = MemoryValueUpdate.model_validate(await _bounded_json(request)).value
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
            return JSONResponse({"error": "invalid memory visibility"}, status_code=400)
    if raw_value not in {"public", "private"}:
        return JSONResponse({"error": "invalid memory visibility"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        if await _first_active(env, uid, memory_id) is None:
            return JSONResponse({"error": "memory not found"}, status_code=404)
        await env.APP_DB.prepare(
            "UPDATE cf_memories SET visibility = ?, updated_at = ? "
            "WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
        ).bind(raw_value, int(time.time()), uid, memory_id).run()
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    return {"status": "ok"}


@router.post("/v3/memories/{memory_id}/review")
async def review_memory(request: Request, memory_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    raw_value = (_query_value(request, "value") or "").lower()
    if raw_value not in {"true", "false"}:
        return JSONResponse({"error": "invalid memory review"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    value = raw_value == "true"
    try:
        if await _first_active(env, uid, memory_id) is None:
            return JSONResponse({"error": "memory not found"}, status_code=404)
        await env.APP_DB.prepare(
            "UPDATE cf_memories SET reviewed = 1, user_review = ?, updated_at = ? "
            "WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
        ).bind(int(value), int(time.time()), uid, memory_id).run()
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    return {"status": "ok"}


@router.patch("/v3/memories/{memory_id}/read")
async def update_memory_read_status(request: Request, memory_id: str):
    """Persist the durable read and dismiss state used by desktop insights."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not memory_id or len(memory_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "memory not found"}, status_code=404)
    try:
        update = MemoryReadStatusUpdate.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid memory read status"}, status_code=422)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        existing = await _mutable_memory(env, uid, memory_id)
        if isinstance(existing, JSONResponse):
            return existing
        assignments: list[str] = []
        values: list[object] = []
        if update.is_read is not None:
            assignments.append("is_read = ?")
            values.append(int(update.is_read))
        if update.is_dismissed is not None:
            assignments.append("is_dismissed = ?")
            values.append(int(update.is_dismissed))
        now = int(time.time())
        assignments.append("updated_at = ?")
        values.extend((now, uid, memory_id))
        await env.APP_DB.prepare(
            "UPDATE cf_memories SET "
            + ", ".join(assignments)
            + " WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
        ).bind(*values).run()
        updated = await _first_active(env, uid, memory_id)
        if not isinstance(updated, dict):
            return JSONResponse({"error": "memory not found"}, status_code=404)
        return _response(updated)
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)


@router.patch("/v3/memories/{memory_id}/baseline")
async def update_memory_baseline(request: Request, memory_id: str):
    """Update the canonical baseline flag without changing memory content."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not memory_id or len(memory_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "memory not found"}, status_code=404)
    value = _query_bool(request, "value")
    if value is None:
        return JSONResponse({"error": "invalid memory baseline"}, status_code=422)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        existing = await _mutable_memory(env, uid, memory_id)
        if isinstance(existing, JSONResponse):
            return existing
        await env.APP_DB.prepare(
            "UPDATE cf_memories SET is_baseline = ?, updated_at = ? "
            "WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
        ).bind(int(value), int(time.time()), uid, memory_id).run()
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    return {"status": "ok"}
