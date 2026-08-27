"""D1-backed workstream, journal, artifact, checkpoint, and intent routes.

The module keeps the canonical workflow contracts on the Worker side without
loading Firestore, Redis, a thread pool, or a local process. Every mutating
operation is uid scoped and records its result in a D1 receipt before the Edge
can treat the write as authoritative. Search/index refreshes and candidate
automation remain legacy-owned until their separate contracts move.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal, Union

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, model_validator

from action_item_routes import _response as _action_item_response
from goal_routes import EvidenceRef
from internal_auth import decode_context

router = APIRouter()

MAX_REQUEST_BYTES = 100_000
MAX_ID_LENGTH = 256
MAX_IDEMPOTENCY_KEY_LENGTH = 256
MAX_LIST_LIMIT = 500


class WorkstreamStatus(str, Enum):
    open = "open"
    paused = "paused"
    completed = "completed"
    archived = "archived"


class WorkstreamEventKind(str, Enum):
    user_note = "user_note"
    conversation = "conversation"
    message = "message"
    screen_observation = "screen_observation"
    task_change = "task_change"
    decision = "decision"
    agent_update = "agent_update"
    artifact_version = "artifact_version"
    external_update = "external_update"
    system = "system"


class WorkstreamSensitivity(str, Enum):
    normal = "normal"
    sensitive = "sensitive"
    restricted = "restricted"


class WorkstreamUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    title: str | None = Field(default=None, min_length=1, max_length=256)
    objective: str | None = Field(default=None, min_length=1, max_length=2048)
    status: WorkstreamStatus | None = None
    current_state_summary: str | None = Field(default=None, max_length=4000)
    next_review_at: datetime | None = None

    @model_validator(mode="after")
    def validate_patch(self) -> "WorkstreamUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one workstream field is required")
        for field_name in ("title", "objective", "status", "current_state_summary"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        for field_name in ("title", "objective", "current_state_summary"):
            value = getattr(self, field_name)
            if isinstance(value, str):
                setattr(self, field_name, value.strip())
                if field_name in {"title", "objective"} and not getattr(self, field_name):
                    raise ValueError(f"{field_name} cannot be blank")
        return self


class WorkstreamEventCreate(BaseModel):
    model_config = {"extra": "forbid"}

    kind: WorkstreamEventKind
    summary: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=50)
    sensitivity: WorkstreamSensitivity = WorkstreamSensitivity.normal

    @model_validator(mode="after")
    def normalize_summary(self) -> "WorkstreamEventCreate":
        self.summary = self.summary.strip()
        if not self.summary:
            raise ValueError("summary cannot be blank")
        return self


class ArtifactStatus(str, Enum):
    draft = "draft"
    awaiting_review = "awaiting_review"
    approved = "approved"
    delivered = "delivered"
    superseded = "superseded"


class ArtifactDescriptorCreate(BaseModel):
    model_config = {"extra": "forbid"}

    logical_key: str = Field(min_length=1, max_length=256)
    version: int = Field(ge=1)
    supersedes_artifact_id: str | None = Field(default=None, min_length=1, max_length=256)
    kind: str = Field(min_length=1, max_length=64)
    uri: str = Field(min_length=1, max_length=2048)
    content_hash: str = Field(min_length=16, max_length=128)
    source_run_id: str | None = Field(default=None, min_length=1, max_length=256)
    evidence_event_ids: list[str] = Field(default_factory=list, max_length=100)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=50)


class ArtifactStatusTransitionRequest(BaseModel):
    model_config = {"extra": "forbid"}

    status: ArtifactStatus


class ContinuationCheckpointUpsert(BaseModel):
    model_config = {"extra": "forbid"}

    runtime_id: str = Field(min_length=1, max_length=256)
    last_event_sequence: int = Field(ge=0)
    context_summary: str = Field(max_length=4000)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=50)


class TaskOriginWorkIntent(BaseModel):
    model_config = {"extra": "forbid"}

    origin: Literal["task"] = "task"
    task_id: str = Field(min_length=1, max_length=256)
    title: str | None = Field(default=None, max_length=256)
    objective: str | None = Field(default=None, max_length=2048)


class GoalOriginWorkIntent(BaseModel):
    model_config = {"extra": "forbid"}

    origin: Literal["goal"] = "goal"
    goal_id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=256)
    objective: str = Field(min_length=1, max_length=2048)
    anchor_task_description: str = Field(min_length=1, max_length=2000)


WorkIntent = Annotated[Union[TaskOriginWorkIntent, GoalOriginWorkIntent], Field(discriminator="origin")]
_WORK_INTENT_ADAPTER = TypeAdapter(WorkIntent)


_WORKSTREAM_SELECT = (
    "SELECT id, goal_id, title, objective, status, current_state_summary, next_review_at, "
    "last_meaningful_progress_at, latest_event_sequence, account_generation, created_at, updated_at "
    "FROM cf_workstreams "
)
_TASK_SELECT = (
    "SELECT id, description, status, completed, goal_id, workstream_id, owner, due_at, due_confidence, "
    "source, provenance_json, priority, sort_order, indent_level, recurrence_rule, recurrence_parent_id, "
    "created_at, updated_at, completed_at, superseded_by, conversation_id, is_locked, exported, export_date, "
    "export_platform, apple_reminder_id FROM cf_action_items "
)
_EVENT_SELECT = (
    "SELECT event_id, workstream_id, sequence, kind, summary, evidence_refs_json, sensitivity, created_at "
    "FROM cf_workstream_events "
)
_ARTIFACT_SELECT = (
    "SELECT artifact_id, workstream_id, logical_key, version, supersedes_artifact_id, kind, uri, content_hash, "
    "source_run_id, evidence_event_ids_json, evidence_refs_json, status, created_at, account_generation "
    "FROM cf_workstream_artifacts "
)
_CHECKPOINT_SELECT = (
    "SELECT checkpoint_id, workstream_id, runtime_id, last_event_sequence, context_summary, "
    "evidence_refs_json, updated_at, account_generation FROM cf_workstream_checkpoints "
)


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


def _iso(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


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


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:32]}"


def _workstream_response(row: dict[str, object]) -> dict[str, object]:
    return {
        "workstream_id": str(row.get("id") or row.get("workstream_id") or ""),
        "goal_id": row.get("goal_id"),
        "title": str(row.get("title") or ""),
        "objective": str(row.get("objective") or ""),
        "status": str(row.get("status") or WorkstreamStatus.open.value),
        "current_state_summary": str(row.get("current_state_summary") or ""),
        "next_review_at": _iso(row.get("next_review_at")),
        "last_meaningful_progress_at": _iso(row.get("last_meaningful_progress_at")),
        "latest_event_sequence": int(row.get("latest_event_sequence") or 0),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def _event_response(row: dict[str, object]) -> dict[str, object]:
    return {
        "event_id": str(row.get("event_id") or ""),
        "workstream_id": str(row.get("workstream_id") or ""),
        "sequence": int(row.get("sequence") or 0),
        "kind": str(row.get("kind") or WorkstreamEventKind.system.value),
        "summary": str(row.get("summary") or ""),
        "evidence_refs": _json_list(row.get("evidence_refs_json")),
        "sensitivity": str(row.get("sensitivity") or WorkstreamSensitivity.normal.value),
        "created_at": _iso(row.get("created_at")),
    }


def _artifact_response(row: dict[str, object]) -> dict[str, object]:
    return {
        "artifact_id": str(row.get("artifact_id") or ""),
        "workstream_id": str(row.get("workstream_id") or ""),
        "logical_key": str(row.get("logical_key") or ""),
        "version": int(row.get("version") or 0),
        "supersedes_artifact_id": row.get("supersedes_artifact_id"),
        "kind": str(row.get("kind") or ""),
        "uri": str(row.get("uri") or ""),
        "content_hash": str(row.get("content_hash") or ""),
        "source_run_id": row.get("source_run_id"),
        "evidence_event_ids": _json_list(row.get("evidence_event_ids_json")),
        "evidence_refs": _json_list(row.get("evidence_refs_json")),
        "status": str(row.get("status") or ArtifactStatus.draft.value),
        "created_at": _iso(row.get("created_at")),
    }


def _checkpoint_response(row: dict[str, object]) -> dict[str, object]:
    return {
        "checkpoint_id": str(row.get("checkpoint_id") or ""),
        "workstream_id": str(row.get("workstream_id") or ""),
        "runtime_id": str(row.get("runtime_id") or ""),
        "last_event_sequence": int(row.get("last_event_sequence") or 0),
        "context_summary": str(row.get("context_summary") or ""),
        "evidence_refs": _json_list(row.get("evidence_refs_json")),
        "updated_at": _iso(row.get("updated_at")),
    }


async def _first_workstream(env: object, uid: str, workstream_id: str) -> dict[str, object] | None:
    row = await env.APP_DB.prepare(_WORKSTREAM_SELECT + "WHERE uid = ? AND id = ?").bind(uid, workstream_id).first()
    return row if isinstance(row, dict) else None


async def _first_artifact(env: object, uid: str, workstream_id: str, artifact_id: str) -> dict[str, object] | None:
    row = (
        await env.APP_DB.prepare(_ARTIFACT_SELECT + "WHERE uid = ? AND workstream_id = ? AND artifact_id = ?")
        .bind(uid, workstream_id, artifact_id)
        .first()
    )
    return row if isinstance(row, dict) else None


def _mutation_inputs(request: Request, payload: object) -> tuple[str, int, str] | None:
    key = request.headers.get("idempotency-key")
    raw_generation = request.headers.get("x-account-generation")
    if not key or len(key) > MAX_IDEMPOTENCY_KEY_LENGTH or not raw_generation:
        return None
    try:
        generation = int(raw_generation)
    except (TypeError, ValueError):
        return None
    if generation < 0:
        return None
    request_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return key, generation, request_hash


async def _load_mutation(
    env: object,
    uid: str,
    operation: str,
    key: str,
    generation: int,
    request_hash: str,
) -> tuple[dict[str, object] | None, JSONResponse | None]:
    row = (
        await env.APP_DB.prepare(
            "SELECT account_generation, request_hash, result_json FROM cf_workstream_mutations "
            "WHERE uid = ? AND operation = ? AND idempotency_key = ?"
        )
        .bind(uid, operation, key)
        .first()
    )
    if not isinstance(row, dict):
        return None, None
    if row.get("account_generation") != generation or row.get("request_hash") != request_hash:
        return None, JSONResponse({"error": "idempotency key reused with different request"}, status_code=409)
    try:
        result = json.loads(str(row.get("result_json") or ""))
    except (TypeError, ValueError):
        return None, JSONResponse({"error": "workflow mutation receipt unavailable"}, status_code=503)
    return result if isinstance(result, dict) else None, None


def _mutation_statement(
    env: object,
    uid: str,
    operation: str,
    key: str,
    generation: int,
    request_hash: str,
    result: dict[str, object],
    now: int,
) -> object:
    return env.APP_DB.prepare(
        "INSERT INTO cf_workstream_mutations "
        "(uid, operation, idempotency_key, account_generation, request_hash, result_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)"
    ).bind(uid, operation, key, generation, request_hash, json.dumps(result, ensure_ascii=False), now)


async def _workstream_tasks(env: object, uid: str, workstream_id: str) -> list[dict[str, object]]:
    result = (
        await env.APP_DB.prepare(
            _TASK_SELECT + "WHERE uid = ? AND workstream_id = ? AND deleted = 0 ORDER BY created_at ASC"
        )
        .bind(uid, workstream_id)
        .all()
    )
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [_action_item_response(row) for row in rows if isinstance(row, dict)]


async def _workstream_events(env: object, uid: str, workstream_id: str, *, after: int = 0, limit: int = 100):
    result = (
        await env.APP_DB.prepare(
            _EVENT_SELECT + "WHERE uid = ? AND workstream_id = ? AND sequence > ? ORDER BY sequence ASC LIMIT ?"
        )
        .bind(uid, workstream_id, after, limit)
        .all()
    )
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [_event_response(row) for row in rows if isinstance(row, dict)]


async def _artifacts(env: object, uid: str, workstream_id: str, limit: int = 100):
    result = (
        await env.APP_DB.prepare(
            _ARTIFACT_SELECT + "WHERE uid = ? AND workstream_id = ? ORDER BY created_at DESC LIMIT ?"
        )
        .bind(uid, workstream_id, limit)
        .all()
    )
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [_artifact_response(row) for row in rows if isinstance(row, dict)]


async def _checkpoints(env: object, uid: str, workstream_id: str):
    result = (
        await env.APP_DB.prepare(_CHECKPOINT_SELECT + "WHERE uid = ? AND workstream_id = ? ORDER BY updated_at DESC")
        .bind(uid, workstream_id)
        .all()
    )
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [_checkpoint_response(row) for row in rows if isinstance(row, dict)]


def _invalid_id(value: str) -> bool:
    return not value or len(value) > MAX_ID_LENGTH


@router.get("/v1/workstreams/{workstream_id}")
async def get_workstream_detail(request: Request, workstream_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if _invalid_id(workstream_id):
        return JSONResponse({"error": "invalid workstream id"}, status_code=400)
    env = request.scope["env"]
    uid = str(context["uid"])
    try:
        workstream = await _first_workstream(env, uid, workstream_id)
        if workstream is None:
            return JSONResponse({"error": "workflow resource not found"}, status_code=404)
        return {
            "workstream": _workstream_response(workstream),
            "recent_events": await _workstream_events(env, uid, workstream_id),
            "tasks": await _workstream_tasks(env, uid, workstream_id),
            "artifacts": await _artifacts(env, uid, workstream_id),
            "checkpoints": await _checkpoints(env, uid, workstream_id),
        }
    except Exception:
        return JSONResponse({"error": "workflow resource unavailable"}, status_code=503)


@router.get("/v1/workstreams/{workstream_id}/events")
async def list_workstream_events(request: Request, workstream_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if _invalid_id(workstream_id):
        return JSONResponse({"error": "invalid workstream id"}, status_code=400)
    raw_after = getattr(request, "query_params", {}).get("after_sequence", "0")
    raw_limit = getattr(request, "query_params", {}).get("limit", "100")
    try:
        after = int(str(raw_after))
        limit = int(str(raw_limit))
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid event pagination"}, status_code=400)
    if after < 0 or not 1 <= limit <= MAX_LIST_LIMIT:
        return JSONResponse({"error": "invalid event pagination"}, status_code=400)
    env = request.scope["env"]
    uid = str(context["uid"])
    try:
        if await _first_workstream(env, uid, workstream_id) is None:
            return JSONResponse({"error": "workflow resource not found"}, status_code=404)
        return await _workstream_events(env, uid, workstream_id, after=after, limit=limit)
    except Exception:
        return JSONResponse({"error": "workflow resource unavailable"}, status_code=503)


@router.post("/v1/workstreams/{workstream_id}/events")
async def append_workstream_event(request: Request, workstream_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if _invalid_id(workstream_id):
        return JSONResponse({"error": "invalid workstream id"}, status_code=400)
    try:
        event = WorkstreamEventCreate.model_validate(await _bounded_json(request))
        payload = event.model_dump(mode="json")
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid workstream event"}, status_code=400)
    mutation = _mutation_inputs(request, payload)
    if mutation is None:
        return JSONResponse({"error": "Idempotency-Key and X-Account-Generation are required"}, status_code=400)
    key, generation, request_hash = mutation
    env = request.scope["env"]
    uid = str(context["uid"])
    operation = f"workstream-event:{workstream_id}"
    try:
        stored, conflict = await _load_mutation(env, uid, operation, key, generation, request_hash)
        if conflict:
            return conflict
        if stored is not None:
            return stored
        target = await _first_workstream(env, uid, workstream_id)
        if target is None:
            return JSONResponse({"error": "workflow resource not found"}, status_code=404)
        if int(target.get("account_generation") or 0) != generation:
            return JSONResponse({"error": "workflow operation conflicts with current state"}, status_code=409)
        sequence = int(target.get("latest_event_sequence") or 0) + 1
        now = int(time.time())
        event_id = _stable_id("wse", uid, workstream_id, generation, key)
        result = {
            "event_id": event_id,
            "workstream_id": workstream_id,
            "sequence": sequence,
            "kind": event.kind.value,
            "summary": event.summary,
            "evidence_refs": [reference.model_dump(mode="json") for reference in event.evidence_refs],
            "sensitivity": event.sensitivity.value,
            "created_at": _iso(now),
        }
        statements = [
            env.APP_DB.prepare(
                "INSERT INTO cf_workstream_events "
                "(uid, event_id, workstream_id, sequence, kind, summary, evidence_refs_json, sensitivity, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ).bind(
                uid,
                event_id,
                workstream_id,
                sequence,
                event.kind.value,
                event.summary,
                json.dumps(result["evidence_refs"], ensure_ascii=False),
                event.sensitivity.value,
                now,
            ),
            env.APP_DB.prepare(
                "UPDATE cf_workstreams SET latest_event_sequence = ?, last_meaningful_progress_at = ?, updated_at = ? "
                "WHERE uid = ? AND id = ?"
            ).bind(sequence, now, now, uid, workstream_id),
            _mutation_statement(env, uid, operation, key, generation, request_hash, result, now),
        ]
        await env.APP_DB.batch(statements)
        return result
    except Exception:
        return JSONResponse({"error": "workflow resource unavailable"}, status_code=503)


@router.patch("/v1/workstreams/{workstream_id}")
async def update_workstream(request: Request, workstream_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if _invalid_id(workstream_id):
        return JSONResponse({"error": "invalid workstream id"}, status_code=400)
    try:
        update = WorkstreamUpdate.model_validate(await _bounded_json(request))
        payload = update.model_dump(mode="json", exclude_unset=True)
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid workstream update"}, status_code=400)
    mutation = _mutation_inputs(request, payload)
    if mutation is None:
        return JSONResponse({"error": "Idempotency-Key and X-Account-Generation are required"}, status_code=400)
    key, generation, request_hash = mutation
    env = request.scope["env"]
    uid = str(context["uid"])
    operation = f"workstream-update:{workstream_id}"
    try:
        stored, conflict = await _load_mutation(env, uid, operation, key, generation, request_hash)
        if conflict:
            return conflict
        if stored is not None:
            return stored
        target = await _first_workstream(env, uid, workstream_id)
        if target is None:
            return JSONResponse({"error": "workflow resource not found"}, status_code=404)
        if int(target.get("account_generation") or 0) != generation:
            return JSONResponse({"error": "workflow operation conflicts with current state"}, status_code=409)
        values: dict[str, object] = {}
        if update.title is not None:
            values["title"] = update.title
        if update.objective is not None:
            values["objective"] = update.objective
        if update.status is not None:
            values["status"] = update.status.value
        if "current_state_summary" in update.model_fields_set:
            values["current_state_summary"] = update.current_state_summary
        if "next_review_at" in update.model_fields_set:
            values["next_review_at"] = _epoch(update.next_review_at)
        now = int(time.time())
        values["updated_at"] = now
        patched = dict(target)
        patched.update(values)
        result = _workstream_response(patched)
        assignments = ", ".join(f"{field} = ?" for field in values)
        statements = [
            env.APP_DB.prepare(f"UPDATE cf_workstreams SET {assignments} WHERE uid = ? AND id = ?").bind(
                *values.values(), uid, workstream_id
            ),
            _mutation_statement(env, uid, operation, key, generation, request_hash, result, now),
        ]
        await env.APP_DB.batch(statements)
        return result
    except Exception:
        return JSONResponse({"error": "workflow resource unavailable"}, status_code=503)


@router.get("/v1/workstreams/{workstream_id}/artifacts")
async def list_artifact_descriptors(request: Request, workstream_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    raw_limit = getattr(request, "query_params", {}).get("limit", "100")
    try:
        limit = int(str(raw_limit))
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid artifact pagination"}, status_code=400)
    if _invalid_id(workstream_id) or not 1 <= limit <= MAX_LIST_LIMIT:
        return JSONResponse({"error": "invalid artifact pagination"}, status_code=400)
    env = request.scope["env"]
    uid = str(context["uid"])
    try:
        if await _first_workstream(env, uid, workstream_id) is None:
            return JSONResponse({"error": "workflow resource not found"}, status_code=404)
        return await _artifacts(env, uid, workstream_id, limit)
    except Exception:
        return JSONResponse({"error": "workflow resource unavailable"}, status_code=503)


@router.post("/v1/workstreams/{workstream_id}/artifacts")
async def create_artifact_descriptor(request: Request, workstream_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if _invalid_id(workstream_id):
        return JSONResponse({"error": "invalid workstream id"}, status_code=400)
    try:
        proposal = ArtifactDescriptorCreate.model_validate(await _bounded_json(request))
        payload = proposal.model_dump(mode="json")
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid artifact descriptor"}, status_code=400)
    mutation = _mutation_inputs(request, payload)
    if mutation is None:
        return JSONResponse({"error": "Idempotency-Key and X-Account-Generation are required"}, status_code=400)
    key, generation, request_hash = mutation
    env = request.scope["env"]
    uid = str(context["uid"])
    operation = f"artifact-create:{workstream_id}"
    artifact_id = _stable_id("artifact", uid, workstream_id, proposal.logical_key, proposal.version)
    try:
        stored, conflict = await _load_mutation(env, uid, operation, key, generation, request_hash)
        if conflict:
            return conflict
        if stored is not None:
            return stored
        target = await _first_workstream(env, uid, workstream_id)
        if target is None:
            return JSONResponse({"error": "workflow resource not found"}, status_code=404)
        if int(target.get("account_generation") or 0) != generation:
            return JSONResponse({"error": "workflow operation conflicts with current state"}, status_code=409)
        existing = await _first_artifact(env, uid, workstream_id, artifact_id)
        if existing is not None:
            existing_projection = _artifact_response(existing)
            comparable = {field: existing_projection.get(field) for field in payload}
            if comparable != payload:
                return JSONResponse(
                    {"error": "artifact version already exists with different content"}, status_code=409
                )
            now = int(time.time())
            await env.APP_DB.batch(
                [_mutation_statement(env, uid, operation, key, generation, request_hash, existing_projection, now)]
            )
            return existing_projection
        latest = (
            await env.APP_DB.prepare(
                _ARTIFACT_SELECT
                + "WHERE uid = ? AND workstream_id = ? AND logical_key = ? ORDER BY version DESC LIMIT 1"
            )
            .bind(uid, workstream_id, proposal.logical_key)
            .first()
        )
        if isinstance(latest, dict):
            if proposal.version != int(latest.get("version") or 0) + 1 or proposal.supersedes_artifact_id != latest.get(
                "artifact_id"
            ):
                return JSONResponse(
                    {"error": "artifact version must advance and supersede the current logical head"}, status_code=409
                )
        elif proposal.version != 1 or proposal.supersedes_artifact_id is not None:
            return JSONResponse(
                {"error": "the first logical artifact version must be version 1 without supersession"}, status_code=409
            )
        if proposal.supersedes_artifact_id is not None and not proposal.evidence_event_ids:
            return JSONResponse(
                {"error": "artifact revisions must cite the journal evidence that caused the change"}, status_code=409
            )
        for event_id in proposal.evidence_event_ids:
            evidence = (
                await env.APP_DB.prepare(_EVENT_SELECT + "WHERE uid = ? AND workstream_id = ? AND event_id = ?")
                .bind(uid, workstream_id, event_id)
                .first()
            )
            if not isinstance(evidence, dict):
                return JSONResponse({"error": "artifact references a missing workstream event"}, status_code=409)
        now = int(time.time())
        sequence = int(target.get("latest_event_sequence") or 0) + 1
        event_id = _stable_id("wse", uid, workstream_id, "artifact", artifact_id)
        artifact = {
            "artifact_id": artifact_id,
            "workstream_id": workstream_id,
            "logical_key": proposal.logical_key,
            "version": proposal.version,
            "supersedes_artifact_id": proposal.supersedes_artifact_id,
            "kind": proposal.kind,
            "uri": proposal.uri,
            "content_hash": proposal.content_hash,
            "source_run_id": proposal.source_run_id,
            "evidence_event_ids": proposal.evidence_event_ids,
            "evidence_refs": [reference.model_dump(mode="json") for reference in proposal.evidence_refs],
            "status": ArtifactStatus.draft.value,
            "created_at": _iso(now),
        }
        statements = [
            env.APP_DB.prepare(
                "INSERT INTO cf_workstream_artifacts "
                "(uid, artifact_id, workstream_id, logical_key, version, supersedes_artifact_id, kind, uri, content_hash, "
                "source_run_id, evidence_event_ids_json, evidence_refs_json, status, created_at, account_generation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ).bind(
                uid,
                artifact_id,
                workstream_id,
                proposal.logical_key,
                proposal.version,
                proposal.supersedes_artifact_id,
                proposal.kind,
                proposal.uri,
                proposal.content_hash,
                proposal.source_run_id,
                json.dumps(proposal.evidence_event_ids, ensure_ascii=False),
                json.dumps(artifact["evidence_refs"], ensure_ascii=False),
                ArtifactStatus.draft.value,
                now,
                generation,
            ),
            env.APP_DB.prepare(
                "INSERT INTO cf_workstream_events "
                "(uid, event_id, workstream_id, sequence, kind, summary, evidence_refs_json, sensitivity, created_at) "
                "VALUES (?, ?, ?, ?, 'artifact_version', ?, ?, 'normal', ?)"
            ).bind(
                uid,
                event_id,
                workstream_id,
                sequence,
                f"Artifact {proposal.logical_key} version {proposal.version} created",
                json.dumps(artifact["evidence_refs"], ensure_ascii=False),
                now,
            ),
        ]
        if proposal.supersedes_artifact_id is not None:
            statements.append(
                env.APP_DB.prepare(
                    "UPDATE cf_workstream_artifacts SET status = 'superseded' "
                    "WHERE uid = ? AND workstream_id = ? AND artifact_id = ?"
                ).bind(uid, workstream_id, proposal.supersedes_artifact_id)
            )
        statements.extend(
            [
                env.APP_DB.prepare(
                    "UPDATE cf_workstreams SET latest_event_sequence = ?, last_meaningful_progress_at = ?, updated_at = ? "
                    "WHERE uid = ? AND id = ?"
                ).bind(sequence, now, now, uid, workstream_id),
                _mutation_statement(env, uid, operation, key, generation, request_hash, artifact, now),
            ]
        )
        await env.APP_DB.batch(statements)
        return artifact
    except Exception:
        return JSONResponse({"error": "workflow resource unavailable"}, status_code=503)


@router.patch("/v1/workstreams/{workstream_id}/artifacts/{artifact_id}/status")
async def transition_artifact_status(request: Request, workstream_id: str, artifact_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if _invalid_id(workstream_id) or _invalid_id(artifact_id):
        return JSONResponse({"error": "invalid workflow resource id"}, status_code=400)
    try:
        update = ArtifactStatusTransitionRequest.model_validate(await _bounded_json(request))
        payload = update.model_dump(mode="json")
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid artifact status"}, status_code=400)
    mutation = _mutation_inputs(request, payload)
    if mutation is None:
        return JSONResponse({"error": "Idempotency-Key and X-Account-Generation are required"}, status_code=400)
    key, generation, request_hash = mutation
    env = request.scope["env"]
    uid = str(context["uid"])
    operation = f"artifact-status:{workstream_id}:{artifact_id}"
    allowed = {
        ArtifactStatus.draft: ArtifactStatus.awaiting_review,
        ArtifactStatus.awaiting_review: ArtifactStatus.approved,
        ArtifactStatus.approved: ArtifactStatus.delivered,
    }
    try:
        stored, conflict = await _load_mutation(env, uid, operation, key, generation, request_hash)
        if conflict:
            return conflict
        if stored is not None:
            return stored
        target = await _first_workstream(env, uid, workstream_id)
        artifact = await _first_artifact(env, uid, workstream_id, artifact_id)
        if target is None or artifact is None:
            return JSONResponse({"error": "workflow resource not found"}, status_code=404)
        if int(target.get("account_generation") or 0) != generation:
            return JSONResponse({"error": "workflow operation conflicts with current state"}, status_code=409)
        current = ArtifactStatus(str(artifact.get("status") or ArtifactStatus.draft.value))
        if current == update.status:
            result = _artifact_response(artifact)
            now = int(time.time())
            await env.APP_DB.batch(
                [_mutation_statement(env, uid, operation, key, generation, request_hash, result, now)]
            )
            return result
        if allowed.get(current) != update.status:
            return JSONResponse({"error": "artifact status transition is not allowed"}, status_code=409)
        now = int(time.time())
        sequence = int(target.get("latest_event_sequence") or 0) + 1
        patched = dict(artifact)
        patched["status"] = update.status.value
        result = _artifact_response(patched)
        event_id = _stable_id("wse", uid, workstream_id, "artifact-status", artifact_id, update.status.value)
        await env.APP_DB.batch(
            [
                env.APP_DB.prepare(
                    "UPDATE cf_workstream_artifacts SET status = ? WHERE uid = ? AND workstream_id = ? AND artifact_id = ?"
                ).bind(update.status.value, uid, workstream_id, artifact_id),
                env.APP_DB.prepare(
                    "INSERT INTO cf_workstream_events "
                    "(uid, event_id, workstream_id, sequence, kind, summary, evidence_refs_json, sensitivity, created_at) "
                    "VALUES (?, ?, ?, ?, 'system', ?, ?, 'normal', ?)"
                ).bind(
                    uid,
                    event_id,
                    workstream_id,
                    sequence,
                    f"Artifact {artifact.get('logical_key')} version {artifact.get('version')} moved to {update.status.value}",
                    artifact.get("evidence_refs_json") or "[]",
                    now,
                ),
                env.APP_DB.prepare(
                    "UPDATE cf_workstreams SET latest_event_sequence = ?, last_meaningful_progress_at = ?, updated_at = ? "
                    "WHERE uid = ? AND id = ?"
                ).bind(sequence, now, now, uid, workstream_id),
                _mutation_statement(env, uid, operation, key, generation, request_hash, result, now),
            ]
        )
        return result
    except Exception:
        return JSONResponse({"error": "workflow resource unavailable"}, status_code=503)


@router.get("/v1/workstreams/{workstream_id}/checkpoints")
async def list_continuation_checkpoints(request: Request, workstream_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if _invalid_id(workstream_id):
        return JSONResponse({"error": "invalid workstream id"}, status_code=400)
    env = request.scope["env"]
    uid = str(context["uid"])
    try:
        if await _first_workstream(env, uid, workstream_id) is None:
            return JSONResponse({"error": "workflow resource not found"}, status_code=404)
        return await _checkpoints(env, uid, workstream_id)
    except Exception:
        return JSONResponse({"error": "workflow resource unavailable"}, status_code=503)


@router.put("/v1/workstreams/{workstream_id}/checkpoints/{runtime_id}")
async def upsert_continuation_checkpoint(request: Request, workstream_id: str, runtime_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if _invalid_id(workstream_id) or _invalid_id(runtime_id):
        return JSONResponse({"error": "invalid workflow resource id"}, status_code=400)
    try:
        checkpoint = ContinuationCheckpointUpsert.model_validate(await _bounded_json(request))
        if checkpoint.runtime_id != runtime_id:
            return JSONResponse({"error": "runtime_id path and body must match"}, status_code=422)
        payload = checkpoint.model_dump(mode="json")
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid continuation checkpoint"}, status_code=400)
    mutation = _mutation_inputs(request, payload)
    if mutation is None:
        return JSONResponse({"error": "Idempotency-Key and X-Account-Generation are required"}, status_code=400)
    key, generation, request_hash = mutation
    env = request.scope["env"]
    uid = str(context["uid"])
    operation = f"checkpoint-upsert:{workstream_id}:{runtime_id}"
    checkpoint_id = _stable_id("checkpoint", uid, workstream_id, runtime_id)
    try:
        stored, conflict = await _load_mutation(env, uid, operation, key, generation, request_hash)
        if conflict:
            return conflict
        if stored is not None:
            return stored
        target = await _first_workstream(env, uid, workstream_id)
        if target is None:
            return JSONResponse({"error": "workflow resource not found"}, status_code=404)
        if int(target.get("account_generation") or 0) != generation:
            return JSONResponse({"error": "workflow operation conflicts with current state"}, status_code=409)
        if checkpoint.last_event_sequence > int(target.get("latest_event_sequence") or 0):
            return JSONResponse({"error": "checkpoint cannot advance beyond the workstream journal"}, status_code=409)
        existing = (
            await env.APP_DB.prepare(_CHECKPOINT_SELECT + "WHERE uid = ? AND checkpoint_id = ?")
            .bind(uid, checkpoint_id)
            .first()
        )
        if isinstance(existing, dict):
            existing_sequence = int(existing.get("last_event_sequence") or 0)
            if checkpoint.last_event_sequence < existing_sequence:
                return JSONResponse({"error": "checkpoint sequence cannot move backwards"}, status_code=409)
            if checkpoint.last_event_sequence == existing_sequence:
                if checkpoint.context_summary != existing.get("context_summary") or _json_list(
                    existing.get("evidence_refs_json")
                ) != [reference.model_dump(mode="json") for reference in checkpoint.evidence_refs]:
                    return JSONResponse(
                        {"error": "checkpoint sequence already stores different content"}, status_code=409
                    )
                result = _checkpoint_response(existing)
                now = int(time.time())
                await env.APP_DB.batch(
                    [_mutation_statement(env, uid, operation, key, generation, request_hash, result, now)]
                )
                return result
        now = int(time.time())
        result = {
            "checkpoint_id": checkpoint_id,
            "workstream_id": workstream_id,
            "runtime_id": runtime_id,
            "last_event_sequence": checkpoint.last_event_sequence,
            "context_summary": checkpoint.context_summary,
            "evidence_refs": [reference.model_dump(mode="json") for reference in checkpoint.evidence_refs],
            "updated_at": _iso(now),
        }
        statement = env.APP_DB.prepare(
            "INSERT INTO cf_workstream_checkpoints "
            "(uid, checkpoint_id, workstream_id, runtime_id, last_event_sequence, context_summary, "
            "evidence_refs_json, updated_at, account_generation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(uid, checkpoint_id) DO UPDATE SET last_event_sequence = excluded.last_event_sequence, "
            "context_summary = excluded.context_summary, evidence_refs_json = excluded.evidence_refs_json, "
            "updated_at = excluded.updated_at"
        ).bind(
            uid,
            checkpoint_id,
            workstream_id,
            runtime_id,
            checkpoint.last_event_sequence,
            checkpoint.context_summary,
            json.dumps(result["evidence_refs"], ensure_ascii=False),
            now,
            generation,
        )
        await env.APP_DB.batch(
            [statement, _mutation_statement(env, uid, operation, key, generation, request_hash, result, now)]
        )
        return result
    except Exception:
        return JSONResponse({"error": "workflow resource unavailable"}, status_code=503)


async def _insert_initial_workstream(
    env: object,
    uid: str,
    workstream_id: str,
    title: str,
    objective: str,
    goal_id: str | None,
    generation: int,
    source_key: str,
    now: int,
    statements: list[object],
) -> None:
    event_id = _stable_id("wse", uid, workstream_id, source_key)
    statements.extend(
        [
            env.APP_DB.prepare(
                "INSERT INTO cf_workstreams "
                "(uid, id, goal_id, title, objective, status, current_state_summary, next_review_at, "
                "last_meaningful_progress_at, latest_event_sequence, account_generation, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'open', '', NULL, ?, 1, ?, ?, ?)"
            ).bind(uid, workstream_id, goal_id, title, objective, now, generation, now, now),
            env.APP_DB.prepare(
                "INSERT INTO cf_workstream_events "
                "(uid, event_id, workstream_id, sequence, kind, summary, evidence_refs_json, sensitivity, created_at) "
                "VALUES (?, ?, ?, 1, 'system', ?, '[]', 'normal', ?)"
            ).bind(
                uid,
                event_id,
                workstream_id,
                source_key.startswith("goal:")
                and "Work initiated from a goal by the user"
                or "Work initiated by the user",
                now,
            ),
        ]
    )


@router.post("/v1/work-intents")
async def resolve_work_intent(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        intent = _WORK_INTENT_ADAPTER.validate_python(await _bounded_json(request))
        payload = intent.model_dump(mode="json")
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid work intent"}, status_code=400)
    mutation = _mutation_inputs(request, payload)
    if mutation is None:
        return JSONResponse({"error": "Idempotency-Key and X-Account-Generation are required"}, status_code=400)
    key, generation, request_hash = mutation
    env = request.scope["env"]
    uid = str(context["uid"])
    operation = "work-intent"
    receipt_id = _stable_id("intent", uid, generation, key)
    try:
        stored, conflict = await _load_mutation(env, uid, operation, key, generation, request_hash)
        if conflict:
            return conflict
        if stored is not None:
            return stored
        now = int(time.time())
        statements: list[object] = []
        newly_created = False
        goal_id: str | None
        if isinstance(intent, TaskOriginWorkIntent):
            task = (
                await env.APP_DB.prepare(_TASK_SELECT + "WHERE uid = ? AND id = ? AND deleted = 0")
                .bind(uid, intent.task_id)
                .first()
            )
            if not isinstance(task, dict):
                return JSONResponse({"error": "workflow resource not found"}, status_code=404)
            goal_id = task.get("goal_id") if isinstance(task.get("goal_id"), str) else None
            existing_id = task.get("workstream_id")
            if isinstance(existing_id, str) and existing_id:
                existing = await _first_workstream(env, uid, existing_id)
                if existing is None:
                    return JSONResponse({"error": "task points to a missing workstream"}, status_code=409)
                if existing.get("goal_id") != goal_id:
                    return JSONResponse({"error": "task and workstream goals disagree"}, status_code=409)
                if int(existing.get("account_generation") or 0) != generation:
                    return JSONResponse({"error": "workflow operation conflicts with current state"}, status_code=409)
                workstream_id = existing_id
                task_id = intent.task_id
            else:
                if goal_id:
                    goal = (
                        await env.APP_DB.prepare("SELECT status FROM cf_goals WHERE uid = ? AND id = ?")
                        .bind(uid, goal_id)
                        .first()
                    )
                    if not isinstance(goal, dict):
                        return JSONResponse({"error": "goal does not exist"}, status_code=409)
                    if goal.get("status") in {"achieved", "abandoned"}:
                        return JSONResponse({"error": "ended goal cannot receive new work"}, status_code=409)
                workstream_id = _stable_id("workstream", uid, generation, "task", intent.task_id)
                existing = await _first_workstream(env, uid, workstream_id)
                if existing is not None:
                    if existing.get("goal_id") != goal_id:
                        return JSONResponse({"error": "deterministic workstream goal collision"}, status_code=409)
                else:
                    description = str(task.get("description") or "Task")
                    title = (intent.title or description).strip()
                    objective = (intent.objective or description).strip()
                    _ = await _insert_initial_workstream(
                        env,
                        uid,
                        workstream_id,
                        title,
                        objective,
                        goal_id,
                        generation,
                        f"task:{intent.task_id}",
                        now,
                        statements,
                    )
                    newly_created = True
                task_id = intent.task_id
                statements.append(
                    env.APP_DB.prepare(
                        "UPDATE cf_action_items SET workstream_id = ?, updated_at = ? WHERE uid = ? AND id = ? AND deleted = 0"
                    ).bind(workstream_id, now, uid, task_id)
                )
        else:
            goal_id = intent.goal_id
            goal = (
                await env.APP_DB.prepare("SELECT status FROM cf_goals WHERE uid = ? AND id = ?")
                .bind(uid, goal_id)
                .first()
            )
            if not isinstance(goal, dict):
                return JSONResponse({"error": "goal does not exist"}, status_code=409)
            if goal.get("status") in {"achieved", "abandoned"}:
                return JSONResponse({"error": "ended goal cannot receive new work"}, status_code=409)
            workstream_id = _stable_id("workstream", uid, "goal-intent", receipt_id)
            task_id = _stable_id("task", uid, "goal-intent", receipt_id)
            if await _first_workstream(env, uid, workstream_id) is not None:
                return JSONResponse({"error": "deterministic goal-origin intent id collision"}, status_code=409)
            _ = await _insert_initial_workstream(
                env,
                uid,
                workstream_id,
                intent.title.strip(),
                intent.objective.strip(),
                goal_id,
                generation,
                f"goal:{goal_id}:{receipt_id}",
                now,
                statements,
            )
            statements.append(
                env.APP_DB.prepare(
                    "INSERT INTO cf_action_items "
                    "(uid, id, description, status, completed, goal_id, workstream_id, owner, source, provenance_json, "
                    "created_at, updated_at, idempotency_key) VALUES (?, ?, ?, 'active', 0, ?, ?, 'user', "
                    "'explicit_goal_intent', '[]', ?, ?, ?)"
                ).bind(
                    uid, task_id, intent.anchor_task_description.strip(), goal_id, workstream_id, now, now, receipt_id
                )
            )
            newly_created = True
        result = {
            "receipt_id": receipt_id,
            "workstream_id": workstream_id,
            "task_id": task_id,
            "goal_id": goal_id,
            "newly_created": newly_created,
            "created_at": _iso(now),
        }
        statements.append(_mutation_statement(env, uid, operation, key, generation, request_hash, result, now))
        await env.APP_DB.batch(statements)
        return result
    except Exception:
        return JSONResponse({"error": "workflow resource unavailable"}, status_code=503)
