"""D1-backed People metadata and R2-backed speech-sample routes.

Person rows own the ordered sample/transcript references while the biometric
audio objects live under the same uid/person-scoped ``SPEECH_PROFILES`` R2
boundary as the account speech profile. Public responses never expose object
keys: they receive the shared short-lived, object-bound playback assertion.
"""

from __future__ import annotations

import json
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from internal_auth import decode_context
from speech_profile_routes import (
    PEOPLE_SAMPLE_PREFIX,
    _bucket as _speech_bucket,
    _list_keys as _list_speech_keys,
    _signed_profile_url as _signed_speech_url,
    _valid_id,
    _valid_object_key,
)

router = APIRouter()


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


def _person_sample_prefix(uid: str, person_id: str) -> str:
    return f"{uid}/{PEOPLE_SAMPLE_PREFIX}/{person_id}/"


def _person_sample_keys(row: dict[str, object], uid: str, person_id: str) -> list[str]:
    prefix = _person_sample_prefix(uid, person_id)
    values = _json_list(row.get("speech_samples_json"))
    keys: list[str] = []
    for value in values:
        if (
            not isinstance(value, str)
            or not _valid_object_key(uid, value)
            or not value.startswith(prefix)
            or not _valid_id(value[len(prefix) :])
        ):
            raise ValueError("invalid person speech sample reference")
        keys.append(value)
    return keys


def _response_with_signed_samples(
    request: Request,
    row: dict[str, object],
    uid: str,
    include_speech_samples: bool,
) -> dict[str, object]:
    response = _response(row, include_speech_samples)
    if not include_speech_samples:
        return response
    person_id = str(row.get("id") or "")
    keys = _person_sample_keys(row, uid, person_id)
    if not keys:
        return response
    env = request.scope["env"]
    if _speech_bucket(env) is None:
        raise RuntimeError("speech profile storage is not configured")
    urls = [_signed_speech_url(request, env, uid, key) for key in keys]
    if any(url is None for url in urls):
        raise RuntimeError("speech profile URL signing is not configured")
    response["speech_samples"] = urls
    return response


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
    try:
        return [
            _response_with_signed_samples(request, row, str(context["uid"]), include_speech_samples)
            for row in rows
            if isinstance(row, dict)
        ]
    except (RuntimeError, ValueError):
        return JSONResponse({"error": "people speech samples unavailable"}, status_code=503)


@router.get("/v1/users/people/{person_id}")
async def get_person(request: Request, person_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _valid_id(person_id):
        return JSONResponse({"error": "invalid person id"}, status_code=400)
    include_speech_samples = _query_bool(request, "include_speech_samples", False)
    if include_speech_samples is None:
        return JSONResponse({"error": "invalid include_speech_samples"}, status_code=400)
    try:
        row = await _first_person(request.scope["env"], str(context["uid"]), person_id)
    except Exception:
        return JSONResponse({"error": "people unavailable"}, status_code=503)
    if row is None:
        return JSONResponse({"error": "person not found"}, status_code=404)
    try:
        return _response_with_signed_samples(request, row, str(context["uid"]), include_speech_samples)
    except (RuntimeError, ValueError):
        return JSONResponse({"error": "people speech samples unavailable"}, status_code=503)


def _rename_value(request: Request) -> str | None:
    value = getattr(request, "query_params", {}).get("value")
    return str(value) if value is not None else None


@router.patch("/v1/users/people/{person_id}/name")
async def rename_person(request: Request, person_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _valid_id(person_id):
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
    if not _valid_id(person_id):
        return JSONResponse({"error": "invalid person id"}, status_code=400)
    env = request.scope["env"]
    bucket = _speech_bucket(env)
    if bucket is None:
        return JSONResponse({"error": "speech profile storage is not configured"}, status_code=503)
    uid = str(context["uid"])
    try:
        keys = await _list_speech_keys(bucket, _person_sample_prefix(uid, person_id))
        for key in keys:
            await bucket.delete(key)
    except OverflowError:
        return JSONResponse({"error": "too many person speech samples"}, status_code=409)
    except Exception:
        return JSONResponse({"error": "people speech samples unavailable"}, status_code=503)
    try:
        await env.APP_DB.prepare("DELETE FROM cf_people WHERE uid = ? AND id = ?").bind(uid, person_id).run()
    except Exception:
        return JSONResponse({"error": "people unavailable"}, status_code=503)
    return Response(status_code=204)


async def _remove_person_sample_reference(env: object, uid: str, person_id: str, key: str) -> bool:
    for _ in range(3):
        row = await _first_person(env, uid, person_id)
        if row is None:
            return True
        samples = _person_sample_keys(row, uid, person_id)
        if key not in samples:
            return True
        index = samples.index(key)
        transcripts_raw = row.get("speech_sample_transcripts_json")
        transcripts = _json_list(transcripts_raw)
        samples.pop(index)
        if index < len(transcripts):
            transcripts.pop(index)
        updated_transcripts = json.dumps(transcripts, separators=(",", ":")) if transcripts_raw is not None else None
        await (
            env.APP_DB.prepare(
                "UPDATE cf_people SET speech_samples_json = ?, speech_sample_transcripts_json = ?, updated_at = ? "
                "WHERE uid = ? AND id = ? AND speech_samples_json = ? AND speech_sample_transcripts_json IS ?"
            )
            .bind(
                json.dumps(samples, separators=(",", ":")),
                updated_transcripts,
                int(time.time()),
                uid,
                person_id,
                row.get("speech_samples_json"),
                transcripts_raw,
            )
            .run()
        )
        refreshed = await _first_person(env, uid, person_id)
        if refreshed is None or key not in _person_sample_keys(refreshed, uid, person_id):
            return True
    return False


@router.delete("/v1/users/people/{person_id}/speech-samples/{sample_index}")
async def delete_person_speech_sample(request: Request, person_id: str, sample_index: int):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _valid_id(person_id):
        return JSONResponse({"error": "invalid person id"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        row = await _first_person(env, uid, person_id)
        if row is None:
            return JSONResponse({"error": "person not found"}, status_code=404)
        samples = _person_sample_keys(row, uid, person_id)
    except ValueError:
        return JSONResponse({"error": "people speech samples unavailable"}, status_code=503)
    except Exception:
        return JSONResponse({"error": "people unavailable"}, status_code=503)
    if sample_index < 0 or sample_index >= len(samples):
        return JSONResponse({"error": "sample not found"}, status_code=404)
    bucket = _speech_bucket(env)
    if bucket is None:
        return JSONResponse({"error": "speech profile storage is not configured"}, status_code=503)
    key = samples[sample_index]
    try:
        await bucket.delete(key)
        removed = await _remove_person_sample_reference(env, uid, person_id, key)
    except ValueError:
        return JSONResponse({"error": "people speech samples unavailable"}, status_code=503)
    except Exception:
        return JSONResponse({"error": "people speech samples unavailable"}, status_code=503)
    if not removed:
        return JSONResponse({"error": "person speech samples changed; retry"}, status_code=409)
    return {"status": "ok"}
