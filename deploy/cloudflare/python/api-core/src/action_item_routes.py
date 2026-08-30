"""D1-backed action-item routes for the isolated Cloudflare profile.

The module owns CRUD/reconciliation plus the D1-to-Vectorize lifecycle used by
semantic task search. Task-link validation and push/reminder side effects stay
explicit until their separate contracts migrate.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError, model_validator

from conversation_routes import _first_conversation
from internal_auth import decode_context
from vector_search import (
    embed_query,
    hydrate_candidate_ids,
    publish_vector_projection,
    query_vector_ids,
    vector_outbox_statement,
)

router = APIRouter()
batch_router = APIRouter()

MAX_REQUEST_BYTES = 64_000
MAX_LIST_LIMIT = 500
MAX_BATCH_ITEMS = 500
MAX_ID_LENGTH = 256
MAX_DESCRIPTION_LENGTH = 4_096
MAX_SOURCE_LENGTH = 64
MAX_RECURRENCE_RULE_LENGTH = 128
MAX_EXPORT_PLATFORM_LENGTH = 64
MAX_REMINDER_ID_LENGTH = 512
MAX_SHARE_TOKEN_LENGTH = 128
MAX_SHARE_TASKS = 20
TASK_SHARE_TTL_SECONDS = 60 * 60 * 24 * 30
TASK_SHARE_BASE_URL = "https://h.omi.me/tasks"


class TaskStatus(str, Enum):
    active = "active"
    completed = "completed"
    cancelled = "cancelled"
    superseded = "superseded"


class TaskOwner(str, Enum):
    user = "user"
    other = "other"
    unknown = "unknown"


class TaskPriority(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class ActionItemCreate(BaseModel):
    model_config = {"extra": "ignore"}

    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_LENGTH)
    status: TaskStatus | None = None
    completed: bool | None = None
    goal_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    workstream_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    owner: TaskOwner = TaskOwner.user
    due_at: datetime | None = None
    due_confidence: float | None = Field(default=None, ge=0, le=1)
    source: str = Field(default="manual", min_length=1, max_length=MAX_SOURCE_LENGTH)
    provenance: list[dict[str, object]] = Field(default_factory=list)
    priority: TaskPriority | None = None
    sort_order: int = 0
    indent_level: int = Field(default=0, ge=0, le=3)
    recurrence_rule: str | None = Field(default=None, max_length=MAX_RECURRENCE_RULE_LENGTH)
    recurrence_parent_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    conversation_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    exported: bool = False
    export_date: datetime | None = None
    export_platform: str | None = Field(default=None, max_length=MAX_EXPORT_PLATFORM_LENGTH)
    apple_reminder_id: str | None = Field(default=None, max_length=MAX_REMINDER_ID_LENGTH)

    @model_validator(mode="after")
    def reconcile_completed(self) -> "ActionItemCreate":
        if self.status is None:
            self.status = TaskStatus.completed if self.completed is True else TaskStatus.active
        expected = self.status == TaskStatus.completed
        if self.completed is not None and self.completed != expected:
            raise ValueError("completed must agree with status")
        self.completed = expected
        return self


class ActionItemUpdate(BaseModel):
    model_config = {"extra": "ignore"}

    description: str | None = Field(default=None, min_length=1, max_length=MAX_DESCRIPTION_LENGTH)
    status: TaskStatus | None = None
    completed: bool | None = None
    goal_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    workstream_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    owner: TaskOwner | None = None
    due_at: datetime | None = None
    due_confidence: float | None = Field(default=None, ge=0, le=1)
    source: str | None = Field(default=None, min_length=1, max_length=MAX_SOURCE_LENGTH)
    provenance: list[dict[str, object]] | None = None
    priority: TaskPriority | None = None
    sort_order: int | None = None
    indent_level: int | None = Field(default=None, ge=0, le=3)
    recurrence_rule: str | None = Field(default=None, max_length=MAX_RECURRENCE_RULE_LENGTH)
    recurrence_parent_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    superseded_by: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    exported: bool | None = None
    export_date: datetime | None = None
    export_platform: str | None = Field(default=None, max_length=MAX_EXPORT_PLATFORM_LENGTH)
    apple_reminder_id: str | None = Field(default=None, max_length=MAX_REMINDER_ID_LENGTH)
    clear_due_at: bool = False

    @model_validator(mode="after")
    def reconcile_completed(self) -> "ActionItemUpdate":
        incoming = set(self.model_fields_set) - {"clear_due_at"}
        if self.status is not None and self.completed is not None:
            if self.completed != (self.status == TaskStatus.completed):
                raise ValueError("completed must agree with status")
        elif self.status is not None:
            self.completed = self.status == TaskStatus.completed
        elif self.completed is not None:
            self.status = TaskStatus.completed if self.completed else TaskStatus.active
        if not incoming and not self.clear_due_at:
            raise ValueError("at least one task field is required")
        return self


class SyncBatchItem(BaseModel):
    model_config = {"extra": "ignore"}

    id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    description: str | None = Field(default=None, min_length=1, max_length=MAX_DESCRIPTION_LENGTH)
    completed: bool | None = None
    due_at: datetime | None = None
    exported: bool | None = None
    export_platform: str | None = Field(default=None, max_length=MAX_EXPORT_PLATFORM_LENGTH)
    apple_reminder_id: str | None = Field(default=None, max_length=MAX_REMINDER_ID_LENGTH)


class SyncBatchRequest(BaseModel):
    model_config = {"extra": "ignore"}

    items: list[SyncBatchItem] = Field(default_factory=list, max_length=100)


class ShareTasksRequest(BaseModel):
    model_config = {"extra": "ignore"}

    task_ids: list[str] = Field(min_length=1, max_length=MAX_SHARE_TASKS)


class AcceptSharedTasksRequest(BaseModel):
    model_config = {"extra": "ignore"}

    token: str = Field(min_length=1, max_length=MAX_SHARE_TOKEN_LENGTH)


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


def _epoch(value: datetime | None) -> int | None:
    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return int(normalized.astimezone(timezone.utc).timestamp())


def _iso(epoch: object) -> str | None:
    if epoch is None or isinstance(epoch, bool):
        return None
    try:
        return datetime.fromtimestamp(int(epoch), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _bool(value: object) -> bool:
    return bool(value) and value not in ("0", "false", "False")


def _json_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _response(row: dict[str, object]) -> dict[str, object]:
    item_id = str(row.get("id") or "")
    status = str(row.get("status") or ("completed" if _bool(row.get("completed")) else "active"))
    completed = status == TaskStatus.completed.value or _bool(row.get("completed"))
    provenance = _json_list(row.get("provenance_json"))
    response: dict[str, object] = {
        "id": item_id,
        "task_id": item_id,
        "description": str(row.get("description") or ""),
        "status": status,
        "completed": completed,
        "goal_id": row.get("goal_id"),
        "workstream_id": row.get("workstream_id"),
        "owner": str(row.get("owner") or TaskOwner.unknown.value),
        "due_at": _iso(row.get("due_at")),
        "due_confidence": row.get("due_confidence"),
        "source": str(row.get("source") or "legacy"),
        "provenance": provenance,
        "priority": row.get("priority"),
        "sort_order": int(row.get("sort_order") or 0),
        "indent_level": int(row.get("indent_level") or 0),
        "recurrence_rule": row.get("recurrence_rule"),
        "recurrence_parent_id": row.get("recurrence_parent_id"),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "completed_at": _iso(row.get("completed_at")),
        "superseded_by": row.get("superseded_by"),
        "conversation_id": row.get("conversation_id"),
        "is_locked": _bool(row.get("is_locked")),
        "exported": _bool(row.get("exported")),
        "export_date": _iso(row.get("export_date")),
        "export_platform": row.get("export_platform"),
        "apple_reminder_id": row.get("apple_reminder_id"),
    }
    shared_from = next(
        (
            entry
            for entry in provenance
            if isinstance(entry, dict) and entry.get("kind") == "shared" and isinstance(entry.get("sender_uid"), str)
        ),
        None,
    )
    if shared_from is not None:
        response["shared_from"] = {
            key: shared_from[key]
            for key in ("token", "sender_uid", "sender_name", "original_task_id")
            if key in shared_from
        }
    return response


def _normalized_description(description: str) -> str:
    return " ".join(description.strip().lower().split())


def _idempotency_key(uid: str, description: str) -> str:
    normalized = _normalized_description(description)
    return hashlib.sha256(f"{len(uid)}:{uid}:{normalized}".encode("utf-8")).hexdigest()


def _query_value(request: Request, name: str) -> str | None:
    params = getattr(request, "query_params", None)
    if params is None:
        return None
    value = params.get(name)
    return value if isinstance(value, str) else None


def _query_int(request: Request, name: str, default: int, minimum: int, maximum: int) -> int | None:
    raw = _query_value(request, name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if minimum <= value <= maximum else None


def _query_bool(request: Request, name: str) -> bool | None | object:
    raw = _query_value(request, name)
    if raw is None or raw == "":
        return None
    normalized = raw.strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return _INVALID


def _query_epoch(request: Request, name: str) -> int | None | object:
    raw = _query_value(request, name)
    if raw is None or raw == "":
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return _INVALID
    return _epoch(parsed)


_INVALID = object()


def _row_query(request: Request, uid: str) -> tuple[str, list[object]] | JSONResponse:
    limit = _query_int(request, "limit", 50, 1, MAX_LIST_LIMIT)
    offset = _query_int(request, "offset", 0, 0, 1_000_000)
    if limit is None or offset is None:
        return JSONResponse({"error": "invalid pagination"}, status_code=400)
    completed = _query_bool(request, "completed")
    if completed is _INVALID:
        return JSONResponse({"error": "invalid completed filter"}, status_code=400)
    clauses = ["uid = ?", "deleted = 0"]
    params: list[object] = [uid]
    if completed is not None:
        clauses.append("completed = ?")
        params.append(1 if completed else 0)
    conversation_id = _query_value(request, "conversation_id")
    if conversation_id:
        if len(conversation_id) > MAX_ID_LENGTH:
            return JSONResponse({"error": "conversation_id is too long"}, status_code=400)
        clauses.append("conversation_id = ?")
        params.append(conversation_id)
    bounds: dict[str, int] = {}
    for name, column in (
        ("start_date", "created_at"),
        ("end_date", "created_at"),
        ("due_start_date", "due_at"),
        ("due_end_date", "due_at"),
    ):
        value = _query_epoch(request, name)
        if value is _INVALID:
            return JSONResponse({"error": f"invalid {name}"}, status_code=400)
        if value is not None:
            bounds[name] = value
            operator = ">=" if name.endswith("start_date") else "<="
            clauses.append(f"{column} {operator} ?")
            params.append(value)
    if "start_date" in bounds and "end_date" in bounds and bounds["start_date"] > bounds["end_date"]:
        return JSONResponse({"error": "start_date must be earlier than or equal to end_date"}, status_code=400)
    if "due_start_date" in bounds and "due_end_date" in bounds and bounds["due_start_date"] > bounds["due_end_date"]:
        return JSONResponse({"error": "due_start_date must be earlier than or equal to due_end_date"}, status_code=400)
    where = " AND ".join(clauses)
    sql = (
        "SELECT id, description, status, completed, goal_id, workstream_id, owner, due_at, due_confidence, "
        "source, provenance_json, priority, sort_order, indent_level, recurrence_rule, recurrence_parent_id, "
        "created_at, updated_at, completed_at, superseded_by, conversation_id, is_locked, exported, export_date, "
        "export_platform, apple_reminder_id FROM cf_action_items WHERE "
        f"{where} ORDER BY completed ASC, CASE WHEN due_at IS NULL THEN 1 ELSE 0 END ASC, due_at ASC, created_at DESC "
        "LIMIT ? OFFSET ?"
    )
    params.extend([limit + 1, offset])
    return sql, params


async def _first_item(env: object, uid: str, item_id: str) -> dict[str, object] | None:
    row = (
        await env.APP_DB.prepare(
            "SELECT id, description, status, completed, goal_id, workstream_id, owner, due_at, due_confidence, "
            "source, provenance_json, priority, sort_order, indent_level, recurrence_rule, recurrence_parent_id, "
            "created_at, updated_at, completed_at, superseded_by, conversation_id, is_locked, exported, export_date, "
            "export_platform, apple_reminder_id FROM cf_action_items WHERE uid = ? AND id = ? AND deleted = 0"
        )
        .bind(uid, item_id)
        .first()
    )
    return row if isinstance(row, dict) else None


async def _items_for_ids(env: object, uid: str, item_ids: list[str]) -> list[dict[str, object]]:
    if not item_ids:
        return []
    placeholders = ",".join("?" for _ in item_ids)
    result = (
        await env.APP_DB.prepare(
            "SELECT id, description, status, completed, goal_id, workstream_id, owner, due_at, due_confidence, "
            "source, provenance_json, priority, sort_order, indent_level, recurrence_rule, recurrence_parent_id, "
            "created_at, updated_at, completed_at, superseded_by, conversation_id, is_locked, exported, export_date, "
            "export_platform, apple_reminder_id FROM cf_action_items "
            f"WHERE uid = ? AND id IN ({placeholders}) AND deleted = 0 AND is_locked = 0"
        )
        .bind(uid, *item_ids)
        .all()
    )
    rows = result.get("results", []) if isinstance(result, dict) else []
    by_id = {str(row["id"]): row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}
    return [by_id[item_id] for item_id in item_ids if item_id in by_id]


async def _insert_item(env: object, uid: str, item: ActionItemCreate) -> dict[str, object]:
    key = _idempotency_key(uid, item.description)
    existing = (
        await env.APP_DB.prepare(
            "SELECT id, description, status, completed, goal_id, workstream_id, owner, due_at, due_confidence, "
            "source, provenance_json, priority, sort_order, indent_level, recurrence_rule, recurrence_parent_id, "
            "created_at, updated_at, completed_at, superseded_by, conversation_id, is_locked, exported, export_date, "
            "export_platform, apple_reminder_id FROM cf_action_items "
            "WHERE uid = ? AND idempotency_key = ? AND deleted = 0 AND completed = 0 LIMIT 1"
        )
        .bind(uid, key)
        .first()
    )
    if isinstance(existing, dict):
        return existing

    now = int(time.time())
    item_id = uuid.uuid4().hex
    insert = env.APP_DB.prepare(
        "INSERT INTO cf_action_items (uid, id, description, status, completed, goal_id, workstream_id, owner, "
        "due_at, due_confidence, source, provenance_json, priority, sort_order, indent_level, recurrence_rule, "
        "recurrence_parent_id, conversation_id, is_locked, exported, export_date, export_platform, "
        "apple_reminder_id, completed_at, created_at, updated_at, idempotency_key, sync_requested, deleted) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)"
    ).bind(
        uid,
        item_id,
        item.description,
        item.status.value if item.status else TaskStatus.active.value,
        1 if item.completed else 0,
        item.goal_id,
        item.workstream_id,
        item.owner.value,
        _epoch(item.due_at),
        item.due_confidence,
        item.source,
        json.dumps(item.provenance, ensure_ascii=False),
        item.priority.value if item.priority else None,
        item.sort_order,
        item.indent_level,
        item.recurrence_rule,
        item.recurrence_parent_id,
        item.conversation_id,
        1 if item.exported else 0,
        _epoch(item.export_date),
        item.export_platform,
        item.apple_reminder_id,
        now if item.completed else None,
        now,
        now,
        key,
    )
    await env.APP_DB.batch(
        [
            insert,
            vector_outbox_statement(
                env,
                uid=uid,
                source_kind="action_item",
                source_id=item_id,
                desired_version=now,
                operation="upsert",
            ),
        ]
    )
    row = await _first_item(env, uid, item_id)
    if row is None:
        raise RuntimeError("created action item could not be loaded")
    await publish_vector_projection(env, uid=uid, source_kind="action_item", source_id=item_id)
    return row


@router.post("/v1/action-items")
async def create_action_item(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        item = ActionItemCreate.model_validate(await _bounded_json(request))
        row = await _insert_item(request.scope["env"], str(context["uid"]), item)
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid action item"}, status_code=400)
    except Exception:
        return JSONResponse({"error": "action item unavailable"}, status_code=503)
    return _response(row)


@router.get("/v1/action-items")
async def list_action_items(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    query = _row_query(request, str(context["uid"]))
    if isinstance(query, JSONResponse):
        return query
    sql, params = query
    try:
        result = await request.scope["env"].APP_DB.prepare(sql).bind(*params).all()
    except Exception:
        return JSONResponse({"error": "action items unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    rows = rows if isinstance(rows, list) else []
    limit = _query_int(request, "limit", 50, 1, MAX_LIST_LIMIT) or 50
    return {
        "action_items": [_response(row) for row in rows[:limit] if isinstance(row, dict)],
        "has_more": len(rows) > limit,
    }


@router.get("/v1/action-items/pending-sync")
async def get_pending_sync_items(request: Request):
    """Return the two D1 projections consumed by Apple Reminders sync."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        pending = (
            await env.APP_DB.prepare(
                "SELECT id, description, status, completed, goal_id, workstream_id, owner, due_at, due_confidence, "
                "source, provenance_json, priority, sort_order, indent_level, recurrence_rule, recurrence_parent_id, "
                "created_at, updated_at, completed_at, superseded_by, conversation_id, is_locked, exported, export_date, "
                "export_platform, apple_reminder_id FROM cf_action_items "
                "WHERE uid = ? AND sync_requested = 1 AND exported = 0 AND deleted = 0 "
                "ORDER BY updated_at DESC LIMIT 50"
            )
            .bind(uid)
            .all()
        )
        synced = (
            await env.APP_DB.prepare(
                "SELECT id, description, status, completed, goal_id, workstream_id, owner, due_at, due_confidence, "
                "source, provenance_json, priority, sort_order, indent_level, recurrence_rule, recurrence_parent_id, "
                "created_at, updated_at, completed_at, superseded_by, conversation_id, is_locked, exported, export_date, "
                "export_platform, apple_reminder_id FROM cf_action_items "
                "WHERE uid = ? AND export_platform = 'apple_reminders' AND exported = 1 AND deleted = 0 "
                "ORDER BY updated_at DESC LIMIT 100"
            )
            .bind(uid)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "action items unavailable"}, status_code=503)
    pending_rows = pending.get("results", []) if isinstance(pending, dict) else []
    synced_rows = synced.get("results", []) if isinstance(synced, dict) else []
    return {
        "pending_export": [_response(row) for row in pending_rows if isinstance(row, dict)],
        "synced_items": [_response(row) for row in synced_rows if isinstance(row, dict)],
    }


@router.get("/v1/action-items/ids")
async def list_action_item_ids(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    completed = _query_bool(request, "completed")
    if completed is _INVALID:
        return JSONResponse({"error": "invalid completed filter"}, status_code=400)
    sql = "SELECT id FROM cf_action_items WHERE uid = ? AND deleted = 0"
    params: list[object] = [str(context["uid"])]
    if completed is not None:
        sql += " AND completed = ?"
        params.append(1 if completed else 0)
    sql += " ORDER BY created_at DESC"
    try:
        result = await request.scope["env"].APP_DB.prepare(sql).bind(*params).all()
    except Exception:
        return JSONResponse({"error": "action items unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    ids = [str(row["id"]) for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)]
    body: dict[str, object] = {"ids": ids}
    if completed is not None:
        body["completed_scope"] = completed
    return body


@router.get("/v1/action-items/search")
async def search_action_items(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    query = _query_value(request, "query")
    limit = _query_int(request, "limit", 10, 1, 50)
    if not query:
        return JSONResponse({"detail": "query is required"}, status_code=422)
    if limit is None:
        return JSONResponse({"detail": "invalid limit"}, status_code=422)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        vector = await embed_query(env, query)
        matches = await query_vector_ids(
            env,
            "ACTION_ITEM_VECTORS",
            uid,
            vector,
            top_k=limit,
        )
        candidates = await hydrate_candidate_ids(
            env,
            uid,
            "action_item",
            matches,
            minimum_score=0.3,
        )
        rows = await _items_for_ids(env, uid, [source_id for source_id, _ in candidates])
    except ValueError as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    except Exception:
        return JSONResponse({"error": "action item search unavailable"}, status_code=503)
    return {"action_items": [_response(row) for row in rows[:limit]]}


async def _task_share(env: object, token: str, now: int) -> dict[str, object] | None:
    row = (
        await env.APP_DB.prepare(
            "SELECT token, sender_uid, sender_name, expires_at FROM cf_task_shares "
            "WHERE token = ? AND expires_at > ?"
        )
        .bind(token, now)
        .first()
    )
    return row if isinstance(row, dict) else None


async def _shared_task_rows(env: object, token: str, now: int) -> list[dict[str, object]]:
    result = (
        await env.APP_DB.prepare(
            "SELECT share.sender_uid, share.sender_name, item.id, item.description, item.due_at "
            "FROM cf_task_shares AS share "
            "JOIN cf_task_share_items AS shared_item ON shared_item.token = share.token "
            "JOIN cf_action_items AS item "
            "ON item.uid = share.sender_uid AND item.id = shared_item.action_item_id "
            "WHERE share.token = ? AND share.expires_at > ? AND item.deleted = 0 AND item.is_locked = 0 "
            "ORDER BY shared_item.ordinal ASC"
        )
        .bind(token, now)
        .all()
    )
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [row for row in rows if isinstance(row, dict)]


@router.post("/v1/action-items/share")
async def share_action_items(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = ShareTasksRequest.model_validate(await _bounded_json(request))
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid share request"}, status_code=400)
    if len(set(payload.task_ids)) != len(payload.task_ids) or any(
        not item_id or len(item_id) > MAX_ID_LENGTH for item_id in payload.task_ids
    ):
        return JSONResponse({"error": "invalid action item ids"}, status_code=400)

    uid = str(context["uid"])
    env = request.scope["env"]
    placeholders = ", ".join("?" for _ in payload.task_ids)
    try:
        result = (
            await env.APP_DB.prepare(
                f"SELECT id, is_locked FROM cf_action_items WHERE uid = ? AND deleted = 0 AND id IN ({placeholders})"
            )
            .bind(uid, *payload.task_ids)
            .all()
        )
        rows = result.get("results", []) if isinstance(result, dict) else []
        by_id = {str(row["id"]): row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}
        missing = next((item_id for item_id in payload.task_ids if item_id not in by_id), None)
        if missing is not None:
            return JSONResponse({"error": f"action item {missing} not found"}, status_code=404)
        if any(_bool(by_id[item_id].get("is_locked")) for item_id in payload.task_ids):
            return JSONResponse({"error": "cannot share locked action items"}, status_code=402)

        now = int(time.time())
        token = uuid.uuid4().hex
        sender_name = str(context.get("displayName") or "Omi user").strip()[:120] or "Omi user"
        statements = [
            env.APP_DB.prepare(
                "INSERT INTO cf_task_shares (token, sender_uid, sender_name, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?)"
            ).bind(token, uid, sender_name, now + TASK_SHARE_TTL_SECONDS, now)
        ]
        statements.extend(
            env.APP_DB.prepare(
                "INSERT INTO cf_task_share_items (token, ordinal, action_item_id) VALUES (?, ?, ?)"
            ).bind(token, ordinal, item_id)
            for ordinal, item_id in enumerate(payload.task_ids)
        )
        await env.APP_DB.batch(statements)
    except Exception:
        return JSONResponse({"error": "task sharing unavailable"}, status_code=503)
    return {"url": f"{TASK_SHARE_BASE_URL}/{token}", "token": token}


@router.get("/v1/action-items/shared/{token}")
async def get_shared_action_items(request: Request, token: str):
    if not token or len(token) > MAX_SHARE_TOKEN_LENGTH:
        return JSONResponse({"error": "share link expired or not found"}, status_code=404)
    try:
        now = int(time.time())
        share = await _task_share(request.scope["env"], token, now)
        if share is None:
            return JSONResponse({"error": "share link expired or not found"}, status_code=404)
        rows = await _shared_task_rows(request.scope["env"], token, now)
    except Exception:
        return JSONResponse({"error": "task sharing unavailable"}, status_code=503)
    return {
        "sender_name": str(share.get("sender_name") or "Omi user"),
        "tasks": [
            {"description": str(row.get("description") or ""), "due_at": _iso(row.get("due_at"))} for row in rows
        ],
        "count": len(rows),
    }


@router.post("/v1/action-items/accept")
async def accept_shared_action_items(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = AcceptSharedTasksRequest.model_validate(await _bounded_json(request))
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid share token"}, status_code=400)

    env = request.scope["env"]
    uid = str(context["uid"])
    now = int(time.time())
    try:
        share = await _task_share(env, payload.token, now)
        if share is None:
            return JSONResponse({"error": "share link expired or not found"}, status_code=404)
        sender_uid = str(share["sender_uid"])
        if sender_uid == uid:
            return JSONResponse({"error": "cannot accept your own shared tasks"}, status_code=400)
        eligible = await _shared_task_rows(env, payload.token, now)
        if not eligible:
            return JSONResponse(
                {"error": "all shared tasks are locked; a paid plan is required"},
                status_code=402,
            )

        nonce = uuid.uuid4().hex
        copies = [(row, uuid.uuid4().hex) for row in eligible]
        statements = [
            env.APP_DB.prepare(
                "INSERT OR IGNORE INTO cf_task_share_acceptances "
                "(token, recipient_uid, acceptance_nonce, accepted_at) "
                "SELECT token, ?, ?, ? FROM cf_task_shares WHERE token = ? AND expires_at > ?"
            ).bind(uid, nonce, now, payload.token, now)
        ]
        for row, item_id in copies:
            original_id = str(row["id"])
            provenance = json.dumps(
                [
                    {
                        "kind": "shared",
                        "token": payload.token,
                        "sender_uid": sender_uid,
                        "sender_name": str(share.get("sender_name") or "Omi user"),
                        "original_task_id": original_id,
                    }
                ],
                ensure_ascii=False,
            )
            idempotency_key = hashlib.sha256(
                f"task-share:{payload.token}:{uid}:{original_id}".encode("utf-8")
            ).hexdigest()
            statements.append(
                env.APP_DB.prepare(
                    "INSERT INTO cf_action_items "
                    "(uid, id, description, status, completed, owner, due_at, source, provenance_json, sort_order, "
                    "indent_level, is_locked, exported, created_at, updated_at, idempotency_key, sync_requested, deleted) "
                    "SELECT ?, ?, source.description, 'active', 0, 'user', source.due_at, 'shared', ?, 0, 0, 0, 0, "
                    "?, ?, ?, 0, 0 FROM cf_action_items AS source "
                    "WHERE source.uid = ? AND source.id = ? AND source.deleted = 0 AND source.is_locked = 0 "
                    "AND EXISTS (SELECT 1 FROM cf_task_share_acceptances "
                    "WHERE token = ? AND recipient_uid = ? AND acceptance_nonce = ?)"
                ).bind(
                    uid,
                    item_id,
                    provenance,
                    now,
                    now,
                    idempotency_key,
                    sender_uid,
                    original_id,
                    payload.token,
                    uid,
                    nonce,
                )
            )
        await env.APP_DB.batch(statements)

        acceptance = (
            await env.APP_DB.prepare(
                "SELECT acceptance_nonce FROM cf_task_share_acceptances WHERE token = ? AND recipient_uid = ?"
            )
            .bind(payload.token, uid)
            .first()
        )
        if not isinstance(acceptance, dict):
            return JSONResponse({"error": "share link expired or not found"}, status_code=404)
        if acceptance.get("acceptance_nonce") != nonce:
            return JSONResponse({"error": "you have already accepted this share"}, status_code=409)

        expected_ids = [item_id for _, item_id in copies]
        placeholders = ", ".join("?" for _ in expected_ids)
        result = (
            await env.APP_DB.prepare(
                f"SELECT id FROM cf_action_items WHERE uid = ? AND deleted = 0 AND id IN ({placeholders})"
            )
            .bind(uid, *expected_ids)
            .all()
        )
        created_rows = result.get("results", []) if isinstance(result, dict) else []
        created_set = {
            str(row["id"]) for row in created_rows if isinstance(row, dict) and isinstance(row.get("id"), str)
        }
        created = [item_id for item_id in expected_ids if item_id in created_set]
        if not created:
            await env.APP_DB.prepare(
                "DELETE FROM cf_task_share_acceptances "
                "WHERE token = ? AND recipient_uid = ? AND acceptance_nonce = ?"
            ).bind(payload.token, uid, nonce).run()
            return JSONResponse({"error": "shared tasks are no longer available"}, status_code=402)
    except Exception:
        return JSONResponse({"error": "task sharing unavailable"}, status_code=503)
    return {"created": created, "count": len(created)}


@router.get("/v1/action-items/{action_item_id}")
async def get_action_item(request: Request, action_item_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not action_item_id or len(action_item_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid action item id"}, status_code=400)
    row = await _first_item(request.scope["env"], str(context["uid"]), action_item_id)
    return _response(row) if row else JSONResponse({"error": "action item not found"}, status_code=404)


@router.get("/v1/conversations/{conversation_id}/action-items")
async def get_conversation_action_items(request: Request, conversation_id: str):
    """Read standalone action-item projections linked to one conversation."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        conversation = await _first_conversation(env, uid, conversation_id)
        if conversation is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        if _bool(conversation.get("is_locked")):
            return JSONResponse({"error": "paid plan required"}, status_code=402)
        result = (
            await env.APP_DB.prepare(
                "SELECT id, description, status, completed, goal_id, workstream_id, owner, due_at, due_confidence, "
                "source, provenance_json, priority, sort_order, indent_level, recurrence_rule, recurrence_parent_id, "
                "created_at, updated_at, completed_at, superseded_by, conversation_id, is_locked, exported, export_date, "
                "export_platform, apple_reminder_id FROM cf_action_items "
                "WHERE uid = ? AND conversation_id = ? AND deleted = 0 "
                "ORDER BY completed ASC, created_at ASC LIMIT 500"
            )
            .bind(uid, conversation_id)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "conversation action items unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return {
        "action_items": [_response(row) for row in rows if isinstance(row, dict)],
        "conversation_id": conversation_id,
    }


@router.get("/v1/conversations/{conversation_id}/action-items/count")
async def get_conversation_action_items_count(request: Request, conversation_id: str):
    """Return standalone action-item counts for a conversation projection."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        conversation = await _first_conversation(env, uid, conversation_id)
        if conversation is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        if _bool(conversation.get("is_locked")):
            return JSONResponse({"error": "paid plan required"}, status_code=402)
        row = (
            await env.APP_DB.prepare(
                "SELECT COUNT(*) AS total, COALESCE(SUM(completed), 0) AS completed "
                "FROM cf_action_items WHERE uid = ? AND conversation_id = ? AND deleted = 0"
            )
            .bind(uid, conversation_id)
            .first()
        )
    except Exception:
        return JSONResponse({"error": "conversation action items unavailable"}, status_code=503)
    total = int(row.get("total") or 0) if isinstance(row, dict) else 0
    completed = int(row.get("completed") or 0) if isinstance(row, dict) else 0
    return {"total": total, "completed": completed, "incomplete": max(total - completed, 0)}


def _update_values(update: ActionItemUpdate) -> dict[str, object]:
    values = update.model_dump(exclude_unset=True)
    values.pop("clear_due_at", None)
    if update.clear_due_at:
        values["due_at"] = None
    if "due_at" in values:
        values["due_at"] = _epoch(update.due_at) if update.due_at is not None and not update.clear_due_at else None
    if "export_date" in values:
        values["export_date"] = _epoch(update.export_date)
    if "provenance" in values:
        values["provenance_json"] = json.dumps(values.pop("provenance"), ensure_ascii=False)
    for name in ("status", "owner", "priority"):
        value = values.get(name)
        if isinstance(value, Enum):
            values[name] = value.value
    return values


async def _apply_update(env: object, uid: str, item_id: str, update: ActionItemUpdate) -> dict[str, object] | None:
    existing = await _first_item(env, uid, item_id)
    if existing is None:
        return None
    values = _update_values(update)
    if "completed" in values:
        values["completed"] = 1 if values["completed"] else 0
        values["status"] = TaskStatus.completed.value if values["completed"] else TaskStatus.active.value
        values["completed_at"] = int(time.time()) if values["completed"] else None
    elif "status" in values:
        values["completed"] = 1 if values["status"] == TaskStatus.completed.value else 0
        values["completed_at"] = int(time.time()) if values["completed"] else None
    if not values:
        return existing
    values["updated_at"] = max(
        int(time.time()),
        int(existing.get("updated_at") or existing.get("created_at") or 0) + 1,
    )
    allowed = {
        "description",
        "status",
        "completed",
        "goal_id",
        "workstream_id",
        "owner",
        "due_at",
        "due_confidence",
        "source",
        "provenance_json",
        "priority",
        "sort_order",
        "indent_level",
        "recurrence_rule",
        "recurrence_parent_id",
        "superseded_by",
        "exported",
        "export_date",
        "export_platform",
        "apple_reminder_id",
        "completed_at",
        "updated_at",
    }
    values = {key: value for key, value in values.items() if key in allowed}
    if "exported" in values:
        values["exported"] = 1 if values["exported"] else 0
    assignments = ", ".join(f"{key} = ?" for key in values)
    desired_version = int(values["updated_at"])
    await env.APP_DB.batch(
        [
            env.APP_DB.prepare(
                f"UPDATE cf_action_items SET {assignments} WHERE uid = ? AND id = ? AND deleted = 0"
            ).bind(*values.values(), uid, item_id),
            vector_outbox_statement(
                env,
                uid=uid,
                source_kind="action_item",
                source_id=item_id,
                desired_version=desired_version,
                operation="upsert",
            ),
        ]
    )
    row = await _first_item(env, uid, item_id)
    await publish_vector_projection(env, uid=uid, source_kind="action_item", source_id=item_id)
    return row


async def _delete_item(
    env: object,
    uid: str,
    item_id: str,
    existing: dict[str, object] | None = None,
) -> bool:
    row = existing or await _first_item(env, uid, item_id)
    if row is None:
        return False
    desired_version = max(
        int(time.time()),
        int(row.get("updated_at") or row.get("created_at") or 0) + 1,
    )
    await env.APP_DB.batch(
        [
            env.APP_DB.prepare("DELETE FROM cf_action_items WHERE uid = ? AND id = ?").bind(uid, item_id),
            vector_outbox_statement(
                env,
                uid=uid,
                source_kind="action_item",
                source_id=item_id,
                desired_version=desired_version,
                operation="delete",
            ),
        ]
    )
    await publish_vector_projection(env, uid=uid, source_kind="action_item", source_id=item_id)
    return True


@router.patch("/v1/action-items/{action_item_id}")
async def update_action_item(request: Request, action_item_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        update = ActionItemUpdate.model_validate(await _bounded_json(request))
        row = await _apply_update(request.scope["env"], str(context["uid"]), action_item_id, update)
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid action item update"}, status_code=400)
    except Exception:
        return JSONResponse({"error": "action item unavailable"}, status_code=503)
    return _response(row) if row else JSONResponse({"error": "action item not found"}, status_code=404)


@router.patch("/v1/action-items/{action_item_id}/completed")
async def toggle_action_item_completion(request: Request, action_item_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    value = _query_bool(request, "completed")
    if value is None or value is _INVALID:
        return JSONResponse({"error": "completed is required"}, status_code=400)
    try:
        update = ActionItemUpdate.model_validate({"completed": value})
        row = await _apply_update(request.scope["env"], str(context["uid"]), action_item_id, update)
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid completion value"}, status_code=400)
    return _response(row) if row else JSONResponse({"error": "action item not found"}, status_code=404)


@router.delete("/v1/action-items/{action_item_id}")
async def delete_action_item(request: Request, action_item_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    row = await _first_item(env, str(context["uid"]), action_item_id)
    if row is None:
        return JSONResponse({"error": "action item not found"}, status_code=404)
    await _delete_item(env, str(context["uid"]), action_item_id, row)
    return Response(status_code=204)


@batch_router.patch("/v1/action-items/batch")
async def batch_update_action_items(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await _bounded_json(request)
        raw_items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(raw_items, list) or len(raw_items) > MAX_BATCH_ITEMS:
            raise ValueError("invalid batch")
        updated_ids: list[str] = []
        missing_ids: list[str] = []
        for raw in raw_items:
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                raise ValueError("invalid batch item")
            item_id = raw["id"]
            update = ActionItemUpdate.model_validate(
                {key: raw[key] for key in ("sort_order", "indent_level") if key in raw}
            )
            row = await _apply_update(request.scope["env"], str(context["uid"]), item_id, update)
            (updated_ids if row else missing_ids).append(item_id)
        return {
            "status": "ok",
            "updated_count": len(updated_ids),
            "updated_ids": updated_ids,
            "missing_ids": missing_ids,
            "noop_ids": [],
        }
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid action item batch"}, status_code=400)


@batch_router.patch("/v1/action-items/sync-batch")
async def sync_batch_update(request: Request):
    """Apply the bounded Apple Reminders reconciliation payload in D1."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await _bounded_json(request)
        payload = SyncBatchRequest.model_validate(body)
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid action item sync batch"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    updated_ids: list[str] = []
    missing_ids: list[str] = []
    locked_ids: list[str] = []
    noop_ids: list[str] = []
    try:
        for item in payload.items:
            existing = await _first_item(env, uid, item.id)
            if existing is None:
                missing_ids.append(item.id)
                continue
            if _bool(existing.get("is_locked")):
                locked_ids.append(item.id)
                continue
            fields = {name: getattr(item, name) for name in item.model_fields_set if name != "id"}
            if not fields:
                noop_ids.append(item.id)
                continue
            update = ActionItemUpdate.model_validate(fields)
            row = await _apply_update(env, uid, item.id, update)
            if row is None:
                missing_ids.append(item.id)
                continue
            if fields.get("exported") is True:
                await env.APP_DB.prepare(
                    "UPDATE cf_action_items SET sync_requested = 0, updated_at = ? "
                    "WHERE uid = ? AND id = ? AND deleted = 0"
                ).bind(int(time.time()), uid, item.id).run()
                row = await _first_item(env, uid, item.id)
            updated_ids.append(item.id)
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid action item sync batch"}, status_code=400)
    except Exception:
        return JSONResponse({"error": "action items unavailable"}, status_code=503)
    return {
        "status": "ok",
        "updated_count": len(updated_ids),
        "updated_ids": updated_ids,
        "missing_ids": missing_ids,
        "locked_ids": locked_ids,
        "noop_ids": noop_ids,
    }


@batch_router.post("/v1/action-items/batch")
async def batch_create_action_items(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await _bounded_json(request)
        if not isinstance(body, list) or len(body) > 50:
            raise ValueError("invalid batch")
        items = [ActionItemCreate.model_validate(raw) for raw in body]
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid action item batch"}, status_code=400)
    try:
        rows = [await _insert_item(request.scope["env"], str(context["uid"]), item) for item in items]
    except Exception:
        return JSONResponse({"error": "action items unavailable"}, status_code=503)
    return {"action_items": [_response(row) for row in rows], "created_count": len(rows)}


@batch_router.post("/v1/action-items/batch-delete")
async def batch_delete_action_items(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await _bounded_json(request)
        raw_ids = body.get("ids") if isinstance(body, dict) else None
        if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > MAX_BATCH_ITEMS:
            raise ValueError("invalid ids")
        ids = [item for item in raw_ids if isinstance(item, str) and 0 < len(item) <= MAX_ID_LENGTH]
        if len(ids) != len(raw_ids):
            raise ValueError("invalid id")
    except (ValueError, TypeError, json.JSONDecodeError):
        return JSONResponse({"error": "invalid action item ids"}, status_code=400)
    uid = str(context["uid"])
    deleted_ids: list[str] = []
    for item_id in ids:
        if not await _delete_item(request.scope["env"], uid, item_id):
            continue
        deleted_ids.append(item_id)
    return {"status": "Ok", "deleted_count": len(deleted_ids), "deleted_ids": deleted_ids}
