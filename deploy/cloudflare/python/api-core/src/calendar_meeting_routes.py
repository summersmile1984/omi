"""D1-backed calendar meeting metadata for the isolated Cloudflare profile.

This projection owns the calendar event metadata written by clients. Google
OAuth tokens and provider event discovery are owned by the Jobs Worker; the
conversation-linking endpoints use that same Worker-owned Calendar authority.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from internal_auth import decode_context

router = APIRouter()

MAX_ID_LENGTH = 256
MAX_EVENT_ID_LENGTH = 512
MAX_SOURCE_LENGTH = 128
MAX_TITLE_LENGTH = 512
MAX_PARTICIPANTS = 200


class MeetingParticipant(BaseModel):
    model_config = {"extra": "ignore"}

    name: str | None = Field(default=None, max_length=256)
    email: str | None = Field(default=None, max_length=512)


class StoreMeetingRequest(BaseModel):
    model_config = {"extra": "ignore"}

    calendar_event_id: str = Field(min_length=1, max_length=MAX_EVENT_ID_LENGTH)
    calendar_source: str = Field(min_length=1, max_length=MAX_SOURCE_LENGTH)
    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    start_time: datetime
    end_time: datetime
    platform: str | None = Field(default=None, max_length=128)
    meeting_link: str | None = Field(default=None, max_length=4096)
    participants: list[MeetingParticipant] = Field(default_factory=list, max_length=MAX_PARTICIPANTS)
    notes: str | None = Field(default=None, max_length=50_000)


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _epoch(value: datetime) -> int:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return int(aware.astimezone(timezone.utc).timestamp())


def _iso(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _meeting_id(uid: str, source: str, event_id: str) -> str:
    seed = f"user:{uid}:calendar_meeting:{source}:{event_id}".encode("utf-8")
    raw = bytearray(hashlib.sha256(seed).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def _participants(value: object) -> list[dict[str, object]]:
    if not isinstance(value, str):
        return []
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(decoded, list):
        return []
    result: list[dict[str, object]] = []
    for item in decoded[:MAX_PARTICIPANTS]:
        if not isinstance(item, dict):
            continue
        result.append({"name": item.get("name"), "email": item.get("email")})
    return result


def _response(row: dict[str, object]) -> dict[str, object] | None:
    start_time = _iso(row.get("start_time"))
    if not start_time or not row.get("calendar_event_id") or not row.get("title"):
        return None
    return {
        "calendar_event_id": str(row["calendar_event_id"]),
        "title": str(row["title"]),
        "participants": _participants(row.get("participants_json")),
        "platform": row.get("platform"),
        "meeting_link": row.get("meeting_link"),
        "start_time": start_time,
        "duration_minutes": int(row.get("duration_minutes") or 0),
        "notes": row.get("notes"),
        "calendar_source": row.get("calendar_source") or "system_calendar",
    }


_SELECT = (
    "SELECT id, calendar_event_id, calendar_source, title, participants_json, platform, meeting_link, "
    "start_time, end_time, duration_minutes, notes, created_at, updated_at "
    "FROM cf_calendar_meetings "
)


async def _first(env: object, uid: str, meeting_id: str) -> dict[str, object] | None:
    row = await env.APP_DB.prepare(_SELECT + "WHERE uid = ? AND id = ?").bind(uid, meeting_id).first()
    return row if isinstance(row, dict) else None


@router.post("/v1/calendar/meetings")
async def store_calendar_meeting(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        meeting = StoreMeetingRequest.model_validate(body)
        start = _epoch(meeting.start_time)
        end = _epoch(meeting.end_time)
        if end <= start:
            raise ValueError("meeting end must be after start")
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid calendar meeting"}, status_code=400)

    uid = str(context["uid"])
    meeting_id = _meeting_id(uid, meeting.calendar_source, meeting.calendar_event_id)
    now = int(time.time())
    participants_json = json.dumps(
        [participant.model_dump() for participant in meeting.participants],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    env = request.scope["env"]
    try:
        await env.APP_DB.prepare(
            "INSERT INTO cf_calendar_meetings "
            "(uid, id, calendar_event_id, calendar_source, title, participants_json, platform, meeting_link, "
            "start_time, end_time, duration_minutes, notes, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(uid, calendar_source, calendar_event_id) DO UPDATE SET "
            "id = excluded.id, title = excluded.title, participants_json = excluded.participants_json, "
            "platform = excluded.platform, meeting_link = excluded.meeting_link, start_time = excluded.start_time, "
            "end_time = excluded.end_time, duration_minutes = excluded.duration_minutes, notes = excluded.notes, "
            "updated_at = excluded.updated_at"
        ).bind(
            uid,
            meeting_id,
            meeting.calendar_event_id,
            meeting.calendar_source,
            meeting.title,
            participants_json,
            meeting.platform,
            meeting.meeting_link,
            start,
            end,
            int((end - start) / 60),
            meeting.notes,
            now,
            now,
        ).run()
    except Exception:
        return JSONResponse({"error": "calendar meetings unavailable"}, status_code=503)
    return {
        "meeting_id": meeting_id,
        "calendar_event_id": meeting.calendar_event_id,
        "message": "Meeting stored successfully",
    }


@router.get("/v1/calendar/meetings/{meeting_id}")
async def get_calendar_meeting(request: Request, meeting_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not meeting_id or len(meeting_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid meeting id"}, status_code=400)
    try:
        row = await _first(request.scope["env"], str(context["uid"]), meeting_id)
    except Exception:
        return JSONResponse({"error": "calendar meetings unavailable"}, status_code=503)
    response = _response(row) if row else None
    return response if response else JSONResponse({"error": "meeting not found"}, status_code=404)


@router.get("/v1/calendar/meetings")
async def list_calendar_meetings(
    request: Request,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    limit: int = 50,
):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 50:
        return JSONResponse({"error": "invalid limit"}, status_code=422)
    clauses = ["uid = ?"]
    args: list[object] = [str(context["uid"])]
    if start_date is not None:
        clauses.append("start_time >= ?")
        args.append(_epoch(start_date))
    if end_date is not None:
        clauses.append("start_time <= ?")
        args.append(_epoch(end_date))
    args.append(limit)
    query = _SELECT + "WHERE " + " AND ".join(clauses) + " ORDER BY start_time DESC, id DESC LIMIT ?"
    try:
        result = await request.scope["env"].APP_DB.prepare(query).bind(*args).all()
    except Exception:
        return JSONResponse({"error": "calendar meetings unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [parsed for row in rows if isinstance(row, dict) if (parsed := _response(row)) is not None]
