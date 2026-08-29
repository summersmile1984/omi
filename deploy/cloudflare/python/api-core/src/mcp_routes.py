"""D1-backed MCP REST data tools for isolated Cloudflare accounts.

These routes authenticate the opaque ``omi_mcp_`` bearer directly against D1.
They never accept a caller-provided uid, never fall back to Firebase, and keep
the same scope names used by the hosted MCP transport. Semantic search and
OAuth transport routes intentionally remain outside this module until their
Vectorize/token lifecycles migrate as one boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from account_routes import usage_source_statement
from action_item_routes import _response as action_item_response
from conversation_routes import (
    MAX_JSON_BYTES as MAX_CONVERSATION_JSON_BYTES,
    _CONVERSATION_SELECT,
    _json_list as conversation_json_list,
    _json_object as conversation_json_object,
    _person_names,
)
from daily_summary_routes import _SELECT as DAILY_SUMMARY_SELECT
from daily_summary_routes import _response as daily_summary_response
from fallback import record_fallback
from goal_routes import _SELECT as GOAL_SELECT
from goal_routes import _response as goal_response
from integration_routes import _json_schema, _workers_ai_json
from internal_auth import create_request_context
from memory_routes import MemoryCreate, _SELECT as MEMORY_SELECT

router = APIRouter()

MCP_KEY_PATTERN = re.compile(r"^Bearer omi_mcp_([0-9a-f]{32})$")
SUPPORTED_SCOPES = frozenset(
    {
        "action_items.read",
        "action_items.write",
        "chat.read",
        "conversations.read",
        "goals.read",
        "memories.read",
        "memories.write",
        "people.read",
        "screen_activity.read",
    }
)
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
MEMORY_ACTIVITY_TAGS = frozenset(
    {"activity", "focus", "screen", "screen_activity", "rewind", "distraction", "distracted"}
)
MEMORY_ACTIVITY_PREFIXES = ("focused on ", "distracted on ", "viewing ")
MAX_BODY_BYTES = 256_000
MAX_ID_LENGTH = 256
MAX_MEMORY_SCAN = 5_000
MAX_ACTION_DESCRIPTION = 2_000
MAX_SCREEN_LIMIT = 200
MAX_SCREEN_SUMMARY_LIMIT = 5_000

MEMORY_CATEGORY_SCHEMA = _json_schema(
    "omi_mcp_memory_category",
    {"category": {"type": "string", "enum": ["system", "interesting"]}},
    ["category"],
)


@dataclass(frozen=True)
class McpPrincipal:
    uid: str
    key_id: str
    scopes: frozenset[str]


class McpActionItemCreate(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    description: str = Field(min_length=1, max_length=MAX_ACTION_DESCRIPTION)
    due_at: datetime | None = None
    completed: bool = False


class McpActionItemUpdate(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    description: str | None = Field(default=None, min_length=1, max_length=MAX_ACTION_DESCRIPTION)
    due_at: datetime | None = None


def _detail(message: str, status: int) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _bool(value: object) -> bool:
    return bool(value) and value not in ("0", "false", "False", "no")


def _json_list(value: object, *, maximum_bytes: int = MAX_BODY_BYTES) -> list[object]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum_bytes:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _iso_epoch(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _epoch(value: datetime | None) -> int | None:
    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return int(normalized.astimezone(timezone.utc).timestamp())


def _query_int(request: Request, name: str, default: int, minimum: int, maximum: int) -> int | JSONResponse:
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _detail(f"Invalid {name}: expected integer.", 422)
    return max(minimum, min(value, maximum))


def _query_bool(request: Request, name: str, default: bool | None = None) -> bool | None | JSONResponse:
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return default
    normalized = str(raw).strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    return _detail(f"Invalid {name}: expected boolean.", 422)


def _parse_datetime(value: str | None, name: str, *, status: int = 422) -> datetime | None | JSONResponse:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return _detail(f"Invalid {name} format: '{value}'. Expected ISO 8601.", status)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


async def _bounded_json(request: Request) -> object:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("request body exceeds size limit")
    return json.loads(raw)


async def _authenticate(request: Request, required_scope: str) -> tuple[McpPrincipal | None, JSONResponse | None]:
    authorization = request.headers.get("authorization")
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        return None, _detail("Missing or invalid Authorization header. Must be 'Bearer API_KEY'", 401)
    match = MCP_KEY_PATTERN.fullmatch(authorization)
    if match is None:
        return None, _detail("Invalid MCP API key", 403)

    secret = match.group(1)
    digest = hashlib.sha256(secret.encode()).hexdigest()
    expected_prefix = f"omi_mcp_{secret[:4]}...{secret[-4:]}"
    env = request.scope["env"]
    try:
        row = (
            await env.APP_DB.prepare(
                "SELECT uid, key_id, key_hash, key_prefix, app_id, scopes_json, created_at "
                "FROM cf_mcp_api_keys WHERE key_hash = ? LIMIT 1"
            )
            .bind(digest)
            .first()
        )
        if not isinstance(row, dict):
            return None, _detail("Invalid MCP API key", 403)
        uid = row.get("uid")
        key_id = row.get("key_id")
        if (
            not isinstance(uid, str)
            or not uid
            or len(uid) > 256
            or not isinstance(key_id, str)
            or not key_id
            or len(key_id) > 64
            or row.get("key_hash") != digest
            or row.get("app_id") != "mcp-api"
            or row.get("key_prefix") not in {expected_prefix, "omi_mcp_legacy"}
        ):
            return None, _error("mcp authentication unavailable", 503)
        raw_scopes = row.get("scopes_json")
        if not isinstance(raw_scopes, str) or len(raw_scopes.encode("utf-8")) > 4_096:
            return None, _error("mcp authentication unavailable", 503)
        parsed_scopes = json.loads(raw_scopes)
        if (
            not isinstance(parsed_scopes, list)
            or any(not isinstance(scope, str) or scope not in SUPPORTED_SCOPES for scope in parsed_scopes)
            or len(parsed_scopes) != len(set(parsed_scopes))
        ):
            return None, _error("mcp authentication unavailable", 503)
        scopes = frozenset(parsed_scopes)
        if required_scope not in scopes:
            return None, _detail(f"Insufficient permissions. Required scope: {required_scope}", 403)

        deletion = (
            await env.APP_DB.prepare(
                "SELECT lifecycle FROM ("
                "SELECT 'deleting' AS lifecycle, 0 AS priority FROM cf_account_deletion_intents WHERE uid = ? "
                "UNION ALL SELECT 'deleted' AS lifecycle, 1 AS priority "
                "FROM cf_account_deletion_tombstones WHERE uid = ? AND expires_at > ?"
                ") ORDER BY priority LIMIT 1"
            )
            .bind(uid, uid, int(time.time()))
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
        if not isinstance(cutover, dict):
            return None, _error("account data plane not active", 409)
        if (
            cutover.get("state") != "new"
            or cutover.get("checkpoint_phase") != "completed"
            or not _bool(cutover.get("destination_backend_bound"))
        ):
            return None, _error("account data plane not active", 409)

        created_at = row.get("created_at")
        if not isinstance(created_at, int) or isinstance(created_at, bool) or created_at < 0:
            return None, _error("mcp authentication unavailable", 503)
        await env.APP_DB.prepare("UPDATE cf_mcp_api_keys SET last_used_at = ? WHERE key_id = ? AND uid = ?").bind(
            max(created_at, int(time.time())), key_id, uid
        ).run()
        return McpPrincipal(uid=uid, key_id=key_id, scopes=scopes), None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, _error("mcp authentication unavailable", 503)
    except Exception:
        return None, _error("mcp authentication unavailable", 503)


async def _contact_profile(request: Request, uid: str) -> dict[str, object]:
    env = request.scope["env"]
    auth = getattr(env, "AUTH", None)
    secret = getattr(env, "INTERNAL_ASSERTION_SECRET", None)
    request_id = request.headers.get("x-request-id") or "mcp-profile"
    signed = create_request_context(
        uid,
        secret,
        audience="auth",
        method="GET",
        path="/internal/profile",
        request_id=request_id[:128],
    )
    if auth is None or signed is None:
        record_fallback(
            component="auth",
            from_mode="auth_worker",
            to_mode="metadata_only",
            reason="dependency_unavailable",
            outcome="degraded",
        )
        return {}
    encoded, signature = signed
    try:
        response = await auth.fetch(
            "https://auth.internal/internal/profile",
            method="GET",
            headers={
                "x-omi-auth-context": encoded,
                "x-omi-internal-signature": signature,
                "x-request-id": request_id[:128],
            },
        )
        if int(response.status) != 200:
            raise ValueError("profile lookup rejected")
        payload = await response.json()
        if not isinstance(payload, dict) or payload.get("uid") != uid:
            record_fallback(
                component="auth",
                from_mode="auth_worker",
                to_mode="metadata_only",
                reason="malformed_doc",
                outcome="degraded",
            )
            return {}
        return {
            "name": payload.get("name") if isinstance(payload.get("name"), str) else None,
            "email": payload.get("email") if isinstance(payload.get("email"), str) else None,
        }
    except Exception:
        record_fallback(
            component="auth",
            from_mode="auth_worker",
            to_mode="metadata_only",
            reason="dependency_unavailable",
            outcome="degraded",
        )
        return {}


async def _memory_category(env: object, content: str) -> str:
    result = await _workers_ai_json(
        env,
        "Classify this memory as system when it is a fact about the user, or interesting when it is external "
        "wisdom/advice from another source. Return only the schema object.\n\nMemory: " + content,
        64,
        MEMORY_CATEGORY_SCHEMA,
    )
    category = result.get("category") if isinstance(result, dict) else None
    if category in {"system", "interesting"}:
        return str(category)
    record_fallback(
        component="llm",
        from_mode="workers_ai",
        to_mode="system_default",
        reason="dependency_unavailable" if result is None else "malformed_doc",
        outcome="recovered",
    )
    return "system"


def _memory_id(content: str) -> str:
    return str(uuid.UUID(bytes=hashlib.sha256(content.encode()).digest()[:16], version=4))


def _memory_score(category: str, created_at: int) -> str:
    category_boost = 1 if category == "interesting" else 0
    return "{:02d}_{:02d}_{:010d}".format(1, 999 - category_boost, created_at)


def _memory_output(row: dict[str, object]) -> dict[str, object]:
    content = str(row.get("content") or "")
    if _bool(row.get("is_locked")) and len(content) > 70:
        content = content[:70] + "..."
    return {
        "id": str(row.get("id") or ""),
        "content": content,
        "category": str(row.get("category") or "interesting"),
        "category_source": None,
        "reviewed": _bool(row.get("reviewed")),
        "reviewed_source": None,
        "manually_added": _bool(row.get("manually_added")),
        "manually_added_source": None,
        "memory_default_memory": None,
        "archive_default_visible": None,
        "policy": None,
    }


def _memory_activity(row: dict[str, object]) -> bool:
    tags = {str(item).lower() for item in _json_list(row.get("tags_json"))}
    if tags.intersection(MEMORY_ACTIVITY_TAGS):
        return True
    content = str(row.get("content") or "").strip().lower()
    return any(content.startswith(prefix) for prefix in MEMORY_ACTIVITY_PREFIXES)


def _memory_sensitive(row: dict[str, object]) -> bool:
    level = str(row.get("data_protection_level") or "").lower()
    return bool(level and level not in {"standard", "none"})


@router.post("/v1/mcp/memories")
async def create_memory(request: Request):
    principal, denial = await _authenticate(request, "memories.write")
    if denial:
        return denial
    try:
        raw = await _bounded_json(request)
        if isinstance(raw, dict) and "category" in raw:
            raw = dict(raw)
            # The legacy contract classifies category after request validation;
            # caller-provided category is therefore intentionally ignored.
            raw["category"] = "interesting"
        memory = MemoryCreate.model_validate(raw)
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return _detail("Invalid memory", 422)
    assert principal is not None
    env = request.scope["env"]
    category = await _memory_category(env, memory.content)
    now = int(time.time())
    memory_id = _memory_id(memory.content)
    try:
        statement = env.APP_DB.prepare(
            "INSERT INTO cf_memories "
            "(uid, id, content, category, visibility, tags_json, headline, predicate, arguments_json, "
            "subject_entity_id, subject_attribution, object_entity_ids_json, qualifiers_json, capture_confidence, "
            "veracity, uncertainty_reasons_json, durability, reviewed, user_review, manually_added, scoring, "
            "memory_tier, valid_at, created_at, updated_at, deleted_at, invalid_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 1, ?, 'long_term', ?, ?, ?, NULL, NULL) "
            "ON CONFLICT(uid, id) DO UPDATE SET content = excluded.content, category = excluded.category, "
            "visibility = excluded.visibility, tags_json = excluded.tags_json, headline = excluded.headline, "
            "predicate = excluded.predicate, arguments_json = excluded.arguments_json, "
            "subject_entity_id = excluded.subject_entity_id, subject_attribution = excluded.subject_attribution, "
            "object_entity_ids_json = excluded.object_entity_ids_json, qualifiers_json = excluded.qualifiers_json, "
            "capture_confidence = excluded.capture_confidence, veracity = excluded.veracity, "
            "uncertainty_reasons_json = excluded.uncertainty_reasons_json, durability = excluded.durability, "
            "reviewed = 1, user_review = 1, manually_added = 1, scoring = excluded.scoring, "
            "memory_tier = 'long_term', valid_at = excluded.valid_at, updated_at = excluded.updated_at, "
            "deleted_at = NULL, invalid_at = NULL"
        ).bind(
            principal.uid,
            memory_id,
            memory.content,
            category,
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
            _memory_score(category, now),
            now,
            now,
            now,
        )
        usage = usage_source_statement(
            env,
            uid=principal.uid,
            source_kind="memory",
            source_id=memory_id,
            occurred_at=now,
            memories_created=1,
            updated_at=now,
        )
        await env.APP_DB.batch([statement, usage])
    except Exception:
        return _error("memories unavailable", 503)
    response = memory.model_dump(mode="json")
    response["category"] = category
    return response


@router.delete("/v1/mcp/memories/{memory_id}")
async def delete_memory(request: Request, memory_id: str):
    principal, denial = await _authenticate(request, "memories.write")
    if denial:
        return denial
    if not memory_id or len(memory_id) > MAX_ID_LENGTH:
        return _detail("Memory not found", 404)
    assert principal is not None
    env = request.scope["env"]
    try:
        row = (
            await env.APP_DB.prepare(
                "SELECT id FROM cf_memories WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
            )
            .bind(principal.uid, memory_id)
            .first()
        )
        if not isinstance(row, dict):
            return _detail("Memory not found", 404)
        now = int(time.time())
        await env.APP_DB.prepare(
            "UPDATE cf_memories SET deleted_at = ?, updated_at = ? "
            "WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
        ).bind(now, now, principal.uid, memory_id).run()
    except Exception:
        return _error("memories unavailable", 503)
    return {"status": "ok"}


@router.patch("/v1/mcp/memories/{memory_id}")
async def edit_memory(request: Request, memory_id: str):
    principal, denial = await _authenticate(request, "memories.write")
    if denial:
        return denial
    value = request.query_params.get("value")
    if not isinstance(value, str) or not value.strip() or len(value) > 50_000:
        return _detail("Invalid memory content", 422)
    assert principal is not None
    env = request.scope["env"]
    try:
        row = (
            await env.APP_DB.prepare(
                "SELECT id FROM cf_memories WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
            )
            .bind(principal.uid, memory_id)
            .first()
        )
        if not isinstance(row, dict):
            return _detail("Memory not found", 404)
        await env.APP_DB.prepare(
            "UPDATE cf_memories SET content = ?, edited = 1, updated_at = ? "
            "WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
        ).bind(value.strip(), int(time.time()), principal.uid, memory_id).run()
    except Exception:
        return _error("memories unavailable", 503)
    return {"status": "ok"}


@router.get("/v1/mcp/profile")
async def get_profile(request: Request):
    principal, denial = await _authenticate(request, "memories.read")
    if denial:
        return denial
    assert principal is not None
    env = request.scope["env"]
    try:
        row = (
            await env.APP_DB.prepare(
                "SELECT profile_text, generated_at, data_sources_used FROM cf_user_ai_profiles WHERE uid = ?"
            )
            .bind(principal.uid)
            .first()
        )
    except Exception:
        return _error("profile unavailable", 503)
    contact = await _contact_profile(request, principal.uid)
    profile = row if isinstance(row, dict) else {}
    return {
        "name": contact.get("name"),
        "email": contact.get("email"),
        "phone_number": None,
        "profile_text": profile.get("profile_text"),
        "generated_at": str(profile["generated_at"]) if profile.get("generated_at") is not None else None,
        "data_sources_used": profile.get("data_sources_used"),
    }


@router.get("/v1/mcp/memories")
async def get_memories(request: Request):
    principal, denial = await _authenticate(request, "memories.read")
    if denial:
        return denial
    limit = _query_int(request, "limit", 25, 1, 500)
    offset = _query_int(request, "offset", 0, 0, 100_000)
    reviewed = _query_bool(request, "reviewed")
    manually_added = _query_bool(request, "manually_added")
    include_activity = _query_bool(request, "include_activity", False)
    include_sensitive = _query_bool(request, "include_sensitive", True)
    for value in (limit, offset, reviewed, manually_added, include_activity, include_sensitive):
        if isinstance(value, JSONResponse):
            return value
    sort = request.query_params.get("sort", "created_desc")
    if sort not in {"scoring_desc", "created_desc", "updated_desc", "manual_first"}:
        return _detail("Invalid sort. Expected one of: scoring_desc, created_desc, updated_desc, manual_first.", 400)
    categories = [item.strip() for item in (request.query_params.get("categories") or "").split(",") if item.strip()]
    if any(category not in {"interesting", "system", "manual", "workflow"} for category in categories):
        return _detail("Invalid category", 400)
    updated_after = _parse_datetime(request.query_params.get("updated_after"), "updated_after", status=400)
    if isinstance(updated_after, JSONResponse):
        return updated_after
    assert principal is not None
    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(
                MEMORY_SELECT
                + "WHERE uid = ? AND deleted_at IS NULL AND invalid_at IS NULL AND memory_tier != 'archive' "
                + "ORDER BY updated_at DESC, id DESC LIMIT ?"
            )
            .bind(principal.uid, MAX_MEMORY_SCAN)
            .all()
        )
    except Exception:
        return _error("memories unavailable", 503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    filtered: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if categories and row.get("category") not in categories:
            continue
        if reviewed is not None and _bool(row.get("reviewed")) != reviewed:
            continue
        if manually_added is not None and _bool(row.get("manually_added")) != manually_added:
            continue
        if not include_activity and _memory_activity(row):
            continue
        if not include_sensitive and _memory_sensitive(row):
            continue
        if isinstance(updated_after, datetime):
            updated_epoch = row.get("updated_at")
            if not isinstance(updated_epoch, int) or updated_epoch < int(updated_after.timestamp()):
                continue
        filtered.append(row)
    if sort == "created_desc":
        filtered.sort(key=lambda row: (int(row.get("created_at") or 0), str(row.get("id") or "")), reverse=True)
    elif sort in {"updated_desc", "scoring_desc"}:
        filtered.sort(key=lambda row: (int(row.get("updated_at") or 0), str(row.get("id") or "")), reverse=True)
    elif sort == "manual_first":
        filtered.sort(
            key=lambda row: (
                _bool(row.get("manually_added")),
                int(row.get("updated_at") or row.get("created_at") or 0),
                str(row.get("id") or ""),
            ),
            reverse=True,
        )
    return [_memory_output(row) for row in filtered[int(offset) : int(offset) + int(limit)]]


def _app_results(value: object) -> list[dict[str, object]] | None:
    raw = conversation_json_list(value)
    results: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            return None
        app_id = item.get("app_id")
        if app_id is not None and not isinstance(app_id, str):
            return None
        results.append({"app_id": app_id, "content": item["content"]})
    return results


def _conversation_base(row: dict[str, object]) -> dict[str, object] | None:
    structured = conversation_json_object(row.get("structured_json"))
    title = structured.get("title")
    overview = structured.get("overview")
    category = structured.get("category")
    if not isinstance(title, str) or not isinstance(overview, str) or category not in CONVERSATION_CATEGORIES:
        return None
    apps_results = _app_results(row.get("apps_results_json"))
    if apps_results is None:
        return None
    if _bool(row.get("is_locked")):
        apps_results = []
    return {
        "id": str(row.get("id") or ""),
        "started_at": _iso_epoch(row.get("started_at")),
        "finished_at": _iso_epoch(row.get("finished_at")),
        "structured": {"title": title, "overview": overview, "category": category},
        "language": row.get("language"),
        "apps_results": apps_results,
        "match_snippets": [],
    }


def _transcript_segments(row: dict[str, object], people: dict[str, str]) -> list[dict[str, object]] | None:
    segments: list[dict[str, object]] = []
    for raw in conversation_json_list(row.get("transcript_segments_json")):
        if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
            return None
        try:
            start = float(raw["start"])
            end = float(raw["end"])
            speaker_id = None if raw.get("speaker_id") is None else int(raw["speaker_id"])
        except (KeyError, TypeError, ValueError):
            return None
        speaker_name = raw.get("speaker_name") if isinstance(raw.get("speaker_name"), str) else None
        if _bool(raw.get("is_user")):
            speaker_name = "User"
        elif raw.get("person_id") is not None:
            speaker_name = people.get(str(raw["person_id"]), speaker_name)
        if not speaker_name:
            speaker_name = f"Speaker {speaker_id or 0}"
        segment_id = raw.get("id")
        if segment_id is not None and not isinstance(segment_id, str):
            return None
        segments.append(
            {
                "id": segment_id,
                "text": raw["text"],
                "speaker_id": speaker_id,
                "speaker_name": speaker_name,
                "start": start,
                "end": end,
            }
        )
    return segments


@router.get("/v1/mcp/conversations")
async def get_conversations(request: Request):
    principal, denial = await _authenticate(request, "conversations.read")
    if denial:
        return denial
    limit = _query_int(request, "limit", 100, 1, 1_000)
    offset = _query_int(request, "offset", 0, 0, 100_000)
    if isinstance(limit, JSONResponse) or isinstance(offset, JSONResponse):
        return limit if isinstance(limit, JSONResponse) else offset
    start = _parse_datetime(request.query_params.get("start_date"), "start_date")
    end = _parse_datetime(request.query_params.get("end_date"), "end_date")
    if isinstance(start, JSONResponse) or isinstance(end, JSONResponse):
        return start if isinstance(start, JSONResponse) else end
    categories = [item.strip() for item in (request.query_params.get("categories") or "").split(",") if item.strip()]
    if any(category not in CONVERSATION_CATEGORIES for category in categories):
        return _detail("Invalid category", 400)
    assert principal is not None
    clauses = ["uid = ?", "discarded = 0", "status = 'completed'"]
    args: list[object] = [principal.uid]
    if isinstance(start, datetime):
        clauses.append("created_at >= ?")
        args.append(int(start.timestamp()))
    if isinstance(end, datetime):
        clauses.append("created_at <= ?")
        args.append(int(end.timestamp()))
    if categories:
        clauses.append("json_extract(structured_json, '$.category') IN (" + ",".join("?" for _ in categories) + ")")
        args.extend(categories)
    query = _CONVERSATION_SELECT + "WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    try:
        result = await request.scope["env"].APP_DB.prepare(query).bind(*args, limit, offset).all()
    except Exception:
        return _error("conversations unavailable", 503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    conversations = [_conversation_base(row) for row in rows if isinstance(row, dict)]
    return [conversation for conversation in conversations if conversation is not None]


@router.get("/v1/mcp/conversations/{conversation_id}")
async def get_conversation(request: Request, conversation_id: str):
    principal, denial = await _authenticate(request, "conversations.read")
    if denial:
        return denial
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return _detail("Conversation not found", 404)
    assert principal is not None
    env = request.scope["env"]
    try:
        row = (
            await env.APP_DB.prepare(_CONVERSATION_SELECT + "WHERE uid = ? AND id = ?")
            .bind(principal.uid, conversation_id)
            .first()
        )
        if not isinstance(row, dict):
            return _detail("Conversation not found", 404)
        if _bool(row.get("is_locked")):
            return _detail("A paid plan is required to access this conversation.", 402)
        person_ids = {
            str(segment["person_id"])
            for segment in conversation_json_list(row.get("transcript_segments_json"))
            if isinstance(segment, dict) and segment.get("person_id")
        }
        people = await _person_names(env, principal.uid, person_ids) if person_ids else {}
    except Exception:
        return _error("conversations unavailable", 503)
    response = _conversation_base(row)
    segments = _transcript_segments(row, people)
    if response is None or segments is None:
        return _detail("Conversation not found", 404)
    response["transcript_segments"] = segments
    return response


def _mcp_action_item(row: dict[str, object]) -> dict[str, object]:
    projected = action_item_response(row)
    description = str(projected.get("description") or "")
    if _bool(row.get("is_locked")) and len(description) > 70:
        description = description[:70] + "..."
    return {
        "id": projected["id"],
        "description": description,
        "completed": projected["completed"],
        "created_at": projected["created_at"],
        "due_at": projected["due_at"],
        "completed_at": projected["completed_at"],
        "conversation_id": projected["conversation_id"],
    }


ACTION_ITEM_SELECT = (
    "SELECT id, description, status, completed, goal_id, workstream_id, owner, due_at, due_confidence, "
    "source, provenance_json, priority, sort_order, indent_level, recurrence_rule, recurrence_parent_id, "
    "created_at, updated_at, completed_at, superseded_by, conversation_id, is_locked, exported, export_date, "
    "export_platform, apple_reminder_id FROM cf_action_items "
)


async def _action_item(env: object, uid: str, item_id: str) -> dict[str, object] | None:
    row = (
        await env.APP_DB.prepare(ACTION_ITEM_SELECT + "WHERE uid = ? AND id = ? AND deleted = 0")
        .bind(uid, item_id)
        .first()
    )
    return row if isinstance(row, dict) else None


def _action_item_key(uid: str, description: str) -> str:
    normalized = description.strip().lower()
    return hashlib.sha256(f"{len(uid)}:{uid}:{normalized}".encode()).hexdigest()


@router.get("/v1/mcp/action-items")
async def get_action_items(request: Request):
    principal, denial = await _authenticate(request, "action_items.read")
    if denial:
        return denial
    completed = _query_bool(request, "completed")
    limit = _query_int(request, "limit", 100, 1, 500)
    offset = _query_int(request, "offset", 0, 0, 1_000_000)
    if isinstance(completed, JSONResponse) or isinstance(limit, JSONResponse) or isinstance(offset, JSONResponse):
        return next(value for value in (completed, limit, offset) if isinstance(value, JSONResponse))
    due_start = _parse_datetime(request.query_params.get("due_start_date"), "due_start_date")
    due_end = _parse_datetime(request.query_params.get("due_end_date"), "due_end_date")
    if isinstance(due_start, JSONResponse) or isinstance(due_end, JSONResponse):
        return due_start if isinstance(due_start, JSONResponse) else due_end
    assert principal is not None
    clauses = ["uid = ?", "deleted = 0"]
    args: list[object] = [principal.uid]
    if completed is not None:
        clauses.append("completed = ?")
        args.append(1 if completed else 0)
    if isinstance(due_start, datetime):
        clauses.append("due_at >= ?")
        args.append(int(due_start.timestamp()))
    if isinstance(due_end, datetime):
        clauses.append("due_at <= ?")
        args.append(int(due_end.timestamp()))
    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(
                ACTION_ITEM_SELECT
                + "WHERE "
                + " AND ".join(clauses)
                + " ORDER BY completed ASC, CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at ASC, created_at DESC "
                + "LIMIT ? OFFSET ?"
            )
            .bind(*args, limit, offset)
            .all()
        )
    except Exception:
        return _error("action items unavailable", 503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [_mcp_action_item(row) for row in rows if isinstance(row, dict)]


@router.post("/v1/mcp/action-items")
async def create_action_item(request: Request):
    principal, denial = await _authenticate(request, "action_items.write")
    if denial:
        return denial
    try:
        item = McpActionItemCreate.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as error:
        return _detail(str(error), 422)
    assert principal is not None
    env = request.scope["env"]
    key = _action_item_key(principal.uid, item.description)
    try:
        existing = (
            await env.APP_DB.prepare(
                ACTION_ITEM_SELECT + "WHERE uid = ? AND idempotency_key = ? AND deleted = 0 AND completed = 0 LIMIT 1"
            )
            .bind(principal.uid, key)
            .first()
        )
        if isinstance(existing, dict):
            return _mcp_action_item(existing)
        now = int(time.time())
        item_id = uuid.uuid4().hex
        await env.APP_DB.prepare(
            "INSERT INTO cf_action_items "
            "(uid, id, description, status, completed, owner, due_at, source, provenance_json, created_at, "
            "updated_at, completed_at, idempotency_key, sync_requested, deleted) "
            "VALUES (?, ?, ?, ?, ?, 'user', ?, 'mcp', '[]', ?, ?, ?, ?, 0, 0)"
        ).bind(
            principal.uid,
            item_id,
            item.description,
            "completed" if item.completed else "active",
            1 if item.completed else 0,
            _epoch(item.due_at),
            now,
            now,
            now if item.completed else None,
            key,
        ).run()
        row = await _action_item(env, principal.uid, item_id)
    except Exception:
        return _error("action item unavailable", 503)
    return _mcp_action_item(row) if row else _error("action item unavailable", 503)


async def _unlocked_action_item(
    env: object, uid: str, item_id: str
) -> tuple[dict[str, object] | None, JSONResponse | None]:
    row = await _action_item(env, uid, item_id)
    if row is None:
        return None, _detail("Action item not found", 404)
    if _bool(row.get("is_locked")):
        return None, _detail("A paid plan is required to modify this action item.", 402)
    return row, None


@router.post("/v1/mcp/action-items/{action_item_id}/complete")
async def complete_action_item(request: Request, action_item_id: str):
    principal, denial = await _authenticate(request, "action_items.write")
    if denial:
        return denial
    completed = _query_bool(request, "completed", True)
    if isinstance(completed, JSONResponse):
        return completed
    assert principal is not None
    env = request.scope["env"]
    try:
        _, item_denial = await _unlocked_action_item(env, principal.uid, action_item_id)
        if item_denial:
            return item_denial
        now = int(time.time())
        await env.APP_DB.prepare(
            "UPDATE cf_action_items SET status = ?, completed = ?, completed_at = ?, updated_at = ? "
            "WHERE uid = ? AND id = ? AND deleted = 0"
        ).bind(
            "completed" if completed else "active",
            1 if completed else 0,
            now if completed else None,
            now,
            principal.uid,
            action_item_id,
        ).run()
        row = await _action_item(env, principal.uid, action_item_id)
    except Exception:
        return _error("action item unavailable", 503)
    return _mcp_action_item(row) if row else _detail("Action item not found", 404)


@router.patch("/v1/mcp/action-items/{action_item_id}")
async def update_action_item(request: Request, action_item_id: str):
    principal, denial = await _authenticate(request, "action_items.write")
    if denial:
        return denial
    try:
        payload = await _bounded_json(request)
        update = McpActionItemUpdate.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as error:
        return _detail(str(error), 422)
    if update.description is None and update.due_at is None:
        return _detail("Provide a description, or a due date in ISO 8601 / YYYY-MM-DD form, to update", 422)
    assert principal is not None
    env = request.scope["env"]
    try:
        _, item_denial = await _unlocked_action_item(env, principal.uid, action_item_id)
        if item_denial:
            return item_denial
        values: list[object] = []
        assignments: list[str] = []
        if update.description is not None:
            assignments.append("description = ?")
            values.append(update.description)
        if update.due_at is not None:
            assignments.append("due_at = ?")
            values.append(_epoch(update.due_at))
        assignments.append("updated_at = ?")
        values.append(int(time.time()))
        await env.APP_DB.prepare(
            "UPDATE cf_action_items SET " + ", ".join(assignments) + " WHERE uid = ? AND id = ? AND deleted = 0"
        ).bind(*values, principal.uid, action_item_id).run()
        row = await _action_item(env, principal.uid, action_item_id)
    except Exception:
        return _error("action item unavailable", 503)
    return _mcp_action_item(row) if row else _detail("Action item not found", 404)


@router.delete("/v1/mcp/action-items/{action_item_id}")
async def delete_action_item(request: Request, action_item_id: str):
    principal, denial = await _authenticate(request, "action_items.write")
    if denial:
        return denial
    assert principal is not None
    env = request.scope["env"]
    try:
        _, item_denial = await _unlocked_action_item(env, principal.uid, action_item_id)
        if item_denial:
            return item_denial
        await env.APP_DB.prepare("DELETE FROM cf_action_items WHERE uid = ? AND id = ? AND deleted = 0").bind(
            principal.uid, action_item_id
        ).run()
    except Exception:
        return _error("action item unavailable", 503)
    return {"status": "ok"}


@router.get("/v1/mcp/goals")
async def get_goals(request: Request):
    principal, denial = await _authenticate(request, "goals.read")
    if denial:
        return denial
    include_inactive = _query_bool(request, "include_inactive", False)
    if isinstance(include_inactive, JSONResponse):
        return include_inactive
    assert principal is not None
    try:
        query = GOAL_SELECT + "WHERE uid = ?" + ("" if include_inactive else " AND is_active = 1")
        result = (
            await request.scope["env"].APP_DB.prepare(query + " ORDER BY created_at DESC").bind(principal.uid).all()
        )
    except Exception:
        return _error("goals unavailable", 503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [goal_response(row) for row in rows if isinstance(row, dict)]


@router.get("/v1/mcp/chat")
async def get_chat(request: Request):
    principal, denial = await _authenticate(request, "chat.read")
    if denial:
        return denial
    limit = _query_int(request, "limit", 50, 1, 200)
    offset = _query_int(request, "offset", 0, 0, 1_000_000)
    if isinstance(limit, JSONResponse) or isinstance(offset, JSONResponse):
        return limit if isinstance(limit, JSONResponse) else offset
    assert principal is not None
    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT id, created_at, message_json FROM cf_chat_messages "
                "WHERE uid = ? AND app_id IS NULL AND COALESCE(json_extract(message_json, '$.reported'), 0) != 1 "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
            )
            .bind(principal.uid, limit, offset)
            .all()
        )
    except Exception:
        return _error("chat unavailable", 503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    messages: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = row.get("message_json")
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_CONVERSATION_JSON_BYTES:
            continue
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(message, dict):
            continue
        messages.append(
            {
                "id": str(message.get("id") or row.get("id") or ""),
                "text": str(message.get("text") or ""),
                "sender": str(message.get("sender") or ""),
                "type": message.get("type"),
                "created_at": message.get("created_at") or _iso_epoch(row.get("created_at")),
            }
        )
    return messages


@router.get("/v1/mcp/people")
async def get_people(request: Request):
    principal, denial = await _authenticate(request, "people.read")
    if denial:
        return denial
    assert principal is not None
    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT id, name, speech_sample_transcripts_json, created_at FROM cf_people "
                "WHERE uid = ? ORDER BY created_at ASC, id ASC"
            )
            .bind(principal.uid)
            .all()
        )
    except Exception:
        return _error("people unavailable", 503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [
        {
            "id": str(row.get("id") or ""),
            "name": str(row.get("name") or ""),
            "created_at": _iso_epoch(row.get("created_at")),
            "speech_sample_transcripts": [
                str(item) for item in _json_list(row.get("speech_sample_transcripts_json"))[:5]
            ],
        }
        for row in rows
        if isinstance(row, dict)
    ]


def _screen_timestamp(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc).replace(tzinfo=None)
    return f"{normalized.strftime('%Y-%m-%d %H:%M:%S')}.{normalized.microsecond // 1000:03d}"


@router.get("/v1/mcp/screen-activity")
async def get_screen_activity(request: Request):
    principal, denial = await _authenticate(request, "screen_activity.read")
    if denial:
        return denial
    summary = _query_bool(request, "summary", False)
    limit = _query_int(request, "limit", 200, 1, MAX_SCREEN_LIMIT)
    if isinstance(summary, JSONResponse) or isinstance(limit, JSONResponse):
        return summary if isinstance(summary, JSONResponse) else limit
    start = _parse_datetime(request.query_params.get("start_date"), "start_date")
    end = _parse_datetime(request.query_params.get("end_date"), "end_date")
    if isinstance(start, JSONResponse) or isinstance(end, JSONResponse):
        return start if isinstance(start, JSONResponse) else end
    app = request.query_params.get("app")
    if app is not None and (not isinstance(app, str) or len(app) > 512):
        return _detail("Invalid app", 422)
    assert principal is not None
    clauses = ["uid = ?"]
    args: list[object] = [principal.uid]
    if isinstance(start, datetime):
        clauses.append("timestamp >= ?")
        args.append(_screen_timestamp(start))
    if isinstance(end, datetime):
        clauses.append("timestamp <= ?")
        args.append(_screen_timestamp(end))
    if app:
        clauses.append("app_name = ?")
        args.append(app)
    requested_limit = MAX_SCREEN_SUMMARY_LIMIT if summary else int(limit)
    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT id, timestamp, app_name, window_title, ocr_text FROM cf_screen_activity WHERE "
                + " AND ".join(clauses)
                + " ORDER BY timestamp ASC, id ASC LIMIT ?"
            )
            .bind(*args, requested_limit)
            .all()
        )
    except Exception:
        return _error("screen activity unavailable", 503)
    rows = [row for row in (result.get("results", []) if isinstance(result, dict) else []) if isinstance(row, dict)]
    if not summary:
        return [
            {
                "id": str(row.get("id") or ""),
                "timestamp": str(row.get("timestamp") or ""),
                "app_name": str(row.get("app_name") or ""),
                "window_title": str(row.get("window_title") or ""),
                "ocr_text": str(row.get("ocr_text") or ""),
            }
            for row in rows
        ]
    apps: dict[str, dict[str, object]] = {}
    for row in rows:
        app_name = str(row.get("app_name") or "Unknown")
        entry = apps.setdefault(
            app_name,
            {"count": 0, "first_seen": row.get("timestamp"), "last_seen": row.get("timestamp"), "window_titles": []},
        )
        entry["count"] = int(entry["count"]) + 1
        entry["last_seen"] = row.get("timestamp")
        title = str(row.get("window_title") or "")
        titles = entry["window_titles"]
        if title and isinstance(titles, list) and title not in titles and len(titles) < 10:
            titles.append(title)
    return {"apps": apps, "total_screenshots": len(rows)}


@router.get("/v1/mcp/daily-summaries")
async def get_daily_summaries(request: Request):
    principal, denial = await _authenticate(request, "conversations.read")
    if denial:
        return denial
    limit = _query_int(request, "limit", 30, 1, 100)
    offset = _query_int(request, "offset", 0, 0, 1_000_000)
    if isinstance(limit, JSONResponse) or isinstance(offset, JSONResponse):
        return limit if isinstance(limit, JSONResponse) else offset
    start = request.query_params.get("start_date")
    end = request.query_params.get("end_date")
    if any(value is not None and (not isinstance(value, str) or len(value) > 64) for value in (start, end)):
        return _detail("Invalid date filter", 422)
    assert principal is not None
    clauses = ["uid = ?"]
    args: list[object] = [principal.uid]
    if start:
        clauses.append("date >= ?")
        args.append(start)
    if end:
        clauses.append("date <= ?")
        args.append(end)
    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(
                DAILY_SUMMARY_SELECT
                + "WHERE "
                + " AND ".join(clauses)
                + " ORDER BY date DESC, id DESC LIMIT ? OFFSET ?"
            )
            .bind(*args, limit, offset)
            .all()
        )
    except Exception:
        return _error("daily summaries unavailable", 503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [daily_summary_response(row) for row in rows if isinstance(row, dict)]


__all__ = ["router"]
