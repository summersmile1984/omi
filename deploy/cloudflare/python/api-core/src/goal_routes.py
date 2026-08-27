"""D1-backed goal metadata routes for the isolated Cloudflare profile.

This goal slice owns the durable metadata, metric projection, daily progress
history, focus-cap mutations, retain-only lifecycle transitions, and progress
event feed used by the released clients. Relationship detach and AI
advice/suggestion routes remain on the legacy owner until their stronger
workflow contracts are migrated.
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
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from action_item_routes import _response as _action_item_response
from internal_auth import decode_context

router = APIRouter()

MAX_REQUEST_BYTES = 100_000
MAX_ID_LENGTH = 256
MAX_IDEMPOTENCY_KEY_LENGTH = 256
FOCUS_CAP = 5


class GoalType(str, Enum):
    boolean = "boolean"
    scale = "scale"
    numeric = "numeric"


class GoalStatus(str, Enum):
    background = "background"
    focused = "focused"
    paused = "paused"
    achieved = "achieved"
    abandoned = "abandoned"


class GoalSource(str, Enum):
    user = "user"
    ai_suggested = "ai_suggested"
    imported = "imported"


class GoalRelationshipDisposition(str, Enum):
    retain = "retain"
    detach = "detach"


class FocusGoalUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    replacement_goal_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    focus_rank: int | None = Field(default=None, ge=0, le=FOCUS_CAP - 1)


class GoalLifecycleUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    status: GoalStatus
    relationship_disposition: GoalRelationshipDisposition

    @model_validator(mode="after")
    def validate_terminal_status(self) -> "GoalLifecycleUpdate":
        if self.status not in {GoalStatus.paused, GoalStatus.achieved, GoalStatus.abandoned}:
            raise ValueError("goal lifecycle transition must pause or end the goal")
        return self


class GoalMetric(BaseModel):
    model_config = {"extra": "forbid"}

    type: GoalType
    current: float
    target: float
    min: float | None = None
    max: float | None = None
    unit: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_bounds(self) -> "GoalMetric":
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("metric min must not exceed max")
        return self


class GoalProgressEventKind(str, Enum):
    evidence = "evidence"
    metric_update = "metric_update"
    milestone = "milestone"
    status_change = "status_change"


class EvidenceKind(str, Enum):
    conversation = "conversation"
    memory_item = "memory_item"
    workstream_event = "workstream_event"
    artifact = "artifact"
    chat_message = "chat_message"
    local_screen = "local_screen"
    external = "external"


class EvidenceScope(str, Enum):
    canonical = "canonical"
    device_local = "device_local"


class EvidenceRef(BaseModel):
    model_config = {"extra": "forbid"}

    kind: EvidenceKind
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    version: str | None = Field(default=None, max_length=128)
    scope: EvidenceScope
    device_id: str | None = Field(default=None, min_length=1, max_length=128)
    excerpt_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    transcript_segment_ids: list[str] | None = Field(default=None, max_length=100)
    start_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    end_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_scope(self) -> "EvidenceRef":
        if self.scope == EvidenceScope.device_local and not self.device_id:
            raise ValueError("device_local evidence requires device_id")
        if self.scope == EvidenceScope.canonical and self.device_id is not None:
            raise ValueError("canonical evidence cannot carry device_id")
        if self.kind == EvidenceKind.local_screen and self.scope != EvidenceScope.device_local:
            raise ValueError("local_screen evidence must be device_local")
        if self.start_seconds is not None and self.end_seconds is not None and self.end_seconds < self.start_seconds:
            raise ValueError("end_seconds must be greater than or equal to start_seconds")
        return self


class GoalProgressEventCreate(BaseModel):
    model_config = {"extra": "forbid"}

    kind: GoalProgressEventKind
    summary: str = Field(min_length=1, max_length=1000)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=50)
    metric: GoalMetric | None = None


class GoalCreate(BaseModel):
    model_config = {"extra": "ignore"}

    title: str = Field(min_length=1, max_length=500)
    desired_outcome: str | None = Field(default=None, max_length=2_000)
    why_it_matters: str | None = Field(default=None, max_length=2_000)
    success_criteria: list[str] = Field(default_factory=list, max_length=20)
    horizon_at: datetime | None = None
    status: GoalStatus = GoalStatus.background
    metric: GoalMetric | None = None
    source: GoalSource = GoalSource.user
    description: str | None = None
    goal_type: GoalType | None = None
    target_value: float | None = None
    current_value: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    unit: str | None = Field(default=None, max_length=64)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_description(cls, value: object) -> object:
        if not isinstance(value, dict) or "description" not in value:
            return value
        normalized = dict(value)
        description = normalized.pop("description")
        if normalized.get("desired_outcome") is None:
            normalized["desired_outcome"] = description
        return normalized

    @field_validator("source", mode="before")
    @classmethod
    def normalize_legacy_source(cls, value: object) -> object:
        return {"ai": GoalSource.ai_suggested.value}.get(value, value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def normalize_legacy_fields(self) -> "GoalCreate":
        self.title = self.title.strip()
        if not self.title:
            raise ValueError("title cannot be blank")
        if self.desired_outcome is None:
            self.desired_outcome = self.title
        else:
            self.desired_outcome = self.desired_outcome.strip()
        if not self.desired_outcome:
            raise ValueError("desired_outcome cannot be blank")
        self.success_criteria = [criterion.strip() for criterion in self.success_criteria if criterion.strip()]
        if self.status == GoalStatus.focused:
            raise ValueError("create the goal first, then focus it explicitly")
        if self.metric is None and (self.goal_type is not None or self.target_value is not None):
            self.metric = GoalMetric(
                type=self.goal_type or GoalType.scale,
                current=self.current_value if self.current_value is not None else 0,
                target=self.target_value if self.target_value is not None else 0,
                min=self.min_value,
                max=self.max_value,
                unit=self.unit,
            )
        return self


class GoalUpdate(BaseModel):
    model_config = {"extra": "ignore"}

    title: str | None = Field(default=None, min_length=1, max_length=500)
    desired_outcome: str | None = Field(default=None, max_length=2_000)
    why_it_matters: str | None = Field(default=None, max_length=2_000)
    success_criteria: list[str] | None = Field(default=None, max_length=20)
    horizon_at: datetime | None = None
    metric: GoalMetric | None = None
    clear_metric: bool = False
    goal_type: GoalType | None = None
    target_value: float | None = None
    current_value: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    unit: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_update(self) -> "GoalUpdate":
        if not (set(self.model_fields_set) - {"clear_metric"}) and not self.clear_metric:
            raise ValueError("at least one goal field is required")
        for field_name in ("title", "desired_outcome"):
            if field_name in self.model_fields_set:
                value = getattr(self, field_name)
                if value is None:
                    raise ValueError(f"{field_name} cannot be null")
                setattr(self, field_name, value.strip())
                if not getattr(self, field_name):
                    raise ValueError(f"{field_name} cannot be blank")
        if "success_criteria" in self.model_fields_set and self.success_criteria is None:
            raise ValueError("success_criteria cannot be null")
        for field_name in ("target_value", "current_value"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
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


def _metric(row: dict[str, object]) -> dict[str, object] | None:
    raw = row.get("metric_json")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (TypeError, ValueError):
        return None


def _response(row: dict[str, object]) -> dict[str, object]:
    status = str(row.get("status") or GoalStatus.background.value)
    metric = _metric(row)
    response: dict[str, object] = {
        "id": str(row.get("id") or ""),
        "goal_id": str(row.get("id") or ""),
        "title": str(row.get("title") or ""),
        "desired_outcome": str(row.get("desired_outcome") or row.get("title") or ""),
        "why_it_matters": row.get("why_it_matters"),
        "success_criteria": _json_list(row.get("success_criteria_json")),
        "horizon_at": _iso(row.get("horizon_at")),
        "status": status,
        "focus_rank": row.get("focus_rank"),
        "metric": metric,
        "source": str(row.get("source") or GoalSource.imported.value),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "ended_at": _iso(row.get("ended_at")),
        "latest_progress_sequence": int(row.get("latest_progress_sequence") or 0),
        "is_active": bool(row.get("is_active")),
        "advice": None,
    }
    if metric is None:
        response.update(
            {
                "goal_type": "scale",
                "target_value": 0.0,
                "current_value": 0.0,
                "min_value": 0.0,
                "max_value": 10.0,
                "unit": None,
            }
        )
    else:
        response.update(
            {
                "goal_type": metric.get("type", GoalType.scale.value),
                "target_value": metric.get("target", 0.0),
                "current_value": metric.get("current", 0.0),
                "min_value": metric.get("min", 0.0),
                "max_value": metric.get("max", 10.0),
                "unit": metric.get("unit"),
            }
        )
    return response


_SELECT = (
    "SELECT id, title, desired_outcome, why_it_matters, success_criteria_json, horizon_at, status, focus_rank, "
    "metric_json, source, relationship_disposition, is_active, latest_progress_sequence, ended_at, created_at, updated_at "
    "FROM cf_goals "
)
_DETAIL_WORKSTREAM_SELECT = (
    "SELECT id, goal_id, title, objective, status, current_state_summary, next_review_at, "
    "last_meaningful_progress_at, latest_event_sequence, created_at, updated_at "
    "FROM cf_workstreams "
)
_DETAIL_TASK_SELECT = (
    "SELECT id, description, status, completed, goal_id, workstream_id, owner, due_at, due_confidence, "
    "source, provenance_json, priority, sort_order, indent_level, recurrence_rule, recurrence_parent_id, "
    "created_at, updated_at, completed_at, superseded_by, conversation_id, is_locked, exported, export_date, "
    "export_platform, apple_reminder_id FROM cf_action_items "
)


def _workstream_detail_response(row: dict[str, object]) -> dict[str, object]:
    return {
        "workstream_id": str(row.get("id") or ""),
        "goal_id": row.get("goal_id"),
        "title": str(row.get("title") or ""),
        "objective": str(row.get("objective") or ""),
        "status": str(row.get("status") or "open"),
        "current_state_summary": str(row.get("current_state_summary") or ""),
        "next_review_at": _iso(row.get("next_review_at")),
        "last_meaningful_progress_at": _iso(row.get("last_meaningful_progress_at")),
        "latest_event_sequence": int(row.get("latest_event_sequence") or 0),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


async def _first_goal(env: object, uid: str, goal_id: str) -> dict[str, object] | None:
    row = await env.APP_DB.prepare(_SELECT + "WHERE uid = ? AND id = ?").bind(uid, goal_id).first()
    return row if isinstance(row, dict) else None


def _history_response(row: dict[str, object]) -> dict[str, object]:
    try:
        value = float(row.get("value", 0))
    except (TypeError, ValueError):
        value = 0.0
    return {
        "date": str(row.get("date") or ""),
        "value": value,
        "recorded_at": _iso(row.get("recorded_at")),
    }


def _event_response(row: dict[str, object]) -> dict[str, object]:
    try:
        sequence = int(row.get("sequence", 0))
    except (TypeError, ValueError):
        sequence = 0
    metric = _metric({"metric_json": row.get("metric_json")})
    return {
        "event_id": str(row.get("event_id") or ""),
        "goal_id": str(row.get("goal_id") or ""),
        "sequence": sequence,
        "kind": str(row.get("kind") or "evidence"),
        "summary": str(row.get("summary") or ""),
        "evidence_refs": _json_list(row.get("evidence_refs_json")),
        "metric": metric,
        "created_at": _iso(row.get("created_at")),
    }


def _query_days(request: Request) -> int | None:
    raw = getattr(request, "query_params", {}).get("days")
    if raw is None or raw == "":
        return 30
    try:
        days = int(str(raw))
    except (TypeError, ValueError):
        return None
    return days if 1 <= days <= 365 else None


def _query_limit(request: Request) -> int | None:
    raw = getattr(request, "query_params", {}).get("limit")
    if raw is None or raw == "":
        return 100
    try:
        limit = int(str(raw))
    except (TypeError, ValueError):
        return None
    return limit if 1 <= limit <= 500 else None


def _mutation_inputs(request: Request, payload: object) -> tuple[str, int, str] | None:
    key = request.headers.get("idempotency-key")
    raw_generation = request.headers.get("x-account-generation")
    if not key or len(key) > MAX_IDEMPOTENCY_KEY_LENGTH or not raw_generation:
        return None
    try:
        account_generation = int(raw_generation)
    except (TypeError, ValueError):
        return None
    if account_generation < 0:
        return None
    request_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return key, account_generation, request_hash


async def _load_mutation(
    env: object,
    uid: str,
    operation: str,
    key: str,
    account_generation: int,
    request_hash: str,
) -> tuple[dict[str, object] | None, JSONResponse | None]:
    row = (
        await env.APP_DB.prepare(
            "SELECT account_generation, request_hash, result_json FROM cf_goal_mutations "
            "WHERE uid = ? AND operation = ? AND idempotency_key = ?"
        )
        .bind(uid, operation, key)
        .first()
    )
    if not isinstance(row, dict):
        return None, None
    if row.get("account_generation") != account_generation or row.get("request_hash") != request_hash:
        return None, JSONResponse({"error": "idempotency key reused with different request"}, status_code=409)
    try:
        result = json.loads(str(row.get("result_json") or ""))
    except (TypeError, ValueError):
        return None, JSONResponse({"error": "goal mutation receipt unavailable"}, status_code=503)
    return result if isinstance(result, dict) else None, None


def _mutation_statement(
    env: object,
    uid: str,
    operation: str,
    key: str,
    account_generation: int,
    request_hash: str,
    result: dict[str, object],
    now: int,
) -> object:
    return env.APP_DB.prepare(
        "INSERT INTO cf_goal_mutations "
        "(uid, operation, idempotency_key, account_generation, request_hash, result_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)"
    ).bind(uid, operation, key, account_generation, request_hash, json.dumps(result, ensure_ascii=False), now)


@router.get("/v1/goals")
async def get_current_goal(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(
                _SELECT + "WHERE uid = ? AND is_active = 1 ORDER BY CASE WHEN status = 'focused' THEN 0 ELSE 1 END, "
                "CASE WHEN focus_rank IS NULL THEN 5 ELSE focus_rank END, created_at ASC LIMIT 100"
            )
            .bind(str(context["uid"]))
            .all()
        )
    except Exception:
        return JSONResponse({"error": "goals unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return _response(rows[0]) if rows and isinstance(rows[0], dict) else None


@router.get("/v1/goals/all")
async def list_goals(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    raw = getattr(request, "query_params", {}).get("include_ended")
    if raw is None or raw == "":
        include_ended = False
    elif str(raw).strip().lower() in {"true", "1", "yes"}:
        include_ended = True
    elif str(raw).strip().lower() in {"false", "0", "no"}:
        include_ended = False
    else:
        return JSONResponse({"error": "invalid include_ended"}, status_code=400)
    try:
        sql = _SELECT + "WHERE uid = " + ("?" if include_ended else "? AND is_active = 1") + " ORDER BY created_at DESC"
        result = await request.scope["env"].APP_DB.prepare(sql).bind(str(context["uid"])).all()
    except Exception:
        return JSONResponse({"error": "goals unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [_response(row) for row in rows if isinstance(row, dict)]


@router.get("/v1/goals/canonical/list")
async def list_canonical_goals(request: Request):
    """Generation-fenced clients share the same uid-scoped D1 projection."""

    return await list_goals(request)


@router.post("/v1/goals")
async def create_goal(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        goal = GoalCreate.model_validate(await _bounded_json(request))
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid goal"}, status_code=400)
    uid = str(context["uid"])
    now = int(time.time())
    goal_id = f"goal_{uuid.uuid4().hex[:12]}"
    metric = goal.metric.model_dump(mode="json") if goal.metric is not None else None
    try:
        await request.scope["env"].APP_DB.prepare(
            "INSERT INTO cf_goals (uid, id, title, desired_outcome, why_it_matters, success_criteria_json, horizon_at, "
            "status, focus_rank, metric_json, source, relationship_disposition, is_active, latest_progress_sequence, "
            "ended_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 'retain', ?, 0, NULL, ?, ?)"
        ).bind(
            uid,
            goal_id,
            goal.title,
            goal.desired_outcome,
            goal.why_it_matters,
            json.dumps(goal.success_criteria, ensure_ascii=False),
            _epoch(goal.horizon_at),
            goal.status.value,
            json.dumps(metric, ensure_ascii=False) if metric is not None else None,
            goal.source.value,
            0 if goal.status in {GoalStatus.achieved, GoalStatus.abandoned} else 1,
            now,
            now,
        ).run()
        row = await _first_goal(request.scope["env"], uid, goal_id)
    except Exception:
        return JSONResponse({"error": "goals unavailable"}, status_code=503)
    return _response(row) if row else JSONResponse({"error": "goal unavailable"}, status_code=503)


@router.post("/v1/goals/canonical")
async def create_canonical_goal(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        goal = GoalCreate.model_validate(await _bounded_json(request))
        payload = goal.model_dump(mode="json", exclude_none=True)
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid goal"}, status_code=400)
    mutation = _mutation_inputs(request, payload)
    if mutation is None:
        return JSONResponse({"error": "Idempotency-Key and X-Account-Generation are required"}, status_code=400)
    key, account_generation, request_hash = mutation
    env = request.scope["env"]
    uid = str(context["uid"])
    operation = "goal-create"
    raw_goal_id = f"{uid}\x1f{account_generation}\x1f{operation}\x1f{key}".encode("utf-8")
    goal_id = f"goal_{hashlib.sha256(raw_goal_id).hexdigest()[:12]}"
    try:
        stored, conflict = await _load_mutation(env, uid, operation, key, account_generation, request_hash)
        if conflict:
            return conflict
        if stored is not None:
            return stored
        now = int(time.time())
        metric = goal.metric.model_dump(mode="json") if goal.metric is not None else None
        is_active = 0 if goal.status in {GoalStatus.achieved, GoalStatus.abandoned} else 1
        result = _response(
            {
                "id": goal_id,
                "title": goal.title,
                "desired_outcome": goal.desired_outcome,
                "why_it_matters": goal.why_it_matters,
                "success_criteria_json": json.dumps(goal.success_criteria, ensure_ascii=False),
                "horizon_at": _epoch(goal.horizon_at),
                "status": goal.status.value,
                "focus_rank": None,
                "metric_json": json.dumps(metric, ensure_ascii=False) if metric is not None else None,
                "source": goal.source.value,
                "relationship_disposition": GoalRelationshipDisposition.retain.value,
                "is_active": is_active,
                "latest_progress_sequence": 0,
                "ended_at": None,
                "created_at": now,
                "updated_at": now,
            }
        )
        statements = [
            env.APP_DB.prepare(
                "INSERT INTO cf_goals (uid, id, title, desired_outcome, why_it_matters, success_criteria_json, horizon_at, "
                "status, focus_rank, metric_json, source, relationship_disposition, is_active, latest_progress_sequence, "
                "ended_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 'retain', ?, 0, NULL, ?, ?)"
            ).bind(
                uid,
                goal_id,
                goal.title,
                goal.desired_outcome,
                goal.why_it_matters,
                json.dumps(goal.success_criteria, ensure_ascii=False),
                _epoch(goal.horizon_at),
                goal.status.value,
                json.dumps(metric, ensure_ascii=False) if metric is not None else None,
                goal.source.value,
                is_active,
                now,
                now,
            ),
            _mutation_statement(env, uid, operation, key, account_generation, request_hash, result, now),
        ]
        await env.APP_DB.batch(statements)
    except Exception:
        return JSONResponse({"error": "goals unavailable"}, status_code=503)
    return result


@router.get("/v1/goals/{goal_id}/history")
async def get_goal_history(request: Request, goal_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not goal_id or len(goal_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid goal id"}, status_code=400)
    days = _query_days(request)
    if days is None:
        return JSONResponse({"error": "invalid days"}, status_code=400)
    env = request.scope["env"]
    uid = str(context["uid"])
    try:
        if await _first_goal(env, uid, goal_id) is None:
            return JSONResponse({"error": "goal not found"}, status_code=404)
        result = (
            await env.APP_DB.prepare(
                "SELECT date, value, recorded_at FROM cf_goal_progress_history "
                "WHERE uid = ? AND goal_id = ? ORDER BY date DESC LIMIT ?"
            )
            .bind(uid, goal_id, days)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "goals unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [_history_response(row) for row in rows if isinstance(row, dict)]


@router.post("/v1/goals/{goal_id}/progress-events")
async def append_goal_progress_event(request: Request, goal_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not goal_id or len(goal_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid goal id"}, status_code=400)
    try:
        event = GoalProgressEventCreate.model_validate(await _bounded_json(request))
        payload = event.model_dump(mode="json")
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid progress event"}, status_code=400)
    mutation = _mutation_inputs(request, payload)
    if mutation is None:
        return JSONResponse({"error": "Idempotency-Key and X-Account-Generation are required"}, status_code=400)
    key, account_generation, request_hash = mutation
    env = request.scope["env"]
    uid = str(context["uid"])
    operation = f"goal-progress-event:{goal_id}"
    event_id = f"gpe_{hashlib.sha256(f'{uid}:{account_generation}:{goal_id}:{key}'.encode()).hexdigest()[:32]}"
    try:
        stored, conflict = await _load_mutation(env, uid, operation, key, account_generation, request_hash)
        if conflict:
            return conflict
        if stored is not None:
            return stored
        target = await _first_goal(env, uid, goal_id)
        if target is None:
            return JSONResponse({"error": "goal not found"}, status_code=404)
        sequence = int(target.get("latest_progress_sequence") or 0) + 1
        now = int(time.time())
        event_row = {
            "event_id": event_id,
            "goal_id": goal_id,
            "sequence": sequence,
            "kind": event.kind.value,
            "summary": event.summary,
            "evidence_refs_json": json.dumps(
                [reference.model_dump(mode="json") for reference in event.evidence_refs], ensure_ascii=False
            ),
            "metric_json": (
                json.dumps(event.metric.model_dump(mode="json"), ensure_ascii=False)
                if event.metric is not None
                else None
            ),
            "created_at": now,
        }
        result = _event_response(event_row)
        patched_goal = dict(target)
        patched_goal["latest_progress_sequence"] = sequence
        patched_goal["updated_at"] = now
        if event.metric is not None:
            patched_goal["metric_json"] = event_row["metric_json"]
        goal_update = (
            env.APP_DB.prepare(
                "UPDATE cf_goals SET latest_progress_sequence = ?, metric_json = ?, updated_at = ? "
                "WHERE uid = ? AND id = ?"
            ).bind(sequence, patched_goal.get("metric_json"), now, uid, goal_id)
            if event.metric is not None
            else env.APP_DB.prepare(
                "UPDATE cf_goals SET latest_progress_sequence = ?, updated_at = ? WHERE uid = ? AND id = ?"
            ).bind(sequence, now, uid, goal_id)
        )
        statements = [
            env.APP_DB.prepare(
                "INSERT INTO cf_goal_progress_events "
                "(uid, event_id, goal_id, sequence, kind, summary, evidence_refs_json, metric_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ).bind(
                uid,
                event_id,
                goal_id,
                sequence,
                event_row["kind"],
                event_row["summary"],
                event_row["evidence_refs_json"],
                event_row["metric_json"],
                now,
            ),
            goal_update,
            _mutation_statement(env, uid, operation, key, account_generation, request_hash, result, now),
        ]
        await env.APP_DB.batch(statements)
    except Exception:
        return JSONResponse({"error": "goals unavailable"}, status_code=503)
    return result


@router.get("/v1/goals/{goal_id}/progress-events")
async def list_goal_progress_events(request: Request, goal_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not goal_id or len(goal_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid goal id"}, status_code=400)
    limit = _query_limit(request)
    if limit is None:
        return JSONResponse({"error": "invalid limit"}, status_code=400)
    env = request.scope["env"]
    uid = str(context["uid"])
    try:
        if await _first_goal(env, uid, goal_id) is None:
            return JSONResponse({"error": "goal not found"}, status_code=404)
        result = (
            await env.APP_DB.prepare(
                "SELECT event_id, goal_id, sequence, kind, summary, evidence_refs_json, metric_json, created_at "
                "FROM cf_goal_progress_events WHERE uid = ? AND goal_id = ? ORDER BY sequence DESC LIMIT ?"
            )
            .bind(uid, goal_id, limit)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "goals unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [_event_response(row) for row in rows if isinstance(row, dict)]


@router.post("/v1/goals/{goal_id}/focus")
async def focus_goal(request: Request, goal_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not goal_id or len(goal_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid goal id"}, status_code=400)
    try:
        update = FocusGoalUpdate.model_validate(await _bounded_json(request))
        payload = update.model_dump(mode="json")
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid focus request"}, status_code=400)
    mutation = _mutation_inputs(request, payload)
    if mutation is None:
        return JSONResponse({"error": "Idempotency-Key and X-Account-Generation are required"}, status_code=400)
    key, account_generation, request_hash = mutation
    env = request.scope["env"]
    uid = str(context["uid"])
    operation = f"goal-focus:{goal_id}"
    try:
        stored, conflict = await _load_mutation(env, uid, operation, key, account_generation, request_hash)
        if conflict:
            return conflict
        if stored is not None:
            return stored
        target = await _first_goal(env, uid, goal_id)
        if target is None:
            return JSONResponse({"error": "goal not found"}, status_code=404)
        if target.get("status") in {GoalStatus.achieved.value, GoalStatus.abandoned.value}:
            return JSONResponse({"error": "ended goals cannot be focused"}, status_code=409)
        focused_result = (
            await env.APP_DB.prepare("SELECT id, status, focus_rank FROM cf_goals WHERE uid = ? AND status = 'focused'")
            .bind(uid)
            .all()
        )
        focused_rows = [
            row
            for row in (focused_result.get("results", []) if isinstance(focused_result, dict) else [])
            if isinstance(row, dict)
        ]
        focused = [row for row in focused_rows if row.get("id") != goal_id]
        occupied = {int(row["focus_rank"]) for row in focused if isinstance(row.get("focus_rank"), (int, float))}
        replacement_id = update.replacement_goal_id
        if len(focused) >= FOCUS_CAP:
            if replacement_id is None:
                return JSONResponse({"error": "focus set is full; replacement_goal_id is required"}, status_code=409)
            replacement = next((row for row in focused if row.get("id") == replacement_id), None)
            if replacement is None:
                return JSONResponse({"error": "replacement_goal_id must name a focused goal"}, status_code=409)
            previous_rank = replacement.get("focus_rank")
            if isinstance(previous_rank, (int, float)):
                occupied.discard(int(previous_rank))
        if target.get("status") == GoalStatus.focused.value and update.focus_rank in {None, target.get("focus_rank")}:
            result = _response(target)
            now = int(time.time())
            await env.APP_DB.batch(
                [_mutation_statement(env, uid, operation, key, account_generation, request_hash, result, now)]
            )
            return result
        requested_rank = update.focus_rank
        if requested_rank is None:
            requested_rank = next((rank for rank in range(FOCUS_CAP) if rank not in occupied), 0)
        if requested_rank in occupied:
            return JSONResponse({"error": "focus_rank is already occupied"}, status_code=409)
        now = int(time.time())
        patched_target = dict(target)
        patched_target.update(
            {"status": GoalStatus.focused.value, "focus_rank": requested_rank, "is_active": 1, "updated_at": now}
        )
        result = _response(patched_target)
        statements = []
        if replacement_id is not None and len(focused) >= FOCUS_CAP:
            statements.append(
                env.APP_DB.prepare(
                    "UPDATE cf_goals SET status = 'background', focus_rank = NULL, updated_at = ? WHERE uid = ? AND id = ?"
                ).bind(now, uid, replacement_id)
            )
        statements.append(
            env.APP_DB.prepare(
                "UPDATE cf_goals SET status = 'focused', focus_rank = ?, is_active = 1, updated_at = ? "
                "WHERE uid = ? AND id = ?"
            ).bind(requested_rank, now, uid, goal_id)
        )
        statements.append(_mutation_statement(env, uid, operation, key, account_generation, request_hash, result, now))
        await env.APP_DB.batch(statements)
    except Exception:
        return JSONResponse({"error": "goals unavailable"}, status_code=503)
    return result


@router.delete("/v1/goals/{goal_id}/focus")
async def unfocus_goal(request: Request, goal_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not goal_id or len(goal_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid goal id"}, status_code=400)
    mutation = _mutation_inputs(request, {})
    if mutation is None:
        return JSONResponse({"error": "Idempotency-Key and X-Account-Generation are required"}, status_code=400)
    key, account_generation, request_hash = mutation
    env = request.scope["env"]
    uid = str(context["uid"])
    operation = f"goal-unfocus:{goal_id}"
    try:
        stored, conflict = await _load_mutation(env, uid, operation, key, account_generation, request_hash)
        if conflict:
            return conflict
        if stored is not None:
            return stored
        target = await _first_goal(env, uid, goal_id)
        if target is None:
            return JSONResponse({"error": "goal not found"}, status_code=404)
        now = int(time.time())
        patched_target = dict(target)
        if target.get("status") == GoalStatus.focused.value:
            patched_target.update({"status": GoalStatus.background.value, "focus_rank": None, "updated_at": now})
        result = _response(patched_target)
        statements = []
        if target.get("status") == GoalStatus.focused.value:
            statements.append(
                env.APP_DB.prepare(
                    "UPDATE cf_goals SET status = 'background', focus_rank = NULL, updated_at = ? WHERE uid = ? AND id = ?"
                ).bind(now, uid, goal_id)
            )
        statements.append(_mutation_statement(env, uid, operation, key, account_generation, request_hash, result, now))
        await env.APP_DB.batch(statements)
    except Exception:
        return JSONResponse({"error": "goals unavailable"}, status_code=503)
    return result


@router.post("/v1/goals/{goal_id}/lifecycle")
async def transition_goal_lifecycle(request: Request, goal_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not goal_id or len(goal_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid goal id"}, status_code=400)
    try:
        update = GoalLifecycleUpdate.model_validate(await _bounded_json(request))
        payload = update.model_dump(mode="json")
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid lifecycle request"}, status_code=400)
    mutation = _mutation_inputs(request, payload)
    if mutation is None:
        return JSONResponse({"error": "Idempotency-Key and X-Account-Generation are required"}, status_code=400)
    key, account_generation, request_hash = mutation
    if update.relationship_disposition == GoalRelationshipDisposition.detach:
        return JSONResponse({"error": "relationship detach requires the legacy workstream authority"}, status_code=409)
    env = request.scope["env"]
    uid = str(context["uid"])
    operation = f"goal-lifecycle:{goal_id}"
    try:
        stored, conflict = await _load_mutation(env, uid, operation, key, account_generation, request_hash)
        if conflict:
            return conflict
        if stored is not None:
            return stored
        target = await _first_goal(env, uid, goal_id)
        if target is None:
            return JSONResponse({"error": "goal not found"}, status_code=404)
        now = int(time.time())
        terminal = update.status in {GoalStatus.achieved, GoalStatus.abandoned}
        patched_target = dict(target)
        patched_target.update(
            {
                "status": update.status.value,
                "focus_rank": None,
                "is_active": 0 if terminal else 1,
                "relationship_disposition": GoalRelationshipDisposition.retain.value,
                "updated_at": now,
                "ended_at": now if terminal else None,
            }
        )
        result = _response(patched_target)
        statements = [
            env.APP_DB.prepare(
                "UPDATE cf_goals SET status = ?, focus_rank = NULL, is_active = ?, relationship_disposition = 'retain', "
                "updated_at = ?, ended_at = ? WHERE uid = ? AND id = ?"
            ).bind(update.status.value, 0 if terminal else 1, now, now if terminal else None, uid, goal_id),
            _mutation_statement(env, uid, operation, key, account_generation, request_hash, result, now),
        ]
        await env.APP_DB.batch(statements)
    except Exception:
        return JSONResponse({"error": "goals unavailable"}, status_code=503)
    return result


@router.get("/v1/goals/{goal_id}")
async def get_goal(request: Request, goal_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not goal_id or len(goal_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid goal id"}, status_code=400)
    try:
        row = await _first_goal(request.scope["env"], str(context["uid"]), goal_id)
    except Exception:
        return JSONResponse({"error": "goals unavailable"}, status_code=503)
    return _response(row) if row else JSONResponse({"error": "goal not found"}, status_code=404)


@router.get("/v1/goals/{goal_id}/detail")
async def get_goal_detail(request: Request, goal_id: str):
    """Return the bounded D1 projection consumed by canonical goal clients."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not goal_id or len(goal_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid goal id"}, status_code=400)
    env = request.scope["env"]
    uid = str(context["uid"])
    try:
        goal = await _first_goal(env, uid, goal_id)
        if goal is None:
            return JSONResponse({"error": "goal not found"}, status_code=404)
        workstream_result = await (
            env.APP_DB.prepare(
                _DETAIL_WORKSTREAM_SELECT
                + "WHERE uid = ? AND goal_id = ? AND status != 'archived' ORDER BY updated_at DESC LIMIT 100"
            )
            .bind(uid, goal_id)
            .all()
        )
        task_result = await (
            env.APP_DB.prepare(
                _DETAIL_TASK_SELECT + "WHERE uid = ? AND goal_id = ? AND deleted = 0 ORDER BY created_at ASC LIMIT 500"
            )
            .bind(uid, goal_id)
            .all()
        )
        event_result = await (
            env.APP_DB.prepare(
                "SELECT event_id, goal_id, sequence, kind, summary, evidence_refs_json, metric_json, created_at "
                "FROM cf_goal_progress_events WHERE uid = ? AND goal_id = ? ORDER BY sequence DESC LIMIT 100"
            )
            .bind(uid, goal_id)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "goal detail unavailable"}, status_code=503)
    workstream_rows = workstream_result.get("results", []) if isinstance(workstream_result, dict) else []
    task_rows = task_result.get("results", []) if isinstance(task_result, dict) else []
    event_rows = event_result.get("results", []) if isinstance(event_result, dict) else []
    return {
        "goal": _response(goal),
        "active_threads": [_workstream_detail_response(row) for row in workstream_rows if isinstance(row, dict)],
        "tasks": [_action_item_response(row) for row in task_rows if isinstance(row, dict)],
        "progress_events": [_event_response(row) for row in event_rows if isinstance(row, dict)],
    }


def _update_values(update: GoalUpdate, existing: dict[str, object]) -> dict[str, object]:
    values = update.model_dump(exclude_unset=True)
    values.pop("clear_metric", None)
    if update.clear_metric:
        values["metric_json"] = None
    elif "metric" in values:
        values["metric_json"] = (
            json.dumps(update.metric.model_dump(mode="json"), ensure_ascii=False) if update.metric is not None else None
        )
    elif any(key in values for key in ("goal_type", "current_value", "target_value", "min_value", "max_value", "unit")):
        current_metric = _metric(existing) or {
            "type": GoalType.scale.value,
            "current": 0.0,
            "target": 0.0,
            "min": None,
            "max": None,
            "unit": None,
        }
        metric = GoalMetric(
            type=values.get("goal_type", current_metric.get("type", GoalType.scale.value)),
            current=values.get("current_value", current_metric.get("current", 0.0)),
            target=values.get("target_value", current_metric.get("target", 0.0)),
            min=values.get("min_value", current_metric.get("min")),
            max=values.get("max_value", current_metric.get("max")),
            unit=values.get("unit", current_metric.get("unit")),
        )
        values["metric_json"] = json.dumps(metric.model_dump(mode="json"), ensure_ascii=False)
    values.pop("metric", None)
    for field_name in ("goal_type", "current_value", "target_value", "min_value", "max_value", "unit"):
        values.pop(field_name, None)
    if "success_criteria" in values:
        values["success_criteria_json"] = json.dumps(values.pop("success_criteria"), ensure_ascii=False)
    if "horizon_at" in values:
        values["horizon_at"] = _epoch(update.horizon_at)
    return values


@router.patch("/v1/goals/{goal_id}")
async def update_goal(request: Request, goal_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        update = GoalUpdate.model_validate(await _bounded_json(request))
        env = request.scope["env"]
        uid = str(context["uid"])
        existing = await _first_goal(env, uid, goal_id)
        if existing is None:
            return JSONResponse({"error": "goal not found"}, status_code=404)
        values = _update_values(update, existing)
        if not values:
            return JSONResponse({"error": "no goal updates"}, status_code=400)
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
            *values.values(), uid, goal_id
        ).run()
        row = await _first_goal(env, uid, goal_id)
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid goal update"}, status_code=400)
    except Exception:
        return JSONResponse({"error": "goals unavailable"}, status_code=503)
    return _response(row) if row else JSONResponse({"error": "goal not found"}, status_code=404)


def _query_float(request: Request, name: str) -> float | None:
    raw = getattr(request, "query_params", {}).get(name)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


@router.patch("/v1/goals/{goal_id}/progress")
async def update_goal_progress(request: Request, goal_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    current_value = _query_float(request, "current_value")
    if current_value is None:
        return JSONResponse({"error": "current_value is required"}, status_code=400)
    env = request.scope["env"]
    uid = str(context["uid"])
    try:
        existing = await _first_goal(env, uid, goal_id)
        if existing is None:
            return JSONResponse({"error": "goal not found"}, status_code=404)
        metric = _metric(existing) or {
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
        event_id = f"gpe_{uuid.uuid4().hex}"
        metric_json = json.dumps(metric, ensure_ascii=False)
        event_insert = env.APP_DB.prepare(
            "INSERT INTO cf_goal_progress_events "
            "(uid, event_id, goal_id, sequence, kind, summary, evidence_refs_json, metric_json, created_at) "
            "VALUES (?, ?, ?, ?, 'metric_update', ?, '[]', ?, ?)"
        ).bind(uid, event_id, goal_id, sequence, "Metric updated", metric_json, now)
        goal_update = env.APP_DB.prepare(
            "UPDATE cf_goals SET metric_json = ?, latest_progress_sequence = ?, updated_at = ? WHERE uid = ? AND id = ?"
        ).bind(metric_json, sequence, now, uid, goal_id)
        history_upsert = env.APP_DB.prepare(
            "INSERT INTO cf_goal_progress_history (uid, goal_id, date, value, recorded_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(uid, goal_id, date) DO UPDATE SET value = excluded.value, recorded_at = excluded.recorded_at"
        ).bind(uid, goal_id, today, current_value, now)
        await env.APP_DB.batch([event_insert, goal_update, history_upsert])
        row = await _first_goal(env, uid, goal_id)
    except Exception:
        return JSONResponse({"error": "goals unavailable"}, status_code=503)
    return _response(row) if row else JSONResponse({"error": "goal not found"}, status_code=404)


@router.delete("/v1/goals/{goal_id}")
async def delete_goal(request: Request, goal_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    uid = str(context["uid"])
    try:
        existing = await _first_goal(env, uid, goal_id)
        if existing is None:
            return JSONResponse({"error": "goal not found"}, status_code=404)
        now = int(time.time())
        await env.APP_DB.prepare(
            "UPDATE cf_goals SET status = 'abandoned', focus_rank = NULL, is_active = 0, ended_at = ?, updated_at = ? "
            "WHERE uid = ? AND id = ?"
        ).bind(now, now, uid, goal_id).run()
    except Exception:
        return JSONResponse({"error": "goals unavailable"}, status_code=503)
    return {"success": True, "deleted_id": goal_id}
