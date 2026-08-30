"""App-key integration APIs backed only by Cloudflare D1 and Workers AI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import math
import re
import time
import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from account_routes import usage_source_statement
from conversation_routes import (
    _CONVERSATION_SEARCH_SELECT,
    _CONVERSATION_SELECT,
    _fts_query,
    _response as conversation_response,
)
from memory_routes import _SELECT as MEMORY_SELECT
from memory_routes import _response as memory_response
from internal_auth import decode_context

router = APIRouter()

MAX_ID_LENGTH = 256
MAX_BODY_BYTES = 1_000_000
MAX_TEXT_LENGTH = 100_000
MAX_MESSAGE_LENGTH = 2_000
MAX_MEMORY_CONTENT_LENGTH = 50_000
MAX_MEMORIES = 100
MAX_TAGS = 100
MAX_TAG_LENGTH = 256
MAX_LIST_LIMIT = 1_000
MAX_OFFSET = 100_000
MAX_STATUSES = 20
MAX_TRANSCRIPT_SEGMENTS = 1_000
DEFAULT_WORKERS_AI_MODEL = "@cf/meta/llama-3.2-3b-instruct"
RATE_LIMITS = {
    "notification": 10,
    "conversation_create": 10,
    "memory_create": 60,
}

TASK_INTEGRATION_KEYS = frozenset({"apple_reminders", "todoist", "asana", "google_tasks", "clickup"})
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


class ExternalConversationCreate(BaseModel):
    model_config = {"extra": "ignore"}

    started_at: datetime | None = None
    finished_at: datetime | None = None
    text: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)
    text_source: str = Field(default="audio", min_length=1, max_length=64)
    text_source_spec: str | None = Field(default=None, max_length=512)
    geolocation: dict[str, object] | None = None
    language: str | None = Field(default=None, max_length=32)
    client_device_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    client_platform: str | None = Field(default=None, max_length=64)

    @field_validator("text")
    @classmethod
    def non_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text is required")
        return value

    @model_validator(mode="after")
    def valid_range(self) -> "ExternalConversationCreate":
        if self.started_at and self.finished_at and self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        return self


class ExplicitMemory(BaseModel):
    model_config = {"extra": "ignore"}

    content: str = Field(min_length=1, max_length=MAX_MEMORY_CONTENT_LENGTH)
    tags: list[str] | None = Field(default=None, max_length=MAX_TAGS)
    source_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    source_url: str | None = Field(default=None, max_length=2_048)
    artifact_ref: dict[str, object] | None = None

    @model_validator(mode="after")
    def valid_tags(self) -> "ExplicitMemory":
        if any(not value.strip() or len(value) > MAX_TAG_LENGTH for value in self.tags or []):
            raise ValueError("invalid memory tags")
        return self


class ExternalMemoryCreate(BaseModel):
    model_config = {"extra": "ignore"}

    text: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)
    text_source: str = Field(default="other", min_length=1, max_length=64)
    text_source_spec: str | None = Field(default=None, max_length=512)
    source_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    source_url: str | None = Field(default=None, max_length=2_048)
    artifact_ref: dict[str, object] | None = None
    memories: list[ExplicitMemory] | None = Field(default=None, max_length=MAX_MEMORIES)

    @model_validator(mode="after")
    def has_input(self) -> "ExternalMemoryCreate":
        if not (self.text and self.text.strip()) and not self.memories:
            raise ValueError("text or explicit memories are required")
        return self


class ConversationSearch(BaseModel):
    model_config = {"extra": "ignore"}

    query: str = Field(default="", max_length=500)
    page: int = Field(default=1, ge=1, le=10_000)
    per_page: int = Field(default=10, ge=1, le=100)
    include_discarded: bool = True
    start_date: str | None = Field(default=None, max_length=64)
    end_date: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def bounded_terms(self) -> "ConversationSearch":
        if len(self.query.split()) > 20:
            raise ValueError("too many search terms")
        return self


def _detail(message: str, status: int) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)


def _better_auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _connected(row: object) -> bool:
    return isinstance(row, dict) and row.get("connected") in (1, True, "1", "true")


async def _google_gmail_connected(env: object, uid: str) -> bool:
    row = (
        await env.APP_DB.prepare(
            "SELECT connected, granted_scopes_json FROM cf_google_calendar_integrations WHERE uid = ? LIMIT 1"
        )
        .bind(uid)
        .first()
    )
    if not _connected(row) or not isinstance(row, dict):
        return False
    raw_scopes = row.get("granted_scopes_json")
    if not isinstance(raw_scopes, str) or len(raw_scopes.encode("utf-8")) > 4_000:
        return False
    try:
        scopes = json.loads(raw_scopes)
    except (TypeError, ValueError):
        return False
    return isinstance(scopes, list) and GMAIL_READONLY_SCOPE in scopes


@router.get("/v1/integrations/{app_key}")
async def get_integration_status(request: Request, app_key: str):
    """Return only the caller's integration connection bit from D1 projections."""
    context = _better_auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not isinstance(app_key, str) or not app_key or len(app_key) > MAX_ID_LENGTH or "/" in app_key:
        return _detail("invalid integration key", 422)

    uid = context.get("uid")
    if not isinstance(uid, str) or not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    try:
        if app_key == "google_calendar":
            row = (
                await env.APP_DB.prepare("SELECT connected FROM cf_google_calendar_integrations WHERE uid = ? LIMIT 1")
                .bind(uid)
                .first()
            )
            connected = _connected(row)
        elif app_key == "gmail":
            connected = await _google_gmail_connected(env, uid)
        elif app_key in TASK_INTEGRATION_KEYS:
            row = (
                await env.APP_DB.prepare(
                    "SELECT connected FROM cf_task_integrations WHERE uid = ? AND app_key = ? LIMIT 1"
                )
                .bind(uid, app_key)
                .first()
            )
            connected = _connected(row)
        else:
            connected = False
    except Exception:
        return JSONResponse({"error": "integration status unavailable"}, status_code=503)
    return {"connected": connected, "app_key": app_key}


def _valid_identifier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_ID_LENGTH or "/" in value or "\\" in value:
        return None
    return value


def _query(request: Request, name: str) -> str | None:
    value = request.query_params.get(name)
    return value if isinstance(value, str) else None


def _query_list(request: Request, name: str) -> list[str]:
    getlist = getattr(request.query_params, "getlist", None)
    if callable(getlist):
        return [str(value) for value in getlist(name)]
    value = _query(request, name)
    return [] if value is None else [value]


async def _bounded_json(request: Request, limit: int = MAX_BODY_BYTES) -> object:
    raw = await request.body()
    if not raw or len(raw) > limit:
        raise ValueError("invalid request body")
    return json.loads(raw)


def _flag(value: object) -> bool:
    return value is True or value == 1 or (isinstance(value, str) and value.strip().lower() in {"1", "true"})


def _paid_app(payload: dict[str, object]) -> bool:
    return _flag(payload.get("is_paid")) or bool(payload.get("payment_link") or payload.get("payment_link_id"))


def _public_webhook_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2_048:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return value
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return None
    return value


async def _webhook_targets(env: object, uid: str) -> list[tuple[str, str]]:
    result = (
        await env.APP_DB.prepare(
            "SELECT c.id, c.data_json, s.status, s.current_period_end "
            "FROM cf_user_enabled_apps u JOIN cf_app_catalog c ON c.id = u.app_id "
            "LEFT JOIN cf_app_subscriptions s ON s.uid = u.uid AND s.app_id = u.app_id "
            "WHERE u.uid = ? AND c.disabled = 0 ORDER BY c.id LIMIT 101"
        )
        .bind(uid)
        .all()
    )
    rows = result.get("results", []) if isinstance(result, dict) else []
    if len(rows) > 100:
        raise ValueError("too many integration webhook targets")
    targets: list[tuple[str, str]] = []
    now = int(time.time())
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        raw = row.get("data_json")
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 500_000:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if _paid_app(payload):
            period_end = row.get("current_period_end")
            if row.get("status") not in {"active", "trialing"} or not isinstance(period_end, int) or period_end <= now:
                continue
        capabilities = payload.get("capabilities")
        external = payload.get("external_integration")
        if (
            not isinstance(capabilities, list)
            or "external_integration" not in capabilities
            or not isinstance(external, dict)
            or external.get("triggers_on") != "memory_creation"
        ):
            continue
        webhook_url = _public_webhook_url(external.get("webhook_url"))
        if webhook_url:
            targets.append((str(row["id"]), webhook_url))
    return targets


def _has_action(payload: dict[str, object], *names: str) -> bool:
    external = payload.get("external_integration")
    if not isinstance(external, dict):
        return False
    actions = external.get("actions")
    if not isinstance(actions, list):
        return False
    allowed = set(names)
    return any(isinstance(item, dict) and item.get("action") in allowed for item in actions)


def _bearer_key(request: Request) -> str | JSONResponse:
    authorization = request.headers.get("authorization")
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        return _detail("Missing or invalid Authorization header. Must be 'Bearer API_KEY'", 401)
    value = authorization[7:]
    if value.startswith("sk_"):
        value = value[3:]
    if not value or len(value) > 256:
        return _detail("Invalid API key", 403)
    return value


async def _authorized_app(
    request: Request,
    app_id: str,
    uid: str,
    actions: tuple[str, ...] = (),
) -> tuple[dict[str, object] | None, JSONResponse | None]:
    api_key = _bearer_key(request)
    if isinstance(api_key, JSONResponse):
        return None, api_key
    digest = hashlib.sha256(api_key.encode()).hexdigest()
    env = request.scope["env"]
    try:
        key = (
            await env.APP_DB.prepare("SELECT 1 AS valid FROM cf_app_api_keys WHERE app_id = ? AND key_hash = ? LIMIT 1")
            .bind(app_id, digest)
            .first()
        )
    except Exception:
        return None, JSONResponse({"error": "integration authentication unavailable"}, status_code=503)
    if not isinstance(key, dict):
        return None, _detail("Invalid API key", 403)
    try:
        row = (
            await env.APP_DB.prepare("SELECT id, disabled, data_json FROM cf_app_catalog WHERE id = ? LIMIT 1")
            .bind(app_id)
            .first()
        )
    except Exception:
        return None, JSONResponse({"error": "app catalog unavailable"}, status_code=503)
    if not isinstance(row, dict) or _flag(row.get("disabled")):
        return None, _detail("App not found", 404)
    raw = row.get("data_json")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 500_000:
        return None, JSONResponse({"error": "app catalog unavailable"}, status_code=503)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None, JSONResponse({"error": "app catalog unavailable"}, status_code=503)
    if not isinstance(payload, dict) or payload.get("id") not in (None, app_id):
        return None, JSONResponse({"error": "app catalog unavailable"}, status_code=503)
    try:
        enabled = (
            await env.APP_DB.prepare(
                "SELECT u.app_id, s.status, s.current_period_end "
                "FROM cf_user_enabled_apps u "
                "LEFT JOIN cf_app_subscriptions s ON s.uid = u.uid AND s.app_id = u.app_id "
                "WHERE u.uid = ? AND u.app_id = ? LIMIT 1"
            )
            .bind(uid, app_id)
            .first()
        )
    except Exception:
        return None, JSONResponse({"error": "enabled apps unavailable"}, status_code=503)
    if not isinstance(enabled, dict):
        return None, _detail("App is not enabled for this user", 403)
    if _paid_app(payload):
        period_end = enabled.get("current_period_end")
        if (
            enabled.get("status") not in {"active", "trialing"}
            or not isinstance(period_end, int)
            or period_end <= int(time.time())
        ):
            return None, _detail("App is not enabled for this user", 403)
    if actions and not _has_action(payload, *actions):
        return None, _detail("App does not have the required integration capability", 403)
    payload = dict(payload)
    payload["id"] = app_id
    return payload, None


async def _rate_limit(env: object, app_id: str, uid: str, operation: str) -> tuple[int, int] | JSONResponse:
    limit = RATE_LIMITS[operation]
    now = int(time.time())
    bucket = now - now % 3_600
    reset = 3_600 - now % 3_600
    try:
        row = (
            await env.APP_DB.prepare(
                "INSERT INTO cf_integration_hourly_usage "
                "(app_id, uid, operation, bucket_start, request_count, updated_at) "
                "VALUES (?, ?, ?, ?, 1, ?) "
                "ON CONFLICT(app_id, uid, operation, bucket_start) DO UPDATE SET "
                "request_count = cf_integration_hourly_usage.request_count + 1, updated_at = excluded.updated_at "
                "WHERE cf_integration_hourly_usage.request_count < ? RETURNING request_count"
            )
            .bind(app_id, uid, operation, bucket, now, limit)
            .first()
        )
        # Traffic-bounded cleanup keeps abandoned account-independent buckets
        # small; account deletion still purges the current uid exhaustively.
        await env.APP_DB.prepare("DELETE FROM cf_integration_hourly_usage WHERE bucket_start < ?").bind(
            bucket - 86_400
        ).run()
    except Exception:
        return JSONResponse({"error": "integration rate limit unavailable"}, status_code=503)
    if not isinstance(row, dict):
        return JSONResponse(
            {"detail": f"Rate limit exceeded. Maximum {limit} requests per hour."},
            status_code=429,
            headers={
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(reset),
                "Retry-After": str(reset),
            },
        )
    count = int(row.get("request_count") or 0)
    return max(0, limit - count), reset


def _rpc_mapping(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return value
    to_py = getattr(value, "to_py", None)
    if callable(to_py):
        converted = to_py()
        if isinstance(converted, dict):
            return converted
    return None


def _structured_json(value: str) -> object | None:
    value = value.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE)
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        starts = [index for index in (value.find("{"), value.find("[")) if index >= 0]
        if not starts:
            return None
        start = min(starts)
        end = max(value.rfind("}"), value.rfind("]"))
        if end <= start:
            return None
        try:
            return json.loads(value[start : end + 1])
        except (TypeError, ValueError):
            return None


def _json_schema(name: str, properties: dict[str, object], required: list[str]) -> dict[str, object]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


CONVERSATION_SCHEMA = _json_schema(
    "omi_integration_conversation",
    {
        "title": {"type": "string"},
        "overview": {"type": "string"},
        "emoji": {"type": "string"},
        "category": {"type": "string"},
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"description": {"type": "string"}},
                "required": ["description"],
                "additionalProperties": False,
            },
        },
    },
    ["title", "overview", "emoji", "category", "action_items"],
)
MEMORY_SCHEMA = _json_schema(
    "omi_integration_memories",
    {"memories": {"type": "array", "items": {"type": "string"}}},
    ["memories"],
)


async def _workers_ai_json(
    env: object,
    prompt: str,
    max_tokens: int,
    response_format: dict[str, object],
) -> object | None:
    ai = getattr(env, "AI", None)
    if ai is None:
        return None
    model = getattr(env, "WORKERS_AI_INTEGRATION_MODEL", DEFAULT_WORKERS_AI_MODEL)
    try:
        result = await ai.run(
            model,
            {
                "messages": [
                    {
                        "role": "system",
                        "content": "Return only valid JSON matching the requested schema. Do not use markdown.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "response_format": response_format,
                "max_tokens": max_tokens,
                "temperature": 0,
            },
        )
    except Exception:
        return None
    mapping = _rpc_mapping(result)
    response = mapping.get("response") if mapping else None
    if isinstance(response, dict):
        return response
    if isinstance(response, str):
        return _structured_json(response)
    return mapping if isinstance(mapping, dict) and "response" not in mapping else None


async def _conversation_summary(env: object, text: str) -> dict[str, object] | None:
    result = await _workers_ai_json(
        env,
        "Summarize the following conversation. Return an object with string fields title, overview, emoji, category; "
        "and an action_items array of objects with a non-empty description. "
        "Keep title under 160 characters, overview under 2000 characters, and at most 20 action items.\n\n" + text,
        1_024,
        CONVERSATION_SCHEMA,
    )
    if not isinstance(result, dict):
        return None
    title = result.get("title")
    overview = result.get("overview")
    if not isinstance(title, str) or not title.strip() or not isinstance(overview, str):
        return None
    action_items = []
    raw_items = result.get("action_items")
    if isinstance(raw_items, list):
        for item in raw_items[:20]:
            description = item.get("description") if isinstance(item, dict) else item
            if isinstance(description, str) and description.strip():
                action_items.append({"description": description.strip()[:4_096], "completed": False, "exported": False})
    emoji = result.get("emoji")
    category = result.get("category")
    return {
        "title": title.strip()[:160],
        "overview": overview.strip()[:2_000],
        "emoji": emoji.strip()[:16] if isinstance(emoji, str) and emoji.strip() else "🧠",
        "category": category.strip()[:64] if isinstance(category, str) and category.strip() else "other",
        "action_items": action_items,
        "events": [],
    }


async def _extracted_memories(env: object, payload: ExternalMemoryCreate) -> list[str] | None:
    if not payload.text or not payload.text.strip():
        return []
    source = payload.text_source_spec or payload.text_source
    result = await _workers_ai_json(
        env,
        "Extract durable, user-specific facts from the text below. Exclude commands, transient details, and guesses. "
        "Return an object with a memories array of concise strings, at most 50 items. "
        f"Source: {source}\n\n{payload.text}",
        1_024,
        MEMORY_SCHEMA,
    )
    values = result.get("memories") if isinstance(result, dict) else None
    if not isinstance(values, list):
        return None
    memories = []
    for item in values[:50]:
        content = item.get("content") if isinstance(item, dict) else item
        if isinstance(content, str) and content.strip():
            memories.append(content.strip()[:MAX_MEMORY_CONTENT_LENGTH])
    return memories


def _epoch(value: datetime | None) -> int | None:
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return int(aware.astimezone(timezone.utc).timestamp())


def _iso(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _date_epoch(value: str, *, end: bool) -> int:
    if len(value) == 10:
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if end:
            parsed = parsed.replace(hour=23, minute=59, second=59)
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp())


def _integration_conversation(row: dict[str, object], maximum: int) -> dict[str, object]:
    response = conversation_response(row, detail=True)
    if _flag(row.get("is_locked")):
        structured = response.get("structured")
        if isinstance(structured, dict):
            structured["title"] = ""
            structured["overview"] = ""
            structured["action_items"] = []
            structured["events"] = []
    segments = response.get("transcript_segments")
    if maximum != -1 and isinstance(segments, list):
        response["transcript_segments"] = segments[:maximum]
    return response


@router.post("/v2/integrations/{app_id}/user/conversations")
async def create_conversation(request: Request, app_id: str):
    app_id = _valid_identifier(app_id)
    uid = _valid_identifier(_query(request, "uid"))
    if not app_id or not uid:
        return _detail("uid and app_id are required", 422)
    try:
        payload = ExternalConversationCreate.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return _detail("Invalid conversation", 422)
    app, error = await _authorized_app(request, app_id, uid, ("create_conversation",))
    if error:
        return error
    env = request.scope["env"]
    limited = await _rate_limit(env, app_id, uid, "conversation_create")
    if isinstance(limited, JSONResponse):
        return limited
    try:
        webhook_targets = await _webhook_targets(env, uid)
    except Exception:
        return JSONResponse({"error": "integration fanout unavailable"}, status_code=503)
    structured = await _conversation_summary(env, payload.text)
    if structured is None:
        return JSONResponse({"error": "conversation processing unavailable"}, status_code=502)
    now = int(time.time())
    started = _epoch(payload.started_at) or now
    finished = _epoch(payload.finished_at) or started + 300
    conversation_id = uuid.uuid4().hex
    duration = max(0, finished - started)
    segment = {
        "id": uuid.uuid4().hex,
        "text": payload.text,
        "speaker": "SPEAKER_00",
        "speaker_id": 0,
        "is_user": False,
        "start": 0.0,
        "end": float(duration),
    }
    external_data = {
        "integration": {
            "app_id": app_id,
            "text_source": payload.text_source,
            "text_source_spec": payload.text_source_spec,
        }
    }
    conversation_payload = {
        "id": conversation_id,
        "created_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
        "finished_at": datetime.fromtimestamp(finished, timezone.utc).isoformat(),
        "source": "external_integration",
        "structured": structured,
        "transcript_segments": [segment],
        "discarded": False,
        "app_id": app_id,
        "language": payload.language or "en",
        "external_data": external_data,
        "geolocation": payload.geolocation,
        "status": "completed",
    }
    fanout_json = json.dumps(conversation_payload, ensure_ascii=False, separators=(",", ":"))
    statements = [
        env.APP_DB.prepare(
            "INSERT INTO cf_conversations "
            "(uid, id, created_at, updated_at, started_at, finished_at, source, language, status, visibility, "
            "starred, discarded, is_locked, deferred, private_cloud_sync_enabled, folder_id, client_device_id, "
            "client_platform, structured_json, transcript_segments_json, photos_json, audio_files_json, "
            "conversation_audio_json, apps_results_json, suggested_apps_json, geolocation_json, external_data_json, "
            "calendar_event_json, app_id) VALUES (?, ?, ?, ?, ?, ?, 'external_integration', ?, 'completed', "
            "'private', 0, 0, 0, 0, 0, NULL, ?, ?, ?, ?, '[]', '[]', NULL, '[]', '[]', ?, ?, NULL, ?)"
        ).bind(
            uid,
            conversation_id,
            now,
            now,
            started,
            finished,
            payload.language or "en",
            payload.client_device_id,
            payload.client_platform,
            json.dumps(structured, ensure_ascii=False, separators=(",", ":")),
            json.dumps([segment], ensure_ascii=False, separators=(",", ":")),
            (
                json.dumps(payload.geolocation, ensure_ascii=False, separators=(",", ":"))
                if payload.geolocation is not None
                else None
            ),
            json.dumps(external_data, ensure_ascii=False, separators=(",", ":")),
            app_id,
        ),
        usage_source_statement(
            env,
            uid=uid,
            source_kind="conversation",
            source_id=conversation_id,
            occurred_at=finished,
            transcription_seconds=duration,
            words_transcribed=len(re.findall(r"\S+", payload.text)),
            insights_gained=1 + len(structured["action_items"]),
            updated_at=now,
        ),
    ]
    for item in structured["action_items"]:
        description = str(item["description"])
        item_id = uuid.uuid4().hex
        idempotency = hashlib.sha256(f"{uid}\0{conversation_id}\0{description}".encode()).hexdigest()
        statements.append(
            env.APP_DB.prepare(
                "INSERT INTO cf_action_items "
                "(uid, id, description, status, completed, owner, source, provenance_json, conversation_id, "
                "created_at, updated_at, idempotency_key) "
                "VALUES (?, ?, ?, 'active', 0, 'unknown', 'integration', ?, ?, ?, ?, ?)"
            ).bind(
                uid,
                item_id,
                description,
                json.dumps([{"kind": "integration", "app_id": app_id}], separators=(",", ":")),
                conversation_id,
                now,
                now,
                idempotency,
            )
        )
    for target_app_id, webhook_url in webhook_targets:
        delivery_id = uuid.uuid4().hex
        statements.append(
            env.APP_DB.prepare(
                "INSERT INTO cf_integration_webhook_outbox "
                "(delivery_id, app_id, uid, conversation_id, webhook_url, payload_json, status, attempts, "
                "not_before, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)"
            ).bind(
                delivery_id,
                target_app_id,
                uid,
                conversation_id,
                webhook_url,
                fanout_json,
                now,
                now,
                now,
            )
        )
    try:
        await env.APP_DB.batch(statements)
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)
    return {}


@router.post("/v2/integrations/{app_id}/user/memories")
async def create_memories(request: Request, app_id: str):
    app_id = _valid_identifier(app_id)
    uid = _valid_identifier(_query(request, "uid"))
    if not app_id or not uid:
        return _detail("uid and app_id are required", 422)
    try:
        payload = ExternalMemoryCreate.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return _detail("Either text or explicit memories(facts) are required and cannot be empty", 422)
    _, error = await _authorized_app(request, app_id, uid, ("create_memories", "create_facts"))
    if error:
        return error
    env = request.scope["env"]
    limited = await _rate_limit(env, app_id, uid, "memory_create")
    if isinstance(limited, JSONResponse):
        return limited
    extracted = await _extracted_memories(env, payload)
    if extracted is None:
        return JSONResponse({"error": "memory extraction unavailable"}, status_code=502)
    values: list[tuple[str, list[str], dict[str, object]]] = []
    for memory in payload.memories or []:
        values.append(
            (
                memory.content,
                memory.tags or [],
                {
                    "kind": "integration_explicit_memory",
                    "source_id": memory.source_id,
                    "source_url": memory.source_url,
                    "artifact_ref": memory.artifact_ref,
                },
            )
        )
    source = payload.text_source_spec or payload.text_source
    for content in extracted:
        values.append(
            (
                content,
                [],
                {
                    "kind": "integration_text",
                    "text_source": source,
                    "source_id": payload.source_id,
                    "source_url": payload.source_url,
                    "artifact_ref": payload.artifact_ref,
                },
            )
        )
    now = int(time.time())
    statements = []
    for content, tags, provenance in values[:MAX_MEMORIES]:
        memory_id = uuid.uuid4().hex
        statements.extend(
            [
                env.APP_DB.prepare(
                    "INSERT INTO cf_memories "
                    "(uid, id, content, category, visibility, tags_json, qualifiers_json, manually_added, app_id, "
                    "memory_tier, valid_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'system', 'private', ?, ?, 0, ?, 'short_term', ?, ?, ?)"
                ).bind(
                    uid,
                    memory_id,
                    content,
                    json.dumps(tags, ensure_ascii=False, separators=(",", ":")),
                    json.dumps({"integration": provenance}, ensure_ascii=False, separators=(",", ":")),
                    app_id,
                    now,
                    now,
                    now,
                ),
                usage_source_statement(
                    env,
                    uid=uid,
                    source_kind="memory",
                    source_id=memory_id,
                    occurred_at=now,
                    memories_created=1,
                    updated_at=now,
                ),
            ]
        )
    try:
        if statements:
            await env.APP_DB.batch(statements)
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    return {}


@router.get("/v2/integrations/{app_id}/memories")
async def list_memories(request: Request, app_id: str):
    app_id = _valid_identifier(app_id)
    uid = _valid_identifier(_query(request, "uid"))
    if not app_id or not uid:
        return _detail("uid and app_id are required", 422)
    _, error = await _authorized_app(request, app_id, uid, ("read_memories", "read_facts"))
    if error:
        return error
    try:
        limit = int(_query(request, "limit") or "100")
        offset = int(_query(request, "offset") or "0")
    except ValueError:
        return _detail("Invalid pagination", 422)
    if limit < 1 or limit > MAX_LIST_LIMIT or offset < 0 or offset > MAX_OFFSET:
        return _detail("Invalid pagination", 422)
    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(
                MEMORY_SELECT + "WHERE uid = ? AND deleted_at IS NULL AND invalid_at IS NULL "
                "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
            )
            .bind(uid, limit, offset)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    memories = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = memory_response(row)
        content = item.get("content")
        if _flag(row.get("is_locked")) and isinstance(content, str) and len(content) > 70:
            item["content"] = content[:70] + "..."
        memories.append(item)
    return {"memories": memories}


def _conversation_filters(request: Request, uid: str) -> tuple[str, list[object]] | JSONResponse:
    try:
        limit = int(_query(request, "limit") or "100")
        offset = int(_query(request, "offset") or "0")
    except ValueError:
        return _detail("Invalid pagination", 422)
    if limit < 1 or limit > MAX_LIST_LIMIT or offset < 0 or offset > MAX_OFFSET:
        return _detail("Invalid pagination", 422)
    clauses = ["uid = ?"]
    args: list[object] = [uid]
    include_discarded = (_query(request, "include_discarded") or "false").lower()
    if include_discarded not in {"true", "false"}:
        return _detail("Invalid include_discarded", 422)
    if include_discarded == "false":
        clauses.append("discarded = 0")
    statuses = [value for value in _query_list(request, "statuses") if value]
    if len(statuses) > MAX_STATUSES:
        return _detail(f"statuses accepts at most {MAX_STATUSES} values", 400)
    if statuses:
        clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
        args.extend(statuses)
    for name, operator, end in (("start_date", ">=", False), ("end_date", "<=", True)):
        value = _query(request, name)
        if not value:
            continue
        try:
            timestamp = _date_epoch(value, end=end)
        except ValueError:
            return _detail(f"Invalid {name} format", 400)
        clauses.append(f"created_at {operator} ?")
        args.append(timestamp)
    query = _CONVERSATION_SELECT + "WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    args.extend((limit, offset))
    return query, args


def _maximum_segments(request: Request) -> int | JSONResponse:
    try:
        maximum = int(_query(request, "max_transcript_segments") or "100")
    except ValueError:
        return _detail("Invalid max_transcript_segments", 422)
    if maximum < -1 or maximum > MAX_TRANSCRIPT_SEGMENTS:
        return _detail("Invalid max_transcript_segments", 422)
    return maximum


@router.get("/v2/integrations/{app_id}/conversations")
async def list_conversations(request: Request, app_id: str):
    app_id = _valid_identifier(app_id)
    uid = _valid_identifier(_query(request, "uid"))
    if not app_id or not uid:
        return _detail("uid and app_id are required", 422)
    _, error = await _authorized_app(request, app_id, uid, ("read_conversations",))
    if error:
        return error
    maximum = _maximum_segments(request)
    if isinstance(maximum, JSONResponse):
        return maximum
    query = _conversation_filters(request, uid)
    if isinstance(query, JSONResponse):
        return query
    sql, args = query
    try:
        result = await request.scope["env"].APP_DB.prepare(sql).bind(*args).all()
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return {"conversations": [_integration_conversation(row, maximum) for row in rows if isinstance(row, dict)]}


@router.post("/v2/integrations/{app_id}/search/conversations")
async def search_conversations(request: Request, app_id: str):
    app_id = _valid_identifier(app_id)
    uid = _valid_identifier(_query(request, "uid"))
    if not app_id or not uid:
        return _detail("uid and app_id are required", 422)
    try:
        search = ConversationSearch.model_validate(await _bounded_json(request, 32_000))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return _detail("Invalid conversation search", 422)
    _, error = await _authorized_app(request, app_id, uid, ("read_conversations",))
    if error:
        return error
    maximum = _maximum_segments(request)
    if isinstance(maximum, JSONResponse):
        return maximum
    fts = _fts_query(uid, search.query)
    if search.query.strip() and fts is None:
        return {"conversations": [], "total_pages": 1, "current_page": search.page, "per_page": search.per_page}
    clauses = ["c.uid = ?", "c.is_locked = 0"]
    args: list[object] = [uid]
    table = "FROM cf_conversations c "
    order = "ORDER BY c.created_at DESC, c.id DESC "
    if fts:
        table += "JOIN cf_conversations_fts ON cf_conversations_fts.rowid = c.rowid "
        clauses.append("cf_conversations_fts MATCH ?")
        args.append(fts)
        order = "ORDER BY rank, c.created_at DESC, c.id DESC "
    if not search.include_discarded:
        clauses.append("c.discarded = 0")
    for value, operator, end in ((search.start_date, ">=", False), (search.end_date, "<=", True)):
        if value:
            try:
                timestamp = _date_epoch(value, end=end)
            except ValueError:
                return _detail("Invalid date format", 400)
            clauses.append(f"c.created_at {operator} ?")
            args.append(timestamp)
    where = "WHERE " + " AND ".join(clauses) + " "
    offset = (search.page - 1) * search.per_page
    env = request.scope["env"]
    try:
        count = await env.APP_DB.prepare("SELECT COUNT(*) AS count " + table + where).bind(*args).first()
        result = (
            await env.APP_DB.prepare(_CONVERSATION_SEARCH_SELECT + table + where + order + "LIMIT ? OFFSET ?")
            .bind(*args, search.per_page, offset)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "conversation search unavailable"}, status_code=503)
    total = int(count.get("count") or 0) if isinstance(count, dict) else 0
    pages = max(1, math.ceil(total / search.per_page))
    rows = result.get("results", []) if isinstance(result, dict) else []
    return {
        "conversations": [_integration_conversation(row, maximum) for row in rows if isinstance(row, dict)],
        "total_pages": pages,
        "current_page": search.page,
        "per_page": search.per_page,
    }


@router.get("/v2/integrations/{app_id}/tasks")
async def list_tasks(request: Request, app_id: str):
    app_id = _valid_identifier(app_id)
    uid = _valid_identifier(_query(request, "uid"))
    if not app_id or not uid:
        return _detail("uid and app_id are required", 422)
    _, error = await _authorized_app(request, app_id, uid, ("read_tasks",))
    if error:
        return error
    try:
        limit = int(_query(request, "limit") or "100")
        offset = int(_query(request, "offset") or "0")
    except ValueError:
        return _detail("Invalid pagination", 422)
    if limit < 1 or limit > MAX_LIST_LIMIT or offset < 0 or offset > MAX_OFFSET:
        return _detail("Invalid pagination", 422)
    clauses = ["uid = ?", "deleted = 0"]
    args: list[object] = [uid]
    completed = _query(request, "completed")
    if completed is not None:
        if completed.lower() not in {"true", "false"}:
            return _detail("Invalid completed filter", 422)
        clauses.append("completed = ?")
        args.append(1 if completed.lower() == "true" else 0)
    conversation_id = _query(request, "conversation_id")
    if conversation_id:
        if not _valid_identifier(conversation_id):
            return _detail("Invalid conversation_id", 422)
        clauses.append("conversation_id = ?")
        args.append(conversation_id)
    for name, column, operator, end in (
        ("start_date", "created_at", ">=", False),
        ("end_date", "created_at", "<=", True),
        ("due_start_date", "due_at", ">=", False),
        ("due_end_date", "due_at", "<=", True),
    ):
        value = _query(request, name)
        if value:
            try:
                timestamp = _date_epoch(value, end=end)
            except ValueError:
                return _detail(f"Invalid {name} format", 400)
            clauses.append(f"{column} {operator} ?")
            args.append(timestamp)
    sql = (
        "SELECT id, description, completed, created_at, updated_at, due_at, completed_at, conversation_id, is_locked "
        "FROM cf_action_items WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    )
    args.extend((limit, offset))
    try:
        result = await request.scope["env"].APP_DB.prepare(sql).bind(*args).all()
    except Exception:
        return JSONResponse({"error": "tasks unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    tasks = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        description = str(row.get("description") or "")
        if _flag(row.get("is_locked")) and len(description) > 70:
            description = description[:70] + "..."
        tasks.append(
            {
                "id": str(row.get("id") or ""),
                "description": description,
                "completed": _flag(row.get("completed")),
                "created_at": _iso(row.get("created_at")),
                "updated_at": _iso(row.get("updated_at")),
                "due_at": _iso(row.get("due_at")),
                "completed_at": _iso(row.get("completed_at")),
                "conversation_id": row.get("conversation_id"),
            }
        )
    return {"tasks": tasks}


async def _notification(request: Request, app_id: str, uid: str, message: str) -> JSONResponse:
    app, error = await _authorized_app(request, app_id, uid)
    if error:
        return error
    assert app is not None
    env = request.scope["env"]
    limited = await _rate_limit(env, app_id, uid, "notification")
    if isinstance(limited, JSONResponse):
        return limited
    remaining, reset = limited
    external = app.get("external_integration")
    chat_enabled = isinstance(external, dict) and _flag(external.get("chat_messages_enabled"))
    target = "main" if chat_enabled and external.get("chat_messages_target") == "main" else "app"
    name = str(app.get("name") or "App")[:160]
    now = int(time.time())
    notification_id = uuid.uuid4().hex
    data = {
        "text": message,
        "plugin_id": app_id,
        "from_integration": "true",
        "type": "text",
        "notification_type": "plugin",
        "navigate_to": "/chat/omi" if target == "main" else f"/chat/{app_id}",
    }
    statements = [
        env.APP_DB.prepare(
            "INSERT INTO cf_notification_outbox "
            "(notification_id, source_kind, source_id, uid, title, body, data_json, status, attempts, "
            "not_before, created_at, updated_at) "
            "VALUES (?, 'integration', ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)"
        ).bind(
            notification_id,
            f"{app_id}:{notification_id}",
            uid,
            f"{name} says",
            message,
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            now,
            now,
            now,
        )
    ]
    if chat_enabled:
        chat_app_id = None if target == "main" else app_id
        app_clause = "app_id IS NULL" if chat_app_id is None else "app_id = ?"
        app_args: tuple[object, ...] = () if chat_app_id is None else (chat_app_id,)
        try:
            session = (
                await env.APP_DB.prepare(
                    "SELECT id FROM cf_chat_sessions WHERE uid = ? AND "
                    + app_clause
                    + " ORDER BY updated_at DESC, id DESC LIMIT 1"
                )
                .bind(uid, *app_args)
                .first()
            )
        except Exception:
            return JSONResponse({"error": "chat messages unavailable"}, status_code=503)
        session_id = (
            str(session["id"]) if isinstance(session, dict) and isinstance(session.get("id"), str) else uuid.uuid4().hex
        )
        if not isinstance(session, dict):
            statements.append(
                env.APP_DB.prepare(
                    "INSERT INTO cf_chat_sessions "
                    "(uid, id, title, preview, created_at, updated_at, app_id, message_count, starred) "
                    "VALUES (?, ?, 'New Chat', NULL, ?, ?, ?, 0, 0)"
                ).bind(uid, session_id, now, now, chat_app_id)
            )
        chat_text = f"[{name}]: {message}" if target == "main" else message
        message_id = uuid.uuid4().hex
        message_json = {
            "id": message_id,
            "text": chat_text,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sender": "ai",
            "type": "text",
            "app_id": chat_app_id,
            "plugin_id": chat_app_id,
            "session_id": session_id,
            "chat_session_id": session_id,
            "from_external_integration": True,
            "rating": None,
            "reported": False,
            "memories_id": [],
            "memories": [],
            "files_id": [],
            "files": [],
            "metadata": {},
            "content_blocks": [],
        }
        statements.extend(
            [
                env.APP_DB.prepare(
                    "INSERT INTO cf_chat_messages (uid, id, app_id, created_at, message_json) VALUES (?, ?, ?, ?, ?)"
                ).bind(
                    uid,
                    message_id,
                    chat_app_id,
                    int(time.time() * 1_000_000) * 2,
                    json.dumps(message_json, ensure_ascii=False, separators=(",", ":")),
                ),
                env.APP_DB.prepare(
                    "UPDATE cf_chat_sessions SET updated_at = ?, message_count = message_count + 1, preview = ? "
                    "WHERE uid = ? AND id = ?"
                ).bind(now, chat_text[:100], uid, session_id),
            ]
        )
    try:
        await env.APP_DB.batch(statements)
    except Exception:
        return JSONResponse({"error": "notification unavailable"}, status_code=503)
    return JSONResponse(
        {"status": "Ok"},
        status_code=200,
        headers={
            "X-RateLimit-Limit": str(RATE_LIMITS["notification"]),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset),
        },
    )


@router.post("/v2/integrations/{app_id}/notification")
async def send_notification(request: Request, app_id: str):
    app_id = _valid_identifier(app_id)
    uid = _valid_identifier(_query(request, "uid"))
    message = _query(request, "message")
    if not app_id or not uid or not isinstance(message, str) or not message.strip():
        return _detail("uid, app_id, and message are required", 422)
    if len(message) > MAX_MESSAGE_LENGTH:
        return _detail("message is too long", 422)
    return await _notification(request, app_id, uid, message)


@router.post("/v1/integrations/notification")
async def send_notification_v1(request: Request):
    try:
        body = await _bounded_json(request, 16_000)
    except (json.JSONDecodeError, ValueError, TypeError):
        return _detail("invalid request body", 400)
    if not isinstance(body, dict):
        return _detail("invalid request body", 400)
    app_id = _valid_identifier(body.get("aid"))
    uid = _valid_identifier(body.get("uid"))
    message = body.get("message")
    if not app_id:
        return _detail("aid (app id) in request body is required", 400)
    if not isinstance(message, str) or not message.strip():
        return _detail("message is required", 400)
    if not uid:
        return _detail("uid is required", 400)
    if len(message) > MAX_MESSAGE_LENGTH:
        return _detail("message is too long", 400)
    return await _notification(request, app_id, uid, message)
