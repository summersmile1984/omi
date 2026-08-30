"""Worker-native Developer conversation creation and enrichment.

The public Developer endpoints finish their canonical D1 write, derived rows,
projection outboxes, and webhook outboxes in one batch. Workers AI is the only
model runtime; no legacy service or local Python process participates.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time
from typing import Literal
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import AliasChoices, BaseModel, Field, ValidationError, field_validator, model_validator

from account_routes import usage_source_statement
from conversation_routes import CONVERSATION_SOURCES, _first_conversation as first_conversation
from developer_routes import _authenticate, _bool
from integration_routes import _json_schema, _public_webhook_url, _webhook_targets, _workers_ai_json
from internal_auth import decode_context
from vector_search import publish_vector_projection

router = APIRouter()

MAX_REQUEST_BYTES = 2_000_000
MAX_TEXT_LENGTH = 100_000
MAX_TRANSCRIPT_TEXT_LENGTH = 500_000
MAX_SEGMENTS = 500
MAX_SEGMENT_TEXT_LENGTH = 100_000
MAX_ACTION_ITEMS = 20
MAX_MEMORIES = 50
MAX_GROUNDED_MEMORIES = 10
MAX_MEMORY_CONTENT_LENGTH = 1_000
CLAIM_STALE_SECONDS = 15 * 60
FROM_SEGMENTS_NAMESPACE = uuid.UUID("fb2f1f36-3c84-47a4-9c62-b3f6fdb3fd13")
FIRST_PARTY_AUTHORITIES = frozenset({"better-auth", "firebase", "internal"})
CLIENT_PLATFORMS = frozenset({"android", "ios", "linux", "macos", "web", "windows"})

MEMORY_SUBJECT_PATTERN = re.compile(
    r"^\s*(?:(?:(?:the\s+)?user|i|my|we|our|l['’]utilisateur|el\s+usuario|la\s+usuaria|"
    r"o\s+usuário|a\s+usuária|der\s+benutzer|die\s+benutzerin|пользователь)\b|"
    r"用户(?:的)?|我(?:们(?:的)?|的)?|ユーザー|使用者|사용자)\s*[,，:：-]*\s*",
    re.IGNORECASE,
)
PERSONAL_SOURCE_PATTERN = re.compile(
    r"(?:(?<![\w])(?:i|i['’]m|i['’]ve|my|mine|me|we|we['’]re|we['’]ve|our|ours|"
    r"(?:the\s+)?user|l['’]utilisateur|el\s+usuario|la\s+usuaria|o\s+usuário|a\s+usuária|"
    r"der\s+benutzer|die\s+benutzerin|пользователь)(?![\w]))|"
    r"(?:用户|我(?:们(?:的)?|的)?|ユーザー|使用者|사용자)",
    re.IGNORECASE,
)
MEMORY_SCAFFOLDING_PATTERN = re.compile(
    r"^\s*(?:speaker[_\s-]*\d+|action\s+items?|to-?dos?|tasks?|next\s+steps?|"
    r"memories?|facts?(?:[-_\s]*\d+)?)\s*[:：-]?\s*$",
    re.IGNORECASE,
)
TRANSIENT_MEMORY_PATTERN = re.compile(
    r"\b(?:action\s+items?|to-?dos?|tasks?|next\s+steps?|follow[-\s]?ups?|"
    r"plan(?:s|ned|ning)?\s+to|intend(?:s|ed|ing)?\s+to|should|must|will|"
    r"need(?:s|ed)?\s+to|today|tomorrow|next\s+(?:week|month|quarter))\b|"
    r"(?:行动项|待办|下一步|跟进|计划|打算|应该|必须|将要|今天|明天|下周|下个月)",
    re.IGNORECASE,
)
MEMORY_RELATION_PATTERN = re.compile(
    r"\b(?:am|is|are|has|have|prefer(?:s|red)?|like(?:s|d)?|love(?:s|d)?|dislike(?:s|d)?|"
    r"use(?:s|d)?|work(?:s|ed)?|live(?:s|d)?|own(?:s|ed)?|speak(?:s)?|value(?:s|d)?|"
    r"avoid(?:s|ed)?|choose(?:s)?|chose|enjoy(?:s|ed)?|want(?:s|ed)?|favorite|favourite|"
    r"préfèr(?:e|es|ent)?|aime|utilise|habite|travaille|possède|parle|est|"
    r"prefiere|gusta|utiliza|vive|trabaja|tiene|habla|"
    r"prefere|gosta|usa|mora|trabalha|possui|fala|"
    r"bevorzugt|mag|nutzt|wohnt|arbeitet|besitzt|spricht|ist)\b|"
    r"(?:偏好|喜欢|不喜欢|使用|常用|居住|住在|工作|拥有|会说|过敏|重视|避免|选择|习惯|"
    r"好き|使う|使用|住む|働く|持つ|話す|です|좋아|사용|살아|일해|가지|말해|알레르기)",
    re.IGNORECASE,
)
MEMORY_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "by",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "use",
        "uses",
        "using",
        "was",
        "were",
        "with",
        "一",
        "个",
        "了",
        "们",
        "和",
        "在",
        "或",
        "是",
        "的",
        "与",
        "用",
    }
)

ENRICHMENT_SCHEMA = _json_schema(
    "omi_developer_conversation",
    {
        "title": {"type": "string"},
        "overview": {"type": "string"},
        "emoji": {"type": "string"},
        "category": {"type": "string"},
        "discarded": {"type": "boolean"},
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"description": {"type": "string"}},
                "required": ["description"],
                "additionalProperties": False,
            },
        },
        "memories": {"type": "array", "items": {"type": "string"}},
    },
    ["title", "overview", "emoji", "category", "discarded", "action_items", "memories"],
)


class GeolocationInput(BaseModel):
    model_config = {"extra": "ignore"}

    google_place_id: str | None = Field(default=None, max_length=512)
    latitude: float
    longitude: float
    address: str | None = Field(default=None, max_length=2_048)
    location_type: str | None = Field(default=None, max_length=128)


class DeveloperConversationCreate(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    text: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)
    text_source: Literal["audio_transcript", "message", "other_text"] = "other_text"
    text_source_spec: str | None = Field(default=None, max_length=256)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    language: str | None = Field(default="en", max_length=32)
    geolocation: GeolocationInput | None = None


class DeveloperTranscriptSegment(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    text: str = Field(min_length=1, max_length=MAX_SEGMENT_TEXT_LENGTH)
    speaker: str | None = Field(default="SPEAKER_00", max_length=128)
    speaker_id: int | None = None
    is_user: bool = False
    person_id: str | None = Field(default=None, max_length=256)
    start: float
    end: float

    @model_validator(mode="after")
    def validate_interval(self) -> "DeveloperTranscriptSegment":
        if not math.isfinite(self.start) or not math.isfinite(self.end):
            raise ValueError("segment times must be finite")
        if self.start < 0:
            raise ValueError("segment start time cannot be negative")
        if self.end <= self.start:
            raise ValueError("segment end time must be after start time")
        return self


class DeveloperConversationFromSegments(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    transcript_segments: list[DeveloperTranscriptSegment] = Field(min_length=1, max_length=MAX_SEGMENTS)
    client_session_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("client_session_id", "client_conversation_id", "session_id", "client_id"),
        min_length=1,
        max_length=200,
    )
    source: str | None = Field(default="phone", max_length=64)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    language: str | None = Field(default="en", max_length=32)
    geolocation: GeolocationInput | None = None
    client_device_id: str | None = Field(default=None, max_length=256)
    client_platform: str | None = Field(default=None, max_length=64)
    conversation_role: Literal["ambient", "meeting"] = "ambient"
    conversation_finalization_reason: (
        Literal[
            "user_stop",
            "finish_and_continue",
            "meeting_started",
            "meeting_ended",
            "max_duration_rotation",
            "crash_recovery",
            "retry",
        ]
        | None
    ) = None

    @field_validator("client_session_id")
    @classmethod
    def normalize_client_session_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("client_session_id cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_total_text(self) -> "DeveloperConversationFromSegments":
        if sum(len(segment.text) for segment in self.transcript_segments) > MAX_TRANSCRIPT_TEXT_LENGTH:
            raise ValueError("transcript text exceeds the size limit")
        return self


async def _bounded_json(request: Request) -> object:
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("request body exceeds size limit")
    return json.loads(raw)


def _epoch(value: datetime) -> int:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return int(aware.astimezone(timezone.utc).timestamp())


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _geolocation(value: GeolocationInput | None) -> dict[str, object] | None:
    if value is None:
        return None
    if not math.isfinite(value.latitude) or not math.isfinite(value.longitude):
        return None
    if not -90 <= value.latitude <= 90 or not -180 <= value.longitude <= 180:
        return None
    return value.model_dump(exclude_none=True)


def _source(value: str | None) -> str:
    normalized = (value or "phone").strip()
    return normalized if normalized in CONVERSATION_SOURCES else "unknown"


def _first_party_uid(request: Request) -> str | None:
    env = request.scope["env"]
    context = decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )
    if not isinstance(context, dict) or context.get("authority") not in FIRST_PARTY_AUTHORITIES:
        return None
    uid = context.get("uid")
    return uid if isinstance(uid, str) and uid else None


def _client_device_from_headers(request: Request) -> tuple[str | None, str | None]:
    platform = (request.headers.get("x-app-platform") or "").strip().lower()
    device_hash = (request.headers.get("x-device-id-hash") or "").strip().lower()
    normalized_platform = platform if 0 < len(platform) <= 64 else None
    client_device_id = (
        f"{platform}_{device_hash}"
        if platform in CLIENT_PLATFORMS and re.fullmatch(r"[0-9a-f]{8}", device_hash)
        else None
    )
    return client_device_id, normalized_platform


def _response(row: dict[str, object], *, meeting_eligible: bool = False) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "status": str(row.get("status") or "completed"),
        "discarded": _bool(row.get("discarded")),
        "meeting_treatment_eligible": meeting_eligible,
    }


def _meeting_eligible_from_row(row: dict[str, object]) -> bool:
    try:
        external_data = json.loads(str(row.get("external_data_json") or "{}"))
        segments = json.loads(str(row.get("transcript_segments_json") or "[]"))
    except (TypeError, ValueError):
        return False
    if not isinstance(external_data, dict) or not isinstance(segments, list):
        return False
    return _meeting_eligible(
        source=str(row.get("source") or "unknown"),
        role=str(external_data.get("conversation_role") or "ambient"),
        finalization_reason=(
            str(external_data["conversation_finalization_reason"])
            if external_data.get("conversation_finalization_reason") is not None
            else None
        ),
        discarded=_bool(row.get("discarded")),
        started=int(row.get("started_at") or 0),
        finished=int(row.get("finished_at") or 0),
        segments=[segment for segment in segments if isinstance(segment, dict)],
    )


def _meeting_eligible(
    *,
    source: str,
    role: str,
    finalization_reason: str | None,
    discarded: bool,
    started: int,
    finished: int,
    segments: list[dict[str, object]],
) -> bool:
    if (
        source != "desktop"
        or role != "meeting"
        or finalization_reason == "max_duration_rotation"
        or discarded
        or finished - started < 300
    ):
        return False
    intervals: list[tuple[float, float]] = []
    for segment in segments:
        if not str(segment.get("text") or "").strip():
            continue
        start = float(segment.get("start") or 0)
        end = float(segment.get("end") or 0)
        if end > start:
            intervals.append((max(0.0, start), end))
    if not intervals:
        return False
    intervals.sort()
    total = 0.0
    current_start, current_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    return total + current_end - current_start >= 60


def _memory_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for raw in re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE):
        expanded = list(raw) if re.search(r"[\u3400-\u9fff]", raw) else [raw]
        tokens.extend(token for token in expanded if token not in MEMORY_STOP_WORDS and not token.isdigit())
    return tokens


def _token_is_grounded(token: str, source_tokens: set[str]) -> bool:
    if token in source_tokens:
        return True
    if not token.isascii() or len(token) < 5:
        return False
    return any(
        source.isascii() and len(source) >= 5 and (source.startswith(token) or token.startswith(source))
        for source in source_tokens
    )


def _memory_limit(text: str) -> int:
    statements = [item for item in re.split(r"(?:[.!?。！？]+|\n+)", text) if _memory_tokens(item)]
    return min(MAX_GROUNDED_MEMORIES, max(1, len(statements)))


def _grounded_memories(text: str, raw_memories: object) -> list[str]:
    if not isinstance(raw_memories, list) or PERSONAL_SOURCE_PATTERN.search(text) is None:
        return []
    source_tokens = set(_memory_tokens(text))
    accepted: list[tuple[str, set[str]]] = []
    for item in raw_memories[:MAX_MEMORIES]:
        if not isinstance(item, str):
            continue
        normalized = item.strip()
        if (
            not normalized
            or len(normalized) > MAX_MEMORY_CONTENT_LENGTH
            or MEMORY_SCAFFOLDING_PATTERN.fullmatch(normalized) is not None
        ):
            continue
        subject = MEMORY_SUBJECT_PATTERN.match(normalized)
        if subject is None:
            continue
        body = normalized[subject.end() :].strip()
        if (
            not body
            or TRANSIENT_MEMORY_PATTERN.search(body) is not None
            or MEMORY_RELATION_PATTERN.search(body) is None
        ):
            continue
        tokens = _memory_tokens(body)
        token_set = set(tokens)
        if len(token_set) < 2:
            continue
        grounded = sum(_token_is_grounded(token, source_tokens) for token in token_set)
        if grounded / len(token_set) < 0.8:
            continue
        if any(len(token_set & prior) / min(len(token_set), len(prior)) >= 0.75 for _, prior in accepted):
            continue
        accepted.append((normalized, token_set))
        if len(accepted) >= _memory_limit(text):
            break
    return [content for content, _ in accepted]


async def _enrichment(env: object, text: str, language: str) -> dict[str, object] | None:
    result = await _workers_ai_json(
        env,
        "Process this conversation using the requested language when practical. Produce a concise title and "
        "overview, a representative emoji and category, actionable tasks, and durable user-specific facts. "
        "Set discarded=true only for empty, incoherent, or non-conversational noise. Every memories entry must be "
        "a complete statement about the user that is directly supported by the conversation. A good entry is "
        "'The user prefers asynchronous updates.' Topic labels, speaker labels, planned tasks, guesses, and "
        "transient details are invalid entries. Return at most 20 action_items and 50 memories.\n"
        f"Language: {language}\n\n{text}",
        2_048,
        ENRICHMENT_SCHEMA,
    )
    if not isinstance(result, dict):
        return None
    title = result.get("title")
    overview = result.get("overview")
    discarded = result.get("discarded")
    if (
        not isinstance(title, str)
        or not title.strip()
        or not isinstance(overview, str)
        or not isinstance(discarded, bool)
    ):
        return None
    action_items: list[str] = []
    seen_actions: set[str] = set()
    raw_actions = result.get("action_items")
    if isinstance(raw_actions, list):
        for item in raw_actions[:MAX_ACTION_ITEMS]:
            description = item.get("description") if isinstance(item, dict) else item
            if not isinstance(description, str):
                continue
            normalized = description.strip()[:4_096]
            key = normalized.casefold()
            if normalized and key not in seen_actions:
                seen_actions.add(key)
                action_items.append(normalized)
    memories = _grounded_memories(text, result.get("memories"))
    emoji = result.get("emoji")
    category = result.get("category")
    return {
        "structured": {
            "title": title.strip()[:160],
            "overview": overview.strip()[:2_000],
            "emoji": emoji.strip()[:16] if isinstance(emoji, str) and emoji.strip() else "🧠",
            "category": category.strip()[:64] if isinstance(category, str) and category.strip() else "other",
            "action_items": [
                {"description": description, "completed": False, "exported": False} for description in action_items
            ],
            "events": [],
        },
        "action_items": action_items,
        "memories": memories,
        "discarded": discarded,
    }


async def _fanout_targets(env: object, uid: str) -> tuple[list[tuple[str, str]], str | None]:
    app_targets = await _webhook_targets(env, uid)
    webhook = (
        await env.APP_DB.prepare(
            "SELECT url FROM cf_user_developer_webhooks "
            "WHERE uid = ? AND webhook_type = 'memory_created' AND enabled = 1"
        )
        .bind(uid)
        .first()
    )
    raw_url = webhook.get("url") if isinstance(webhook, dict) else None
    developer_url = _public_webhook_url(raw_url) if isinstance(raw_url, str) else None
    return app_targets, developer_url


def _action_rows(uid: str, conversation_id: str, descriptions: list[str], now: int) -> list[dict[str, object]]:
    rows = []
    for index, description in enumerate(descriptions):
        item_id = uuid.uuid5(FROM_SEGMENTS_NAMESPACE, f"action\0{uid}\0{conversation_id}\0{index}\0{description}").hex
        rows.append(
            {
                "uid": uid,
                "id": item_id,
                "description": description,
                "idempotency_key": hashlib.sha256(f"{uid}\0{conversation_id}\0{description}".encode()).hexdigest(),
                "conversation_id": conversation_id,
                "created_at": now,
                "updated_at": now,
            }
        )
    return rows


def _memory_rows(uid: str, conversation_id: str, contents: list[str], now: int) -> list[dict[str, object]]:
    rows = []
    for index, content in enumerate(contents):
        memory_id = uuid.uuid5(FROM_SEGMENTS_NAMESPACE, f"memory\0{uid}\0{conversation_id}\0{index}\0{content}").hex
        rows.append(
            {
                "uid": uid,
                "id": memory_id,
                "content": content,
                "conversation_id": conversation_id,
                "valid_at": now,
                "created_at": now,
                "updated_at": now,
            }
        )
    return rows


def _conversation_payload(
    *,
    conversation_id: str,
    created: int,
    started: int,
    finished: int,
    source: str,
    language: str,
    structured: dict[str, object],
    segments: list[dict[str, object]],
    discarded: bool,
    geolocation: dict[str, object] | None,
    external_data: dict[str, object],
    client_device_id: str | None,
    client_platform: str | None,
) -> dict[str, object]:
    return {
        "id": conversation_id,
        "created_at": _iso(created),
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "source": source,
        "language": language,
        "status": "completed",
        "visibility": "private",
        "starred": False,
        "discarded": discarded,
        "is_locked": False,
        "folder_id": None,
        "folder_name": None,
        "client_device_id": client_device_id,
        "client_platform": client_platform,
        "structured": structured,
        "transcript_segments": segments,
        "photos": [],
        "audio_files": [],
        "apps_results": [],
        "suggested_summarization_apps": [],
        "geolocation": geolocation,
        "external_data": external_data,
        "calendar_event": None,
    }


def _conversation_insert(
    env: object,
    *,
    uid: str,
    payload: dict[str, object],
    now: int,
):
    return env.APP_DB.prepare(
        "INSERT INTO cf_conversations "
        "(uid, id, created_at, updated_at, started_at, finished_at, source, language, status, visibility, starred, "
        "discarded, is_locked, deferred, private_cloud_sync_enabled, folder_id, client_device_id, client_platform, "
        "structured_json, transcript_segments_json, photos_json, audio_files_json, conversation_audio_json, "
        "apps_results_json, suggested_apps_json, geolocation_json, external_data_json, calendar_event_json, app_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'completed', 'private', 0, ?, 0, 0, 0, NULL, ?, ?, ?, ?, '[]', '[]', "
        "NULL, '[]', '[]', ?, ?, NULL, NULL)"
    ).bind(
        uid,
        payload["id"],
        int(datetime.fromisoformat(str(payload["created_at"])).timestamp()),
        now,
        int(datetime.fromisoformat(str(payload["started_at"])).timestamp()),
        int(datetime.fromisoformat(str(payload["finished_at"])).timestamp()),
        payload["source"],
        payload["language"],
        1 if payload["discarded"] else 0,
        payload["client_device_id"],
        payload["client_platform"],
        _json(payload["structured"]),
        _json(payload["transcript_segments"]),
        _json(payload["geolocation"]) if payload["geolocation"] is not None else None,
        _json(payload["external_data"]),
    )


def _conversation_complete_update(env: object, *, uid: str, payload: dict[str, object], now: int, claim_json: str):
    return env.APP_DB.prepare(
        "UPDATE cf_conversations SET updated_at = ?, status = 'completed', discarded = ?, structured_json = ?, "
        "transcript_segments_json = ?, geolocation_json = ?, external_data_json = ? "
        "WHERE uid = ? AND id = ? AND status = 'processing' AND external_data_json = ?"
    ).bind(
        now,
        1 if payload["discarded"] else 0,
        _json(payload["structured"]),
        _json(payload["transcript_segments"]),
        _json(payload["geolocation"]) if payload["geolocation"] is not None else None,
        _json(payload["external_data"]),
        uid,
        payload["id"],
        claim_json,
    )


def _action_insert(env: object, rows: list[dict[str, object]]):
    return env.APP_DB.prepare(
        "INSERT INTO cf_action_items "
        "(uid, id, description, status, completed, owner, source, provenance_json, conversation_id, created_at, "
        "updated_at, idempotency_key) SELECT json_extract(value, '$.uid'), json_extract(value, '$.id'), "
        "json_extract(value, '$.description'), 'active', 0, 'unknown', 'developer', '[{\"kind\":\"developer\"}]', "
        "json_extract(value, '$.conversation_id'), CAST(json_extract(value, '$.created_at') AS INTEGER), "
        "CAST(json_extract(value, '$.updated_at') AS INTEGER), json_extract(value, '$.idempotency_key') "
        "FROM json_each(?)"
    ).bind(_json(rows))


def _memory_insert(env: object, rows: list[dict[str, object]]):
    return env.APP_DB.prepare(
        "INSERT INTO cf_memories "
        "(uid, id, content, category, visibility, tags_json, subject_attribution, conversation_id, reviewed, "
        "manually_added, memory_tier, valid_at, created_at, updated_at) "
        "SELECT json_extract(value, '$.uid'), json_extract(value, '$.id'), json_extract(value, '$.content'), "
        "'interesting', 'private', '[]', 'unknown', json_extract(value, '$.conversation_id'), 0, 0, 'short_term', "
        "CAST(json_extract(value, '$.valid_at') AS INTEGER), CAST(json_extract(value, '$.created_at') AS INTEGER), "
        "CAST(json_extract(value, '$.updated_at') AS INTEGER) FROM json_each(?)"
    ).bind(_json(rows))


def _memory_usage_insert(env: object, uid: str, rows: list[dict[str, object]]):
    ids = [row["id"] for row in rows]
    return env.APP_DB.prepare(
        "INSERT INTO cf_usage_sources "
        "(uid, source_kind, source_id, occurred_at, transcription_seconds, words_transcribed, insights_gained, "
        "memories_created, updated_at) SELECT uid, 'memory', id, created_at, 0, 0, 0, 1, updated_at "
        "FROM cf_memories WHERE uid = ? AND id IN (SELECT CAST(value AS TEXT) FROM json_each(?))"
    ).bind(uid, _json(ids))


def _vector_insert(env: object, uid: str, rows: list[dict[str, object]], now: int):
    projections = [
        {
            "kind": "conversation",
            "id": rows[0]["conversation_id"] if rows else "",
            "operation": rows[0].get("operation", "upsert") if rows else "upsert",
        },
        *(
            {"kind": "action_item", "id": row["id"], "operation": "upsert"}
            for row in rows
            if row.get("row_kind") == "action_item"
        ),
        *(
            {"kind": "memory", "id": row["id"], "operation": "upsert"}
            for row in rows
            if row.get("row_kind") == "memory"
        ),
    ]
    return env.APP_DB.prepare(
        "INSERT INTO cf_vector_projection_outbox "
        "(uid, source_kind, source_id, desired_version, operation, attempts, next_attempt_at, last_error, created_at, "
        "updated_at) SELECT ?, json_extract(value, '$.kind'), json_extract(value, '$.id'), ?, "
        "json_extract(value, '$.operation'), 0, ?, NULL, ?, ? "
        "FROM json_each(?) WHERE 1 ON CONFLICT(uid, source_kind, source_id) DO UPDATE SET "
        "desired_version = excluded.desired_version, operation = excluded.operation, attempts = 0, "
        "next_attempt_at = excluded.next_attempt_at, last_error = NULL, updated_at = excluded.updated_at "
        "WHERE excluded.desired_version >= cf_vector_projection_outbox.desired_version"
    ).bind(uid, now, now, now, now, _json(projections))


def _integration_webhook_insert(
    env: object,
    *,
    app_id: str,
    uid: str,
    conversation_id: str,
    webhook_url: str,
    payload_json: str,
    now: int,
):
    delivery_id = hashlib.sha256(f"app\0{app_id}\0{uid}\0{conversation_id}".encode()).hexdigest()
    return env.APP_DB.prepare(
        "INSERT INTO cf_integration_webhook_outbox "
        "(delivery_id, app_id, uid, conversation_id, webhook_url, payload_json, status, attempts, not_before, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)"
    ).bind(delivery_id, app_id, uid, conversation_id, webhook_url, payload_json, now, now, now)


def _developer_webhook_insert(
    env: object,
    *,
    uid: str,
    conversation_id: str,
    webhook_url: str,
    payload_json: str,
    now: int,
):
    delivery_id = hashlib.sha256(f"developer\0memory_created\0{uid}\0{conversation_id}".encode()).hexdigest()
    return env.APP_DB.prepare(
        "INSERT INTO cf_developer_webhook_outbox "
        "(delivery_id, uid, webhook_type, conversation_id, webhook_url, payload_json, status, attempts, not_before, "
        "created_at, updated_at) VALUES (?, ?, 'memory_created', ?, ?, ?, 'pending', 0, ?, ?, ?)"
    ).bind(delivery_id, uid, conversation_id, webhook_url, payload_json, now, now, now)


async def _persist_completed(
    env: object,
    *,
    uid: str,
    payload: dict[str, object],
    app_targets: list[tuple[str, str]],
    developer_webhook_url: str | None,
    now: int,
    claim_json: str | None = None,
) -> None:
    conversation_id = str(payload["id"])
    structured = payload["structured"] if isinstance(payload["structured"], dict) else {}
    discarded = bool(payload["discarded"])
    descriptions = (
        []
        if discarded
        else [
            str(item["description"])
            for item in structured.get("action_items", [])
            if isinstance(item, dict) and isinstance(item.get("description"), str)
        ]
    )
    memory_contents = [] if discarded else list(payload.pop("_memory_contents", []))
    action_rows = _action_rows(uid, conversation_id, descriptions, now)
    memory_rows = _memory_rows(uid, conversation_id, memory_contents, now)
    projection_rows: list[dict[str, object]] = [
        {"conversation_id": conversation_id, "operation": "delete" if discarded else "upsert"}
    ]
    projection_rows.extend({**row, "row_kind": "action_item"} for row in action_rows)
    projection_rows.extend({**row, "row_kind": "memory"} for row in memory_rows)
    fanout_json = _json(payload)
    started = int(datetime.fromisoformat(str(payload["started_at"])).timestamp())
    finished = int(datetime.fromisoformat(str(payload["finished_at"])).timestamp())
    segments = payload["transcript_segments"] if isinstance(payload["transcript_segments"], list) else []
    statements = [
        (
            _conversation_complete_update(env, uid=uid, payload=payload, now=now, claim_json=claim_json)
            if claim_json is not None
            else _conversation_insert(env, uid=uid, payload=payload, now=now)
        ),
        usage_source_statement(
            env,
            uid=uid,
            source_kind="conversation",
            source_id=conversation_id,
            occurred_at=finished,
            transcription_seconds=max(0, min(finished - started, 7 * 24 * 60 * 60)),
            words_transcribed=sum(len(re.findall(r"\S+", str(segment.get("text") or ""))) for segment in segments),
            insights_gained=0 if discarded else 1 + len(descriptions),
            memories_created=0,
            updated_at=now,
        ),
        _vector_insert(env, uid, projection_rows, now),
    ]
    if action_rows:
        statements.append(_action_insert(env, action_rows))
    if memory_rows:
        statements.extend([_memory_insert(env, memory_rows), _memory_usage_insert(env, uid, memory_rows)])
    if not discarded:
        statements.extend(
            _integration_webhook_insert(
                env,
                app_id=app_id,
                uid=uid,
                conversation_id=conversation_id,
                webhook_url=url,
                payload_json=fanout_json,
                now=now,
            )
            for app_id, url in app_targets
        )
    if developer_webhook_url:
        statements.append(
            _developer_webhook_insert(
                env,
                uid=uid,
                conversation_id=conversation_id,
                webhook_url=developer_webhook_url,
                payload_json=fanout_json,
                now=now,
            )
        )
    await env.APP_DB.batch(statements)
    for row in projection_rows:
        source_kind = str(row.get("row_kind") or "conversation")
        source_id = str(row.get("id") or conversation_id)
        await publish_vector_projection(env, uid=uid, source_kind=source_kind, source_id=source_id)


async def _create(
    request: Request,
    *,
    uid: str,
    conversation_id: str,
    created: int,
    started: int,
    finished: int,
    source: str,
    language: str,
    segments: list[dict[str, object]],
    geolocation: dict[str, object] | None,
    external_data: dict[str, object],
    client_device_id: str | None,
    client_platform: str | None,
    claim_json: str | None = None,
) -> dict[str, object] | JSONResponse:
    env = request.scope["env"]
    transcript = "\n".join(f"{segment.get('speaker') or 'SPEAKER_00'}: {segment['text']}" for segment in segments)
    try:
        app_targets, developer_webhook_url = await _fanout_targets(env, uid)
    except Exception:
        return JSONResponse({"error": "conversation fanout unavailable"}, status_code=503)
    enrichment = await _enrichment(env, transcript, language)
    if enrichment is None:
        return JSONResponse({"error": "conversation processing unavailable"}, status_code=502)
    structured = enrichment["structured"]
    discarded = bool(enrichment["discarded"])
    payload = _conversation_payload(
        conversation_id=conversation_id,
        created=created,
        started=started,
        finished=finished,
        source=source,
        language=language,
        structured=structured if isinstance(structured, dict) else {},
        segments=segments,
        discarded=discarded,
        geolocation=geolocation,
        external_data=external_data,
        client_device_id=client_device_id,
        client_platform=client_platform,
    )
    payload["_memory_contents"] = enrichment["memories"]
    try:
        await _persist_completed(
            env,
            uid=uid,
            payload=payload,
            app_targets=app_targets,
            developer_webhook_url=developer_webhook_url,
            now=int(time.time()),
            claim_json=claim_json,
        )
    except Exception:
        return JSONResponse({"error": "conversations unavailable"}, status_code=503)
    eligible = _meeting_eligible(
        source=source,
        role=str(external_data.get("conversation_role") or "ambient"),
        finalization_reason=(
            str(external_data["conversation_finalization_reason"])
            if external_data.get("conversation_finalization_reason") is not None
            else None
        ),
        discarded=discarded,
        started=started,
        finished=finished,
        segments=segments,
    )
    return {
        "id": conversation_id,
        "status": "completed",
        "discarded": discarded,
        "meeting_treatment_eligible": eligible,
    }


@router.post("/v1/dev/user/conversations")
async def create_developer_conversation(request: Request):
    principal, denial = await _authenticate(request, "conversations:write")
    if denial:
        return denial
    try:
        body = DeveloperConversationCreate.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    assert principal is not None
    now = int(time.time())
    started = _epoch(body.started_at) if body.started_at is not None else now
    finished = _epoch(body.finished_at) if body.finished_at is not None else started + 300
    if finished < started:
        return JSONResponse({"detail": "finished_at must be after started_at"}, status_code=422)
    duration = max(0, finished - started)
    conversation_id = uuid.uuid4().hex
    segment = {
        "id": uuid.uuid4().hex,
        "text": body.text,
        "speaker": "SPEAKER_00",
        "speaker_id": 0,
        "is_user": False,
        "person_id": None,
        "start": 0.0,
        "end": float(duration),
    }
    return await _create(
        request,
        uid=principal.uid,
        conversation_id=conversation_id,
        created=now,
        started=started,
        finished=finished,
        source="external_integration",
        language=body.language or "en",
        segments=[segment],
        geolocation=_geolocation(body.geolocation),
        external_data={
            "developer_api": {
                "text_source": body.text_source,
                "text_source_spec": body.text_source_spec,
            }
        },
        client_device_id=None,
        client_platform=None,
    )


def _claim_external_data(
    body: DeveloperConversationFromSegments, claim_token: str, claimed_at: int
) -> dict[str, object]:
    result: dict[str, object] = {
        "from_segments_client_session_id": body.client_session_id,
        "from_segments_claimed_at": _iso(claimed_at),
        "from_segments_claim_token": claim_token,
        "conversation_role": body.conversation_role,
    }
    if body.conversation_finalization_reason is not None:
        result["conversation_finalization_reason"] = body.conversation_finalization_reason
    return result


async def _claim_idempotent_conversation(
    env: object,
    *,
    uid: str,
    conversation_id: str,
    created: int,
    started: int,
    finished: int,
    source: str,
    language: str,
    segments: list[dict[str, object]],
    geolocation: dict[str, object] | None,
    client_device_id: str | None,
    client_platform: str | None,
    external_data: dict[str, object],
) -> tuple[dict[str, object], str] | dict[str, object] | None:
    claim_json = _json(external_data)
    insert = env.APP_DB.prepare(
        "INSERT OR IGNORE INTO cf_conversations "
        "(uid, id, created_at, updated_at, started_at, finished_at, source, language, status, visibility, starred, "
        "discarded, is_locked, deferred, private_cloud_sync_enabled, client_device_id, client_platform, "
        "structured_json, transcript_segments_json, photos_json, audio_files_json, apps_results_json, "
        "suggested_apps_json, geolocation_json, external_data_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'processing', 'private', 0, 0, 0, 0, 0, ?, ?, '{}', ?, '[]', '[]', "
        "'[]', '[]', ?, ?)"
    ).bind(
        uid,
        conversation_id,
        created,
        created,
        started,
        finished,
        source,
        language,
        client_device_id,
        client_platform,
        _json(segments),
        _json(geolocation) if geolocation is not None else None,
        claim_json,
    )
    await insert.run()
    row = await first_conversation(env, uid, conversation_id)
    if row is None:
        return None
    if row.get("status") != "processing" or row.get("external_data_json") == claim_json:
        return (row, claim_json) if row.get("external_data_json") == claim_json else row
    try:
        previous = json.loads(str(row.get("external_data_json") or "{}"))
        claimed = datetime.fromisoformat(str(previous.get("from_segments_claimed_at") or "").replace("Z", "+00:00"))
        claimed_epoch = _epoch(claimed)
    except (TypeError, ValueError):
        claimed_epoch = created
    if int(time.time()) - claimed_epoch <= CLAIM_STALE_SECONDS:
        return row
    await env.APP_DB.prepare(
        "UPDATE cf_conversations SET updated_at = ?, started_at = ?, finished_at = ?, source = ?, language = ?, "
        "client_device_id = ?, client_platform = ?, transcript_segments_json = ?, geolocation_json = ?, "
        "external_data_json = ? WHERE uid = ? AND id = ? AND status = 'processing' AND updated_at <= ?"
    ).bind(
        created,
        started,
        finished,
        source,
        language,
        client_device_id,
        client_platform,
        _json(segments),
        _json(geolocation) if geolocation is not None else None,
        claim_json,
        uid,
        conversation_id,
        created - CLAIM_STALE_SECONDS,
    ).run()
    row = await first_conversation(env, uid, conversation_id)
    if row is not None and row.get("external_data_json") == claim_json:
        return row, claim_json
    return row


async def _create_conversation_from_segments(
    request: Request,
    *,
    uid: str,
    client_device_id: str | None = None,
    client_platform: str | None = None,
) -> dict[str, object] | JSONResponse:
    try:
        body = DeveloperConversationFromSegments.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    now = int(time.time())
    started = _epoch(body.started_at) if body.started_at is not None else now
    finished = (
        _epoch(body.finished_at)
        if body.finished_at is not None
        else started + int(math.ceil(body.transcript_segments[-1].end))
    )
    if finished <= started:
        return JSONResponse({"detail": "finished_at must be after started_at"}, status_code=422)
    source = _source(body.source)
    language = body.language or "en"
    conversation_id = (
        str(uuid.uuid5(FROM_SEGMENTS_NAMESPACE, f"{uid}\0{body.client_session_id}"))
        if body.client_session_id
        else uuid.uuid4().hex
    )
    resolved_client_device_id = client_device_id or body.client_device_id
    resolved_client_platform = client_platform or body.client_platform
    segments = [
        {
            "id": uuid.uuid5(FROM_SEGMENTS_NAMESPACE, f"segment\0{conversation_id}\0{index}").hex,
            "text": segment.text,
            "speaker": segment.speaker or "SPEAKER_00",
            "speaker_id": segment.speaker_id,
            "is_user": segment.is_user,
            "person_id": segment.person_id,
            "start": segment.start,
            "end": segment.end,
        }
        for index, segment in enumerate(body.transcript_segments)
    ]
    geolocation = _geolocation(body.geolocation)
    external_data = _claim_external_data(body, uuid.uuid4().hex, now)
    claim_json = None
    env = request.scope["env"]
    if body.client_session_id:
        try:
            claimed = await _claim_idempotent_conversation(
                env,
                uid=uid,
                conversation_id=conversation_id,
                created=now,
                started=started,
                finished=finished,
                source=source,
                language=language,
                segments=segments,
                geolocation=geolocation,
                client_device_id=resolved_client_device_id,
                client_platform=resolved_client_platform,
                external_data=external_data,
            )
        except Exception:
            return JSONResponse({"error": "conversations unavailable"}, status_code=503)
        if claimed is None:
            return JSONResponse({"error": "conversations unavailable"}, status_code=503)
        if isinstance(claimed, tuple):
            _, claim_json = claimed
        else:
            return _response(claimed, meeting_eligible=_meeting_eligible_from_row(claimed))
    result = await _create(
        request,
        uid=uid,
        conversation_id=conversation_id,
        created=now,
        started=started,
        finished=finished,
        source=source,
        language=language,
        segments=segments,
        geolocation=geolocation,
        external_data={key: value for key, value in external_data.items() if key != "from_segments_claim_token"},
        client_device_id=resolved_client_device_id,
        client_platform=resolved_client_platform,
        claim_json=claim_json,
    )
    if isinstance(result, JSONResponse) and claim_json is not None:
        try:
            await env.APP_DB.prepare(
                "DELETE FROM cf_conversations WHERE uid = ? AND id = ? AND status = 'processing' "
                "AND external_data_json = ?"
            ).bind(uid, conversation_id, claim_json).run()
        except Exception:
            pass
    return result


@router.post("/v1/conversations/from-segments")
async def create_user_conversation_from_segments(request: Request):
    uid = _first_party_uid(request)
    if uid is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client_device_id, client_platform = _client_device_from_headers(request)
    return await _create_conversation_from_segments(
        request,
        uid=uid,
        client_device_id=client_device_id,
        client_platform=client_platform,
    )


@router.post("/v1/dev/user/conversations/from-segments")
async def create_developer_conversation_from_segments(request: Request):
    principal, denial = await _authenticate(request, "conversations:write")
    if denial:
        return denial
    assert principal is not None
    return await _create_conversation_from_segments(request, uid=principal.uid)


__all__ = [
    "create_developer_conversation",
    "create_developer_conversation_from_segments",
    "create_user_conversation_from_segments",
    "router",
]
