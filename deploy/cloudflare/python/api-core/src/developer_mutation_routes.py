"""Developer API mutations backed by the canonical Cloudflare D1 projections."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import time
from typing import Literal
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, model_validator

from account_routes import usage_source_statement
from action_item_routes import (
    ActionItemUpdate,
    _apply_update as apply_action_item_update,
    _first_item as first_action_item,
)
from conversation_routes import (
    _first_conversation as first_conversation,
    _share_index_statement as conversation_share_index_statement,
)
from developer_routes import (
    MAX_ID_LENGTH,
    _authenticate,
    _bool,
    _developer_action_item,
    _developer_conversation,
    _developer_goal,
    _developer_memory,
    _folder_names,
)
from goal_routes import (
    GoalCreate,
    GoalType,
    GoalUpdate,
    _first_goal as first_goal,
    _metric as goal_metric,
    _update_values as goal_update_values,
)
from mcp_routes import _memory_category, _memory_score
from memory_routes import _first_active as first_active_memory
from vector_search import publish_vector_projection, vector_outbox_statement

router = APIRouter()

MAX_REQUEST_BYTES = 256_000
MAX_BATCH_REQUEST_BYTES = 512_000
MAX_MEMORY_CONTENT_LENGTH = 500
MAX_ACTION_DESCRIPTION_LENGTH = 500
MAX_TAGS = 100
MAX_TAG_LENGTH = 256
MAX_MEMORY_BATCH = 25
MAX_ACTION_BATCH = 50


class DeveloperMemoryCreate(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    content: str = Field(min_length=1, max_length=MAX_MEMORY_CONTENT_LENGTH)
    category: Literal["interesting", "system", "manual"] | None = None
    visibility: Literal["public", "private"] = "private"
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)

    @model_validator(mode="after")
    def validate_tags(self) -> "DeveloperMemoryCreate":
        if any(not tag or len(tag) > MAX_TAG_LENGTH for tag in self.tags):
            raise ValueError("invalid memory tag")
        return self


class DeveloperMemoryBatch(BaseModel):
    model_config = {"extra": "ignore"}

    memories: list[DeveloperMemoryCreate] = Field(max_length=MAX_MEMORY_BATCH)


class DeveloperMemoryUpdate(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    content: str | None = Field(default=None, min_length=1, max_length=MAX_MEMORY_CONTENT_LENGTH)
    category: Literal["interesting", "system", "manual"] | None = None
    visibility: Literal["public", "private"] | None = None
    tags: list[str] | None = Field(default=None, max_length=MAX_TAGS)

    @model_validator(mode="after")
    def validate_update(self) -> "DeveloperMemoryUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one memory field is required")
        if self.tags is not None and any(not tag or len(tag) > MAX_TAG_LENGTH for tag in self.tags):
            raise ValueError("invalid memory tag")
        return self


class DeveloperActionItemCreate(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    description: str = Field(min_length=1, max_length=MAX_ACTION_DESCRIPTION_LENGTH)
    completed: bool = False
    due_at: datetime | None = None


class DeveloperActionItemBatch(BaseModel):
    model_config = {"extra": "ignore"}

    action_items: list[DeveloperActionItemCreate] = Field(max_length=MAX_ACTION_BATCH)


class DeveloperActionItemUpdate(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    description: str | None = Field(default=None, min_length=1, max_length=MAX_ACTION_DESCRIPTION_LENGTH)
    completed: bool | None = None
    due_at: datetime | None = None

    @model_validator(mode="after")
    def validate_update(self) -> "DeveloperActionItemUpdate":
        if not any(value is not None for value in (self.description, self.completed, self.due_at)):
            raise ValueError("at least one action-item field is required")
        return self


class DeveloperConversationUpdate(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    title: str | None = Field(default=None, min_length=1, max_length=500)
    discarded: bool | None = None

    @model_validator(mode="after")
    def validate_update(self) -> "DeveloperConversationUpdate":
        if self.title is None and self.discarded is None:
            raise ValueError("at least one conversation field is required")
        return self


class DeveloperGoalCreate(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    title: str = Field(min_length=1, max_length=500)
    desired_outcome: str | None = Field(default=None, max_length=2_000)
    why_it_matters: str | None = Field(default=None, max_length=2_000)
    success_criteria: list[str] = Field(default_factory=list, max_length=20)
    horizon_at: datetime | None = None
    goal_type: Literal["boolean", "scale", "numeric"] | None = None
    target_value: float | None = None
    current_value: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    unit: str | None = Field(default=None, max_length=64)


class DeveloperGoalUpdate(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    title: str | None = Field(default=None, min_length=1, max_length=500)
    desired_outcome: str | None = Field(default=None, max_length=2_000)
    why_it_matters: str | None = Field(default=None, max_length=2_000)
    success_criteria: list[str] | None = Field(default=None, max_length=20)
    horizon_at: datetime | None = None
    target_value: float | None = None
    current_value: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    unit: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_update(self) -> "DeveloperGoalUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one goal field is required")
        for field_name in ("title", "desired_outcome"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        if "success_criteria" in self.model_fields_set and self.success_criteria is None:
            raise ValueError("success_criteria cannot be null")
        for field_name in ("target_value", "current_value"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


async def _bounded_json(request: Request, maximum: int = MAX_REQUEST_BYTES) -> object:
    raw = await request.body()
    if len(raw) > maximum:
        raise ValueError("request body exceeds size limit")
    return json.loads(raw)


def _memory_row(
    *,
    uid: str,
    memory_id: str,
    payload: DeveloperMemoryCreate,
    category: str,
    now: int,
) -> dict[str, object]:
    return {
        "uid": uid,
        "id": memory_id,
        "content": payload.content,
        "category": category,
        "visibility": payload.visibility,
        "tags_json": json.dumps(payload.tags, ensure_ascii=False, separators=(",", ":")),
        "reviewed": 1,
        "user_review": 1,
        "manually_added": 1,
        "edited": 0,
        "scoring": _memory_score(category, now),
        "is_locked": 0,
        "memory_tier": "long_term",
        "valid_at": now,
        "created_at": now,
        "updated_at": now,
    }


def _memory_insert_statement(env: object, row: dict[str, object]):
    return env.APP_DB.prepare(
        "INSERT INTO cf_memories "
        "(uid, id, content, category, visibility, tags_json, reviewed, user_review, manually_added, edited, "
        "scoring, is_locked, memory_tier, valid_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, 1, 1, 0, ?, 0, 'long_term', ?, ?, ?)"
    ).bind(
        row["uid"],
        row["id"],
        row["content"],
        row["category"],
        row["visibility"],
        row["tags_json"],
        row["scoring"],
        row["valid_at"],
        row["created_at"],
        row["updated_at"],
    )


async def _publish_projection(env: object, uid: str, source_kind: str, source_id: str) -> None:
    await publish_vector_projection(env, uid=uid, source_kind=source_kind, source_id=source_id)


def _epoch(value: datetime | None) -> int | None:
    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return int(normalized.astimezone(timezone.utc).timestamp())


@router.post("/v1/dev/user/memories")
async def create_developer_memory(request: Request):
    principal, denial = await _authenticate(request, "memories:write")
    if denial is not None:
        return denial
    assert principal is not None
    try:
        payload = DeveloperMemoryCreate.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return JSONResponse({"detail": "Invalid memory"}, status_code=422)
    env = request.scope["env"]
    category = payload.category or await _memory_category(env, payload.content)
    now = int(time.time())
    memory_id = uuid.uuid4().hex
    row = _memory_row(uid=principal.uid, memory_id=memory_id, payload=payload, category=category, now=now)
    try:
        await env.APP_DB.batch(
            [
                _memory_insert_statement(env, row),
                usage_source_statement(
                    env,
                    uid=principal.uid,
                    source_kind="memory",
                    source_id=memory_id,
                    occurred_at=now,
                    memories_created=1,
                    updated_at=now,
                ),
                vector_outbox_statement(
                    env,
                    uid=principal.uid,
                    source_kind="memory",
                    source_id=memory_id,
                    desired_version=now,
                    operation="upsert",
                ),
            ]
        )
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    await _publish_projection(env, principal.uid, "memory", memory_id)
    return _developer_memory(row)


@router.post("/v1/dev/user/memories/batch")
async def create_developer_memories_batch(request: Request):
    principal, denial = await _authenticate(request, "memories:write")
    if denial is not None:
        return denial
    assert principal is not None
    try:
        batch = DeveloperMemoryBatch.model_validate(await _bounded_json(request, MAX_BATCH_REQUEST_BYTES))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return JSONResponse({"detail": "Invalid memory batch"}, status_code=422)
    if not batch.memories:
        return {"memories": [], "created_count": 0}
    env = request.scope["env"]
    now = int(time.time())
    rows: list[dict[str, object]] = []
    statements: list[object] = []
    for payload in batch.memories:
        category = payload.category or await _memory_category(env, payload.content)
        memory_id = uuid.uuid4().hex
        row = _memory_row(uid=principal.uid, memory_id=memory_id, payload=payload, category=category, now=now)
        rows.append(row)
        statements.extend(
            [
                _memory_insert_statement(env, row),
                usage_source_statement(
                    env,
                    uid=principal.uid,
                    source_kind="memory",
                    source_id=memory_id,
                    occurred_at=now,
                    memories_created=1,
                    updated_at=now,
                ),
                vector_outbox_statement(
                    env,
                    uid=principal.uid,
                    source_kind="memory",
                    source_id=memory_id,
                    desired_version=now,
                    operation="upsert",
                ),
            ]
        )
    try:
        await env.APP_DB.batch(statements)
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    for row in rows:
        await _publish_projection(env, principal.uid, "memory", str(row["id"]))
    return {"memories": [_developer_memory(row) for row in rows], "created_count": len(rows)}


@router.patch("/v1/dev/user/memories/{memory_id}")
async def update_developer_memory(request: Request, memory_id: str):
    principal, denial = await _authenticate(request, "memories:write")
    if denial is not None:
        return denial
    assert principal is not None
    if not memory_id or len(memory_id) > MAX_ID_LENGTH:
        return JSONResponse({"detail": "Memory not found"}, status_code=404)
    try:
        update = DeveloperMemoryUpdate.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return JSONResponse({"detail": "Invalid memory update"}, status_code=422)
    env = request.scope["env"]
    try:
        existing = await first_active_memory(env, principal.uid, memory_id)
        if existing is None:
            return JSONResponse({"detail": "Memory not found"}, status_code=404)
        if _bool(existing.get("is_locked")):
            return JSONResponse(
                {"detail": "A paid plan is required to access this memory."},
                status_code=402,
            )
        values: dict[str, object] = {}
        if update.content is not None:
            values["content"] = update.content
            values["edited"] = 1
        if update.visibility is not None:
            values["visibility"] = update.visibility
        if update.tags is not None:
            values["tags_json"] = json.dumps(update.tags, ensure_ascii=False, separators=(",", ":"))
        if update.category is not None:
            values["category"] = update.category
        now = int(time.time())
        values["updated_at"] = now
        assignments = ", ".join(f"{key} = ?" for key in values)
        mutation = env.APP_DB.prepare(
            f"UPDATE cf_memories SET {assignments} "
            "WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
        ).bind(*values.values(), principal.uid, memory_id)
        projection = vector_outbox_statement(
            env,
            uid=principal.uid,
            source_kind="memory",
            source_id=memory_id,
            desired_version=now,
            operation="upsert",
        )
        await env.APP_DB.batch([mutation, projection])
        row = await first_active_memory(env, principal.uid, memory_id)
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    if row is None:
        return JSONResponse({"detail": "Memory not found"}, status_code=404)
    await _publish_projection(env, principal.uid, "memory", memory_id)
    return _developer_memory(row)


@router.delete("/v1/dev/user/memories/{memory_id}")
async def delete_developer_memory(request: Request, memory_id: str):
    principal, denial = await _authenticate(request, "memories:write")
    if denial is not None:
        return denial
    assert principal is not None
    if not memory_id or len(memory_id) > MAX_ID_LENGTH:
        return JSONResponse({"detail": "Memory not found"}, status_code=404)
    env = request.scope["env"]
    try:
        existing = await first_active_memory(env, principal.uid, memory_id)
        if existing is None:
            return JSONResponse({"detail": "Memory not found"}, status_code=404)
        if _bool(existing.get("is_locked")):
            return JSONResponse(
                {"detail": "A paid plan is required to access this memory."},
                status_code=402,
            )
        now = int(time.time())
        mutation = env.APP_DB.prepare(
            "UPDATE cf_memories SET deleted_at = ?, updated_at = ? "
            "WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
        ).bind(now, now, principal.uid, memory_id)
        projection = vector_outbox_statement(
            env,
            uid=principal.uid,
            source_kind="memory",
            source_id=memory_id,
            desired_version=now,
            operation="delete",
        )
        await env.APP_DB.batch([mutation, projection])
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    await _publish_projection(env, principal.uid, "memory", memory_id)
    return {"success": True}


def _action_item_row(
    *,
    uid: str,
    payload: DeveloperActionItemCreate,
    now: int,
) -> dict[str, object]:
    item_id = uuid.uuid4().hex
    return {
        "uid": uid,
        "id": item_id,
        "description": payload.description,
        "status": "completed" if payload.completed else "active",
        "completed": 1 if payload.completed else 0,
        "owner": "user",
        "due_at": _epoch(payload.due_at),
        "source": "developer_api",
        "provenance_json": "[]",
        "sort_order": 0,
        "indent_level": 0,
        "is_locked": 0,
        "exported": 0,
        "completed_at": now if payload.completed else None,
        "created_at": now,
        "updated_at": now,
        # Developer API creates are intentionally non-idempotent, matching the
        # legacy route. A unique audit key avoids inheriting the universal
        # action-item route's description-based deduplication contract.
        "idempotency_key": f"developer_api:{item_id}",
        "sync_requested": 0,
        "deleted": 0,
        "conversation_id": None,
    }


def _action_item_insert_statement(env: object, row: dict[str, object]):
    return env.APP_DB.prepare(
        "INSERT INTO cf_action_items "
        "(uid, id, description, status, completed, owner, due_at, source, provenance_json, sort_order, indent_level, "
        "is_locked, exported, completed_at, created_at, updated_at, idempotency_key, sync_requested, deleted) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    ).bind(
        row["uid"],
        row["id"],
        row["description"],
        row["status"],
        row["completed"],
        row["owner"],
        row["due_at"],
        row["source"],
        row["provenance_json"],
        row["sort_order"],
        row["indent_level"],
        row["is_locked"],
        row["exported"],
        row["completed_at"],
        row["created_at"],
        row["updated_at"],
        row["idempotency_key"],
        row["sync_requested"],
        row["deleted"],
    )


def _action_item_statements(env: object, row: dict[str, object]) -> list[object]:
    return [
        _action_item_insert_statement(env, row),
        vector_outbox_statement(
            env,
            uid=str(row["uid"]),
            source_kind="action_item",
            source_id=str(row["id"]),
            desired_version=int(row["updated_at"]),
            operation="upsert",
        ),
    ]


@router.post("/v1/dev/user/action-items")
async def create_developer_action_item(request: Request):
    principal, denial = await _authenticate(request, "action_items:write")
    if denial is not None:
        return denial
    assert principal is not None
    try:
        payload = DeveloperActionItemCreate.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return JSONResponse({"detail": "Invalid action item"}, status_code=422)
    env = request.scope["env"]
    row = _action_item_row(uid=principal.uid, payload=payload, now=int(time.time()))
    try:
        await env.APP_DB.batch(_action_item_statements(env, row))
    except Exception:
        return JSONResponse({"error": "action item unavailable"}, status_code=503)
    await _publish_projection(env, principal.uid, "action_item", str(row["id"]))
    return _developer_action_item(row)


@router.post("/v1/dev/user/action-items/batch")
async def create_developer_action_items_batch(request: Request):
    principal, denial = await _authenticate(request, "action_items:write")
    if denial is not None:
        return denial
    assert principal is not None
    try:
        batch = DeveloperActionItemBatch.model_validate(await _bounded_json(request, MAX_BATCH_REQUEST_BYTES))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return JSONResponse({"detail": "Invalid action item batch"}, status_code=422)
    env = request.scope["env"]
    now = int(time.time())
    rows = [_action_item_row(uid=principal.uid, payload=payload, now=now) for payload in batch.action_items]
    statements = [statement for row in rows for statement in _action_item_statements(env, row)]
    try:
        if statements:
            await env.APP_DB.batch(statements)
    except Exception:
        return JSONResponse({"error": "action items unavailable"}, status_code=503)
    for row in rows:
        await _publish_projection(env, principal.uid, "action_item", str(row["id"]))
    return {"action_items": [_developer_action_item(row) for row in rows], "created_count": len(rows)}


@router.patch("/v1/dev/user/action-items/{action_item_id}")
async def update_developer_action_item(request: Request, action_item_id: str):
    principal, denial = await _authenticate(request, "action_items:write")
    if denial is not None:
        return denial
    assert principal is not None
    if not action_item_id or len(action_item_id) > MAX_ID_LENGTH:
        return JSONResponse({"detail": "Action item not found"}, status_code=404)
    try:
        payload = DeveloperActionItemUpdate.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return JSONResponse({"detail": "Invalid action item update"}, status_code=422)
    env = request.scope["env"]
    try:
        existing = await first_action_item(env, principal.uid, action_item_id)
        if existing is None:
            return JSONResponse({"detail": "Action item not found"}, status_code=404)
        if _bool(existing.get("is_locked")):
            return JSONResponse(
                {"detail": "A paid plan is required to access this action item."},
                status_code=402,
            )
        values: dict[str, object] = {}
        if payload.description is not None:
            values["description"] = payload.description
        if payload.completed is not None:
            values["completed"] = payload.completed
        if payload.due_at is not None:
            values["due_at"] = payload.due_at
        row = await apply_action_item_update(env, principal.uid, action_item_id, ActionItemUpdate(**values))
        if row is None:
            return JSONResponse({"detail": "Action item not found"}, status_code=404)
        version = int(row.get("updated_at") or time.time())
        await vector_outbox_statement(
            env,
            uid=principal.uid,
            source_kind="action_item",
            source_id=action_item_id,
            desired_version=version,
            operation="upsert",
        ).run()
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"detail": "Invalid action item update"}, status_code=422)
    except Exception:
        return JSONResponse({"error": "action item unavailable"}, status_code=503)
    await _publish_projection(env, principal.uid, "action_item", action_item_id)
    return _developer_action_item(row)


@router.delete("/v1/dev/user/action-items/{action_item_id}")
async def delete_developer_action_item(request: Request, action_item_id: str):
    principal, denial = await _authenticate(request, "action_items:write")
    if denial is not None:
        return denial
    assert principal is not None
    if not action_item_id or len(action_item_id) > MAX_ID_LENGTH:
        return JSONResponse({"detail": "Action item not found"}, status_code=404)
    env = request.scope["env"]
    try:
        existing = await first_action_item(env, principal.uid, action_item_id)
        if existing is None:
            return JSONResponse({"detail": "Action item not found"}, status_code=404)
        if _bool(existing.get("is_locked")):
            return JSONResponse(
                {"detail": "A paid plan is required to access this action item."},
                status_code=402,
            )
        now = int(time.time())
        deletion = env.APP_DB.prepare("DELETE FROM cf_action_items WHERE uid = ? AND id = ?").bind(
            principal.uid, action_item_id
        )
        projection = vector_outbox_statement(
            env,
            uid=principal.uid,
            source_kind="action_item",
            source_id=action_item_id,
            desired_version=now,
            operation="delete",
        )
        await env.APP_DB.batch([deletion, projection])
    except Exception:
        return JSONResponse({"error": "action item unavailable"}, status_code=503)
    await _publish_projection(env, principal.uid, "action_item", action_item_id)
    return {"success": True}


async def _developer_conversation_response(env: object, uid: str, row: dict[str, object]) -> dict[str, object]:
    await _folder_names(env, uid, [row])
    return _developer_conversation(row, include_transcript=True)


@router.patch("/v1/dev/user/conversations/{conversation_id}")
async def update_developer_conversation(request: Request, conversation_id: str):
    principal, denial = await _authenticate(request, "conversations:write")
    if denial is not None:
        return denial
    assert principal is not None
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"detail": "Conversation not found"}, status_code=404)
    try:
        update = DeveloperConversationUpdate.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return JSONResponse({"detail": "Invalid conversation update"}, status_code=422)
    env = request.scope["env"]
    try:
        existing = await first_conversation(env, principal.uid, conversation_id)
        if existing is None:
            return JSONResponse({"detail": "Conversation not found"}, status_code=404)
        if _bool(existing.get("is_locked")):
            return JSONResponse(
                {"detail": "A paid plan is required to access this conversation."},
                status_code=402,
            )
        structured_raw = existing.get("structured_json")
        try:
            structured = json.loads(structured_raw) if isinstance(structured_raw, str) else {}
        except (TypeError, ValueError):
            structured = {}
        if not isinstance(structured, dict):
            structured = {}
        if update.title is not None:
            structured["title"] = update.title
        discarded = _bool(existing.get("discarded")) if update.discarded is None else update.discarded
        previous_version = int(existing.get("updated_at") or existing.get("created_at") or 0)
        now = max(int(time.time()), previous_version + 1)
        mutation = env.APP_DB.prepare(
            "UPDATE cf_conversations SET structured_json = ?, discarded = ?, updated_at = ? WHERE uid = ? AND id = ?"
        ).bind(
            json.dumps(structured, ensure_ascii=False, separators=(",", ":")),
            1 if discarded else 0,
            now,
            principal.uid,
            conversation_id,
        )
        share_index = conversation_share_index_statement(
            env,
            uid=principal.uid,
            conversation_id=conversation_id,
            visibility="private" if discarded else str(existing.get("visibility") or "private"),
            updated_at=now,
        )
        folder_counts = env.APP_DB.prepare(
            "UPDATE cf_folders SET conversation_count = ("
            "SELECT COUNT(*) FROM cf_conversations c "
            "WHERE c.uid = cf_folders.uid AND c.folder_id = cf_folders.id AND c.discarded = 0"
            ") WHERE uid = ?"
        ).bind(principal.uid)
        projection = vector_outbox_statement(
            env,
            uid=principal.uid,
            source_kind="conversation",
            source_id=conversation_id,
            desired_version=now,
            operation=("upsert" if str(existing.get("status") or "") == "completed" and not discarded else "delete"),
        )
        await env.APP_DB.batch([mutation, share_index, folder_counts, projection])
        row = await first_conversation(env, principal.uid, conversation_id)
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)
    if row is None:
        return JSONResponse({"detail": "Conversation not found"}, status_code=404)
    await _publish_projection(env, principal.uid, "conversation", conversation_id)
    return await _developer_conversation_response(env, principal.uid, row)


@router.delete("/v1/dev/user/conversations/{conversation_id}")
async def delete_developer_conversation(request: Request, conversation_id: str):
    principal, denial = await _authenticate(request, "conversations:write")
    if denial is not None:
        return denial
    assert principal is not None
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"detail": "Conversation not found"}, status_code=404)
    env = request.scope["env"]
    try:
        existing = await first_conversation(env, principal.uid, conversation_id)
        if existing is None:
            return JSONResponse({"detail": "Conversation not found"}, status_code=404)
        if _bool(existing.get("is_locked")):
            return JSONResponse(
                {"detail": "A paid plan is required to access this conversation."},
                status_code=402,
            )
        previous_version = int(existing.get("updated_at") or existing.get("created_at") or 0)
        now = max(int(time.time()), previous_version + 1)
        await env.APP_DB.batch(
            [
                env.APP_DB.prepare(
                    "DELETE FROM cf_shared_conversation_index WHERE conversation_id = ? AND uid = ?"
                ).bind(conversation_id, principal.uid),
                env.APP_DB.prepare("DELETE FROM cf_conversations WHERE uid = ? AND id = ?").bind(
                    principal.uid, conversation_id
                ),
                env.APP_DB.prepare(
                    "UPDATE cf_folders SET conversation_count = ("
                    "SELECT COUNT(*) FROM cf_conversations c "
                    "WHERE c.uid = cf_folders.uid AND c.folder_id = cf_folders.id AND c.discarded = 0"
                    ") WHERE uid = ?"
                ).bind(principal.uid),
                vector_outbox_statement(
                    env,
                    uid=principal.uid,
                    source_kind="conversation",
                    source_id=conversation_id,
                    desired_version=now,
                    operation="delete",
                ),
            ]
        )
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)
    await _publish_projection(env, principal.uid, "conversation", conversation_id)
    return {"success": True}


@router.post("/v1/dev/user/goals")
async def create_developer_goal(request: Request):
    principal, denial = await _authenticate(request, "goals:write")
    if denial is not None:
        return denial
    assert principal is not None
    try:
        payload = DeveloperGoalCreate.model_validate(await _bounded_json(request))
        goal = GoalCreate.model_validate(payload.model_dump(exclude_none=True))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return JSONResponse({"detail": "Invalid goal"}, status_code=422)
    env = request.scope["env"]
    now = int(time.time())
    goal_id = f"goal_{uuid.uuid4().hex[:12]}"
    metric = goal.metric.model_dump(mode="json") if goal.metric is not None else None
    try:
        await env.APP_DB.prepare(
            "INSERT INTO cf_goals "
            "(uid, id, title, desired_outcome, why_it_matters, success_criteria_json, horizon_at, status, focus_rank, "
            "metric_json, source, relationship_disposition, is_active, latest_progress_sequence, ended_at, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'background', NULL, ?, 'user', 'retain', 1, 0, "
            "NULL, ?, ?)"
        ).bind(
            principal.uid,
            goal_id,
            goal.title,
            goal.desired_outcome,
            goal.why_it_matters,
            json.dumps(goal.success_criteria, ensure_ascii=False),
            _epoch(goal.horizon_at),
            json.dumps(metric, ensure_ascii=False) if metric is not None else None,
            now,
            now,
        ).run()
        row = await first_goal(env, principal.uid, goal_id)
    except Exception:
        return JSONResponse({"error": "goals unavailable"}, status_code=503)
    return _developer_goal(row) if row else JSONResponse({"error": "goal unavailable"}, status_code=503)


@router.patch("/v1/dev/user/goals/{goal_id}/progress")
async def update_developer_goal_progress(request: Request, goal_id: str):
    principal, denial = await _authenticate(request, "goals:write")
    if denial is not None:
        return denial
    assert principal is not None
    if not goal_id or len(goal_id) > MAX_ID_LENGTH:
        return JSONResponse({"detail": "Goal not found"}, status_code=404)
    raw_value = request.query_params.get("current_value")
    try:
        current_value = float(raw_value) if raw_value not in {None, ""} else None
    except (TypeError, ValueError):
        current_value = None
    if current_value is None or not math.isfinite(current_value):
        return JSONResponse({"detail": "current_value is required"}, status_code=422)
    env = request.scope["env"]
    try:
        existing = await first_goal(env, principal.uid, goal_id)
        if existing is None:
            return JSONResponse({"detail": "Goal not found"}, status_code=404)
        metric = goal_metric(existing) or {
            "type": GoalType.numeric.value,
            "current": 0.0,
            "target": 0.0,
            "min": None,
            "max": None,
            "unit": None,
        }
        metric["current"] = current_value
        now = int(time.time())
        today = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        sequence = int(existing.get("latest_progress_sequence") or 0) + 1
        metric_json = json.dumps(metric, ensure_ascii=False)
        await env.APP_DB.batch(
            [
                env.APP_DB.prepare(
                    "INSERT INTO cf_goal_progress_events "
                    "(uid, event_id, goal_id, sequence, kind, summary, evidence_refs_json, metric_json, created_at) "
                    "VALUES (?, ?, ?, ?, 'metric_update', 'Metric updated', '[]', ?, ?)"
                ).bind(principal.uid, f"gpe_{uuid.uuid4().hex}", goal_id, sequence, metric_json, now),
                env.APP_DB.prepare(
                    "UPDATE cf_goals SET metric_json = ?, latest_progress_sequence = ?, updated_at = ? "
                    "WHERE uid = ? AND id = ?"
                ).bind(metric_json, sequence, now, principal.uid, goal_id),
                env.APP_DB.prepare(
                    "INSERT INTO cf_goal_progress_history (uid, goal_id, date, value, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(uid, goal_id, date) DO UPDATE SET "
                    "value = excluded.value, recorded_at = excluded.recorded_at"
                ).bind(principal.uid, goal_id, today, current_value, now),
            ]
        )
        row = await first_goal(env, principal.uid, goal_id)
    except Exception:
        return JSONResponse({"error": "goals unavailable"}, status_code=503)
    return _developer_goal(row) if row else JSONResponse({"detail": "Goal not found"}, status_code=404)


@router.patch("/v1/dev/user/goals/{goal_id}")
async def update_developer_goal(request: Request, goal_id: str):
    principal, denial = await _authenticate(request, "goals:write")
    if denial is not None:
        return denial
    assert principal is not None
    if not goal_id or len(goal_id) > MAX_ID_LENGTH:
        return JSONResponse({"detail": "Goal not found"}, status_code=404)
    try:
        payload = DeveloperGoalUpdate.model_validate(await _bounded_json(request))
        update = GoalUpdate.model_validate(payload.model_dump(exclude_unset=True))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return JSONResponse({"detail": "Invalid goal update"}, status_code=422)
    env = request.scope["env"]
    try:
        existing = await first_goal(env, principal.uid, goal_id)
        if existing is None:
            return JSONResponse({"detail": "Goal not found"}, status_code=404)
        values = goal_update_values(update, existing)
        values["updated_at"] = int(time.time())
        allowed = {
            "title",
            "desired_outcome",
            "why_it_matters",
            "success_criteria_json",
            "horizon_at",
            "metric_json",
            "updated_at",
        }
        values = {key: value for key, value in values.items() if key in allowed}
        assignments = ", ".join(f"{key} = ?" for key in values)
        await env.APP_DB.prepare(f"UPDATE cf_goals SET {assignments} WHERE uid = ? AND id = ?").bind(
            *values.values(), principal.uid, goal_id
        ).run()
        row = await first_goal(env, principal.uid, goal_id)
    except Exception:
        return JSONResponse({"error": "goals unavailable"}, status_code=503)
    return _developer_goal(row) if row else JSONResponse({"detail": "Goal not found"}, status_code=404)


@router.delete("/v1/dev/user/goals/{goal_id}")
async def delete_developer_goal(request: Request, goal_id: str):
    principal, denial = await _authenticate(request, "goals:write")
    if denial is not None:
        return denial
    assert principal is not None
    if not goal_id or len(goal_id) > MAX_ID_LENGTH:
        return JSONResponse({"detail": "Goal not found"}, status_code=404)
    env = request.scope["env"]
    try:
        existing = await first_goal(env, principal.uid, goal_id)
        if existing is None:
            return JSONResponse({"detail": "Goal not found"}, status_code=404)
        now = int(time.time())
        await env.APP_DB.prepare(
            "UPDATE cf_goals SET status = 'abandoned', focus_rank = NULL, is_active = 0, ended_at = ?, updated_at = ? "
            "WHERE uid = ? AND id = ?"
        ).bind(now, now, principal.uid, goal_id).run()
    except Exception:
        return JSONResponse({"error": "goals unavailable"}, status_code=503)
    return {"success": True}
