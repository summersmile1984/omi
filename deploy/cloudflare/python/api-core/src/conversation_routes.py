"""D1-backed conversation read projection for the isolated Cloudflare profile.

This module owns bounded list/count/detail reads over an explicit conversation
projection. It deliberately does not claim conversation finalization, memory
extraction, merge, search indexes, audio deletion, or downstream integrations;
those authorities remain legacy until their write and reader contracts move
together. Projection rows can be loaded by the reviewed D1 backfill workflow.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, model_validator

from internal_auth import decode_context

router = APIRouter()

MAX_ID_LENGTH = 256
MAX_LIST_LIMIT = 100
MAX_OFFSET = 100_000
MAX_FILTER_VALUES = 20
MAX_JSON_BYTES = 1_000_000
MAX_WRITE_BYTES = 4_000_000
MAX_SEGMENTS = 2_000
CONVERSATION_STATUSES = frozenset({"in_progress", "processing", "merging", "completed", "failed"})
CONVERSATION_SOURCES = frozenset(
    {
        "friend",
        "omi",
        "fieldy",
        "bee",
        "plaud",
        "frame",
        "friend_com",
        "apple_watch",
        "phone",
        "phone_call",
        "desktop",
        "openglass",
        "screenpipe",
        "workflow",
        "sdcard",
        "external_integration",
        "limitless",
        "rayban_meta",
        "onboarding",
        "unknown",
    }
)
CONVERSATION_VISIBILITIES = frozenset({"private", "shared", "public"})


class ConversationProjectionWrite(BaseModel):
    """Bounded, client-facing write shape for a pre-transcribed conversation."""

    model_config = {"extra": "ignore"}

    id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    created_at: datetime
    updated_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    source: str = Field(default="omi", min_length=1, max_length=64)
    language: str | None = Field(default=None, max_length=32)
    status: str = Field(default="completed", min_length=1, max_length=32)
    visibility: str = Field(default="private", min_length=1, max_length=16)
    starred: bool = False
    discarded: bool = False
    is_locked: bool = False
    deferred: bool = False
    private_cloud_sync_enabled: bool = False
    folder_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    client_device_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    client_platform: str | None = Field(default=None, max_length=64)
    structured: dict[str, object] = Field(default_factory=dict)
    transcript_segments: list[dict[str, object]] = Field(default_factory=list, max_length=MAX_SEGMENTS)
    photos: list[dict[str, object]] = Field(default_factory=list, max_length=MAX_SEGMENTS)
    audio_files: list[dict[str, object]] = Field(default_factory=list, max_length=MAX_SEGMENTS)
    conversation_audio: dict[str, object] | None = None
    apps_results: list[dict[str, object]] = Field(default_factory=list, max_length=MAX_SEGMENTS)
    suggested_summarization_apps: list[str] = Field(default_factory=list, max_length=MAX_SEGMENTS)
    geolocation: dict[str, object] | None = None
    external_data: dict[str, object] | None = None
    calendar_event: dict[str, object] | None = None

    @model_validator(mode="after")
    def validate_projection(self) -> "ConversationProjectionWrite":
        if self.status not in CONVERSATION_STATUSES:
            raise ValueError("unsupported conversation status")
        if self.source not in CONVERSATION_SOURCES:
            raise ValueError("unsupported conversation source")
        if self.visibility not in CONVERSATION_VISIBILITIES:
            raise ValueError("unsupported conversation visibility")
        if self.started_at and self.finished_at and self.finished_at < self.started_at:
            raise ValueError("finished_at must be after started_at")
        return self


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _bool(value: object) -> bool:
    return bool(value) and value not in ("0", "false", "False", "no")


def _iso(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _json_value(value: object, fallback: object) -> object:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_JSON_BYTES:
        return fallback
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    return parsed


def _json_object(value: object) -> dict[str, object]:
    parsed = _json_value(value, {})
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: object) -> list[object]:
    parsed = _json_value(value, [])
    return parsed if isinstance(parsed, list) else []


def _response(row: dict[str, object], *, detail: bool) -> dict[str, object]:
    structured = _json_object(row.get("structured_json"))
    # Keep the projection self-describing when an older import only populated
    # indexed columns. Nested structured fields remain JSON-owned, not a
    # generic document compatibility layer.
    structured.setdefault("title", str(row.get("title") or ""))
    structured.setdefault("overview", str(row.get("overview") or ""))
    structured.setdefault("category", str(row.get("category") or ""))
    structured.setdefault("action_items", [])
    structured.setdefault("events", [])

    segments = _json_list(row.get("transcript_segments_json")) if detail else []
    apps_results = _json_list(row.get("apps_results_json")) if detail else []
    photos = _json_list(row.get("photos_json")) if detail else []
    audio_files = _json_list(row.get("audio_files_json")) if detail else []
    suggested_apps = _json_list(row.get("suggested_apps_json")) if detail else []
    if _bool(row.get("is_locked")):
        segments = []
        apps_results = []
        suggested_apps = []
        structured["action_items"] = []
        structured["events"] = []

    response: dict[str, object] = {
        "id": str(row.get("id") or ""),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "started_at": _iso(row.get("started_at")),
        "finished_at": _iso(row.get("finished_at")),
        "source": row.get("source") or "omi",
        "language": row.get("language"),
        "structured": structured,
        "transcript_segments": segments,
        "transcript_segments_compressed": False,
        "geolocation": _json_value(row.get("geolocation_json"), None),
        "photos": photos,
        "audio_files": audio_files,
        "conversation_audio": _json_value(row.get("conversation_audio_json"), None),
        "private_cloud_sync_enabled": _bool(row.get("private_cloud_sync_enabled")),
        "apps_results": apps_results,
        "suggested_summarization_apps": suggested_apps,
        "plugins_results": [],
        "external_data": _json_value(row.get("external_data_json"), None),
        "app_id": None,
        "discarded": _bool(row.get("discarded")),
        "visibility": row.get("visibility") or "private",
        "starred": _bool(row.get("starred")),
        "processing_memory_id": None,
        "processing_conversation_id": None,
        "status": row.get("status") or "completed",
        "is_locked": _bool(row.get("is_locked")),
        "deferred": _bool(row.get("deferred")),
        "data_protection_level": None,
        "folder_id": row.get("folder_id"),
        "call_id": None,
        "calendar_event": _json_value(row.get("calendar_event_json"), None),
        "client_device_id": row.get("client_device_id"),
        "client_platform": row.get("client_platform"),
    }
    return response


def _parse_csv(value: str | None, field: str) -> list[str] | JSONResponse:
    values = [item.strip() for item in (value or "").split(",") if item.strip()]
    if len(values) > MAX_FILTER_VALUES:
        return JSONResponse({"error": f"{field} accepts at most {MAX_FILTER_VALUES} values"}, status_code=400)
    return values


def _epoch(value: datetime | None) -> int | None:
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return int(aware.astimezone(timezone.utc).timestamp())


def _dump_json(value: object, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be JSON serializable") from error
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError(f"{field} exceeds the size limit")
    return encoded


def _base_query(request: Request, *, count: bool = False) -> tuple[str, list[object]] | JSONResponse:
    params = request.query_params
    raw_statuses = params.get("statuses")
    if raw_statuses is None and not count:
        raw_statuses = "processing,completed"
    statuses = _parse_csv(raw_statuses, "statuses")
    if isinstance(statuses, JSONResponse):
        return statuses
    sources = _parse_csv(params.get("sources"), "sources")
    if isinstance(sources, JSONResponse):
        return sources
    start_date = params.get("start_date")
    end_date = params.get("end_date")
    clauses = ["uid = ?"]
    args: list[object] = [str(_auth_context(request)["uid"])]  # type: ignore[index]
    if statuses:
        clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
        args.extend(statuses)
    if sources:
        clauses.append("source IN (" + ",".join("?" for _ in sources) + ")")
        args.extend(sources)
    include_discarded = params.get("include_discarded", "false" if count else "true").lower() == "true"
    if not include_discarded:
        clauses.append("discarded = 0")
    folder_id = params.get("folder_id")
    if folder_id is not None:
        if not folder_id or len(folder_id) > MAX_ID_LENGTH:
            return JSONResponse({"error": "invalid folder id"}, status_code=400)
        clauses.append("folder_id = ?")
        args.append(folder_id)
    starred = params.get("starred")
    if starred is not None:
        if starred.lower() not in {"true", "false"}:
            return JSONResponse({"error": "invalid starred filter"}, status_code=400)
        clauses.append("starred = ?")
        args.append(1 if starred.lower() == "true" else 0)
    for raw, operator in ((start_date, ">="), (end_date, "<=")):
        if raw is None:
            continue
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return JSONResponse({"error": "invalid date filter"}, status_code=400)
        timestamp = _epoch(value)
        clauses.append(f"created_at {operator} ?")
        args.append(timestamp)
    select = "COUNT(*) AS count" if count else (
        "uid, id, created_at, updated_at, started_at, finished_at, source, language, status, visibility, "
        "starred, discarded, is_locked, deferred, private_cloud_sync_enabled, folder_id, client_device_id, client_platform, "
        "structured_json, transcript_segments_json, photos_json, audio_files_json, conversation_audio_json, "
        "apps_results_json, suggested_apps_json, geolocation_json, external_data_json, calendar_event_json"
    )
    query = f"SELECT {select} FROM cf_conversations WHERE " + " AND ".join(clauses)
    if not count:
        query += " ORDER BY created_at DESC, id DESC"
    return query, args


@router.post("/v1/cf/conversations")
async def store_conversation_projection(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        raw = await request.body()
        if len(raw) > MAX_WRITE_BYTES:
            return JSONResponse({"error": "conversation body too large"}, status_code=413)
        projection = ConversationProjectionWrite.model_validate(json.loads(raw))
        json_fields = {
            "structured_json": _dump_json(projection.structured, "structured"),
            "transcript_segments_json": _dump_json(projection.transcript_segments, "transcript_segments"),
            "photos_json": _dump_json(projection.photos, "photos"),
            "audio_files_json": _dump_json(projection.audio_files, "audio_files"),
            "conversation_audio_json": _dump_json(projection.conversation_audio, "conversation_audio", nullable=True),
            "apps_results_json": _dump_json(projection.apps_results, "apps_results"),
            "suggested_apps_json": _dump_json(
                projection.suggested_summarization_apps, "suggested_summarization_apps"
            ),
            "geolocation_json": _dump_json(projection.geolocation, "geolocation", nullable=True),
            "external_data_json": _dump_json(projection.external_data, "external_data", nullable=True),
            "calendar_event_json": _dump_json(projection.calendar_event, "calendar_event", nullable=True),
        }
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
        return JSONResponse({"error": "invalid conversation projection"}, status_code=400)

    uid = str(context["uid"])
    now = int(time.time())
    created_at = _epoch(projection.created_at)
    updated_at = _epoch(projection.updated_at) or now
    started_at = _epoch(projection.started_at)
    finished_at = _epoch(projection.finished_at)
    env = request.scope["env"]
    try:
        await env.APP_DB.prepare(
            "INSERT INTO cf_conversations "
            "(uid, id, created_at, updated_at, started_at, finished_at, source, language, status, visibility, "
            "starred, discarded, is_locked, deferred, private_cloud_sync_enabled, folder_id, client_device_id, "
            "client_platform, structured_json, transcript_segments_json, photos_json, audio_files_json, "
            "conversation_audio_json, apps_results_json, suggested_apps_json, geolocation_json, external_data_json, "
            "calendar_event_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(uid, id) DO UPDATE SET updated_at = excluded.updated_at, started_at = excluded.started_at, "
            "finished_at = excluded.finished_at, source = excluded.source, language = excluded.language, "
            "status = excluded.status, visibility = excluded.visibility, starred = excluded.starred, "
            "discarded = excluded.discarded, is_locked = excluded.is_locked, deferred = excluded.deferred, "
            "private_cloud_sync_enabled = excluded.private_cloud_sync_enabled, folder_id = excluded.folder_id, "
            "client_device_id = excluded.client_device_id, client_platform = excluded.client_platform, "
            "structured_json = excluded.structured_json, transcript_segments_json = excluded.transcript_segments_json, "
            "photos_json = excluded.photos_json, audio_files_json = excluded.audio_files_json, "
            "conversation_audio_json = excluded.conversation_audio_json, apps_results_json = excluded.apps_results_json, "
            "suggested_apps_json = excluded.suggested_apps_json, geolocation_json = excluded.geolocation_json, "
            "external_data_json = excluded.external_data_json, calendar_event_json = excluded.calendar_event_json"
        ).bind(
            uid,
            projection.id,
            created_at,
            updated_at,
            started_at,
            finished_at,
            projection.source,
            projection.language,
            projection.status,
            projection.visibility,
            int(projection.starred),
            int(projection.discarded),
            int(projection.is_locked),
            int(projection.deferred),
            int(projection.private_cloud_sync_enabled),
            projection.folder_id,
            projection.client_device_id,
            projection.client_platform,
            json_fields["structured_json"],
            json_fields["transcript_segments_json"],
            json_fields["photos_json"],
            json_fields["audio_files_json"],
            json_fields["conversation_audio_json"],
            json_fields["apps_results_json"],
            json_fields["suggested_apps_json"],
            json_fields["geolocation_json"],
            json_fields["external_data_json"],
            json_fields["calendar_event_json"],
        ).run()
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)
    return {"conversation_id": projection.id, "status": "stored"}


@router.get("/v1/conversations")
@router.get("/v1/cf/conversations")
async def list_conversations(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    params = request.query_params
    try:
        limit = int(params.get("limit", "100"))
        offset = int(params.get("offset", "0"))
    except ValueError:
        return JSONResponse({"error": "invalid pagination"}, status_code=400)
    if limit < 1 or limit > MAX_LIST_LIMIT or offset < 0 or offset > MAX_OFFSET:
        return JSONResponse({"error": "invalid pagination"}, status_code=400)
    query_result = _base_query(request)
    if isinstance(query_result, JSONResponse):
        return query_result
    query, args = query_result
    try:
        rows = await request.scope["env"].APP_DB.prepare(query + " LIMIT ? OFFSET ?").bind(*args, limit, offset).all()
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)
    results = rows.get("results", []) if isinstance(rows, dict) else []
    return [_response(row, detail=False) for row in results if isinstance(row, dict)]


@router.get("/v1/conversations/count")
@router.get("/v1/cf/conversations/count")
async def count_conversations(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    query_result = _base_query(request, count=True)
    if isinstance(query_result, JSONResponse):
        return query_result
    query, args = query_result
    try:
        row = await request.scope["env"].APP_DB.prepare(query).bind(*args).first()
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)
    return {"count": int(row.get("count") or 0) if isinstance(row, dict) else 0}


@router.get("/v1/conversations/{conversation_id}")
@router.get("/v1/cf/conversations/{conversation_id}")
async def get_conversation(request: Request, conversation_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    try:
        row = await request.scope["env"].APP_DB.prepare(
            "SELECT uid, id, created_at, updated_at, started_at, finished_at, source, language, status, visibility, "
            "starred, discarded, is_locked, deferred, private_cloud_sync_enabled, folder_id, client_device_id, client_platform, "
            "structured_json, transcript_segments_json, photos_json, audio_files_json, conversation_audio_json, "
            "apps_results_json, suggested_apps_json, geolocation_json, external_data_json, calendar_event_json "
            "FROM cf_conversations WHERE uid = ? AND id = ?"
        ).bind(str(context["uid"]), conversation_id).first()
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)
    if not isinstance(row, dict):
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    # Locked conversations preserve metadata but never expose transcript or
    # derived action/event content; `_response` applies that redaction.
    return _response(row, detail=True)
