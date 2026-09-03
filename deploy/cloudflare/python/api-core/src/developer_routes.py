"""D1-backed Developer API reads authenticated by ``omi_dev_`` credentials."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from action_item_routes import _response as action_item_response
from conversation_routes import _CONVERSATION_SELECT, _response as conversation_response
from folder_routes import _SELECT as FOLDER_SELECT, _response as folder_response
from goal_routes import (
    _SELECT as GOAL_SELECT,
    _history_response as goal_history_response,
    _response as goal_response,
)
from memory_routes import _SELECT as MEMORY_SELECT, _response as memory_response
from vector_search import embed_query, hydrate_candidate_ids, query_vector_ids

router = APIRouter()

MAX_ID_LENGTH = 256
MAX_JSON_BYTES = 4_096
MAX_OFFSET = 1_000_000
DEVELOPER_KEY_PATTERN = re.compile(r"Bearer omi_dev_([0-9a-f]{32})")
DEVELOPER_SCOPES = frozenset(
    {
        "conversations:read",
        "conversations:write",
        "memories:read",
        "memories:write",
        "action_items:read",
        "action_items:write",
        "goals:read",
        "goals:write",
    }
)
MEMORY_CATEGORIES = frozenset({"interesting", "system", "manual"})
CONVERSATION_CATEGORIES = frozenset(
    {
        "personal",
        "education",
        "health",
        "finance",
        "legal",
        "philosophy",
        "spiritual",
        "science",
        "entrepreneurship",
        "parenting",
        "romantic",
        "travel",
        "inspiration",
        "technology",
        "business",
        "social",
        "work",
        "sports",
        "politics",
        "literature",
        "history",
        "architecture",
        "music",
        "weather",
        "news",
        "entertainment",
        "psychology",
        "real",
        "design",
        "family",
        "economics",
        "environment",
        "other",
    }
)


@dataclass(frozen=True)
class DeveloperPrincipal:
    uid: str
    key_id: str
    scopes: frozenset[str]


def _detail(detail: object, status: int) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def _error(error: str, status: int) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status)


def _bool(value: object) -> bool:
    return bool(value) and value not in ("0", "false", "False", "no")


def _query_bool(request: Request, name: str, default: bool | None = None) -> bool | None | JSONResponse:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    normalized = str(raw).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return _detail(f"Invalid {name}", 400)


def _query_int(request: Request, name: str, default: int, minimum: int, maximum: int) -> int | JSONResponse:
    try:
        value = int(request.query_params.get(name, str(default)))
    except (TypeError, ValueError):
        return _detail("Invalid pagination", 400)
    return max(minimum, min(value, maximum))


def _parse_datetime(value: str | None, field: str) -> int | None | JSONResponse:
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return _detail(f"Invalid {field}", 400)
    aware = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return int(aware.astimezone(timezone.utc).timestamp())


async def _authenticate(request: Request, required_scope: str) -> tuple[DeveloperPrincipal | None, JSONResponse | None]:
    authorization = request.headers.get("authorization")
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        return None, _detail("Missing or invalid Authorization header. Must be 'Bearer API_KEY'", 401)
    match = DEVELOPER_KEY_PATTERN.fullmatch(authorization)
    if match is None:
        return None, _detail("Invalid Developer API key", 403)

    secret = match.group(1)
    digest = hashlib.sha256(secret.encode()).hexdigest()
    expected_prefix = f"omi_dev_{secret[:4]}...{secret[-4:]}"
    env = request.scope["env"]
    try:
        row = (
            await env.APP_DB.prepare(
                "SELECT uid, key_id, key_hash, key_prefix, app_id, scopes_json, created_at "
                "FROM cf_developer_api_keys WHERE key_hash = ? LIMIT 1"
            )
            .bind(digest)
            .first()
        )
        if not isinstance(row, dict):
            return None, _detail("Invalid Developer API key", 403)
        uid = row.get("uid")
        key_id = row.get("key_id")
        if (
            not isinstance(uid, str)
            or not uid
            or len(uid) > MAX_ID_LENGTH
            or not isinstance(key_id, str)
            or not key_id
            or len(key_id) > 64
            or row.get("key_hash") != digest
            or row.get("app_id") != "developer_api"
            or row.get("key_prefix") not in {expected_prefix, "omi_dev_legacy"}
        ):
            return None, _error("developer authentication unavailable", 503)
        raw_scopes = row.get("scopes_json")
        if not isinstance(raw_scopes, str) or len(raw_scopes.encode("utf-8")) > MAX_JSON_BYTES:
            return None, _error("developer authentication unavailable", 503)
        parsed_scopes = json.loads(raw_scopes)
        if (
            not isinstance(parsed_scopes, list)
            or len(parsed_scopes) > len(DEVELOPER_SCOPES)
            or any(not isinstance(scope, str) or scope not in DEVELOPER_SCOPES for scope in parsed_scopes)
        ):
            return None, _error("developer authentication unavailable", 503)
        created_at = row.get("created_at")
        if not isinstance(created_at, int) or isinstance(created_at, bool) or created_at < 0:
            return None, _error("developer authentication unavailable", 503)
        principal = DeveloperPrincipal(uid=uid, key_id=key_id, scopes=frozenset(parsed_scopes))
        if required_scope not in principal.scopes:
            return None, _detail(f"Insufficient permissions. Required scope: {required_scope}", 403)

        now = int(time.time())
        deletion = (
            await env.APP_DB.prepare(
                "SELECT lifecycle FROM ("
                "SELECT 'deleting' AS lifecycle, 0 AS priority FROM cf_account_deletion_intents WHERE uid = ? "
                "UNION ALL SELECT 'deleted' AS lifecycle, 1 AS priority "
                "FROM cf_account_deletion_tombstones WHERE uid = ? AND expires_at > ?"
                ") ORDER BY priority LIMIT 1"
            )
            .bind(uid, uid, now)
            .first()
        )
        if deletion is not None:
            return None, _error("account data plane not active", 409)
        cutover = (
            await env.APP_DB.prepare(
                "SELECT state, checkpoint_phase, destination_backend_bound FROM cf_account_cutover WHERE uid = ?"
            )
            .bind(uid)
            .first()
        )
        if (
            not isinstance(cutover, dict)
            or cutover.get("state") != "new"
            or cutover.get("checkpoint_phase") != "completed"
            or not _bool(cutover.get("destination_backend_bound"))
        ):
            return None, _error("account data plane not active", 409)
        await env.APP_DB.prepare("UPDATE cf_developer_api_keys SET last_used_at = ? WHERE key_id = ? AND uid = ?").bind(
            max(created_at, now), key_id, uid
        ).run()
        return principal, None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, _error("developer authentication unavailable", 503)
    except Exception:
        return None, _error("developer authentication unavailable", 503)


def _developer_memory(row: dict[str, object]) -> dict[str, object]:
    response = memory_response(row)
    return {
        key: response[key]
        for key in (
            "id",
            "content",
            "category",
            "visibility",
            "tags",
            "created_at",
            "updated_at",
            "manually_added",
            "reviewed",
            "user_review",
            "edited",
            "scoring",
        )
    }


def _developer_action_item(row: dict[str, object]) -> dict[str, object]:
    response = action_item_response(row)
    return {
        key: response[key]
        for key in (
            "id",
            "description",
            "completed",
            "created_at",
            "updated_at",
            "due_at",
            "completed_at",
            "conversation_id",
        )
    }


def _developer_folder(row: dict[str, object]) -> dict[str, object]:
    response = folder_response(row)
    return {
        key: response[key]
        for key in (
            "id",
            "name",
            "description",
            "color",
            "icon",
            "created_at",
            "updated_at",
            "order",
            "is_default",
            "is_system",
            "conversation_count",
        )
    }


def _developer_conversation(row: dict[str, object], *, include_transcript: bool) -> dict[str, object]:
    response = conversation_response(row, detail=include_transcript)
    structured = response.get("structured") if isinstance(response.get("structured"), dict) else {}
    projection: dict[str, object] = {
        "id": response["id"],
        "created_at": response["created_at"],
        "started_at": response["started_at"],
        "finished_at": response["finished_at"],
        "structured": {
            "title": str(structured.get("title") or ""),
            "overview": str(structured.get("overview") or ""),
            "emoji": str(structured.get("emoji") or "🧠"),
            "category": str(structured.get("category") or "other"),
            "action_items": structured.get("action_items") if isinstance(structured.get("action_items"), list) else [],
            "events": structured.get("events") if isinstance(structured.get("events"), list) else [],
        },
        "language": response.get("language"),
        "source": response.get("source"),
        "geolocation": response.get("geolocation"),
        "folder_id": response.get("folder_id"),
        "folder_name": row.get("folder_name"),
    }
    if include_transcript:
        segments = response.get("transcript_segments")
        projection["transcript_segments"] = [
            {
                "id": segment.get("id"),
                "text": str(segment.get("text") or ""),
                "speaker_id": segment.get("speaker_id"),
                "speaker_name": segment.get("speaker_name"),
                "start": float(segment.get("start") or 0),
                "end": float(segment.get("end") or 0),
            }
            for segment in (segments if isinstance(segments, list) else [])
            if isinstance(segment, dict)
        ]
    else:
        projection["transcript_segments"] = None
    return projection


def _developer_goal(row: dict[str, object]) -> dict[str, object]:
    response = goal_response(row)
    return {
        key: response[key]
        for key in (
            "id",
            "goal_id",
            "title",
            "desired_outcome",
            "why_it_matters",
            "success_criteria",
            "horizon_at",
            "status",
            "focus_rank",
            "metric",
            "source",
            "goal_type",
            "target_value",
            "current_value",
            "min_value",
            "max_value",
            "unit",
            "is_active",
            "created_at",
            "updated_at",
        )
    }


async def _folder_names(env: object, uid: str, rows: list[dict[str, object]]) -> None:
    folder_ids = list(
        dict.fromkeys(
            str(row["folder_id"]) for row in rows if isinstance(row.get("folder_id"), str) and row.get("folder_id")
        )
    )
    if not folder_ids:
        return
    placeholders = ",".join("?" for _ in folder_ids)
    result = (
        await env.APP_DB.prepare(f"SELECT id, name FROM cf_folders WHERE uid = ? AND id IN ({placeholders})")
        .bind(uid, *folder_ids)
        .all()
    )
    values = result.get("results", []) if isinstance(result, dict) else []
    names = {
        str(row["id"]): str(row.get("name") or "")
        for row in values
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    for row in rows:
        row["folder_name"] = names.get(str(row.get("folder_id") or ""))


@router.get("/v1/dev/user/memories")
async def list_developer_memories(request: Request):
    principal, denial = await _authenticate(request, "memories:read")
    if denial is not None:
        return denial
    assert principal is not None
    limit = _query_int(request, "limit", 25, 1, 1_000)
    offset = _query_int(request, "offset", 0, 0, MAX_OFFSET)
    if isinstance(limit, JSONResponse) or isinstance(offset, JSONResponse):
        return limit if isinstance(limit, JSONResponse) else offset
    categories_raw = request.query_params.get("categories")
    categories = [item.strip() for item in (categories_raw or "").split(",") if item.strip()]
    if any(category not in MEMORY_CATEGORIES for category in categories):
        return _detail("Invalid memory category", 400)
    clauses = [
        "uid = ?",
        "deleted_at IS NULL",
        "invalid_at IS NULL",
        "memory_tier != 'archive'",
        "COALESCE(user_review, 1) != 0",
        "is_locked = 0",
    ]
    args: list[object] = [principal.uid]
    if categories:
        clauses.append("category IN (" + ",".join("?" for _ in categories) + ")")
        args.extend(categories)
    query = MEMORY_SELECT + "WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
    args.extend((limit, offset))
    try:
        result = await request.scope["env"].APP_DB.prepare(query).bind(*args).all()
    except Exception:
        return _error("memories unavailable", 503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [_developer_memory(row) for row in rows if isinstance(row, dict)]


async def _memory_rows_for_ids(env: object, uid: str, ids: list[str]) -> list[dict[str, object]]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    result = (
        await env.APP_DB.prepare(
            MEMORY_SELECT + f"WHERE uid = ? AND id IN ({placeholders}) AND deleted_at IS NULL AND invalid_at IS NULL "
            "AND memory_tier != 'archive' AND COALESCE(user_review, 1) != 0 AND is_locked = 0"
        )
        .bind(uid, *ids)
        .all()
    )
    rows = result.get("results", []) if isinstance(result, dict) else []
    by_id = {str(row.get("id")): row for row in rows if isinstance(row, dict)}
    return [by_id[item_id] for item_id in ids if item_id in by_id]


@router.get("/v1/dev/user/memories/vector/search")
async def search_developer_memories(request: Request):
    principal, denial = await _authenticate(request, "memories:read")
    if denial is not None:
        return denial
    assert principal is not None
    query = request.query_params.get("query")
    limit = _query_int(request, "limit", 10, 1, 100)
    if isinstance(limit, JSONResponse):
        return limit
    result_limit = min(limit, 20)
    env = request.scope["env"]
    try:
        vector = await embed_query(env, query if isinstance(query, str) else "")
        matches = await query_vector_ids(
            env,
            "MEMORY_VECTORS",
            principal.uid,
            vector,
            top_k=min(result_limit * 3, 100),
        )
        candidates = await hydrate_candidate_ids(env, principal.uid, "memory", matches)
        rows = await _memory_rows_for_ids(env, principal.uid, [source_id for source_id, _ in candidates])
    except ValueError as error:
        return _detail(str(error), 422)
    except Exception:
        return _error("memory search unavailable", 503)
    score_by_id = dict(candidates)
    items = [
        {
            "id": str(row.get("id") or ""),
            "content": str(row.get("content") or ""),
            "category": str(row.get("category") or "interesting"),
            "relevance_score": round(float(score_by_id.get(str(row.get("id") or ""), 0.0)), 4),
        }
        for row in rows
    ][:result_limit]
    return {
        "items": items,
        "returned_count": len(items),
        "archive_default_visible": False,
        "policy": {
            "consumer": "developer_api",
            "app_has_default_memory_grant": True,
            "archive_capability": False,
            "raw_provenance_capability": False,
        },
    }


@router.get("/v1/dev/user/action-items")
async def list_developer_action_items(request: Request):
    principal, denial = await _authenticate(request, "action_items:read")
    if denial is not None:
        return denial
    assert principal is not None
    limit = _query_int(request, "limit", 100, 1, 1_000)
    offset = _query_int(request, "offset", 0, 0, MAX_OFFSET)
    if isinstance(limit, JSONResponse) or isinstance(offset, JSONResponse):
        return limit if isinstance(limit, JSONResponse) else offset
    clauses = ["uid = ?", "deleted = 0", "is_locked = 0"]
    args: list[object] = [principal.uid]
    completed = _query_bool(request, "completed")
    if isinstance(completed, JSONResponse):
        return completed
    if completed is not None:
        clauses.append("completed = ?")
        args.append(1 if completed else 0)
    conversation_id = request.query_params.get("conversation_id")
    if conversation_id:
        if len(conversation_id) > MAX_ID_LENGTH:
            return _detail("conversation_id is too long", 400)
        clauses.append("conversation_id = ?")
        args.append(conversation_id)
    bounds: dict[str, int] = {}
    for name, operator in (("start_date", ">="), ("end_date", "<=")):
        parsed = _parse_datetime(request.query_params.get(name), name)
        if isinstance(parsed, JSONResponse):
            return parsed
        if parsed is not None:
            bounds[name] = parsed
            clauses.append(f"created_at {operator} ?")
            args.append(parsed)
    if bounds.get("start_date", 0) > bounds.get("end_date", bounds.get("start_date", 0)):
        return _detail("start_date must be earlier than or equal to end_date", 400)
    sql = (
        "SELECT id, description, status, completed, goal_id, workstream_id, owner, due_at, due_confidence, "
        "source, provenance_json, priority, sort_order, indent_level, recurrence_rule, recurrence_parent_id, "
        "created_at, updated_at, completed_at, superseded_by, conversation_id, is_locked, exported, export_date, "
        "export_platform, apple_reminder_id FROM cf_action_items WHERE "
        + " AND ".join(clauses)
        + " ORDER BY completed ASC, CASE WHEN due_at IS NULL THEN 1 ELSE 0 END ASC, due_at ASC, created_at DESC "
        "LIMIT ? OFFSET ?"
    )
    args.extend((limit, offset))
    try:
        result = await request.scope["env"].APP_DB.prepare(sql).bind(*args).all()
    except Exception:
        return _error("action items unavailable", 503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [_developer_action_item(row) for row in rows if isinstance(row, dict)]


@router.get("/v1/dev/user/folders")
async def list_developer_folders(request: Request):
    principal, denial = await _authenticate(request, "conversations:read")
    if denial is not None:
        return denial
    assert principal is not None
    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(FOLDER_SELECT + "WHERE uid = ? ORDER BY display_order ASC, created_at ASC, id ASC")
            .bind(principal.uid)
            .all()
        )
    except Exception:
        return _error("folders unavailable", 503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [_developer_folder(row) for row in rows if isinstance(row, dict)]


@router.get("/v1/dev/user/conversations")
async def list_developer_conversations(request: Request):
    principal, denial = await _authenticate(request, "conversations:read")
    if denial is not None:
        return denial
    assert principal is not None
    include_transcript = _query_bool(request, "include_transcript", False)
    starred = _query_bool(request, "starred")
    if isinstance(include_transcript, JSONResponse) or isinstance(starred, JSONResponse):
        return include_transcript if isinstance(include_transcript, JSONResponse) else starred
    limit = _query_int(request, "limit", 25, 1, 25 if include_transcript else 100)
    offset = _query_int(request, "offset", 0, 0, MAX_OFFSET)
    if isinstance(limit, JSONResponse) or isinstance(offset, JSONResponse):
        return limit if isinstance(limit, JSONResponse) else offset
    categories_raw = request.query_params.get("categories")
    categories = [item.strip() for item in (categories_raw or "").split(",") if item.strip()]
    if any(category not in CONVERSATION_CATEGORIES for category in categories):
        return _detail("Invalid conversation category", 400)
    clauses = ["uid = ?", "discarded = 0", "status = 'completed'", "is_locked = 0"]
    args: list[object] = [principal.uid]
    if categories:
        clauses.append("json_extract(structured_json, '$.category') IN (" + ",".join("?" for _ in categories) + ")")
        args.extend(categories)
    folder_id = request.query_params.get("folder_id")
    if folder_id is not None:
        if not folder_id or len(folder_id) > MAX_ID_LENGTH:
            return _detail("Invalid folder_id", 400)
        clauses.append("folder_id = ?")
        args.append(folder_id)
    if starred is not None:
        clauses.append("starred = ?")
        args.append(1 if starred else 0)
    bounds: dict[str, int] = {}
    for name, operator in (("start_date", ">="), ("end_date", "<=")):
        parsed = _parse_datetime(request.query_params.get(name), name)
        if isinstance(parsed, JSONResponse):
            return parsed
        if parsed is not None:
            bounds[name] = parsed
            clauses.append(f"created_at {operator} ?")
            args.append(parsed)
    if bounds.get("start_date", 0) > bounds.get("end_date", bounds.get("start_date", 0)):
        return _detail("start_date must be earlier than or equal to end_date", 400)
    query = (
        _CONVERSATION_SELECT + "WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    )
    args.extend((limit, offset))
    try:
        env = request.scope["env"]
        result = await env.APP_DB.prepare(query).bind(*args).all()
        rows = [row for row in (result.get("results", []) if isinstance(result, dict) else []) if isinstance(row, dict)]
        await _folder_names(env, principal.uid, rows)
    except Exception:
        return _error("conversations unavailable", 503)
    return [_developer_conversation(row, include_transcript=bool(include_transcript)) for row in rows]


@router.get("/v1/dev/user/conversations/{conversation_id}")
async def get_developer_conversation(request: Request, conversation_id: str):
    principal, denial = await _authenticate(request, "conversations:read")
    if denial is not None:
        return denial
    assert principal is not None
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return _detail("Invalid conversation id", 400)
    include_transcript = _query_bool(request, "include_transcript", False)
    if isinstance(include_transcript, JSONResponse):
        return include_transcript
    try:
        env = request.scope["env"]
        row = (
            await env.APP_DB.prepare(
                _CONVERSATION_SELECT
                + "WHERE uid = ? AND id = ? AND discarded = 0 AND status = 'completed' AND is_locked = 0"
            )
            .bind(principal.uid, conversation_id)
            .first()
        )
        rows = [row] if isinstance(row, dict) else []
        await _folder_names(env, principal.uid, rows)
    except Exception:
        return _error("conversations unavailable", 503)
    if not rows:
        return _detail("Conversation not found", 404)
    return _developer_conversation(rows[0], include_transcript=bool(include_transcript))


@router.get("/v1/dev/user/goals")
async def list_developer_goals(request: Request):
    principal, denial = await _authenticate(request, "goals:read")
    if denial is not None:
        return denial
    assert principal is not None
    limit = _query_int(request, "limit", 10, 1, 1_000)
    include_inactive = _query_bool(request, "include_inactive", False)
    if isinstance(limit, JSONResponse) or isinstance(include_inactive, JSONResponse):
        return limit if isinstance(limit, JSONResponse) else include_inactive
    query = GOAL_SELECT + "WHERE uid = ?"
    if not include_inactive:
        query += " AND is_active = 1"
    query += " ORDER BY created_at DESC, id ASC LIMIT ?"
    try:
        result = await request.scope["env"].APP_DB.prepare(query).bind(principal.uid, limit).all()
    except Exception:
        return _error("goals unavailable", 503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [_developer_goal(row) for row in rows if isinstance(row, dict)]


@router.get("/v1/dev/user/goals/{goal_id}")
async def get_developer_goal(request: Request, goal_id: str):
    principal, denial = await _authenticate(request, "goals:read")
    if denial is not None:
        return denial
    assert principal is not None
    if not goal_id or len(goal_id) > MAX_ID_LENGTH:
        return _detail("Invalid goal id", 400)
    try:
        row = (
            await request.scope["env"]
            .APP_DB.prepare(GOAL_SELECT + "WHERE uid = ? AND id = ?")
            .bind(principal.uid, goal_id)
            .first()
        )
    except Exception:
        return _error("goals unavailable", 503)
    if not isinstance(row, dict):
        return _detail("Goal not found", 404)
    return _developer_goal(row)


@router.get("/v1/dev/user/goals/{goal_id}/history")
async def get_developer_goal_history(request: Request, goal_id: str):
    principal, denial = await _authenticate(request, "goals:read")
    if denial is not None:
        return denial
    assert principal is not None
    if not goal_id or len(goal_id) > MAX_ID_LENGTH:
        return _detail("Invalid goal id", 400)
    days = _query_int(request, "days", 30, 1, 365)
    if isinstance(days, JSONResponse):
        return days
    try:
        goal = (
            await request.scope["env"]
            .APP_DB.prepare("SELECT 1 AS present FROM cf_goals WHERE uid = ? AND id = ?")
            .bind(principal.uid, goal_id)
            .first()
        )
        if not isinstance(goal, dict):
            return _detail("Goal not found", 404)
        result = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT date, value, recorded_at FROM cf_goal_progress_history "
                "WHERE uid = ? AND goal_id = ? ORDER BY date DESC LIMIT ?"
            )
            .bind(principal.uid, goal_id, days)
            .all()
        )
    except Exception:
        return _error("goals unavailable", 503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [goal_history_response(row) for row in rows if isinstance(row, dict)]
