"""First-party retrieval tools backed by D1, Vectorize, Workers AI, and Queue.

The response envelope matches the platform `/v1/tools/*` contract used by
desktop, Web, and mobile agents.  D1 is authoritative; vector hits are only
ranked candidates and are always re-hydrated through the signed uid boundary.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, model_validator

from action_item_routes import (
    ActionItemCreate,
    ActionItemUpdate,
    _apply_update as apply_action_item_update,
    _first_item as first_action_item,
    _insert_item as insert_action_item,
)
from conversation_routes import (
    _CONVERSATION_SELECT,
    _json_list as json_list,
    _json_object as json_object,
)
from fallback import record_fallback
from internal_auth import decode_context
from vector_search import embed_query, hydrate_candidate_ids, query_vector_ids

router = APIRouter()

MAX_REQUEST_BYTES = 64_000
MAX_ID_LENGTH = 256
MAX_TOOL_QUERY_CHARS = 4_096
MAX_TOOL_RESULT_ROWS = 5_000
MAX_OFFSET = 1_000_000
TRANSCRIPT_WINDOW = 8
TRANSCRIPT_STRIDE = 6
EXACT_CONVERSATION_ID = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f-]{27})$", re.IGNORECASE)

ACTION_ITEM_SELECT = (
    "SELECT id, description, status, completed, goal_id, workstream_id, owner, due_at, due_confidence, "
    "source, provenance_json, priority, sort_order, indent_level, recurrence_rule, recurrence_parent_id, "
    "created_at, updated_at, completed_at, superseded_by, conversation_id, is_locked, exported, export_date, "
    "export_platform, apple_reminder_id FROM cf_action_items "
)
MEMORY_SELECT = (
    "SELECT id, content, category, visibility, memory_tier, created_at, updated_at, conversation_id, is_locked "
    "FROM cf_memories "
)


class ConversationSearch(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    query: str = Field(min_length=1, max_length=MAX_TOOL_QUERY_CHARS)
    start_date: str | None = Field(default=None, max_length=80)
    end_date: str | None = Field(default=None, max_length=80)
    limit: int = Field(default=5, ge=1, le=20)
    include_transcript: bool = True


class ChunkSearch(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    query: str = Field(min_length=1, max_length=MAX_TOOL_QUERY_CHARS)
    limit: int = Field(default=20, ge=1, le=30)


class MemorySearch(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    query: str = Field(min_length=1, max_length=MAX_TOOL_QUERY_CHARS)
    limit: int = Field(default=5, ge=1, le=20)


class ToolActionItemCreate(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    description: str = Field(min_length=1, max_length=4_096)
    due_at: str | None = Field(default=None, max_length=80)
    conversation_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)


class ToolActionItemUpdate(BaseModel):
    model_config = {"extra": "ignore"}

    completed: bool | None = None
    description: str | None = Field(default=None, max_length=4_096)
    due_at: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def validate_fields(self) -> "ToolActionItemUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one task field is required")
        return self


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


async def _bounded_json(request: Request) -> object:
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("request body is too large")
    return json.loads(raw)


def _ok(tool_name: str, text: str, sources: list[dict[str, object]] | None = None) -> dict[str, object]:
    is_error = text.startswith("Error")
    return {
        "tool_name": tool_name,
        "result_text": text,
        "is_error": is_error,
        "sources": [] if is_error else (sources or []),
    }


def _query_int(request: Request, name: str, default: int, minimum: int, maximum: int) -> int | None:
    try:
        value = int(request.query_params.get(name, str(default)))
    except (TypeError, ValueError):
        return None
    return value if minimum <= value <= maximum else None


def _query_bool(request: Request, name: str, default: bool | None = None) -> bool | None | object:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    normalized = str(raw).strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    return _INVALID


_INVALID = object()


def _parse_iso(value: str | None, name: str) -> datetime | None:
    if value is None:
        return None
    cleaned = re.sub(r" (\d{2}:\d{2})$", r"+\1", value.strip())
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO date with timezone") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: object) -> str | None:
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _flatten(value: object, limit: int = 600) -> str:
    return " ".join(str(value or "").split())[:limit]


def _source(
    kind: str,
    source_id: str,
    *,
    title: str,
    preview: str,
    created_at: object,
) -> dict[str, object]:
    return {
        "kind": kind[:32],
        "source_id": source_id[:512],
        "title": title[:160],
        "preview": _flatten(preview),
        "created_at": _iso(created_at),
        "moment_timestamp_ms": None,
        "app_name": None,
        "url": None,
    }


def _date_bounds(start_value: str | None, end_value: str | None) -> tuple[int | None, int | None]:
    start = _parse_iso(start_value, "start_date")
    end = _parse_iso(end_value, "end_date")
    start_epoch = int(start.timestamp()) if start else None
    end_epoch = int(end.timestamp()) if end else None
    if start_epoch is not None and end_epoch is not None and start_epoch > end_epoch:
        raise ValueError("start_date must be earlier than or equal to end_date")
    return start_epoch, end_epoch


def _conversation_parts(row: dict[str, object], include_transcript: bool) -> tuple[str, dict[str, object]]:
    structured = json_object(row.get("structured_json"))
    title = str(structured.get("title") or "Conversation")
    overview = str(structured.get("overview") or "")
    lines = [f"Title: {title}"]
    created_at = _iso(row.get("created_at"))
    if created_at:
        lines.append(f"Date: {created_at}")
    if overview:
        lines.append(f"Summary: {overview}")
    if include_transcript:
        transcript: list[str] = []
        for segment in json_list(row.get("transcript_segments_json")):
            if not isinstance(segment, dict):
                continue
            text = _flatten(segment.get("text"), 10_000)
            if not text:
                continue
            speaker = "User" if segment.get("is_user") else str(segment.get("speaker") or "Speaker")
            transcript.append(f"{speaker}: {text}")
        if transcript:
            lines.extend(["Transcript:", *transcript])
    source = _source(
        "conversation",
        str(row.get("id") or ""),
        title=title,
        preview=overview,
        created_at=row.get("created_at"),
    )
    return "\n".join(lines), source


async def _conversation_rows(
    env: object,
    uid: str,
    *,
    limit: int,
    offset: int = 0,
    start: int | None = None,
    end: int | None = None,
) -> list[dict[str, object]]:
    clauses = ["uid = ?", "discarded = 0", "status IN ('processing', 'completed')", "is_locked = 0"]
    args: list[object] = [uid]
    if start is not None:
        clauses.append("created_at >= ?")
        args.append(start)
    if end is not None:
        clauses.append("created_at <= ?")
        args.append(end)
    result = (
        await env.APP_DB.prepare(
            _CONVERSATION_SELECT
            + "WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        )
        .bind(*args, limit, offset)
        .all()
    )
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [row for row in rows if isinstance(row, dict)]


async def _conversation_rows_for_ids(
    env: object,
    uid: str,
    ids: list[str],
    *,
    start: int | None = None,
    end: int | None = None,
) -> list[dict[str, object]]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    clauses = ["uid = ?", f"id IN ({placeholders})", "discarded = 0", "status = 'completed'", "is_locked = 0"]
    args: list[object] = [uid, *ids]
    if start is not None:
        clauses.append("created_at >= ?")
        args.append(start)
    if end is not None:
        clauses.append("created_at <= ?")
        args.append(end)
    result = await env.APP_DB.prepare(_CONVERSATION_SELECT + "WHERE " + " AND ".join(clauses)).bind(*args).all()
    rows = result.get("results", []) if isinstance(result, dict) else []
    by_id = {str(row["id"]): row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}
    return [by_id[item_id] for item_id in ids if item_id in by_id]


def _exact_conversation_id(query: str) -> str | None:
    value = query.strip()
    if EXACT_CONVERSATION_ID.fullmatch(value):
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"h.omi.me", "www.h.omi.me"}:
        return None
    candidate = parsed.path.rstrip("/").split("/")[-1]
    return candidate if EXACT_CONVERSATION_ID.fullmatch(candidate) else None


@router.get("/v1/tools/conversations")
async def get_conversations(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    limit = _query_int(request, "limit", 20, 1, MAX_TOOL_RESULT_ROWS)
    offset = _query_int(request, "offset", 0, 0, MAX_OFFSET)
    include_transcript = _query_bool(request, "include_transcript", True)
    if limit is None or offset is None or include_transcript is _INVALID:
        return JSONResponse({"detail": "invalid tool query"}, status_code=422)
    try:
        start, end = _date_bounds(request.query_params.get("start_date"), request.query_params.get("end_date"))
    except ValueError as error:
        return _ok("get_conversations", f"Error: Invalid date filter: {error}")
    try:
        rows = await _conversation_rows(
            request.scope["env"],
            str(context["uid"]),
            limit=limit,
            offset=offset,
            start=start,
            end=end,
        )
    except Exception:
        return _ok("get_conversations", "Error retrieving conversations: data store unavailable")
    if not rows:
        return _ok("get_conversations", "No conversations found.")
    rendered = [_conversation_parts(row, bool(include_transcript)) for row in rows]
    return _ok(
        "get_conversations",
        f"User Conversations ({len(rendered)} total):\n\n" + "\n\n".join(item[0] for item in rendered),
        [item[1] for item in rendered[:128]],
    )


@router.post("/v1/tools/conversations/search")
async def search_conversations(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        search = ConversationSearch.model_validate(await _bounded_json(request))
        start, end = _date_bounds(search.start_date, search.end_date)
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    uid = str(context["uid"])
    env = request.scope["env"]
    exact_id = _exact_conversation_id(search.query)
    try:
        if exact_id:
            ordered_ids = [exact_id]
        else:
            vector = await embed_query(env, search.query)
            created_at_filter: dict[str, object] = {}
            if start is not None:
                created_at_filter["$gte"] = start
            if end is not None:
                created_at_filter["$lte"] = end
            summary_result, transcript_result = await asyncio.gather(
                query_vector_ids(
                    env,
                    "CONVERSATION_VECTORS",
                    uid,
                    vector,
                    top_k=search.limit,
                    created_at_filter=created_at_filter or None,
                ),
                query_vector_ids(
                    env,
                    "TRANSCRIPT_CHUNK_VECTORS",
                    uid,
                    vector,
                    top_k=min(search.limit * 3, 60),
                    created_at_filter=created_at_filter or None,
                ),
                return_exceptions=True,
            )
            if isinstance(summary_result, BaseException):
                raise summary_result
            summary = await hydrate_candidate_ids(env, uid, "conversation", summary_result)
            transcript: list[tuple[str, float]] = []
            if isinstance(transcript_result, BaseException):
                record_fallback(
                    component="other",
                    from_mode="transcript_vectorize",
                    to_mode="summary_vectorize",
                    reason="dependency_unavailable",
                    outcome="degraded",
                )
            else:
                transcript = await hydrate_candidate_ids(env, uid, "transcript_chunk", transcript_result)
            ordered_ids = []
            for source_id, _ in transcript + summary:
                if source_id not in ordered_ids:
                    ordered_ids.append(source_id)
                if len(ordered_ids) >= search.limit:
                    break
        rows = await _conversation_rows_for_ids(env, uid, ordered_ids, start=start, end=end)
    except Exception:
        return _ok("search_conversations", "Error performing conversation search: search unavailable")
    if not rows:
        return _ok("search_conversations", f"No conversations found matching '{search.query}'.")
    rendered = [_conversation_parts(row, search.include_transcript) for row in rows]
    match_kind = "matching exactly" if exact_id else "matching"
    return _ok(
        "search_conversations",
        f"Found {len(rendered)} conversations {match_kind} '{search.query}':\n\n"
        + "\n\n".join(item[0] for item in rendered),
        [item[1] for item in rendered[:128]],
    )


async def _chunk_state_rows(
    env: object,
    uid: str,
    matches: list[tuple[str, float]],
) -> list[tuple[str, str, float]]:
    if not matches:
        return []
    vector_ids = [vector_id for vector_id, _ in matches]
    placeholders = ",".join("?" for _ in vector_ids)
    result = (
        await env.APP_DB.prepare(
            "SELECT vector_id, source_id, sub_id FROM cf_vector_projection_state "
            f"WHERE uid = ? AND projection_kind = 'transcript_chunk' AND vector_id IN ({placeholders})"
        )
        .bind(uid, *vector_ids)
        .all()
    )
    rows = result.get("results", []) if isinstance(result, dict) else []
    by_vector = {
        str(row["vector_id"]): (str(row["source_id"]), str(row.get("sub_id") or "000000"))
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("vector_id"), str) and isinstance(row.get("source_id"), str)
    }
    return [(*by_vector[vector_id], score) for vector_id, score in matches if vector_id in by_vector]


def _chunk_text(row: dict[str, object], sub_id: str) -> str:
    try:
        offset = int(sub_id) * TRANSCRIPT_STRIDE
    except ValueError:
        offset = 0
    lines: list[str] = []
    for raw in json_list(row.get("transcript_segments_json"))[offset : offset + TRANSCRIPT_WINDOW]:
        if not isinstance(raw, dict):
            continue
        text = _flatten(raw.get("text"), 10_000)
        if not text:
            continue
        if raw.get("is_user"):
            speaker = "User"
        elif raw.get("speaker_id") is not None:
            speaker = f"Speaker {raw['speaker_id']}"
        else:
            speaker = str(raw.get("speaker") or "Speaker")
        lines.append(f"{speaker}: {text}")
    date = _iso(row.get("created_at"))
    return (f"[Conversation on {date}]\n" if date else "") + "\n".join(lines)


@router.post("/v1/tools/conversations/search-chunks")
async def search_conversation_chunks(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        search = ChunkSearch.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        vector = await embed_query(env, search.query)
        matches = await query_vector_ids(
            env,
            "TRANSCRIPT_CHUNK_VECTORS",
            uid,
            vector,
            top_k=search.limit,
        )
        states = await _chunk_state_rows(env, uid, matches)
        rows = await _conversation_rows_for_ids(env, uid, list(dict.fromkeys(item[0] for item in states)))
    except Exception:
        return _ok("search_conversation_chunks", "Error searching transcript excerpts: search unavailable")
    by_id = {str(row.get("id") or ""): row for row in rows}
    parts: list[str] = []
    sources: list[dict[str, object]] = []
    seen: set[str] = set()
    for conversation_id, sub_id, score in states:
        row = by_id.get(conversation_id)
        if row is None:
            continue
        text = _chunk_text(row, sub_id)
        if not text.strip():
            continue
        parts.append(f"Excerpt {len(parts) + 1} (relevance: {score:.2f}):\n{text}")
        if conversation_id not in seen:
            seen.add(conversation_id)
            structured = json_object(row.get("structured_json"))
            sources.append(
                _source(
                    "conversation",
                    conversation_id,
                    title=str(structured.get("title") or "Conversation"),
                    preview=text,
                    created_at=row.get("created_at"),
                )
            )
    if not parts:
        return _ok(
            "search_conversation_chunks",
            f"No transcript excerpts found matching '{search.query}'.",
        )
    return _ok("search_conversation_chunks", "\n\n".join(parts), sources[:128])


async def _memory_rows(
    env: object,
    uid: str,
    *,
    limit: int,
    offset: int = 0,
    start: int | None = None,
    end: int | None = None,
) -> list[dict[str, object]]:
    clauses = [
        "uid = ?",
        "deleted_at IS NULL",
        "invalid_at IS NULL",
        "memory_tier != 'archive'",
        "COALESCE(user_review, 1) != 0",
        "is_locked = 0",
    ]
    args: list[object] = [uid]
    if start is not None:
        clauses.append("created_at >= ?")
        args.append(start)
    if end is not None:
        clauses.append("created_at <= ?")
        args.append(end)
    result = (
        await env.APP_DB.prepare(
            MEMORY_SELECT + "WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
        )
        .bind(*args, limit, offset)
        .all()
    )
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [row for row in rows if isinstance(row, dict)]


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
    by_id = {str(row["id"]): row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}
    return [by_id[item_id] for item_id in ids if item_id in by_id]


def _memory_source(row: dict[str, object]) -> dict[str, object]:
    return _source(
        "memory",
        str(row.get("id") or ""),
        title="Memory",
        preview=str(row.get("content") or ""),
        created_at=row.get("created_at"),
    )


@router.get("/v1/tools/memories")
async def get_memories(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    limit = _query_int(request, "limit", 50, 1, MAX_TOOL_RESULT_ROWS)
    offset = _query_int(request, "offset", 0, 0, MAX_OFFSET)
    if limit is None or offset is None:
        return JSONResponse({"detail": "invalid pagination"}, status_code=422)
    try:
        start, end = _date_bounds(request.query_params.get("start_date"), request.query_params.get("end_date"))
    except ValueError as error:
        return _ok("get_memories", f"Error: Invalid date filter: {error}")
    try:
        rows = await _memory_rows(
            request.scope["env"],
            str(context["uid"]),
            limit=limit,
            offset=offset,
            start=start,
            end=end,
        )
    except Exception:
        return _ok("get_memories", "Error retrieving memories: data store unavailable")
    if not rows:
        return _ok("get_memories", "No memories found.")
    lines = [
        f"- {row.get('content') or ''} (category: {row.get('category') or 'interesting'}, date: "
        f"{(_iso(row.get('created_at')) or 'Unknown')[:10]})"
        for row in rows
    ]
    return _ok(
        "get_memories",
        f"User Memories ({len(rows)} total):\n\n" + "\n".join(lines),
        [_memory_source(row) for row in rows[:128]],
    )


@router.post("/v1/tools/memories/search")
async def search_memories(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        search = MemorySearch.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        vector = await embed_query(env, search.query)
        matches = await query_vector_ids(
            env,
            "MEMORY_VECTORS",
            uid,
            vector,
            top_k=min(search.limit * 3, 60),
        )
        candidates = await hydrate_candidate_ids(env, uid, "memory", matches)
        rows = await _memory_rows_for_ids(env, uid, [source_id for source_id, _ in candidates])
    except Exception:
        return _ok("search_memories", "Error searching memories: search unavailable")
    if not rows:
        return _ok("search_memories", f"No memories found matching '{search.query}'.")
    score_by_id = dict(candidates)
    lines = [
        f"- {row.get('content') or ''} (relevance: {score_by_id.get(str(row.get('id') or ''), 0.0):.2f}, "
        f"category: {row.get('category') or 'interesting'}, date: {(_iso(row.get('created_at')) or 'Unknown')[:10]})"
        for row in rows[: search.limit]
    ]
    visible = rows[: search.limit]
    return _ok(
        "search_memories",
        f"Found {len(visible)} memories matching '{search.query}':\n\n" + "\n".join(lines),
        [_memory_source(row) for row in visible[:128]],
    )


def _action_source(row: dict[str, object]) -> dict[str, object]:
    description = str(row.get("description") or "Task")
    return _source(
        "task",
        str(row.get("id") or ""),
        title=description,
        preview=description,
        created_at=row.get("created_at"),
    )


def _format_action_items(rows: list[dict[str, object]]) -> str:
    lines = [f"User Action Items ({len(rows)} total):", ""]
    for index, row in enumerate(rows, 1):
        completed = bool(row.get("completed"))
        lines.append(
            f"{index}. [{'✅ Completed' if completed else '⬜ Pending'}] {row.get('description') or 'No description'}"
        )
        lines.append(f"   ID: {row.get('id') or ''}")
        if created := _iso(row.get("created_at")):
            lines.append(f"   Created: {created}")
        if due := _iso(row.get("due_at")):
            lines.append(f"   Due: {due}")
        if completed_at := _iso(row.get("completed_at")):
            lines.append(f"   Completed: {completed_at}")
        if row.get("conversation_id"):
            lines.append(f"   From conversation: {row['conversation_id']}")
        lines.append("")
    return "\n".join(lines).strip()


@router.get("/v1/tools/action-items")
async def get_action_items(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    limit = _query_int(request, "limit", 50, 1, 500)
    offset = _query_int(request, "offset", 0, 0, MAX_OFFSET)
    completed = _query_bool(request, "completed")
    if limit is None or offset is None or completed is _INVALID:
        return JSONResponse({"detail": "invalid action item query"}, status_code=422)
    clauses = ["uid = ?", "deleted = 0", "is_locked = 0"]
    args: list[object] = [str(context["uid"])]
    if completed is not None:
        clauses.append("completed = ?")
        args.append(1 if completed else 0)
    conversation_id = request.query_params.get("conversation_id")
    if conversation_id:
        if len(conversation_id) > MAX_ID_LENGTH:
            return JSONResponse({"detail": "conversation_id is too long"}, status_code=422)
        clauses.append("conversation_id = ?")
        args.append(conversation_id)
    try:
        for start_name, end_name, column in (
            ("start_date", "end_date", "created_at"),
            ("due_start_date", "due_end_date", "due_at"),
        ):
            start, end = _date_bounds(request.query_params.get(start_name), request.query_params.get(end_name))
            if start is not None:
                clauses.append(f"{column} >= ?")
                args.append(start)
            if end is not None:
                clauses.append(f"{column} <= ?")
                args.append(end)
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
    except ValueError as error:
        return _ok("get_action_items", f"Error: Invalid date filter: {error}")
    except Exception:
        return _ok("get_action_items", "Error retrieving action items: data store unavailable")
    rows = result.get("results", []) if isinstance(result, dict) else []
    visible = [row for row in rows if isinstance(row, dict)]
    if not visible:
        return _ok("get_action_items", "No action items found.")
    return _ok(
        "get_action_items",
        _format_action_items(visible),
        [_action_source(row) for row in visible[:128]],
    )


@router.post("/v1/tools/action-items")
async def create_action_item(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = ToolActionItemCreate.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    try:
        due_at = (
            _parse_iso(body.due_at, "due_at")
            if body.due_at is not None
            else datetime.now(timezone.utc) + timedelta(hours=24)
        )
    except ValueError as error:
        return _ok("create_action_item", f"Error: Invalid due_at format: {error}")
    now = datetime.now(timezone.utc)
    if due_at is not None and due_at < now - timedelta(days=1):
        return _ok("create_action_item", f"Error: due_at '{body.due_at}' is in the past.")
    try:
        row = await insert_action_item(
            request.scope["env"],
            str(context["uid"]),
            ActionItemCreate(
                description=body.description,
                due_at=due_at,
                conversation_id=body.conversation_id,
                source="manual",
            ),
        )
    except Exception:
        return _ok("create_action_item", "Error creating action item: data store unavailable")
    result = f"✅ Added: {row.get('description') or 'Task'}"
    if due_at:
        result += f" (due {due_at.strftime('%b %d')})"
    return _ok("create_action_item", result)


@router.patch("/v1/tools/action-items/{action_item_id}")
async def update_action_item(request: Request, action_item_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not action_item_id or len(action_item_id) > MAX_ID_LENGTH:
        return _ok("update_action_item", f"Error: Action item '{action_item_id}' not found.")
    try:
        body = ToolActionItemUpdate.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        existing = await first_action_item(env, uid, action_item_id)
    except Exception:
        return _ok("update_action_item", "Error updating action item: data store unavailable")
    if existing is None:
        return _ok("update_action_item", f"Error: Action item '{action_item_id}' not found.")
    if bool(existing.get("is_locked")):
        return _ok("update_action_item", "Error: A paid plan is required to modify this action item.")
    changes: list[str] = []
    values: dict[str, object] = {}
    if body.completed is not None:
        values["completed"] = body.completed
        changes.append("marked as completed" if body.completed else "marked as pending")
    if body.description is not None:
        description = body.description.strip()
        if not description:
            return _ok("update_action_item", "Error: Description is required.")
        values["description"] = description
        changes.append(f"description updated to '{description}'")
    if body.due_at is not None:
        try:
            due_at = _parse_iso(body.due_at, "due_at")
        except ValueError as error:
            return _ok("update_action_item", f"Error: Invalid due_at format: {error}")
        values["due_at"] = due_at
        changes.append(f"due date set to {due_at.strftime('%Y-%m-%d %H:%M')}")
    if not values:
        return _ok("update_action_item", "No changes specified.")
    try:
        row = await apply_action_item_update(env, uid, action_item_id, ActionItemUpdate.model_validate(values))
    except (ValidationError, ValueError, TypeError):
        return _ok("update_action_item", "Error: Invalid action item update.")
    except Exception:
        return _ok("update_action_item", "Error updating action item: data store unavailable")
    description = str((row or existing).get("description") or "Task")
    return _ok("update_action_item", f"✅ Updated '{description}': {', '.join(changes)}")


__all__ = [
    "create_action_item",
    "get_action_items",
    "get_conversations",
    "get_memories",
    "router",
    "search_conversation_chunks",
    "search_conversations",
    "search_memories",
    "update_action_item",
]
