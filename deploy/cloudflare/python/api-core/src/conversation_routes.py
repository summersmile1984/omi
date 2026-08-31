"""D1-backed conversation read projection for the isolated Cloudflare profile.

This module owns bounded list/count/detail/search reads and projection deletion
over an explicit conversation projection. The explicit finalize/status admission
and processor live in ``conversation_finalization_routes``; Jobs owns its lease
and retry loop. Reprocess, merge, generic memory extraction, audio deletion,
and downstream integrations remain legacy until their write and reader
contracts move together. Projection
rows can also be loaded by the reviewed D1 backfill workflow.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
import time
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, ValidationError, model_validator

from internal_auth import decode_context
from account_routes import usage_source_statement
from vector_search import publish_vector_projection, vector_outbox_statement

router = APIRouter()

MAX_ID_LENGTH = 256
MAX_LIST_LIMIT = 100
MAX_OFFSET = 100_000
MAX_FILTER_VALUES = 20
MAX_JSON_BYTES = 1_000_000
MAX_WRITE_BYTES = 4_000_000
MAX_SEGMENT_TEXT_LENGTH = 10_000
MAX_ACTION_ITEM_DESCRIPTION_LENGTH = 4_096
MAX_SEGMENTS = 2_000
MAX_SEARCH_QUERY_LENGTH = 500
MAX_SEARCH_TERMS = 20
MAX_AUDIO_FILES = 100
MAX_SUGGESTED_APP_IDS = 100
MAX_APP_PAYLOAD_BYTES = 500_000
AUDIO_URL_TTL_SECONDS = 60 * 60
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
TRANSCRIPT_PROVIDER_BUCKETS = ("deepgram", "soniox", "speechmatics", "whisperx")


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
    app_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
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


class ConversationSegmentTextUpdate(BaseModel):
    """Bounded text-only edit for one projected transcript segment."""

    model_config = {"extra": "ignore"}

    segment_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    text: str = Field(min_length=1, max_length=MAX_SEGMENT_TEXT_LENGTH)


class ConversationBulkSegmentAssignment(BaseModel):
    """Assignment targets used by the released desktop bulk speaker editor."""

    model_config = {"extra": "ignore"}

    segment_ids: list[str] = Field(default_factory=list, max_length=MAX_SEGMENTS)
    assign_type: str = Field(min_length=1, max_length=32)
    value: str | None = Field(default=None, max_length=MAX_ID_LENGTH)


class ConversationEventsStateUpdate(BaseModel):
    """Parallel event indexes and created flags used by the legacy client."""

    model_config = {"extra": "ignore"}

    events_idx: list[int] = Field(max_length=MAX_SEGMENTS)
    values: list[bool] = Field(max_length=MAX_SEGMENTS)

    @model_validator(mode="after")
    def validate_parallel_arrays(self) -> "ConversationEventsStateUpdate":
        if len(self.events_idx) != len(self.values):
            raise ValueError("events_idx and values must have the same length")
        if any(index < 0 for index in self.events_idx):
            raise ValueError("event indexes must be non-negative")
        return self


class ConversationActionItemsStateUpdate(BaseModel):
    """Parallel action-item indexes and completion flags used by legacy clients."""

    model_config = {"extra": "ignore"}

    items_idx: list[int] = Field(max_length=100)
    values: list[bool] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_parallel_arrays(self) -> "ConversationActionItemsStateUpdate":
        if len(self.items_idx) != len(self.values):
            raise ValueError("items_idx and values must have the same length")
        if any(index < 0 for index in self.items_idx):
            raise ValueError("action-item indexes must be non-negative")
        return self


class ConversationActionItemDescriptionUpdate(BaseModel):
    """Description replacement for the legacy conversation action-item editor."""

    model_config = {"extra": "ignore"}

    old_description: str = Field(min_length=1, max_length=MAX_ACTION_ITEM_DESCRIPTION_LENGTH)
    description: str = Field(min_length=1, max_length=MAX_ACTION_ITEM_DESCRIPTION_LENGTH)


class ConversationActionItemDelete(BaseModel):
    """Description identity retained for the legacy conversation delete route."""

    model_config = {"extra": "ignore"}

    description: str = Field(min_length=1, max_length=MAX_ACTION_ITEM_DESCRIPTION_LENGTH)
    completed: bool


class ConversationSummaryUpdate(BaseModel):
    """Summary content written to the default or app-specific projection."""

    model_config = {"extra": "ignore"}

    app_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    content: str = Field(min_length=1, max_length=10_000)


class ConversationSearchRequest(BaseModel):
    """Bounded full-text search over the uid-scoped D1 projection."""

    model_config = {"extra": "ignore"}

    query: str = Field(default="", max_length=MAX_SEARCH_QUERY_LENGTH)
    page: int = Field(default=1, ge=1, le=10_000)
    per_page: int = Field(default=10, ge=1, le=MAX_LIST_LIMIT)
    include_discarded: bool = False
    start_date: datetime | None = None
    end_date: datetime | None = None
    speaker_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)

    @model_validator(mode="after")
    def validate_date_range(self) -> "ConversationSearchRequest":
        if self.start_date and self.end_date and _epoch(self.end_date) < _epoch(self.start_date):
            raise ValueError("end_date must not precede start_date")
        if len(self.query.split()) > MAX_SEARCH_TERMS:
            raise ValueError("search query has too many terms")
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


def _suggested_app(row: dict[str, object], uid: str) -> dict[str, object] | None:
    """Project one approved marketplace app for a conversation suggestion."""
    if not _bool(row.get("approved")) or _bool(row.get("disabled")):
        return None
    raw = row.get("data_json")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_APP_PAYLOAD_BYTES:
        raise ValueError("invalid app payload")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("invalid app payload")
    capabilities = payload.get("capabilities", [])
    if not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities):
        raise ValueError("invalid app capabilities")
    if "persona" in capabilities or _bool(payload.get("private")):
        return None

    app = dict(payload)
    app["id"] = str(row.get("id") or app.get("id") or "")
    app["approved"] = True
    app["rejected"] = False
    app["disabled"] = False
    app["is_popular"] = _bool(row.get("is_popular"))
    app["installs"] = max(0, int(row.get("installs") or 0))
    app["rating_count"] = max(0, int(row.get("rating_count") or 0))
    if row.get("rating_avg") is not None:
        app["rating_avg"] = float(row["rating_avg"])
    paid = _bool(app.get("is_paid")) or bool(app.get("payment_link") or app.get("payment_link_id"))
    app["is_user_paid"] = _bool(row.get("user_entitled"))
    app["enabled"] = _bool(row.get("user_enabled")) and (app["is_user_paid"] if paid else True)
    payment_link = app.get("payment_link")
    if isinstance(payment_link, str) and payment_link:
        separator = "&" if "?" in payment_link else "?"
        app["payment_link"] = f"{payment_link}{separator}client_reference_id=uid_{quote(uid, safe='')}"
    for key in (
        "email",
        "memory_prompt",
        "chat_prompt",
        "persona_prompt",
        "payment_product_id",
        "payment_price_id",
        "payment_link_id",
        "money_made",
        "usage_count",
    ):
        app.pop(key, None)
    return app


def _usage_words(segments: list[dict[str, object]]) -> int:
    return sum(len(re.findall(r"\S+", str(segment.get("text") or ""))) for segment in segments)


def _usage_insights(projection: ConversationProjectionWrite) -> int:
    count = 0
    for key in ("title", "overview"):
        value = projection.structured.get(key)
        if isinstance(value, str):
            count += sum(1 for sentence in re.split(r"[.!?]+", value) if len(sentence.split()) > 5)
    for key in ("action_items", "events"):
        value = projection.structured.get(key)
        if isinstance(value, list):
            count += len(value)
    for result in projection.apps_results:
        value = result.get("content")
        if isinstance(value, str):
            count += sum(1 for sentence in re.split(r"[.!?]+", value) if len(sentence.split()) > 5)
    return count


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
        "app_id": row.get("app_id"),
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
    select = (
        "COUNT(*) AS count"
        if count
        else (
            "uid, id, created_at, updated_at, started_at, finished_at, source, language, status, visibility, "
            "starred, discarded, is_locked, deferred, private_cloud_sync_enabled, folder_id, client_device_id, client_platform, "
            "structured_json, transcript_segments_json, photos_json, audio_files_json, conversation_audio_json, "
            "apps_results_json, suggested_apps_json, geolocation_json, external_data_json, calendar_event_json, app_id"
        )
    )
    query = f"SELECT {select} FROM cf_conversations WHERE " + " AND ".join(clauses)
    if not count:
        query += " ORDER BY created_at DESC, id DESC"
    return query, args


_CONVERSATION_SELECT = (
    "SELECT uid, id, created_at, updated_at, started_at, finished_at, source, language, status, visibility, "
    "starred, discarded, is_locked, deferred, private_cloud_sync_enabled, folder_id, client_device_id, client_platform, "
    "structured_json, transcript_segments_json, photos_json, audio_files_json, conversation_audio_json, "
    "apps_results_json, suggested_apps_json, geolocation_json, external_data_json, calendar_event_json, app_id "
    "FROM cf_conversations "
)

_CONVERSATION_SEARCH_SELECT = (
    "SELECT c.uid, c.id, c.created_at, c.updated_at, c.started_at, c.finished_at, c.source, c.language, "
    "c.status, c.visibility, c.starred, c.discarded, c.is_locked, c.deferred, "
    "c.private_cloud_sync_enabled, c.folder_id, c.client_device_id, c.client_platform, "
    "c.structured_json, c.transcript_segments_json, c.photos_json, c.audio_files_json, "
    "c.conversation_audio_json, c.apps_results_json, c.suggested_apps_json, c.geolocation_json, "
    "c.external_data_json, c.calendar_event_json, c.app_id "
)


def _fts_query(uid: str, value: str) -> str | None:
    tokens = re.findall(r"[^\W_]+", value, flags=re.UNICODE)
    if not tokens:
        return None
    quoted = [f'uid_token:"{uid.encode().hex()}"']
    for token in tokens:
        escaped = token.replace('"', '""')
        quoted.append(f'searchable_text:"{escaped}"*')
    return " AND ".join(quoted)


async def _first_conversation(env: object, uid: str, conversation_id: str) -> dict[str, object] | None:
    row = await env.APP_DB.prepare(_CONVERSATION_SELECT + "WHERE uid = ? AND id = ?").bind(uid, conversation_id).first()
    return row if isinstance(row, dict) else None


def _share_index_statement(
    env: object,
    *,
    uid: str,
    conversation_id: str,
    visibility: str,
    updated_at: int,
):
    if visibility == "private":
        return env.APP_DB.prepare(
            "DELETE FROM cf_shared_conversation_index WHERE conversation_id = ? AND uid = ?"
        ).bind(conversation_id, uid)
    return env.APP_DB.prepare(
        "INSERT INTO cf_shared_conversation_index (conversation_id, uid, visibility, updated_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(conversation_id) DO UPDATE SET "
        "visibility = excluded.visibility, updated_at = excluded.updated_at "
        "WHERE cf_shared_conversation_index.uid = excluded.uid"
    ).bind(conversation_id, uid, visibility, updated_at)


def _speaker_assignment(segment: dict[str, object]) -> str:
    if _bool(segment.get("is_user")):
        return "self"
    person_id = segment.get("person_id")
    if person_id:
        digest = hashlib.sha256(str(person_id).encode()).hexdigest()[:16]
        return f"person:{digest}"
    return "unassigned"


def _record_speaker_identity_confirmation(*, scope: str, before: list[str], after: list[str]) -> None:
    """Emit a bounded, PII-free equivalent of the legacy product event."""

    if not after:
        return
    kinds = ["person" if value.startswith("person:") else value for value in after]
    payload: dict[str, object] = {
        "event": "Speaker Identity Confirmed",
        "confirmation": "accepted" if before == after else "corrected",
        "assignment": kinds[0] if len(set(kinds)) == 1 else "mixed",
        "scope": scope,
        "affected_segment_count": len(after),
    }
    if len(set(after)) == 1 and kinds[0] == "person":
        payload["assignment_id"] = after[0]
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def _apply_speaker_assignment(segment: dict[str, object], assign_type: str, value: str | None) -> None:
    if assign_type == "is_user":
        segment["is_user"] = bool(value) if value is not None else False
        segment["person_id"] = None
        return
    segment["is_user"] = False
    segment["person_id"] = value


def _resolve_bulk_segment_indices(segments: list[object], requested_ids: list[str], *, status: object) -> list[int]:
    """Resolve exact transcript IDs and the legacy positional compatibility form."""

    by_id = {
        str(segment.get("id")): index
        for index, segment in enumerate(segments)
        if isinstance(segment, dict) and segment.get("id")
    }
    resolved: list[int] = []
    unresolved: list[str] = []
    allow_legacy_indices = str(status or "") == "completed"
    for requested_id in requested_ids:
        index = by_id.get(requested_id)
        if index is None and allow_legacy_indices and requested_id.startswith("#index:"):
            raw_index = requested_id[len("#index:") :]
            if raw_index.isascii() and raw_index.isdecimal():
                candidate = int(raw_index)
                if candidate < len(segments):
                    index = candidate
        if index is None:
            unresolved.append(requested_id)
        elif index not in resolved:
            resolved.append(index)
    if unresolved:
        targets = ", ".join(unresolved)[:1_000]
        raise ValueError(f"Unable to resolve transcript segment assignment target(s): {targets}")
    return resolved


async def _write_conversation_segments(
    env: object,
    *,
    uid: str,
    conversation_id: str,
    existing: dict[str, object],
    segments: list[object],
) -> dict[str, object] | JSONResponse:
    current_updated_at = int(existing.get("updated_at") or 0)
    next_updated_at = max(int(time.time()), current_updated_at + 1)
    encoded = _dump_json(segments, "transcript_segments")
    result = (
        await env.APP_DB.prepare(
            "UPDATE cf_conversations SET transcript_segments_json = ?, updated_at = ? "
            "WHERE uid = ? AND id = ? AND updated_at = ? RETURNING id"
        )
        .bind(encoded, next_updated_at, uid, conversation_id, current_updated_at)
        .first()
    )
    if not isinstance(result, dict) or result.get("id") != conversation_id:
        return JSONResponse({"error": "conversation changed, retry"}, status_code=409)
    updated = dict(existing)
    updated["transcript_segments_json"] = encoded
    updated["updated_at"] = next_updated_at
    return updated


async def _shared_people(env: object, uid: str, segments: list[object]) -> list[dict[str, object]]:
    person_ids = sorted(
        {str(segment["person_id"]) for segment in segments if isinstance(segment, dict) and segment.get("person_id")}
    )
    people: list[dict[str, object]] = []
    for start in range(0, len(person_ids), 100):
        chunk = person_ids[start : start + 100]
        placeholders = ", ".join("?" for _ in chunk)
        result = (
            await env.APP_DB.prepare(
                "SELECT id, name, speech_samples_json, speech_sample_transcripts_json, "
                "speech_samples_version, created_at, updated_at FROM cf_people "
                f"WHERE uid = ? AND id IN ({placeholders})"
            )
            .bind(uid, *chunk)
            .all()
        )
        rows = result.get("results", []) if isinstance(result, dict) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            people.append(
                {
                    "id": str(row.get("id") or ""),
                    "name": str(row.get("name") or ""),
                    "created_at": _iso(row.get("created_at")),
                    "updated_at": _iso(row.get("updated_at")),
                    "speech_samples": _json_list(row.get("speech_samples_json")),
                    "speech_sample_transcripts": (
                        _json_list(row.get("speech_sample_transcripts_json"))
                        if row.get("speech_sample_transcripts_json")
                        else None
                    ),
                    "speech_samples_version": int(row.get("speech_samples_version") or 3),
                }
            )
    people.sort(key=lambda item: str(item["id"]))
    return people


def _audio_signing_secret(env: object) -> str | None:
    value = getattr(env, "AUDIO_URL_SIGNING_SECRET", None) or getattr(env, "INTERNAL_ASSERTION_SECRET", None)
    return value if isinstance(value, str) and len(value) >= 16 else None


async def _recording_access_enabled(env: object, uid: str) -> bool:
    row = (
        await env.APP_DB.prepare(
            "SELECT EXISTS("
            "SELECT 1 FROM cf_user_privacy_settings "
            "WHERE uid = ? AND store_recording_permission = 1"
            ") AND NOT EXISTS("
            "SELECT 1 FROM cf_recording_deletion_intents WHERE uid = ?"
            ") AS allowed"
        )
        .bind(uid, uid)
        .first()
    )
    return isinstance(row, dict) and bool(row.get("allowed"))


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _audio_token(env: object, uid: str, conversation_id: str, audio_file_id: str, expires_at: int) -> str | None:
    secret = _audio_signing_secret(env)
    if not secret:
        return None
    payload = json.dumps(
        {"u": uid, "c": conversation_id, "a": audio_file_id, "e": expires_at, "v": 1},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    encoded = _b64url(payload)
    signature = _b64url(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def _audio_token_uid(
    env: object,
    token: object,
    conversation_id: str,
    audio_file_id: str,
    now: int,
) -> str | None:
    secret = _audio_signing_secret(env)
    if not secret or not isinstance(token, str) or len(token) > 2_048 or token.count(".") != 1:
        return None
    encoded, supplied = token.split(".", 1)
    expected = _b64url(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(supplied, expected):
        return None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        expires_at = int(payload["e"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        payload.get("v") != 1
        or payload.get("c") != conversation_id
        or payload.get("a") != audio_file_id
        or expires_at < now
        or expires_at > now + AUDIO_URL_TTL_SECONDS + 60
    ):
        return None
    uid = payload.get("u")
    return uid if isinstance(uid, str) and 0 < len(uid) <= MAX_ID_LENGTH else None


def _audio_files(row: dict[str, object]) -> list[dict[str, object]]:
    return [item for item in _json_list(row.get("audio_files_json"))[:MAX_AUDIO_FILES] if isinstance(item, dict)]


def _audio_file(row: dict[str, object], audio_file_id: str) -> dict[str, object] | None:
    return next((item for item in _audio_files(row) if item.get("id") == audio_file_id), None)


def _conversation_audio(row: dict[str, object]) -> dict[str, object] | None:
    value = _json_value(row.get("conversation_audio_json"), None)
    return value if isinstance(value, dict) else None


def _audio_storage_candidates(
    uid: str,
    conversation_id: str,
    audio_file: dict[str, object],
) -> list[tuple[str, str]]:
    prefixes = (
        f"sync-playback/{uid}/{conversation_id}/",
        f"playback/{uid}/{conversation_id}/",
        f"merged/{uid}/{conversation_id}/",
    )
    candidates: list[tuple[str, str]] = []
    explicit = audio_file.get("storage_key")
    content_type = audio_file.get("content_type")
    if isinstance(explicit, str) and explicit.startswith(prefixes):
        candidates.append((explicit, str(content_type or "audio/wav")[:100]))
    audio_file_id = str(audio_file.get("id") or "")
    if audio_file_id:
        candidates.extend(
            [
                (f"playback/{uid}/{conversation_id}/{audio_file_id}.mp3", "audio/mpeg"),
                (f"merged/{uid}/{conversation_id}/{audio_file_id}.wav", "audio/wav"),
            ]
        )
    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate[0] not in seen:
            unique.append(candidate)
            seen.add(candidate[0])
    return unique


async def _stored_audio(
    bucket: object,
    uid: str,
    conversation_id: str,
    audio_file: dict[str, object],
) -> tuple[str, str, object] | None:
    for key, content_type in _audio_storage_candidates(uid, conversation_id, audio_file):
        metadata = await bucket.head(key)
        if metadata is not None:
            return key, content_type, metadata
    return None


def _signed_audio_url(
    request: Request,
    env: object,
    uid: str,
    conversation_id: str,
    audio_file_id: str,
) -> str | None:
    expires_at = int(time.time()) + AUDIO_URL_TTL_SECONDS
    token = _audio_token(env, uid, conversation_id, audio_file_id, expires_at)
    if token is None:
        return None
    source = urlsplit(str(request.url))
    path = f"/v1/sync/audio/{quote(conversation_id, safe='')}/{quote(audio_file_id, safe='')}"
    return urlunsplit((source.scheme, source.netloc, path, urlencode({"token": token}), ""))


def _parse_audio_range(raw: str | None, size: int) -> tuple[int, int] | None:
    if not raw:
        return None
    if not raw.startswith("bytes=") or "," in raw or "-" not in raw[6:]:
        raise ValueError("unsupported range")
    start_raw, end_raw = raw[6:].strip().split("-", 1)
    if size <= 0 or (not start_raw and not end_raw):
        raise ValueError("unsatisfiable range")
    try:
        if not start_raw:
            suffix = int(end_raw)
            if suffix <= 0:
                raise ValueError("invalid suffix")
            return max(0, size - suffix), size - 1
        start = int(start_raw)
        end = size - 1 if not end_raw else min(size - 1, int(end_raw))
    except ValueError as error:
        raise ValueError("invalid range") from error
    if start < 0 or start >= size or end < start:
        raise ValueError("unsatisfiable range")
    return start, end


def _audio_object_size(metadata: object) -> int:
    value = metadata.get("size") if isinstance(metadata, dict) else getattr(metadata, "size", 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


async def _r2_audio_chunks(stored: object):
    stream = getattr(stored, "body", None)
    get_reader = getattr(stream, "getReader", None)
    if callable(get_reader):
        reader = get_reader()
        try:
            while True:
                result = await reader.read()
                if bool(getattr(result, "done", False)):
                    break
                value = getattr(result, "value", b"")
                to_py = getattr(value, "to_py", None)
                yield bytes(to_py() if callable(to_py) else value)
        finally:
            release_lock = getattr(reader, "releaseLock", None)
            if callable(release_lock):
                release_lock()
        return
    array_buffer = getattr(stored, "arrayBuffer", None)
    if callable(array_buffer):
        yield bytes(await array_buffer())


def _transcript_provider_bucket(segment: dict[str, object]) -> str | None:
    """Map imported provider names to the four legacy response buckets."""

    provider = segment.get("stt_provider")
    if not isinstance(provider, str):
        return None
    normalized = provider.strip().lower()
    if normalized.startswith("deepgram"):
        return "deepgram"
    if normalized.startswith("soniox"):
        return "soniox"
    if normalized.startswith("speechmatics"):
        return "speechmatics"
    if normalized.startswith("fal_whisperx") or normalized.startswith("whisperx"):
        return "whisperx"
    return None


def _transcript_start(segment: dict[str, object]) -> float:
    value = segment.get("start", 0)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _analytics_number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


async def _person_names(env: object, uid: str, person_ids: set[str]) -> dict[str, str]:
    """Load only the person names referenced by the bounded transcript projection."""

    names: dict[str, str] = {}
    ids = sorted(person_ids)
    for start in range(0, len(ids), 100):
        chunk = ids[start : start + 100]
        placeholders = ", ".join("?" for _ in chunk)
        result = (
            await env.APP_DB.prepare(f"SELECT id, name FROM cf_people WHERE uid = ? AND id IN ({placeholders})")
            .bind(uid, *chunk)
            .all()
        )
        rows = result.get("results", []) if isinstance(result, dict) else []
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                names[row["id"]] = str(row.get("name") or "")
    return names


def _conversation_analytics(
    conversation_id: str, segments: list[dict[str, object]], names: dict[str, str]
) -> dict[str, object]:
    seconds: dict[str, float] = {}
    words: dict[str, int] = {}
    labels: dict[str, str] = {}
    person_ids: dict[str, str | None] = {}
    is_user_flags: dict[str, bool] = {}

    for segment in segments:
        if _bool(segment.get("is_user")):
            key, label, person_id, is_user = "user", "You", None, True
        else:
            raw_person_id = segment.get("person_id")
            person_id = str(raw_person_id) if raw_person_id else None
            if person_id:
                key, label, is_user = f"person:{person_id}", names.get(person_id) or "Unknown", False
            else:
                speaker_id = segment.get("speaker_id")
                if speaker_id is not None:
                    key, label = f"speaker:{speaker_id}", f"Speaker {speaker_id}"
                else:
                    speaker = segment.get("speaker") or "SPEAKER_00"
                    key, label = f"speaker:{speaker}", str(speaker)
                is_user = False
        talk_seconds = max(0.0, _analytics_number(segment.get("end")) - _analytics_number(segment.get("start")))
        text = segment.get("text") if isinstance(segment.get("text"), str) else ""
        seconds[key] = seconds.get(key, 0.0) + talk_seconds
        words[key] = words.get(key, 0) + len(text.split())
        labels[key] = label
        person_ids[key] = person_id
        is_user_flags[key] = is_user

    total_seconds = sum(seconds.values())
    total_words = sum(words.values())
    speakers: list[dict[str, object]] = []
    for key, talk_seconds in seconds.items():
        word_count = words[key]
        speakers.append(
            {
                "speaker": labels[key],
                "person_id": person_ids[key],
                "is_user": is_user_flags[key],
                "talk_seconds": round(talk_seconds, 1),
                "word_count": word_count,
                "words_per_minute": round(word_count / (talk_seconds / 60.0), 1) if talk_seconds > 0 else 0.0,
                "talk_share": round(talk_seconds / total_seconds, 3) if total_seconds > 0 else 0.0,
                "_sort_talk_seconds": talk_seconds,
            }
        )
    speakers.sort(
        key=lambda item: (-float(item.pop("_sort_talk_seconds", 0)), -int(item["word_count"]), str(item["speaker"]))
    )
    return {
        "conversation_id": conversation_id,
        "total_seconds": round(total_seconds, 1),
        "total_words": total_words,
        "words_per_minute": round(total_words / (total_seconds / 60.0), 1) if total_seconds > 0 else 0.0,
        "speaker_count": len(speakers),
        "speakers": speakers,
    }


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
            "suggested_apps_json": _dump_json(projection.suggested_summarization_apps, "suggested_summarization_apps"),
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
        recording_enabled = await _recording_access_enabled(env, uid)
        if not recording_enabled:
            json_fields["audio_files_json"] = "[]"
            json_fields["conversation_audio_json"] = None
        conversation_statement = env.APP_DB.prepare(
            "INSERT INTO cf_conversations "
            "(uid, id, created_at, updated_at, started_at, finished_at, source, language, status, visibility, "
            "starred, discarded, is_locked, deferred, private_cloud_sync_enabled, folder_id, client_device_id, "
            "client_platform, structured_json, transcript_segments_json, photos_json, audio_files_json, "
            "conversation_audio_json, apps_results_json, suggested_apps_json, geolocation_json, external_data_json, "
            "calendar_event_json, app_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
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
            "external_data_json = excluded.external_data_json, calendar_event_json = excluded.calendar_event_json, "
            "app_id = COALESCE(excluded.app_id, cf_conversations.app_id)"
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
            int(projection.private_cloud_sync_enabled and recording_enabled),
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
            projection.app_id,
        )
        duration = 0
        if started_at is not None and finished_at is not None:
            duration = max(0, min(finished_at - started_at, 7 * 24 * 60 * 60))
        usage_statement = usage_source_statement(
            env,
            uid=uid,
            source_kind="conversation",
            source_id=projection.id,
            occurred_at=finished_at or created_at,
            transcription_seconds=duration,
            words_transcribed=_usage_words(projection.transcript_segments),
            insights_gained=0 if projection.discarded else _usage_insights(projection),
            updated_at=updated_at,
        )
        vector_projection = vector_outbox_statement(
            env,
            uid=uid,
            source_kind="conversation",
            source_id=projection.id,
            desired_version=updated_at,
            operation="upsert" if projection.status == "completed" and not projection.discarded else "delete",
        )
        share_index = _share_index_statement(
            env,
            uid=uid,
            conversation_id=projection.id,
            visibility=projection.visibility,
            updated_at=updated_at,
        )
        await env.APP_DB.batch([conversation_statement, usage_statement, vector_projection, share_index])
    except Exception as error:
        if "conversation share id collision" in str(error):
            return JSONResponse({"error": "conversation share id is already owned"}, status_code=409)
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)
    await publish_vector_projection(env, uid=uid, source_kind="conversation", source_id=projection.id)
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


@router.post("/v1/conversations/search")
async def search_conversations(request: Request):
    """Search indexed titles, summaries, transcript text, and exact IDs."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        raw = await request.body()
        if len(raw) > 32_000:
            return JSONResponse({"error": "search body too large"}, status_code=413)
        search = ConversationSearchRequest.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError, TypeError):
        return JSONResponse({"error": "invalid conversation search"}, status_code=400)

    uid = str(context["uid"])
    env = request.scope["env"]
    if search.speaker_id and search.speaker_id != "user":
        try:
            person = (
                await env.APP_DB.prepare("SELECT id FROM cf_people WHERE uid = ? AND id = ?")
                .bind(uid, search.speaker_id)
                .first()
            )
        except Exception:
            return JSONResponse({"error": "conversation search unavailable"}, status_code=503)
        if not isinstance(person, dict):
            return JSONResponse({"error": "speaker not found"}, status_code=404)
    fts_query = _fts_query(uid, search.query)
    if search.query.strip() and fts_query is None:
        return {
            "items": [],
            "total_pages": 1,
            "current_page": search.page,
            "per_page": search.per_page,
        }
    clauses = ["c.uid = ?", "c.is_locked = 0"]
    args: list[object] = [uid]
    table = "FROM cf_conversations c "
    order = "ORDER BY c.created_at DESC, c.id DESC "
    if fts_query:
        table += "JOIN cf_conversations_fts ON cf_conversations_fts.rowid = c.rowid "
        clauses.append("cf_conversations_fts MATCH ?")
        args.append(fts_query)
        order = "ORDER BY rank, c.created_at DESC, c.id DESC "
    if not search.include_discarded:
        clauses.append("c.discarded = 0")
    if search.start_date:
        clauses.append("c.created_at >= ?")
        args.append(_epoch(search.start_date))
    if search.end_date:
        clauses.append("c.created_at <= ?")
        args.append(_epoch(search.end_date))
    if search.speaker_id == "user":
        clauses.append(
            "EXISTS (SELECT 1 FROM json_each(c.transcript_segments_json) segment "
            "WHERE json_extract(segment.value, '$.is_user') IN (1, 'true'))"
        )
    elif search.speaker_id:
        clauses.append(
            "EXISTS (SELECT 1 FROM json_each(c.transcript_segments_json) segment "
            "WHERE json_extract(segment.value, '$.person_id') = ?)"
        )
        args.append(search.speaker_id)

    where = "WHERE " + " AND ".join(clauses) + " "
    offset = (search.page - 1) * search.per_page
    try:
        count_row = await env.APP_DB.prepare("SELECT COUNT(*) AS count " + table + where).bind(*args).first()
        rows = (
            await env.APP_DB.prepare(_CONVERSATION_SEARCH_SELECT + table + where + order + "LIMIT ? OFFSET ?")
            .bind(*args, search.per_page, offset)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "conversation search unavailable"}, status_code=503)

    total = int(count_row.get("count") or 0) if isinstance(count_row, dict) else 0
    total_pages = max(1, (total + search.per_page - 1) // search.per_page)
    results = rows.get("results", []) if isinstance(rows, dict) else []
    return {
        "items": [_response(row, detail=False) for row in results if isinstance(row, dict)],
        "total_pages": total_pages,
        "current_page": search.page,
        "per_page": search.per_page,
    }


@router.delete("/v1/conversations/{conversation_id}")
async def delete_conversation(request: Request, conversation_id: str):
    """Delete only the uid-owned D1 projection, matching legacy cascade=false."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    raw_cascade = request.query_params.get("cascade", "false").lower()
    if raw_cascade not in {"true", "false"}:
        return JSONResponse({"error": "invalid cascade flag"}, status_code=400)
    if raw_cascade == "true":
        return JSONResponse(
            {"error": "cascade conversation deletion is not migrated"},
            status_code=409,
        )

    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        conversation = await _first_conversation(env, uid, conversation_id)
        if conversation is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        previous_version = int(conversation.get("updated_at") or conversation.get("created_at") or 0)
        desired_version = max(int(time.time()), previous_version + 1)
        await env.APP_DB.batch(
            [
                env.APP_DB.prepare(
                    "DELETE FROM cf_shared_conversation_index WHERE conversation_id = ? AND uid = ?"
                ).bind(conversation_id, uid),
                env.APP_DB.prepare("DELETE FROM cf_conversations WHERE uid = ? AND id = ?").bind(uid, conversation_id),
                env.APP_DB.prepare(
                    "UPDATE cf_folders SET conversation_count = ("
                    "SELECT COUNT(*) FROM cf_conversations c "
                    "WHERE c.uid = cf_folders.uid AND c.folder_id = cf_folders.id AND c.discarded = 0"
                    ") WHERE uid = ?"
                ).bind(uid),
                vector_outbox_statement(
                    env,
                    uid=uid,
                    source_kind="conversation",
                    source_id=conversation_id,
                    desired_version=desired_version,
                    operation="delete",
                ),
            ]
        )
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)
    await publish_vector_projection(env, uid=uid, source_kind="conversation", source_id=conversation_id)
    return {"status": "Ok"}


@router.get("/v1/conversations/{conversation_id}/shared")
async def get_shared_conversation(conversation_id: str, request: Request):
    """Return a public shared projection through its unique D1 owner index."""

    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    env = request.scope["env"]
    try:
        row = (
            await env.APP_DB.prepare(
                _CONVERSATION_SEARCH_SELECT + "FROM cf_shared_conversation_index i "
                "JOIN cf_conversations c ON c.uid = i.uid AND c.id = i.conversation_id "
                "WHERE i.conversation_id = ? AND i.visibility IN ('shared', 'public') "
                "AND c.visibility IN ('shared', 'public') LIMIT 1"
            )
            .bind(conversation_id)
            .first()
        )
        if not isinstance(row, dict):
            return JSONResponse({"error": "Conversation is private"}, status_code=404)
        if _bool(row.get("is_locked")):
            return JSONResponse(
                {"error": "A paid plan is required to access this conversation."},
                status_code=402,
            )
        response = _response(row, detail=True)
        response["geolocation"] = None
        response["external_data"] = None
        response["data_protection_level"] = None
        response["people"] = await _shared_people(env, str(row["uid"]), response["transcript_segments"])
        return response
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)


@router.patch("/v1/conversations/{conversation_id}/visibility")
async def set_conversation_visibility(request: Request, conversation_id: str):
    """Atomically update the owner row and public D1 owner index."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    visibility = request.query_params.get("value")
    if visibility not in CONVERSATION_VISIBILITIES:
        return JSONResponse({"error": "invalid conversation visibility"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        existing = await _first_conversation(env, uid, conversation_id)
        if existing is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        if _bool(existing.get("is_locked")):
            return JSONResponse(
                {"error": "A paid plan is required to access this conversation."},
                status_code=402,
            )
        current_updated_at = int(existing.get("updated_at") or 0)
        next_updated_at = max(int(time.time()), current_updated_at + 1)
        conversation_update = env.APP_DB.prepare(
            "UPDATE cf_conversations SET visibility = ?, updated_at = ? WHERE uid = ? AND id = ?"
        ).bind(visibility, next_updated_at, uid, conversation_id)
        share_update = _share_index_statement(
            env,
            uid=uid,
            conversation_id=conversation_id,
            visibility=visibility,
            updated_at=next_updated_at,
        )
        await env.APP_DB.batch([conversation_update, share_update])
    except Exception as error:
        if "conversation share id collision" in str(error):
            return JSONResponse({"error": "conversation share id is already owned"}, status_code=409)
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)
    return {"status": "Ok"}


@router.patch("/v1/conversations/{conversation_id}/segments/{segment_idx}/assign")
async def assign_conversation_segment(request: Request, conversation_id: str, segment_idx: int):
    """Assign one bounded transcript segment without the legacy-disabled training path."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        existing = await _first_conversation(env, uid, conversation_id)
        if existing is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        if _bool(existing.get("is_locked")):
            return JSONResponse(
                {"error": "A paid plan is required to access this conversation."},
                status_code=402,
            )
        assign_type = request.query_params.get("assign_type")
        if assign_type not in {"is_user", "person_id"}:
            return JSONResponse({"error": "Invalid assign type"}, status_code=400)
        value = request.query_params.get("value")
        if value == "null":
            value = None
        segments = _json_list(existing.get("transcript_segments_json"))
        if segment_idx < 0 or segment_idx >= len(segments) or not isinstance(segments[segment_idx], dict):
            return JSONResponse({"error": "Segment not found"}, status_code=404)
        segment = segments[segment_idx]
        before = [_speaker_assignment(segment)]
        _apply_speaker_assignment(segment, assign_type, value)
        after = [_speaker_assignment(segment)]
        updated = await _write_conversation_segments(
            env,
            uid=uid,
            conversation_id=conversation_id,
            existing=existing,
            segments=segments,
        )
        if isinstance(updated, JSONResponse):
            return updated
        _record_speaker_identity_confirmation(scope="segment", before=before, after=after)
        return _response(updated, detail=True)
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)


@router.patch("/v1/conversations/{conversation_id}/assign-speaker/{speaker_id}")
async def assign_conversation_speaker(request: Request, conversation_id: str, speaker_id: int):
    """Assign every transcript segment owned by one numeric diarization speaker."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        existing = await _first_conversation(env, uid, conversation_id)
        if existing is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        if _bool(existing.get("is_locked")):
            return JSONResponse(
                {"error": "A paid plan is required to access this conversation."},
                status_code=402,
            )
        assign_type = request.query_params.get("assign_type")
        if assign_type not in {"is_user", "person_id"}:
            return JSONResponse({"error": "Invalid assign type"}, status_code=400)
        value = request.query_params.get("value")
        if value == "null":
            value = None
        segments = _json_list(existing.get("transcript_segments_json"))
        targets = [
            segment for segment in segments if isinstance(segment, dict) and segment.get("speaker_id") == speaker_id
        ]
        before = [_speaker_assignment(segment) for segment in targets]
        for segment in targets:
            _apply_speaker_assignment(segment, assign_type, value)
        after = [_speaker_assignment(segment) for segment in targets]
        updated = await _write_conversation_segments(
            env,
            uid=uid,
            conversation_id=conversation_id,
            existing=existing,
            segments=segments,
        )
        if isinstance(updated, JSONResponse):
            return updated
        _record_speaker_identity_confirmation(scope="speaker", before=before, after=after)
        return _response(updated, detail=True)
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)


@router.patch("/v1/conversations/{conversation_id}/segments/assign-bulk")
async def assign_conversation_segments_bulk(request: Request, conversation_id: str):
    """Assign a bounded set of transcript segments in one CAS-protected write."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    try:
        raw = await request.body()
        if len(raw) > 64_000:
            return JSONResponse({"error": "bulk assignment body too large"}, status_code=413)
        assignment = ConversationBulkSegmentAssignment.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError, TypeError):
        return JSONResponse({"error": "invalid bulk segment assignment"}, status_code=400)

    if assignment.assign_type not in {"is_user", "person_id"}:
        return JSONResponse({"error": "Invalid assign type"}, status_code=400)
    value = None if assignment.value == "null" else assignment.value
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        existing = await _first_conversation(env, uid, conversation_id)
        if existing is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        if _bool(existing.get("is_locked")):
            return JSONResponse(
                {"error": "A paid plan is required to access this conversation."},
                status_code=402,
            )
        segments = _json_list(existing.get("transcript_segments_json"))
        try:
            indices = _resolve_bulk_segment_indices(
                segments,
                assignment.segment_ids,
                status=existing.get("status"),
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        before = [_speaker_assignment(segments[index]) for index in indices if isinstance(segments[index], dict)]
        for index in indices:
            segment = segments[index]
            if isinstance(segment, dict):
                _apply_speaker_assignment(segment, assignment.assign_type, value)
        updated = await _write_conversation_segments(
            env,
            uid=uid,
            conversation_id=conversation_id,
            existing=existing,
            segments=segments,
        )
        if isinstance(updated, JSONResponse):
            return updated
        after = [_speaker_assignment(segments[index]) for index in indices if isinstance(segments[index], dict)]
        _record_speaker_identity_confirmation(scope="bulk", before=before, after=after)
        return _response(updated, detail=True)
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)


@router.get("/v1/conversations/{conversation_id}")
@router.get("/v1/cf/conversations/{conversation_id}")
async def get_conversation(request: Request, conversation_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    try:
        row = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT uid, id, created_at, updated_at, started_at, finished_at, source, language, status, visibility, "
                "starred, discarded, is_locked, deferred, private_cloud_sync_enabled, folder_id, client_device_id, client_platform, "
                "structured_json, transcript_segments_json, photos_json, audio_files_json, conversation_audio_json, "
                "apps_results_json, suggested_apps_json, geolocation_json, external_data_json, calendar_event_json, app_id "
                "FROM cf_conversations WHERE uid = ? AND id = ?"
            )
            .bind(str(context["uid"]), conversation_id)
            .first()
        )
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)
    if not isinstance(row, dict):
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    source = request.query_params.get("source")
    if source is not None and source != "omi":
        return JSONResponse({"error": "only source=omi is supported"}, status_code=400)
    raw_include_discarded = request.query_params.get("include_discarded", "true").lower()
    if raw_include_discarded not in {"true", "false"}:
        return JSONResponse({"error": "invalid include_discarded"}, status_code=400)
    if source == "omi" and row.get("source") != "omi":
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    if raw_include_discarded == "false" and _bool(row.get("discarded")):
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    # Locked conversations preserve metadata but never expose transcript or
    # derived action/event content; `_response` applies that redaction.
    return _response(row, detail=True)


@router.get("/v1/conversations/{conversation_id}/suggested-apps")
async def get_conversation_suggested_apps(request: Request, conversation_id: str):
    """Return approved marketplace apps suggested by a conversation projection."""
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
            return {"suggested_apps": [], "conversation_id": conversation_id}
        app_ids = [
            str(value)
            for value in _json_list(conversation.get("suggested_apps_json"))
            if isinstance(value, str) and value and len(value) <= MAX_ID_LENGTH
        ]
        app_ids = list(dict.fromkeys(app_ids))
        if len(app_ids) > MAX_SUGGESTED_APP_IDS:
            return JSONResponse({"error": "too many suggested apps"}, status_code=400)
        if not app_ids:
            return {"suggested_apps": [], "conversation_id": conversation_id}
        placeholders = ", ".join("?" for _ in app_ids)
        result = (
            await env.APP_DB.prepare(
                "SELECT c.id, c.approved, c.disabled, c.is_popular, c.installs, c.rating_avg, c.rating_count, c.data_json, "
                "CASE WHEN u.app_id IS NULL THEN 0 ELSE 1 END AS user_enabled, "
                "CASE WHEN s.status IN ('active', 'trialing') AND s.current_period_end > unixepoch() "
                "THEN 1 ELSE 0 END AS user_entitled "
                "FROM cf_app_catalog c "
                "LEFT JOIN cf_user_enabled_apps u ON u.app_id = c.id AND u.uid = ? "
                "LEFT JOIN cf_app_subscriptions s ON s.app_id = c.id AND s.uid = ? "
                f"WHERE c.id IN ({placeholders})"
            )
            .bind(uid, uid, *app_ids)
            .all()
        )
        rows = result.get("results", []) if isinstance(result, dict) else []
        by_id: dict[str, dict[str, object]] = {}
        for row in rows:
            if not isinstance(row, dict):
                return JSONResponse({"error": "app catalog unavailable"}, status_code=503)
            app_id = str(row.get("id") or "")
            projected = _suggested_app(row, uid)
            if projected is not None:
                by_id[app_id] = projected
        return {
            "suggested_apps": [by_id[app_id] for app_id in app_ids if app_id in by_id],
            "conversation_id": conversation_id,
        }
    except (TypeError, ValueError, OverflowError, json.JSONDecodeError):
        return JSONResponse({"error": "app catalog unavailable"}, status_code=503)
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)


@router.get("/v1/conversations/{conversation_id}/photos")
async def get_conversation_photos(request: Request, conversation_id: str):
    """Read the bounded photo projection without touching the legacy subcollection."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    try:
        row = await _first_conversation(request.scope["env"], str(context["uid"]), conversation_id)
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)
    if row is None:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    # Locked conversations fail closed for image payloads. The projection is
    # intentionally bounded at write time; filter malformed imported values so
    # the canonical List[ConversationPhoto] contract remains stable.
    if _bool(row.get("is_locked")):
        return []
    return [photo for photo in _json_list(row.get("photos_json")) if isinstance(photo, dict)][:MAX_SEGMENTS]


@router.get("/v1/conversations/{conversation_id}/transcripts")
async def get_conversation_transcripts(request: Request, conversation_id: str):
    """Read provider-specific transcript projections without touching Firestore."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    try:
        row = await _first_conversation(request.scope["env"], str(context["uid"]), conversation_id)
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)
    if row is None:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    if _bool(row.get("is_locked")):
        return JSONResponse(
            {"error": "A paid plan is required to access this conversation."},
            status_code=402,
        )

    grouped = {bucket: [] for bucket in TRANSCRIPT_PROVIDER_BUCKETS}
    for segment in _json_list(row.get("transcript_segments_json"))[:MAX_SEGMENTS]:
        if not isinstance(segment, dict):
            continue
        bucket = _transcript_provider_bucket(segment)
        if bucket is not None:
            grouped[bucket].append(segment)
    for bucket in TRANSCRIPT_PROVIDER_BUCKETS:
        grouped[bucket].sort(key=_transcript_start)
    return grouped


@router.get("/v1/conversations/{conversation_id}/analytics")
async def get_conversation_analytics(request: Request, conversation_id: str):
    """Compute bounded per-speaker analytics from the D1 transcript projection."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    env = request.scope["env"]
    uid = str(context["uid"])
    try:
        row = await _first_conversation(env, uid, conversation_id)
        if row is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        if _bool(row.get("is_locked")):
            return JSONResponse(
                {"error": "A paid plan is required to access this conversation."},
                status_code=402,
            )
        segments = [
            segment
            for segment in _json_list(row.get("transcript_segments_json"))[:MAX_SEGMENTS]
            if isinstance(segment, dict)
        ]
        person_ids = {str(segment["person_id"]) for segment in segments if segment.get("person_id")}
        names = await _person_names(env, uid, person_ids)
        return _conversation_analytics(conversation_id, segments, names)
    except Exception:
        return JSONResponse({"error": "conversation analytics unavailable"}, status_code=503)


@router.get("/v1/sync/audio/{conversation_id}/urls")
async def get_conversation_audio_urls(request: Request, conversation_id: str):
    """Return short-lived Worker URLs for uid-scoped R2 playback objects."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    env = request.scope["env"]
    uid = str(context["uid"])
    try:
        row = await _first_conversation(env, uid, conversation_id)
        if row is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        if _bool(row.get("is_locked")):
            return JSONResponse(
                {"error": "A paid plan is required to access this conversation."},
                status_code=402,
            )
        if not await _recording_access_enabled(env, uid):
            return {"audio_files": [], "conversation_audio": None, "poll_after_ms": None}
        bucket = getattr(env, "ASSETS", None)
        if bucket is None or not callable(getattr(bucket, "head", None)):
            return JSONResponse({"error": "recording storage is not configured"}, status_code=503)
        results: list[dict[str, object]] = []
        for audio_file in _audio_files(row):
            audio_file_id = str(audio_file.get("id") or "")
            if not audio_file_id:
                continue
            stored = await _stored_audio(bucket, uid, conversation_id, audio_file)
            if stored is None:
                results.append(
                    {
                        "id": audio_file_id,
                        "status": "unavailable",
                        "signed_url": None,
                        "duration": float(audio_file.get("duration") or 0),
                    }
                )
                continue
            signed_url = _signed_audio_url(request, env, uid, conversation_id, audio_file_id)
            if signed_url is None:
                return JSONResponse({"error": "audio URL signing is not configured"}, status_code=503)
            results.append(
                {
                    "id": audio_file_id,
                    "status": "cached",
                    "signed_url": signed_url,
                    "content_type": stored[1],
                    "duration": float(audio_file.get("duration") or 0),
                }
            )
        conversation_result = None
        conversation_audio = _conversation_audio(row)
        if conversation_audio is not None:
            stored = await _stored_audio(
                bucket,
                uid,
                conversation_id,
                {**conversation_audio, "id": "conversation"},
            )
            if stored is not None:
                signed_url = _signed_audio_url(request, env, uid, conversation_id, "conversation")
                if signed_url is None:
                    return JSONResponse({"error": "audio URL signing is not configured"}, status_code=503)
                conversation_result = {
                    "status": "cached",
                    "signed_url": signed_url,
                    "content_type": stored[1],
                    "duration": conversation_audio.get("duration"),
                    "captured_duration": conversation_audio.get("captured_duration"),
                    "spans": conversation_audio.get("spans", []),
                }
    except Exception:
        return JSONResponse({"error": "recordings unavailable"}, status_code=503)
    return {"audio_files": results, "conversation_audio": conversation_result, "poll_after_ms": None}


@router.get("/v1/sync/audio/{conversation_id}/{audio_file_id}")
async def download_conversation_audio(
    request: Request,
    conversation_id: str,
    audio_file_id: str,
):
    """Stream a private R2 playback object through an authenticated or signed URL."""

    if (
        not conversation_id
        or len(conversation_id) > MAX_ID_LENGTH
        or not audio_file_id
        or len(audio_file_id) > MAX_ID_LENGTH
    ):
        return JSONResponse({"error": "invalid audio identity"}, status_code=400)
    env = request.scope["env"]
    context = _auth_context(request)
    uid = (
        str(context["uid"])
        if context
        else _audio_token_uid(
            env,
            request.query_params.get("token"),
            conversation_id,
            audio_file_id,
            int(time.time()),
        )
    )
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    requested_format = request.query_params.get("format", "wav")
    if requested_format != "wav":
        return JSONResponse({"error": "unsupported audio format"}, status_code=400)
    try:
        row = await _first_conversation(env, uid, conversation_id)
        if row is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        if _bool(row.get("is_locked")):
            return JSONResponse(
                {"error": "A paid plan is required to access this conversation."},
                status_code=402,
            )
        if not await _recording_access_enabled(env, uid):
            return JSONResponse({"error": "audio file not found"}, status_code=404)
        conversation_audio = _conversation_audio(row)
        audio_file = (
            ({**conversation_audio, "id": "conversation"} if conversation_audio else None)
            if audio_file_id == "conversation"
            else _audio_file(row, audio_file_id)
        )
        if audio_file is None:
            return JSONResponse({"error": "audio file not found"}, status_code=404)
        bucket = getattr(env, "ASSETS", None)
        if bucket is None or not callable(getattr(bucket, "get", None)):
            return JSONResponse({"error": "recording storage is not configured"}, status_code=503)
        resolved = await _stored_audio(bucket, uid, conversation_id, audio_file)
        if resolved is None:
            return JSONResponse({"error": "audio file not found"}, status_code=404)
        storage_key, content_type, metadata = resolved
        size = _audio_object_size(metadata)
        try:
            byte_range = _parse_audio_range(request.headers.get("range"), size)
        except ValueError:
            return Response(status_code=416, headers={"content-range": f"bytes */{size}", "accept-ranges": "bytes"})
        options: dict[str, object] = {}
        headers = {"accept-ranges": "bytes", "cache-control": "private, max-age=300"}
        status_code = 200
        if byte_range is not None:
            start, end = byte_range
            options = {"range": {"offset": start, "length": end - start + 1}}
            headers["content-range"] = f"bytes {start}-{end}/{size}"
            headers["content-length"] = str(end - start + 1)
            status_code = 206
        elif size:
            headers["content-length"] = str(size)
        stored = await bucket.get(storage_key, options) if options else await bucket.get(storage_key)
        if stored is None:
            return JSONResponse({"error": "audio file not found"}, status_code=404)
    except Exception:
        return JSONResponse({"error": "recordings unavailable"}, status_code=503)
    return StreamingResponse(
        _r2_audio_chunks(stored),
        media_type=content_type,
        headers=headers,
        status_code=status_code,
    )


@router.get("/v1/conversations/{conversation_id}/recording")
async def conversation_has_recording(request: Request, conversation_id: str):
    """Check uid-scoped Worker playback or the canonical imported recording."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    env = request.scope["env"]
    uid = str(context["uid"])
    try:
        row = await _first_conversation(env, uid, conversation_id)
        if row is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        if _bool(row.get("is_locked")):
            return JSONResponse(
                {"error": "A paid plan is required to access this conversation."},
                status_code=402,
            )
        if not await _recording_access_enabled(env, uid):
            return {"has_recording": False}
        bucket = getattr(env, "ASSETS", None)
        recordings_bucket = getattr(env, "CONVERSATION_RECORDINGS", None)
        if (
            bucket is None
            or not callable(getattr(bucket, "head", None))
            or recordings_bucket is None
            or not callable(getattr(recordings_bucket, "head", None))
        ):
            return JSONResponse({"error": "recording storage is not configured"}, status_code=503)
        metadata = None
        for audio_file in _audio_files(row):
            if await _stored_audio(bucket, uid, conversation_id, audio_file):
                metadata = True
                break
        if metadata is None:
            # This key matches the legacy recording namespace (`uid/id.wav`)
            # after its data is copied into the uid-scoped R2 binding.
            metadata = await recordings_bucket.head(f"{uid}/{conversation_id}.wav")
    except Exception:
        return JSONResponse({"error": "recordings unavailable"}, status_code=503)
    return {"has_recording": metadata is not None}


@router.patch("/v1/conversations/{conversation_id}/segments/text")
async def patch_conversation_segment_text(request: Request, conversation_id: str):
    """Update one transcript segment with an updated-at compare-and-set."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    try:
        raw = await request.body()
        if len(raw) > 32_000:
            return JSONResponse({"error": "segment body too large"}, status_code=413)
        update = ConversationSegmentTextUpdate.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError, TypeError):
        return JSONResponse({"error": "invalid segment update"}, status_code=400)

    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        existing = await _first_conversation(env, uid, conversation_id)
        if existing is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        if _bool(existing.get("is_locked")):
            return JSONResponse(
                {"error": "Unlimited Plan Required to access this conversation."},
                status_code=402,
            )
        segments = _json_list(existing.get("transcript_segments_json"))
        found = False
        for segment in segments:
            if isinstance(segment, dict) and segment.get("id") == update.segment_id:
                segment["text"] = update.text
                found = True
                break
        if not found:
            return JSONResponse({"error": "segment not found"}, status_code=404)

        updated = await _write_conversation_segments(
            env,
            uid=uid,
            conversation_id=conversation_id,
            existing=existing,
            segments=segments,
        )
        if isinstance(updated, JSONResponse):
            return updated
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)
    return {"status": "Ok"}


@router.patch("/v1/conversations/{conversation_id}/events")
async def patch_conversation_events(request: Request, conversation_id: str):
    """Update event-created flags in the bounded structured projection."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    try:
        raw = await request.body()
        if len(raw) > 32_000:
            return JSONResponse({"error": "event body too large"}, status_code=413)
        update = ConversationEventsStateUpdate.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError, TypeError):
        return JSONResponse({"error": "invalid event update"}, status_code=400)

    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        existing = await _first_conversation(env, uid, conversation_id)
        if existing is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        if _bool(existing.get("is_locked")):
            return JSONResponse(
                {"error": "A paid plan is required to access this conversation."},
                status_code=402,
            )
        structured = _json_object(existing.get("structured_json"))
        raw_events = structured.get("events")
        events = raw_events if isinstance(raw_events, list) else []
        for index, value in zip(update.events_idx, update.values):
            if index < len(events) and isinstance(events[index], dict):
                events[index]["created"] = value
        structured["events"] = events
        await env.APP_DB.prepare(
            "UPDATE cf_conversations SET structured_json = ?, updated_at = ? WHERE uid = ? AND id = ?"
        ).bind(_dump_json(structured, "structured"), int(time.time()), uid, conversation_id).run()
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)
    return {"status": "Ok"}


@router.patch("/v1/conversations/{conversation_id}/action-items")
async def patch_conversation_action_items(request: Request, conversation_id: str):
    """Update indexed action-item completion in the D1 conversation projection."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    try:
        raw = await request.body()
        if len(raw) > 32_000:
            return JSONResponse({"error": "action-item body too large"}, status_code=413)
        update = ConversationActionItemsStateUpdate.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError, TypeError):
        return JSONResponse({"error": "invalid action-item update"}, status_code=400)

    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        existing = await _first_conversation(env, uid, conversation_id)
        if existing is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        if _bool(existing.get("is_locked")):
            return JSONResponse(
                {"error": "A paid plan is required to access this conversation."},
                status_code=402,
            )
        structured = _json_object(existing.get("structured_json"))
        raw_items = structured.get("action_items")
        items = raw_items if isinstance(raw_items, list) else []
        updated_descriptions: dict[str, bool] = {}
        now = datetime.now(timezone.utc).isoformat()
        conversation_created_at = _iso(existing.get("created_at"))
        for index, value in zip(update.items_idx, update.values):
            if index >= len(items) or not isinstance(items[index], dict):
                continue
            item = items[index]
            item["completed"] = value
            item["completed_at"] = now if value else None
            item.setdefault("created_at", conversation_created_at)
            description = item.get("description")
            if isinstance(description, str) and description:
                updated_descriptions[description] = value
        structured["action_items"] = items

        statements = [
            env.APP_DB.prepare(
                "UPDATE cf_conversations SET structured_json = ?, updated_at = ? WHERE uid = ? AND id = ?"
            ).bind(_dump_json(structured, "structured"), int(time.time()), uid, conversation_id)
        ]
        for description, value in updated_descriptions.items():
            statements.append(
                env.APP_DB.prepare(
                    "UPDATE cf_action_items SET completed = ?, status = ?, completed_at = ?, updated_at = ? "
                    "WHERE uid = ? AND conversation_id = ? AND description = ? AND deleted = 0"
                ).bind(
                    int(value),
                    "completed" if value else "active",
                    int(time.time()) if value else None,
                    int(time.time()),
                    uid,
                    conversation_id,
                    description,
                )
            )
        await env.APP_DB.batch(statements)
    except Exception:
        return JSONResponse({"error": "conversation action items unavailable"}, status_code=503)
    return {"status": "Ok"}


@router.patch("/v1/conversations/{conversation_id}/action-items/{action_item_idx}")
async def patch_conversation_action_item_description(request: Request, conversation_id: str, action_item_idx: str):
    """Update one projected action-item description and its standalone mirror."""

    del action_item_idx  # The legacy path component is retained for wire compatibility.
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    try:
        raw = await request.body()
        if len(raw) > 32_000:
            return JSONResponse({"error": "action-item body too large"}, status_code=413)
        update = ConversationActionItemDescriptionUpdate.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError, TypeError):
        return JSONResponse({"error": "invalid action-item description"}, status_code=400)

    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        existing = await _first_conversation(env, uid, conversation_id)
        if existing is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        if _bool(existing.get("is_locked")):
            return JSONResponse(
                {"error": "A paid plan is required to access this conversation."},
                status_code=402,
            )
        structured = _json_object(existing.get("structured_json"))
        raw_items = structured.get("action_items")
        items = raw_items if isinstance(raw_items, list) else []
        found = False
        for item in items:
            if isinstance(item, dict) and item.get("description") == update.old_description:
                item["description"] = update.description
                found = True
                break
        if not found:
            return JSONResponse(
                {"error": f"Action item with description '{update.old_description}' not found"},
                status_code=404,
            )
        structured["action_items"] = items
        now = int(time.time())
        await env.APP_DB.batch(
            [
                env.APP_DB.prepare(
                    "UPDATE cf_conversations SET structured_json = ?, updated_at = ? WHERE uid = ? AND id = ?"
                ).bind(_dump_json(structured, "structured"), now, uid, conversation_id),
                env.APP_DB.prepare(
                    "UPDATE cf_action_items SET description = ?, updated_at = ? "
                    "WHERE uid = ? AND conversation_id = ? AND description = ? AND deleted = 0"
                ).bind(update.description, now, uid, conversation_id, update.old_description),
            ]
        )
    except Exception:
        return JSONResponse({"error": "conversation action items unavailable"}, status_code=503)
    return {"status": "Ok"}


@router.delete("/v1/conversations/{conversation_id}/action-items")
async def delete_conversation_action_item(request: Request, conversation_id: str):
    """Delete matching action-item projections with the legacy description identity."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    try:
        raw = await request.body()
        if len(raw) > 32_000:
            return JSONResponse({"error": "action-item body too large"}, status_code=413)
        delete = ConversationActionItemDelete.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError, TypeError):
        return JSONResponse({"error": "invalid action-item deletion"}, status_code=400)

    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        existing = await _first_conversation(env, uid, conversation_id)
        if existing is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        if _bool(existing.get("is_locked")):
            return JSONResponse(
                {"error": "A paid plan is required to access this conversation."},
                status_code=402,
            )
        structured = _json_object(existing.get("structured_json"))
        raw_items = structured.get("action_items")
        items = raw_items if isinstance(raw_items, list) else []
        structured["action_items"] = [
            item for item in items if not (isinstance(item, dict) and item.get("description") == delete.description)
        ]
        now = int(time.time())
        await env.APP_DB.batch(
            [
                env.APP_DB.prepare(
                    "UPDATE cf_conversations SET structured_json = ?, updated_at = ? WHERE uid = ? AND id = ?"
                ).bind(_dump_json(structured, "structured"), now, uid, conversation_id),
                env.APP_DB.prepare(
                    "DELETE FROM cf_action_items WHERE uid = ? AND conversation_id = ? AND description = ?"
                ).bind(uid, conversation_id, delete.description),
            ]
        )
    except Exception:
        return JSONResponse({"error": "conversation action items unavailable"}, status_code=503)
    return {"status": "Ok"}


@router.patch("/v1/conversations/{conversation_id}/summary")
async def patch_conversation_summary(request: Request, conversation_id: str):
    """Update the default overview or one app-specific summary in D1."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    try:
        raw = await request.body()
        if len(raw) > 32_000:
            return JSONResponse({"error": "summary body too large"}, status_code=413)
        update = ConversationSummaryUpdate.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError, TypeError):
        return JSONResponse({"error": "invalid summary update"}, status_code=400)

    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        existing = await _first_conversation(env, uid, conversation_id)
        if existing is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        if _bool(existing.get("is_locked")):
            return JSONResponse(
                {"error": "A paid plan is required to access this conversation."},
                status_code=402,
            )
        now = int(time.time())
        if update.app_id is None:
            structured = _json_object(existing.get("structured_json"))
            structured["overview"] = update.content
            await env.APP_DB.prepare(
                "UPDATE cf_conversations SET structured_json = ?, updated_at = ? WHERE uid = ? AND id = ?"
            ).bind(_dump_json(structured, "structured"), now, uid, conversation_id).run()
            return {"status": "Ok"}

        apps_results = _json_list(existing.get("apps_results_json"))
        found = False
        for entry in apps_results:
            if isinstance(entry, dict) and entry.get("app_id") == update.app_id:
                entry["content"] = update.content
                found = True
                break
        if not found:
            return JSONResponse({"error": "app summary not found for this conversation"}, status_code=404)
        await env.APP_DB.prepare(
            "UPDATE cf_conversations SET apps_results_json = ?, updated_at = ? WHERE uid = ? AND id = ?"
        ).bind(_dump_json(apps_results, "apps_results"), now, uid, conversation_id).run()
    except Exception:
        return JSONResponse({"error": "conversation summaries unavailable"}, status_code=503)
    return {"status": "Ok"}


@router.delete("/v1/conversations/{conversation_id}/calendar-event")
async def unlink_conversation_calendar_event(request: Request, conversation_id: str):
    """Remove the local calendar-event link from the D1 conversation projection."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        if await _first_conversation(env, uid, conversation_id) is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        await env.APP_DB.prepare(
            "UPDATE cf_conversations SET calendar_event_json = NULL, updated_at = ? WHERE uid = ? AND id = ?"
        ).bind(int(time.time()), uid, conversation_id).run()
    except Exception:
        return JSONResponse({"error": "conversation calendar link unavailable"}, status_code=503)
    return {"status": "Ok"}


@router.patch("/v1/conversations/{conversation_id}/title")
@router.patch("/v1/cf/conversations/{conversation_id}/title")
async def patch_conversation_title(request: Request, conversation_id: str):
    """Update the local conversation title without invoking legacy enrichment."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    title = request.query_params.get("title")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 500:
        return JSONResponse({"error": "invalid title"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        existing = await _first_conversation(env, uid, conversation_id)
        if existing is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        structured = _json_object(existing.get("structured_json"))
        structured["title"] = title.strip()
        await env.APP_DB.prepare(
            "UPDATE cf_conversations SET structured_json = ?, updated_at = ? WHERE uid = ? AND id = ?"
        ).bind(_dump_json(structured, "structured"), int(time.time()), uid, conversation_id).run()
        row = await _first_conversation(env, uid, conversation_id)
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)
    if row is None:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    return {"status": "Ok", "conversation": _response(row, detail=True)}


@router.patch("/v1/conversations/{conversation_id}/starred")
@router.patch("/v1/cf/conversations/{conversation_id}/starred")
async def set_conversation_starred(request: Request, conversation_id: str):
    """Persist the uid-scoped starred flag in the conversation projection."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    raw_starred = request.query_params.get("starred")
    if not isinstance(raw_starred, str) or raw_starred.strip().lower() not in {"true", "false", "1", "0"}:
        return JSONResponse({"error": "invalid starred"}, status_code=400)
    starred = 1 if raw_starred.strip().lower() in {"true", "1"} else 0
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        if await _first_conversation(env, uid, conversation_id) is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        await env.APP_DB.prepare(
            "UPDATE cf_conversations SET starred = ?, updated_at = ? WHERE uid = ? AND id = ?"
        ).bind(starred, int(time.time()), uid, conversation_id).run()
        row = await _first_conversation(env, uid, conversation_id)
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)
    if row is None:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    return {"status": "Ok", "conversation": _response(row, detail=True)}
