"""D1-backed People metadata routes for the isolated Cloudflare profile.

This route group owns the person id/name metadata used by transcript speaker
labels. Speech sample paths, signed URL generation, and sample deletion remain
on the legacy GCS owner until the R2 media contract is migrated.
"""

from __future__ import annotations

import json
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from internal_auth import decode_context

router = APIRouter()

MAX_ID_LENGTH = 256


class PersonCreate(BaseModel):
    model_config = {"extra": "ignore"}

    name: str = Field(min_length=2, max_length=40)


class PersonRename(BaseModel):
    model_config = {"extra": "ignore"}

    name: str = Field(min_length=2, max_length=40)


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _query_bool(request: Request, name: str, default: bool) -> bool | None:
    raw = getattr(request, "query_params", {}).get(name)
    if raw is None or raw == "":
        return default
    normalized = str(raw).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
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


def _iso(epoch: object) -> str | None:
    if epoch is None or isinstance(epoch, bool):
        return None
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(int(epoch), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _response(row: dict[str, object], include_speech_samples: bool) -> dict[str, object]:
    return {
        "id": str(row.get("id") or ""),
        "name": str(row.get("name") or ""),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "speech_samples": _json_list(row.get("speech_samples_json")) if include_speech_samples else [],
        "speech_sample_transcripts": (
            _json_list(row.get("speech_sample_transcripts_json"))
            if include_speech_samples and row.get("speech_sample_transcripts_json")
            else None
        ),
        "speech_samples_version": int(row.get("speech_samples_version") or 3),
    }


_SELECT = (
    "SELECT id, name, speech_samples_json, speech_sample_transcripts_json, speech_samples_version, "
    "created_at, updated_at FROM cf_people "
)


async def _first_person(env: object, uid: str, person_id: str) -> dict[str, object] | None:
    row = await env.APP_DB.prepare(_SELECT + "WHERE uid = ? AND id = ?").bind(uid, person_id).first()
    return row if isinstance(row, dict) else None


async def _person_by_name(env: object, uid: str, name: str) -> dict[str, object] | None:
    row = await env.APP_DB.prepare(_SELECT + "WHERE uid = ? AND name = ? LIMIT 1").bind(uid, name).first()
    return row if isinstance(row, dict) else None


@router.post("/v1/users/people")
async def get_or_create_person(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        person = PersonCreate.model_validate(body)
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid person"}, status_code=400)

    env = request.scope["env"]
    uid = str(context["uid"])
    existing = await _person_by_name(env, uid, person.name)
    if existing:
        return _response(existing, include_speech_samples=False)

    now = int(time.time())
    person_id = uuid.uuid4().hex
    try:
        await env.APP_DB.prepare(
            "INSERT INTO cf_people (uid, id, name, speech_samples_json, speech_sample_transcripts_json, "
            "speech_samples_version, created_at, updated_at) VALUES (?, ?, ?, '[]', NULL, 3, ?, ?) "
            "ON CONFLICT(uid, name) DO NOTHING"
        ).bind(uid, person_id, person.name, now, now).run()
        created = await _person_by_name(env, uid, person.name)
    except Exception:
        return JSONResponse({"error": "people unavailable"}, status_code=503)
    if created is None:
        return JSONResponse({"error": "person unavailable"}, status_code=503)
    return _response(created, include_speech_samples=False)


@router.get("/v1/users/people")
async def list_people(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    include_speech_samples = _query_bool(request, "include_speech_samples", True)
    if include_speech_samples is None:
        return JSONResponse({"error": "invalid include_speech_samples"}, status_code=400)
    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(_SELECT + "WHERE uid = ? ORDER BY created_at ASC, id ASC")
            .bind(str(context["uid"]))
            .all()
        )
    except Exception:
        return JSONResponse({"error": "people unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [_response(row, include_speech_samples) for row in rows if isinstance(row, dict)]


@router.get("/v1/users/people/{person_id}")
async def get_person(request: Request, person_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not person_id or len(person_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid person id"}, status_code=400)
    include_speech_samples = _query_bool(request, "include_speech_samples", False)
    if include_speech_samples is None:
        return JSONResponse({"error": "invalid include_speech_samples"}, status_code=400)
    try:
        row = await _first_person(request.scope["env"], str(context["uid"]), person_id)
    except Exception:
        return JSONResponse({"error": "people unavailable"}, status_code=503)
    return (
        _response(row, include_speech_samples) if row else JSONResponse({"error": "person not found"}, status_code=404)
    )


def _rename_value(request: Request) -> str | None:
    value = getattr(request, "query_params", {}).get("value")
    return str(value) if value is not None else None


@router.patch("/v1/users/people/{person_id}/name")
async def rename_person(request: Request, person_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not person_id or len(person_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid person id"}, status_code=400)
    value = _rename_value(request)
    if value is None:
        try:
            body = await request.json()
            value = body.get("value") if isinstance(body, dict) else None
        except (TypeError, ValueError):
            value = None
    try:
        rename = PersonRename.model_validate({"name": value})
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid person name"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    existing = await _first_person(env, uid, person_id)
    if existing is None:
        return JSONResponse({"error": "person not found"}, status_code=404)
    try:
        await env.APP_DB.prepare("UPDATE cf_people SET name = ?, updated_at = ? WHERE uid = ? AND id = ?").bind(
            rename.name, int(time.time()), uid, person_id
        ).run()
    except Exception:
        return JSONResponse({"error": "person name already exists"}, status_code=409)
    return {"status": "ok"}


@router.delete("/v1/users/people/{person_id}")
async def delete_person(request: Request, person_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not person_id or len(person_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "invalid person id"}, status_code=400)
    try:
        await request.scope["env"].APP_DB.prepare("DELETE FROM cf_people WHERE uid = ? AND id = ?").bind(
            str(context["uid"]), person_id
        ).run()
    except Exception:
        return JSONResponse({"error": "people unavailable"}, status_code=503)
    return Response(status_code=204)
