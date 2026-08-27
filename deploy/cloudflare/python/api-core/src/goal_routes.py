"""D1-backed goal metadata routes for the isolated Cloudflare profile.

This first goal slice owns the durable metadata and metric projection used by
the released clients. Focus-cap transactions, relationship lifecycle events,
progress history/events, and AI advice/suggestion routes remain on the legacy
owner until their stronger workflow contracts are migrated.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from internal_auth import decode_context

router = APIRouter()

MAX_REQUEST_BYTES = 100_000
MAX_ID_LENGTH = 256


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


async def _first_goal(env: object, uid: str, goal_id: str) -> dict[str, object] | None:
    row = await env.APP_DB.prepare(_SELECT + "WHERE uid = ? AND id = ?").bind(uid, goal_id).first()
    return row if isinstance(row, dict) else None


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
        await env.APP_DB.prepare("UPDATE cf_goals SET metric_json = ?, updated_at = ? WHERE uid = ? AND id = ?").bind(
            json.dumps(metric, ensure_ascii=False), int(time.time()), uid, goal_id
        ).run()
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
