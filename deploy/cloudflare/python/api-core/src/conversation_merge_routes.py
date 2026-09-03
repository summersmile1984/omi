"""D1/Queue-owned conversation merge lifecycle.

The legacy merge endpoint created a replacement conversation, optionally ran
the enrichment pipeline, and removed every source conversation and derived
row.  This module keeps that contract inside the Cloudflare data plane: Edge
admits a uid-scoped merge, Jobs owns the lease/retry loop, and this API Core
processor commits the replacement plus source cleanup in one D1 transaction.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import time
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from conversation_routes import _audio_storage_candidates
from developer_conversation_create_routes import (
    _conversation_payload,
    _enrichment,
    _fanout_targets,
    _persist_completed,
)
from internal_auth import decode_context

router = APIRouter()

MAX_SOURCE_CONVERSATIONS = 20
MAX_CONVERSATION_ID_LENGTH = 256
MAX_REQUEST_BYTES = 32_000
MAX_SEGMENTS = 2_000
MAX_TRANSCRIPT_TEXT_LENGTH = 500_000
MAX_PHOTOS = 2_000
MAX_AUDIO_FILES = 500
MAX_MERGE_ATTEMPTS = 3
PROCESSOR_PATH = "/internal/conversations/merge"
MERGE_NAMESPACE = uuid.UUID("74c5f8fb-3f90-44d1-9e2a-1a4db9b18863")


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_object(value: object) -> dict[str, object]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _json_list(value: object, *, maximum: int) -> list[object]:
    if not isinstance(value, str) or not value:
        return []
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return decoded[:maximum] if isinstance(decoded, list) else []


def _segments(value: object) -> list[dict[str, object]] | None:
    raw = _json_list(value, maximum=MAX_SEGMENTS)
    if not raw:
        return None
    result: list[dict[str, object]] = []
    total_text = 0
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip():
            return None
        try:
            start = float(item.get("start", 0))
            end = float(item.get("end", 0))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            return None
        total_text += len(item["text"])
        if total_text > MAX_TRANSCRIPT_TEXT_LENGTH:
            return None
        normalized = dict(item)
        normalized["start"] = start
        normalized["end"] = end
        normalized.setdefault("speaker", "SPEAKER_00")
        result.append(normalized)
    return result


def _epoch(row: dict[str, object], key: str, fallback: int = 0) -> int:
    value = row.get(key)
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return fallback
    return parsed if parsed >= 0 else fallback


def _source_order(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(
        rows, key=lambda row: (_epoch(row, "started_at", _epoch(row, "created_at")), str(row.get("id") or ""))
    )


def _merge_segments(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    cumulative_offset = 0.0
    for index, row in enumerate(rows):
        source_segments = _segments(row.get("transcript_segments_json")) or []
        if index == 0:
            merged.extend(dict(segment) for segment in source_segments)
            if source_segments:
                cumulative_offset = max(float(segment.get("end") or 0) for segment in source_segments)
            else:
                cumulative_offset = float(max(0, _epoch(row, "finished_at") - _epoch(row, "started_at")))
            continue

        previous = rows[index - 1]
        gap = max(0, _epoch(row, "started_at") - _epoch(previous, "finished_at"))
        offset = cumulative_offset + float(gap)
        for segment in source_segments:
            copied = dict(segment)
            copied["start"] = float(segment.get("start") or 0) + offset
            copied["end"] = float(segment.get("end") or 0) + offset
            merged.append(copied)
        if source_segments:
            cumulative_offset = offset + max(float(segment.get("end") or 0) for segment in source_segments)
        else:
            cumulative_offset = offset + float(max(0, _epoch(row, "finished_at") - _epoch(row, "started_at")))
    return merged


def _photos(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in rows:
        for raw in _json_list(row.get("photos_json"), maximum=MAX_PHOTOS):
            if not isinstance(raw, dict):
                continue
            photo = dict(raw)
            photo_id = photo.get("id")
            key = str(photo_id) if photo_id else _dump(photo)
            if key in seen:
                continue
            seen.add(key)
            result.append(photo)
    result.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")))
    return result[:MAX_PHOTOS]


def _visibility(rows: list[dict[str, object]]) -> str:
    rank = {"private": 0, "shared": 1, "public": 2}
    values = [str(row.get("visibility") or "private") for row in rows]
    return min(values, key=lambda value: rank.get(value, 0)) if values else "private"


def _shared_provenance(rows: list[dict[str, object]]) -> tuple[str | None, str | None]:
    values = {(row.get("client_device_id"), row.get("client_platform")) for row in rows}
    if len(values) != 1:
        return None, None
    device, platform = next(iter(values))
    if not isinstance(device, str) or not device or not isinstance(platform, str) or not platform:
        return None, None
    return device, platform


def _warning(rows: list[dict[str, object]]) -> str | None:
    ordered = _source_order(rows)
    warnings: list[str] = []
    for previous, current in zip(ordered, ordered[1:]):
        gap = (_epoch(current, "started_at") - _epoch(previous, "finished_at")) / 3600
        if gap > 1:
            warnings.append(f"{gap:.1f}h gap detected")
    return "; ".join(warnings) if warnings else None


def _job_response(row: dict[str, object], *, warning: str | None = None) -> dict[str, object]:
    status = str(row.get("status") or "queued")
    attempts = max(0, int(row.get("attempts") or 0))
    response: dict[str, object] = {
        "status": "merging" if status in {"queued", "running"} else status,
        "job_id": str(row.get("job_id") or ""),
        "result_conversation_id": str(row.get("result_conversation_id") or ""),
        "conversation_ids": _json_list(row.get("source_conversation_ids_json"), maximum=MAX_SOURCE_CONVERSATIONS),
        "reprocess": bool(row.get("reprocess")),
        "attempt_count": attempts,
    }
    if warning:
        response["warning"] = warning
    return response


async def _bounded_json(request: Request) -> dict[str, object]:
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("request body too large")
    decoded = json.loads(raw or b"{}")
    if not isinstance(decoded, dict):
        raise ValueError("request body must be an object")
    return decoded


def _conversation_ids(value: object) -> list[str] | None:
    if not isinstance(value, list) or not 2 <= len(value) <= MAX_SOURCE_CONVERSATIONS:
        return None
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > MAX_CONVERSATION_ID_LENGTH or "/" in item:
            return None
        if item in result:
            return None
        result.append(item)
    return result


async def _load_rows(env: object, uid: str, ids: list[str]) -> list[dict[str, object]]:
    placeholders = ",".join("?" for _ in ids)
    result = (
        await env.APP_DB.prepare(
            "SELECT uid, id, created_at, updated_at, started_at, finished_at, source, language, status, visibility, "
            "starred, discarded, is_locked, private_cloud_sync_enabled, folder_id, client_device_id, client_platform, "
            "structured_json, transcript_segments_json, photos_json, audio_files_json, conversation_audio_json, "
            "apps_results_json, suggested_apps_json, geolocation_json, external_data_json, calendar_event_json, app_id, "
            "merge_job_id, merge_revision FROM cf_conversations WHERE uid = ? AND id IN (" + placeholders + ")"
        )
        .bind(uid, *ids)
        .all()
    )
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [row for row in rows if isinstance(row, dict)]


async def _queue_merge(request: Request, uid: str, job_id: str, ids: list[str], revision: int, reprocess: bool) -> bool:
    queue = getattr(request.scope["env"], "JOBS", None)
    if queue is None:
        return False
    try:
        await queue.send(
            {
                "jobId": job_id,
                "uid": uid,
                "kind": "conversation_merge",
                "payload": {
                    "conversationIds": ids,
                    "revision": revision,
                    "reprocess": reprocess,
                },
            }
        )
    except Exception:
        return False
    return True


@router.post("/v1/conversations/merge")
async def merge_conversations(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await _bounded_json(request)
    except (json.JSONDecodeError, TypeError, ValueError):
        return JSONResponse({"error": "invalid merge request"}, status_code=400)
    ids = _conversation_ids(body.get("conversation_ids"))
    reprocess = body.get("reprocess", True)
    if ids is None or not isinstance(reprocess, bool):
        return JSONResponse({"error": "invalid merge request"}, status_code=400)
    uid = str(context.get("uid") or "")
    env = request.scope["env"]
    try:
        rows = await _load_rows(env, uid, ids)
    except Exception:
        return JSONResponse({"error": "conversation merge unavailable"}, status_code=503)
    if len(rows) != len(ids):
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    by_id = {str(row.get("id")): row for row in rows}
    ordered = [by_id[item] for item in ids]
    revision = max(_epoch(row, "updated_at", _epoch(row, "created_at")) for row in ordered)
    fingerprint = hashlib.sha256(f"{uid}\0{_dump(ids)}\0{int(reprocess)}".encode()).hexdigest()
    job_id = "conversation-merge-" + fingerprint[:48]
    result_id = str(uuid.uuid5(MERGE_NAMESPACE, f"{uid}\0{job_id}"))
    try:
        existing = (
            await env.APP_DB.prepare(
                "SELECT job_id, source_conversation_ids_json, result_conversation_id, reprocess, status, attempts "
                "FROM cf_conversation_merge_jobs WHERE uid = ? AND request_fingerprint = ?"
            )
            .bind(uid, fingerprint)
            .first()
        )
    except Exception:
        return JSONResponse({"error": "conversation merge unavailable"}, status_code=503)
    if isinstance(existing, dict):
        return _job_response(existing, warning=_warning(ordered))
    locked = next((row for row in ordered if int(row.get("is_locked") or 0)), None)
    if locked is not None:
        return JSONResponse({"error": "Cannot merge locked conversations. Please unlock them first."}, status_code=402)
    not_ready = next((row for row in ordered if str(row.get("status") or "completed") != "completed"), None)
    if not_ready is not None:
        return JSONResponse(
            {
                "error": f"Conversation {not_ready.get('id')} is not ready (status: {not_ready.get('status')}). Wait for it to complete."
            },
            status_code=409,
        )

    now = int(time.time())
    source_json = _dump(ids)
    try:
        admission = await env.APP_DB.batch(
            [
                env.APP_DB.prepare(
                    "INSERT INTO cf_conversation_merge_jobs "
                    "(uid, job_id, source_conversation_ids_json, result_conversation_id, merge_revision, reprocess, status, "
                    "attempts, next_attempt_at, request_fingerprint, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?) ON CONFLICT DO NOTHING"
                ).bind(
                    uid, job_id, source_json, result_id, revision, 1 if reprocess else 0, now, fingerprint, now, now
                ),
                env.APP_DB.prepare(
                    "UPDATE cf_conversations SET status = 'merging', merge_job_id = ?, merge_revision = ? "
                    "WHERE uid = ? AND id IN ("
                    + ",".join("?" for _ in ids)
                    + ") AND status = 'completed' AND merge_job_id IS NULL"
                ).bind(job_id, revision, uid, *ids),
            ]
        )
    except Exception:
        return JSONResponse({"error": "conversation merge unavailable"}, status_code=503)
    inserted = admission[0] if isinstance(admission, list) and admission else None
    updated = admission[1] if isinstance(admission, list) and len(admission) > 1 else None
    if int((inserted or {}).get("meta", {}).get("changes", 0)) != 1:
        existing = (
            await env.APP_DB.prepare(
                "SELECT job_id, source_conversation_ids_json, result_conversation_id, reprocess, status, attempts "
                "FROM cf_conversation_merge_jobs WHERE uid = ? AND request_fingerprint = ?"
            )
            .bind(uid, fingerprint)
            .first()
        )
        return (
            _job_response(existing, warning=_warning(ordered))
            if isinstance(existing, dict)
            else JSONResponse({"error": "merge job conflict"}, status_code=409)
        )
    if int((updated or {}).get("meta", {}).get("changes", 0)) != len(ids):
        await env.APP_DB.batch(
            [
                env.APP_DB.prepare(
                    "UPDATE cf_conversations SET status = 'completed', merge_job_id = NULL, merge_revision = NULL "
                    "WHERE uid = ? AND merge_job_id = ?"
                ).bind(uid, job_id),
                env.APP_DB.prepare("DELETE FROM cf_conversation_merge_jobs WHERE uid = ? AND job_id = ?").bind(
                    uid, job_id
                ),
            ]
        )
        return JSONResponse({"error": "conversation merge conflict"}, status_code=409)
    if not await _queue_merge(request, uid, job_id, ids, revision, reprocess):
        await env.APP_DB.batch(
            [
                env.APP_DB.prepare(
                    "UPDATE cf_conversations SET status = 'completed', merge_job_id = NULL, merge_revision = NULL "
                    "WHERE uid = ? AND merge_job_id = ?"
                ).bind(uid, job_id),
                env.APP_DB.prepare(
                    "UPDATE cf_conversation_merge_jobs SET status = 'failed', last_error = ?, updated_at = ? "
                    "WHERE uid = ? AND job_id = ?"
                ).bind("queue unavailable", now, uid, job_id),
            ]
        )
        return JSONResponse({"error": "conversation merge queue unavailable"}, status_code=503)
    response = {
        "status": "merging",
        "message": "Merge started",
        "warning": _warning(ordered),
        "conversation_ids": ids,
        "job_id": job_id,
        "result_conversation_id": result_id,
        "reprocess": reprocess,
    }
    return {key: value for key, value in response.items() if value is not None}


async def _copy_r2_object(
    source_bucket: object, target_bucket: object, source_key: str, target_key: str, content_type: str
) -> None:
    stored = await source_bucket.get(source_key)
    if stored is None:
        raise RuntimeError("conversation audio object missing")
    body = getattr(stored, "body", None)
    if body is None:
        array_buffer = getattr(stored, "arrayBuffer", None)
        if not callable(array_buffer):
            raise RuntimeError("conversation audio object is unreadable")
        body = await array_buffer()
    await target_bucket.put(target_key, body, httpMetadata={"contentType": content_type[:100] or "audio/wav"})


async def _copy_audio_metadata(
    env: object, uid: str, rows: list[dict[str, object]], result_id: str
) -> tuple[list[dict[str, object]], dict[str, object] | None]:
    target_bucket = getattr(env, "ASSETS", None)
    recording_bucket = getattr(env, "CONVERSATION_RECORDINGS", None)
    if (
        target_bucket is None
        or not callable(getattr(target_bucket, "head", None))
        or not callable(getattr(target_bucket, "put", None))
    ):
        # Audio is optional. A source with audio metadata must not be silently
        # deleted when the destination R2 binding is unavailable.
        if any(
            _json_list(row.get("audio_files_json"), maximum=MAX_AUDIO_FILES)
            or _json_object(row.get("conversation_audio_json"))
            for row in rows
        ):
            raise RuntimeError("conversation recording storage is not configured")
        return [], None

    copied_files: list[dict[str, object]] = []
    for row in rows:
        source_id = str(row.get("id") or "")
        for raw in _json_list(row.get("audio_files_json"), maximum=MAX_AUDIO_FILES):
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            original_id = str(item.get("id") or uuid.uuid4().hex)
            file_id = original_id
            if any(existing.get("id") == file_id for existing in copied_files):
                file_id = uuid.uuid5(MERGE_NAMESPACE, f"{source_id}\0{original_id}\0{len(copied_files)}").hex
            content_type = str(item.get("content_type") or "audio/wav")
            source_key = None
            for candidate, _ in _audio_storage_candidates(uid, source_id, item):
                if await target_bucket.head(candidate) is not None:
                    source_key = candidate
                    break
            if (
                source_key is None
                and recording_bucket is not None
                and callable(getattr(recording_bucket, "head", None))
            ):
                legacy_key = f"{uid}/{source_id}.wav"
                if await recording_bucket.head(legacy_key) is not None:
                    source_key = legacy_key
                    source_bucket = recording_bucket
                else:
                    source_bucket = target_bucket
            else:
                source_bucket = target_bucket
            if source_key is None and isinstance(item.get("storage_key"), str) and item["storage_key"]:
                raise RuntimeError("conversation audio object missing")
            target_key = f"merged/{uid}/{result_id}/{file_id}.wav"
            if source_key is not None:
                await _copy_r2_object(source_bucket, target_bucket, source_key, target_key, content_type)
                item["storage_key"] = target_key
                item["provider"] = "cloudflare-r2"
            item["id"] = file_id
            item["conversation_id"] = result_id
            copied_files.append(item)

    conversation_audio: dict[str, object] | None = None
    for row in rows:
        raw = _json_object(row.get("conversation_audio_json"))
        if not raw:
            continue
        conversation_audio = dict(raw)
        source_id = str(row.get("id") or "")
        source_key = None
        explicit = raw.get("storage_key")
        if isinstance(explicit, str) and explicit:
            for bucket in (target_bucket, recording_bucket):
                if (
                    bucket is not None
                    and callable(getattr(bucket, "head", None))
                    and await bucket.head(explicit) is not None
                ):
                    source_key = explicit
                    source_bucket = bucket
                    break
        if source_key is None:
            for candidate in (
                f"merged/{uid}/{source_id}/conversation.wav",
                f"playback/{uid}/{source_id}/conversation.wav",
            ):
                if await target_bucket.head(candidate) is not None:
                    source_key = candidate
                    source_bucket = target_bucket
                    break
        if source_key is not None:
            target_key = f"merged/{uid}/{result_id}/conversation.wav"
            await _copy_r2_object(
                source_bucket, target_bucket, source_key, target_key, str(raw.get("content_type") or "audio/wav")
            )
            conversation_audio["storage_key"] = target_key
            conversation_audio["provider"] = "cloudflare-r2"
        elif explicit:
            raise RuntimeError("merged conversation audio object missing")
        conversation_audio["conversation_id"] = result_id
        break
    return copied_files, conversation_audio


async def _source_cleanup(env: object, uid: str, ids: list[str]) -> tuple[list[object], list[dict[str, object]]]:
    source_json = _dump(ids)
    action_result = (
        await env.APP_DB.prepare(
            "SELECT id FROM cf_action_items WHERE uid = ? AND conversation_id IN (SELECT value FROM json_each(?))"
        )
        .bind(uid, source_json)
        .all()
    )
    memory_result = (
        await env.APP_DB.prepare(
            "SELECT id FROM cf_memories WHERE uid = ? AND conversation_id IN (SELECT value FROM json_each(?))"
        )
        .bind(uid, source_json)
        .all()
    )
    action_ids = [
        str(row["id"])
        for row in (action_result.get("results", []) if isinstance(action_result, dict) else [])
        if isinstance(row, dict) and row.get("id")
    ]
    memory_ids = [
        str(row["id"])
        for row in (memory_result.get("results", []) if isinstance(memory_result, dict) else [])
        if isinstance(row, dict) and row.get("id")
    ]
    projections = [{"row_kind": "conversation", "id": item, "operation": "delete"} for item in ids]
    projections.extend({"row_kind": "action_item", "id": item, "operation": "delete"} for item in action_ids)
    projections.extend({"row_kind": "memory", "id": item, "operation": "delete"} for item in memory_ids)
    cleanup: list[object] = [
        env.APP_DB.prepare(
            "DELETE FROM cf_usage_sources WHERE uid = ? AND ((source_kind = 'conversation' AND source_id IN (SELECT value FROM json_each(?))) OR (source_kind = 'memory' AND source_id IN (SELECT value FROM json_each(?))))"
        ).bind(uid, source_json, _dump(memory_ids)),
        env.APP_DB.prepare(
            "DELETE FROM cf_action_items WHERE uid = ? AND conversation_id IN (SELECT value FROM json_each(?))"
        ).bind(uid, source_json),
        env.APP_DB.prepare(
            "DELETE FROM cf_memories WHERE uid = ? AND conversation_id IN (SELECT value FROM json_each(?))"
        ).bind(uid, source_json),
        env.APP_DB.prepare(
            "DELETE FROM cf_conversations WHERE uid = ? AND id IN (SELECT value FROM json_each(?))"
        ).bind(uid, source_json),
    ]
    return cleanup, projections


@router.post(PROCESSOR_PATH)
async def process_conversation_merge(request: Request):
    context = _auth_context(request)
    if not context or context.get("authority") != "internal":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await _bounded_json(request)
    except (json.JSONDecodeError, TypeError, ValueError):
        return JSONResponse({"error": "invalid merge payload"}, status_code=400)
    uid = str(context.get("uid") or "")
    job_id = body.get("job_id")
    ids = _conversation_ids(body.get("conversation_ids"))
    revision = body.get("revision")
    reprocess = body.get("reprocess")
    if not isinstance(job_id, str) or not job_id or len(job_id) > 128 or ids is None or not isinstance(reprocess, bool):
        return JSONResponse({"error": "invalid merge payload"}, status_code=400)
    try:
        expected_revision = int(revision)
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid merge revision"}, status_code=400)
    env = request.scope["env"]
    job = (
        await env.APP_DB.prepare(
            "SELECT uid, job_id, source_conversation_ids_json, result_conversation_id, merge_revision, reprocess, status, attempts "
            "FROM cf_conversation_merge_jobs WHERE uid = ? AND job_id = ?"
        )
        .bind(uid, job_id)
        .first()
    )
    if not isinstance(job, dict):
        return JSONResponse({"error": "merge job not found"}, status_code=404)
    stored_ids = _conversation_ids(
        _json_list(job.get("source_conversation_ids_json"), maximum=MAX_SOURCE_CONVERSATIONS)
    )
    if (
        stored_ids != ids
        or int(job.get("merge_revision") or -1) != expected_revision
        or bool(job.get("reprocess")) != reprocess
    ):
        return JSONResponse({"error": "merge payload mismatch"}, status_code=409)
    if str(job.get("status")) == "completed":
        return _job_response(job)
    result_id = str(job.get("result_conversation_id") or "")
    existing_target = (
        await env.APP_DB.prepare("SELECT id, status FROM cf_conversations WHERE uid = ? AND id = ?")
        .bind(uid, result_id)
        .first()
    )
    if isinstance(existing_target, dict) and str(existing_target.get("status")) == "completed":
        now = int(time.time())
        await env.APP_DB.prepare(
            "UPDATE cf_conversation_merge_jobs SET status = 'completed', lease_until = NULL, last_error = NULL, result_json = ?, updated_at = ? WHERE uid = ? AND job_id = ?"
        ).bind(_dump({"id": result_id, "status": "completed"}), now, uid, job_id).run()
        return {"status": "completed", "result_conversation_id": result_id}

    rows = await _load_rows(env, uid, ids)
    by_id = {str(row.get("id")): row for row in rows}
    if len(rows) != len(ids) or any(
        str(by_id[item].get("merge_job_id") or "") != job_id for item in ids if item in by_id
    ):
        return JSONResponse({"error": "merge source ownership changed"}, status_code=409)
    ordered = _source_order([by_id[item] for item in ids])
    merged_segments = _merge_segments(ordered)
    if not merged_segments:
        return JSONResponse({"error": "conversation transcript is empty or invalid"}, status_code=422)
    try:
        audio_files, conversation_audio = await _copy_audio_metadata(env, uid, ordered, result_id)
        app_targets, developer_webhook_url = await _fanout_targets(env, uid)
        started = _epoch(ordered[0], "started_at", _epoch(ordered[0], "created_at"))
        finished = max(_epoch(row, "finished_at", _epoch(row, "started_at")) for row in ordered)
        segment_end = max(float(item.get("end") or 0) for item in merged_segments)
        finished = max(finished, started + math.ceil(segment_end))
        language = str(ordered[0].get("language") or "en")
        source = str(ordered[0].get("source") or "unknown")
        discarded = all(int(row.get("discarded") or 0) == 1 for row in ordered)
        external_data = {
            "merge_metadata": {
                "merged_at": datetime.now(timezone.utc).isoformat(),
                "source_conversation_ids": ids,
                "source_details": [
                    {
                        "id": str(row.get("id")),
                        "started_at": _epoch(row, "started_at"),
                        "finished_at": _epoch(row, "finished_at"),
                        "source": str(row.get("source") or "unknown"),
                    }
                    for row in ordered
                ],
            }
        }
        structured: dict[str, object] = {}
        memory_contents: list[str] = []
        if reprocess:
            transcript = "\n".join(f"{item.get('speaker') or 'SPEAKER_00'}: {item['text']}" for item in merged_segments)
            enrichment = await _enrichment(env, transcript, language)
            if not isinstance(enrichment, dict):
                return JSONResponse({"error": "conversation processing unavailable"}, status_code=502)
            structured = enrichment.get("structured") if isinstance(enrichment.get("structured"), dict) else {}
            memory_contents = enrichment.get("memories") if isinstance(enrichment.get("memories"), list) else []
            discarded = bool(enrichment.get("discarded"))
        device, platform = _shared_provenance(ordered)
        payload = _conversation_payload(
            conversation_id=result_id,
            created=min(_epoch(row, "created_at") for row in ordered),
            started=started,
            finished=finished,
            source=source,
            language=language,
            structured=structured,
            segments=merged_segments,
            discarded=discarded,
            geolocation=_json_object(ordered[0].get("geolocation_json")) or None,
            external_data=external_data,
            client_device_id=device,
            client_platform=platform,
            visibility=_visibility(ordered),
            private_cloud_sync_enabled=any(int(row.get("private_cloud_sync_enabled") or 0) == 1 for row in ordered),
            photos=_photos(ordered),
            audio_files=audio_files,
            conversation_audio=conversation_audio,
        )
        payload["_memory_contents"] = memory_contents
        cleanup, projections = await _source_cleanup(env, uid, ids)
        await _persist_completed(
            env,
            uid=uid,
            payload=payload,
            app_targets=app_targets,
            developer_webhook_url=developer_webhook_url,
            now=int(time.time()),
            extra_cleanup_statements=cleanup,
            extra_projection_rows=projections,
        )
    except Exception:
        return JSONResponse({"error": "conversation merge unavailable"}, status_code=503)
    now = int(time.time())
    result = {"id": result_id, "status": "completed", "source_conversation_ids": ids}
    try:
        await env.APP_DB.prepare(
            "UPDATE cf_conversation_merge_jobs SET status = 'completed', lease_until = NULL, last_error = NULL, result_json = ?, updated_at = ? WHERE uid = ? AND job_id = ?"
        ).bind(_dump(result), now, uid, job_id).run()
    except Exception:
        return JSONResponse({"error": "conversation merge state unavailable"}, status_code=503)
    return result


__all__ = ["router", "merge_conversations", "process_conversation_merge"]
