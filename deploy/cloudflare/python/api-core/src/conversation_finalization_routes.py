"""D1/Queue-owned explicit conversation finalization.

The public admission and status routes run behind the Better Auth Edge.  Jobs
owns the lease and retry loop, then calls the private processor below through a
signed service-binding assertion.  The processor reuses the Worker-native
enrichment/persistence contract used by ``from-segments`` so a terminal write
updates the conversation, action items, memories, usage, vectors, and webhook
outboxes atomically.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from conversation_routes import _response as conversation_response
from developer_conversation_create_routes import (
    _conversation_payload,
    _enrichment,
    _fanout_targets,
    _meeting_eligible,
    _persist_completed,
)
from internal_auth import decode_context

router = APIRouter()

MAX_CONVERSATION_ID_LENGTH = 256
MAX_REQUEST_BYTES = 32_000
MAX_SEGMENTS = 2_000
MAX_SEGMENT_TEXT_LENGTH = 100_000
MAX_TRANSCRIPT_TEXT_LENGTH = 500_000
MAX_FINALIZATION_ATTEMPTS = 3
PROCESSOR_PATH = "/internal/conversations/finalize"
SUPPORTED_OPERATIONS = frozenset({"finalize", "reprocess"})


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _json_object(value: object) -> dict[str, object]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_segments(value: object) -> list[dict[str, object]] | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, list) or len(parsed) > MAX_SEGMENTS:
        return None
    segments: list[dict[str, object]] = []
    total_text = 0
    for item in parsed:
        if not isinstance(item, dict):
            return None
        text = item.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_SEGMENT_TEXT_LENGTH:
            return None
        try:
            start = float(item.get("start", 0))
            end = float(item.get("end", 0))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            return None
        total_text += len(text)
        if total_text > MAX_TRANSCRIPT_TEXT_LENGTH:
            return None
        normalized = dict(item)
        normalized["text"] = text
        normalized["start"] = start
        normalized["end"] = end
        normalized.setdefault("speaker", "SPEAKER_00")
        segments.append(normalized)
    return segments


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _conversation_revision(row: dict[str, object]) -> int:
    return int(row.get("updated_at") or row.get("created_at") or 0)


def _job_response(row: dict[str, object], *, meeting_treatment_eligible: bool = False) -> dict[str, object]:
    status = str(row.get("status") or "queued")
    attempts = max(0, int(row.get("attempts") or 0))
    terminal = status in {"completed", "failed"}
    return {
        "job_id": str(row.get("job_id") or ""),
        "operation": str(row.get("operation") or "finalize"),
        "status": status,
        "terminal": terminal,
        "retryable": not terminal or (status == "failed" and attempts < MAX_FINALIZATION_ATTEMPTS),
        "attempt_count": attempts,
        "task_retry_count": attempts,
        "meeting_treatment_eligible": meeting_treatment_eligible,
    }


async def _bounded_json(request: Request) -> dict[str, object]:
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("request body too large")
    if not raw.strip():
        return {}
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise ValueError("request body must be an object")
    return decoded


async def _enqueue(
    request: Request,
    *,
    uid: str,
    job_id: str,
    conversation_id: str,
    revision: int,
    operation: str = "finalize",
    language_code: str | None = None,
    app_id: str | None = None,
) -> bool:
    queue = getattr(request.scope["env"], "JOBS", None)
    if queue is None:
        return False
    try:
        await queue.send(
            {
                "jobId": job_id,
                "uid": uid,
                "kind": "conversation_reprocess" if operation == "reprocess" else "conversation_finalize",
                "payload": {
                    "conversationId": conversation_id,
                    "revision": revision,
                    **({"languageCode": language_code} if language_code else {}),
                    **({"appId": app_id} if app_id else {}),
                },
            }
        )
    except Exception:
        return False
    return True


async def _conversation_row(env: object, uid: str, conversation_id: str) -> dict[str, object] | None:
    row = (
        await env.APP_DB.prepare(
            "SELECT uid, id, created_at, updated_at, started_at, finished_at, source, language, status, visibility, "
            "starred, discarded, is_locked, deferred, private_cloud_sync_enabled, folder_id, client_device_id, "
            "client_platform, structured_json, transcript_segments_json, photos_json, audio_files_json, "
            "conversation_audio_json, apps_results_json, suggested_apps_json, geolocation_json, external_data_json, "
            "calendar_event_json, app_id, finalization_job_id, finalization_revision, finalization_status "
            "FROM cf_conversations WHERE uid = ? AND id = ?"
        )
        .bind(uid, conversation_id)
        .first()
    )
    return row if isinstance(row, dict) else None


async def _latest_in_progress_conversation(env: object, uid: str) -> dict[str, object] | None:
    row = (
        await env.APP_DB.prepare(
            "SELECT uid, id, created_at, updated_at, started_at, finished_at, source, language, status, visibility, "
            "starred, discarded, is_locked, deferred, private_cloud_sync_enabled, folder_id, client_device_id, "
            "client_platform, structured_json, transcript_segments_json, photos_json, audio_files_json, "
            "conversation_audio_json, apps_results_json, suggested_apps_json, geolocation_json, external_data_json, "
            "calendar_event_json, app_id, finalization_job_id, finalization_revision, finalization_status "
            "FROM cf_conversations WHERE uid = ? AND status = 'in_progress' "
            "ORDER BY COALESCE(updated_at, created_at) DESC, id DESC LIMIT 1"
        )
        .bind(uid)
        .first()
    )
    return row if isinstance(row, dict) else None


async def _complete_without_processing(
    env: object,
    uid: str,
    conversation_id: str,
    job_id: str,
    now: int,
) -> dict[str, object] | JSONResponse:
    row = (
        await env.APP_DB.prepare(
            "UPDATE cf_conversation_finalization_jobs SET status = 'completed', lease_until = NULL, "
            "last_error = NULL, updated_at = ? WHERE uid = ? AND job_id = ? "
            "AND status IN ('queued', 'running') RETURNING job_id, status, attempts"
        )
        .bind(now, uid, job_id)
        .first()
    )
    if not isinstance(row, dict):
        row = (
            await env.APP_DB.prepare(
                "SELECT job_id, status, attempts FROM cf_conversation_finalization_jobs WHERE uid = ? AND job_id = ?"
            )
            .bind(uid, job_id)
            .first()
        )
    if not isinstance(row, dict):
        return JSONResponse({"error": "finalization job not found"}, status_code=404)
    await env.APP_DB.prepare(
        "UPDATE cf_conversations SET finalization_status = 'completed' "
        "WHERE uid = ? AND id = ? AND finalization_job_id = ?"
    ).bind(uid, conversation_id, job_id).run()
    return _job_response(row)


@router.post(PROCESSOR_PATH)
async def process_conversation_finalization(request: Request):
    context = _auth_context(request)
    if not context or context.get("authority") != "internal":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await _bounded_json(request)
    except (json.JSONDecodeError, TypeError, ValueError):
        return JSONResponse({"error": "invalid finalization payload"}, status_code=400)
    uid = str(context.get("uid") or "")
    job_id = body.get("job_id")
    conversation_id = body.get("conversation_id")
    revision = body.get("revision")
    operation = body.get("operation") or "finalize"
    if not isinstance(operation, str) or operation not in SUPPORTED_OPERATIONS:
        return JSONResponse({"error": "invalid conversation operation"}, status_code=400)
    language_code = body.get("language_code")
    app_id = body.get("app_id")
    if language_code is not None and (not isinstance(language_code, str) or len(language_code) > 32):
        return JSONResponse({"error": "invalid reprocess language"}, status_code=400)
    if app_id is not None and (not isinstance(app_id, str) or len(app_id) > 256):
        return JSONResponse({"error": "invalid reprocess app"}, status_code=400)
    if (
        not uid
        or not isinstance(job_id, str)
        or not job_id
        or len(job_id) > 128
        or not isinstance(conversation_id, str)
        or not conversation_id
        or len(conversation_id) > MAX_CONVERSATION_ID_LENGTH
    ):
        return JSONResponse({"error": "invalid finalization payload"}, status_code=400)
    try:
        expected_revision = int(revision)
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid finalization revision"}, status_code=400)
    env = request.scope["env"]
    job = (
        await env.APP_DB.prepare(
            "SELECT job_id, uid, conversation_id, finalization_revision, operation, status, attempts "
            "FROM cf_conversation_finalization_jobs WHERE uid = ? AND job_id = ?"
        )
        .bind(uid, job_id)
        .first()
    )
    if not isinstance(job, dict) or str(job.get("conversation_id")) != conversation_id:
        return JSONResponse({"error": "finalization job not found"}, status_code=404)
    if str(job.get("operation") or "finalize") != operation:
        return JSONResponse({"error": "conversation operation mismatch"}, status_code=409)
    if int(job.get("finalization_revision") or -1) != expected_revision:
        return JSONResponse({"error": "finalization revision mismatch"}, status_code=409)
    if str(job.get("status")) == "completed":
        return _job_response(job)

    conversation = await _conversation_row(env, uid, conversation_id)
    if conversation is None:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    if str(conversation.get("status")) == "completed" and operation == "finalize":
        return await _complete_without_processing(env, uid, conversation_id, job_id, int(time.time()))
    if str(conversation.get("status")) != "processing":
        return JSONResponse({"error": "conversation is not being processed"}, status_code=409)
    if str(conversation.get("finalization_job_id") or "") != job_id:
        return JSONResponse({"error": "finalization ownership changed"}, status_code=409)

    segments = _json_segments(conversation.get("transcript_segments_json"))
    if not segments:
        return JSONResponse({"error": "conversation transcript is empty or invalid"}, status_code=422)
    claimed_external_data = _json_object(conversation.get("external_data_json"))
    external_data = claimed_external_data
    if operation == "reprocess":
        marker = claimed_external_data.get("_cf_reprocess")
        if not isinstance(marker, dict):
            return JSONResponse({"error": "reprocess claim is invalid"}, status_code=409)
        original = marker.get("external_data")
        external_data = original if isinstance(original, dict) else {}
        if language_code is None:
            language_code = marker.get("language_code") if isinstance(marker.get("language_code"), str) else None
        if app_id is None:
            app_id = marker.get("app_id") if isinstance(marker.get("app_id"), str) else None
    started = int(conversation.get("started_at") or conversation.get("created_at") or 0)
    last_end = max(float(segment.get("end") or 0) for segment in segments)
    finished = int(conversation.get("finished_at") or (started + math.ceil(last_end)))
    if finished <= started:
        finished = started + max(1, math.ceil(last_end))
    source = str(conversation.get("source") or "unknown")
    language = str(language_code or conversation.get("language") or "en")
    transcript = "\n".join(f"{segment.get('speaker') or 'SPEAKER_00'}: {segment['text']}" for segment in segments)
    try:
        app_targets, developer_webhook_url = await _fanout_targets(env, uid)
        if app_id:
            app_targets = [(target_id, url) for target_id, url in app_targets if target_id == app_id]
        enrichment = await _enrichment(env, transcript, language)
        if not isinstance(enrichment, dict):
            return JSONResponse({"error": "conversation processing unavailable"}, status_code=502)
        payload = _conversation_payload(
            conversation_id=conversation_id,
            created=int(conversation.get("created_at") or started),
            started=started,
            finished=finished,
            source=source,
            language=language,
            structured=enrichment.get("structured") if isinstance(enrichment.get("structured"), dict) else {},
            segments=segments,
            discarded=bool(enrichment.get("discarded")),
            geolocation=_json_object(conversation.get("geolocation_json")) or None,
            external_data=external_data,
            client_device_id=(
                str(conversation["client_device_id"]) if conversation.get("client_device_id") is not None else None
            ),
            client_platform=(
                str(conversation["client_platform"]) if conversation.get("client_platform") is not None else None
            ),
        )
        payload["_memory_contents"] = enrichment.get("memories", [])
        await _persist_completed(
            env,
            uid=uid,
            payload=payload,
            app_targets=app_targets,
            developer_webhook_url=developer_webhook_url,
            now=int(time.time()),
            claim_json=(
                str(conversation["external_data_json"])
                if isinstance(conversation.get("external_data_json"), str)
                else None
            ),
            replace_derived=operation == "reprocess",
        )
    except Exception:
        return JSONResponse({"error": "conversation finalization unavailable"}, status_code=503)

    now = int(time.time())
    eligible = _meeting_eligible(
        source=source,
        role=str(external_data.get("conversation_role") or "ambient"),
        finalization_reason=(
            str(external_data["conversation_finalization_reason"])
            if external_data.get("conversation_finalization_reason") is not None
            else None
        ),
        discarded=bool(payload.get("discarded")),
        started=started,
        finished=finished,
        segments=segments,
    )
    result = {
        "id": conversation_id,
        "status": "completed",
        "discarded": bool(payload.get("discarded")),
        "operation": operation,
    }
    try:
        await env.APP_DB.batch(
            [
                env.APP_DB.prepare(
                    "UPDATE cf_conversation_finalization_jobs SET status = 'completed', lease_until = NULL, "
                    "last_error = NULL, result_json = ?, updated_at = ? WHERE uid = ? AND job_id = ?"
                ).bind(_dump(result), now, uid, job_id),
                env.APP_DB.prepare(
                    "UPDATE cf_conversations SET finalization_status = 'completed' "
                    "WHERE uid = ? AND id = ? AND finalization_job_id = ?"
                ).bind(uid, conversation_id, job_id),
            ]
        )
    except Exception:
        return JSONResponse({"error": "conversation finalization state unavailable"}, status_code=503)
    return {**result, "meeting_treatment_eligible": eligible}


@router.post("/v1/conversations")
async def process_in_progress_conversation(request: Request):
    """Finalize the uid's newest D1 in-progress conversation.

    This is the Cloudflare equivalent of the legacy Redis pointer endpoint.
    The D1 status is authoritative, so a stale client cannot finalize a newer
    recording and a missing pointer fails closed with the historical 404.
    """
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    uid = str(context.get("uid") or "")
    try:
        conversation = await _latest_in_progress_conversation(request.scope["env"], uid)
    except Exception:
        return JSONResponse({"error": "conversation finalization unavailable"}, status_code=503)
    if conversation is None:
        return JSONResponse({"error": "Conversation in progress not found"}, status_code=404)
    return await finalize_conversation(request, str(conversation["id"]))


@router.post("/v1/conversations/{conversation_id}/finalize")
async def finalize_conversation(request: Request, conversation_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_CONVERSATION_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    if context.get("byokActive") is True:
        return JSONResponse(
            {"detail": "BYOK finalization is not supported on this route; use the live listen session"},
            status_code=409,
        )
    try:
        body = await _bounded_json(request)
    except (json.JSONDecodeError, TypeError, ValueError):
        return JSONResponse({"error": "invalid finalization request"}, status_code=400)
    uid = str(context.get("uid") or "")
    env = request.scope["env"]
    conversation = await _conversation_row(env, uid, conversation_id)
    if conversation is None:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    if int(conversation.get("is_locked") or 0):
        return JSONResponse({"error": "A paid plan is required to access this conversation."}, status_code=402)
    current_status = str(conversation.get("status") or "")
    if current_status != "in_progress":
        job_id = conversation.get("finalization_job_id")
        if isinstance(job_id, str) and job_id:
            job = (
                await env.APP_DB.prepare(
                    "SELECT job_id, status, attempts FROM cf_conversation_finalization_jobs WHERE uid = ? AND job_id = ?"
                )
                .bind(uid, job_id)
                .first()
            )
            if isinstance(job, dict):
                return {"conversation": conversation_response(conversation, detail=True), "messages": []}
        return {"conversation": conversation_response(conversation, detail=True), "messages": []}

    external_data = _json_object(conversation.get("external_data_json"))
    meeting_context = body.get("calendar_meeting_context")
    if meeting_context is not None:
        if not isinstance(meeting_context, dict) or len(_dump(meeting_context).encode("utf-8")) > 16_000:
            return JSONResponse({"error": "invalid calendar meeting context"}, status_code=422)
        external_data["calendar_meeting_context"] = meeting_context
    claim_json = _dump(external_data)
    revision = _conversation_revision(conversation)
    job_id = (
        "conversation-finalize-" + hashlib.sha256(f"{uid}\0{conversation_id}\0{revision}".encode()).hexdigest()[:48]
    )
    now = int(time.time())
    try:
        admission = await env.APP_DB.batch(
            [
                env.APP_DB.prepare(
                    "INSERT INTO cf_conversation_finalization_jobs "
                    "(uid, conversation_id, job_id, finalization_revision, status, attempts, next_attempt_at, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, ?) ON CONFLICT DO NOTHING"
                ).bind(uid, conversation_id, job_id, revision, now, now, now),
                env.APP_DB.prepare(
                    "UPDATE cf_conversations SET status = 'processing', finalization_job_id = ?, "
                    "finalization_revision = ?, finalization_status = 'queued', external_data_json = ? "
                    "WHERE uid = ? AND id = ? AND status = 'in_progress' "
                    "AND COALESCE(updated_at, created_at) = ?"
                ).bind(job_id, revision, claim_json, uid, conversation_id, revision),
            ]
        )
    except Exception:
        return JSONResponse({"error": "conversation finalization unavailable"}, status_code=503)
    update_result = admission[1] if isinstance(admission, list) and len(admission) > 1 else None
    update_changes = update_result.get("meta", {}).get("changes") if isinstance(update_result, dict) else None
    if int(update_changes or 0) != 1:
        updated = await _conversation_row(env, uid, conversation_id)
        if updated is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        if str(updated.get("finalization_job_id") or "") != job_id:
            try:
                await env.APP_DB.prepare(
                    "DELETE FROM cf_conversation_finalization_jobs WHERE uid = ? AND job_id = ? AND status = 'queued'"
                ).bind(uid, job_id).run()
            except Exception:
                pass
            return {"conversation": conversation_response(updated, detail=True), "messages": []}
    if not await _enqueue(request, uid=uid, job_id=job_id, conversation_id=conversation_id, revision=revision):
        return JSONResponse({"error": "conversation finalization queue unavailable"}, status_code=503)
    updated = await _conversation_row(env, uid, conversation_id)
    if updated is None:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    return {"conversation": conversation_response(updated, detail=True), "messages": []}


def _reprocess_revision(
    row: dict[str, object], uid: str, conversation_id: str, language: str, app_id: str | None
) -> int:
    base = max(0, _conversation_revision(row))
    fingerprint = hashlib.sha256(f"{uid}\0{conversation_id}\0{language}\0{app_id or ''}".encode()).hexdigest()
    return base * 1_000_000 + int(fingerprint[:8], 16)


@router.post("/v1/conversations/{conversation_id}/reprocess")
async def reprocess_conversation(request: Request, conversation_id: str):
    """Admit an idempotent D1/Queue reprocess job.

    Reprocessing intentionally revives discarded conversations, but a missing D1
    row is a hard 404 and therefore cannot resurrect a deleted/tombstoned record.
    The response keeps the conversation wire shape; callers can poll the shared
    ``/finalization`` status endpoint for the asynchronous terminal result.
    """
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_CONVERSATION_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    query = getattr(request, "query_params", {})
    language_code = query.get("language_code") if hasattr(query, "get") else None
    app_id = query.get("app_id") if hasattr(query, "get") else None
    if language_code is not None and (not isinstance(language_code, str) or len(language_code) > 32):
        return JSONResponse({"error": "invalid reprocess language"}, status_code=400)
    if app_id is not None and (not isinstance(app_id, str) or len(app_id) > 256):
        return JSONResponse({"error": "invalid reprocess app"}, status_code=400)
    language = (language_code or "").strip() or None
    selected_app = (app_id or "").strip() or None
    uid = str(context.get("uid") or "")
    env = request.scope["env"]
    conversation = await _conversation_row(env, uid, conversation_id)
    if conversation is None:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    if int(conversation.get("is_locked") or 0):
        return JSONResponse({"error": "A paid plan is required to access this conversation."}, status_code=402)
    current_status = str(conversation.get("status") or "")
    existing_job_id = str(conversation.get("finalization_job_id") or "")
    if current_status == "processing":
        if existing_job_id:
            existing = (
                await env.APP_DB.prepare(
                    "SELECT job_id, operation, status, attempts FROM cf_conversation_finalization_jobs "
                    "WHERE uid = ? AND job_id = ?"
                )
                .bind(uid, existing_job_id)
                .first()
            )
            if isinstance(existing, dict) and str(existing.get("operation") or "finalize") == "reprocess":
                return conversation_response(conversation, detail=True)
        return JSONResponse({"error": "conversation is already being processed"}, status_code=409)
    effective_language = language or str(conversation.get("language") or "en")
    revision = _reprocess_revision(conversation, uid, conversation_id, effective_language, selected_app)
    job_id = (
        "conversation-reprocess-" + hashlib.sha256(f"{uid}\0{conversation_id}\0{revision}".encode()).hexdigest()[:48]
    )
    original_external_data = _json_object(conversation.get("external_data_json"))
    claim_json = _dump(
        {
            "_cf_reprocess": {
                "external_data": original_external_data,
                "language_code": effective_language,
                "app_id": selected_app,
            }
        }
    )
    now = int(time.time())
    try:
        admission = await env.APP_DB.batch(
            [
                env.APP_DB.prepare(
                    "INSERT INTO cf_conversation_finalization_jobs "
                    "(uid, conversation_id, job_id, finalization_revision, operation, language_code, app_id, status, "
                    "attempts, next_attempt_at, created_at, updated_at) VALUES (?, ?, ?, ?, 'reprocess', ?, ?, 'queued', "
                    "0, ?, ?, ?) ON CONFLICT DO NOTHING"
                ).bind(uid, conversation_id, job_id, revision, effective_language, selected_app, now, now, now),
                env.APP_DB.prepare(
                    "UPDATE cf_conversations SET status = 'processing', finalization_job_id = ?, "
                    "finalization_revision = ?, finalization_status = 'queued', external_data_json = ? "
                    "WHERE uid = ? AND id = ? AND status <> 'processing' "
                    "AND COALESCE(updated_at, created_at) = ?"
                ).bind(job_id, revision, claim_json, uid, conversation_id, _conversation_revision(conversation)),
            ]
        )
    except Exception:
        return JSONResponse({"error": "conversation reprocess unavailable"}, status_code=503)
    update_result = admission[1] if isinstance(admission, list) and len(admission) > 1 else None
    update_changes = update_result.get("meta", {}).get("changes") if isinstance(update_result, dict) else None
    if int(update_changes or 0) != 1:
        updated = await _conversation_row(env, uid, conversation_id)
        if updated is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        if str(updated.get("finalization_job_id") or "") != job_id:
            try:
                await env.APP_DB.prepare(
                    "DELETE FROM cf_conversation_finalization_jobs WHERE uid = ? AND job_id = ? AND status = 'queued'"
                ).bind(uid, job_id).run()
            except Exception:
                pass
            return conversation_response(updated, detail=True)
    if not await _enqueue(
        request,
        uid=uid,
        job_id=job_id,
        conversation_id=conversation_id,
        revision=revision,
        operation="reprocess",
        language_code=effective_language,
        app_id=selected_app,
    ):
        return JSONResponse({"error": "conversation reprocess queue unavailable"}, status_code=503)
    updated = await _conversation_row(env, uid, conversation_id)
    if updated is None:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    return conversation_response(updated, detail=True)


@router.get("/v1/conversations/{conversation_id}/finalization")
async def get_conversation_finalization_status(request: Request, conversation_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not conversation_id or len(conversation_id) > MAX_CONVERSATION_ID_LENGTH:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    env = request.scope["env"]
    conversation = await _conversation_row(env, str(context["uid"]), conversation_id)
    if conversation is None:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    job = (
        await env.APP_DB.prepare(
            "SELECT job_id, operation, status, attempts FROM cf_conversation_finalization_jobs "
            "WHERE uid = ? AND conversation_id = ? ORDER BY finalization_revision DESC LIMIT 1"
        )
        .bind(str(context["uid"]), conversation_id)
        .first()
    )
    if not isinstance(job, dict):
        return JSONResponse({"detail": "Conversation finalization job not found"}, status_code=404)
    eligible = False
    if str(job.get("status")) == "completed":
        try:
            from developer_conversation_create_routes import _meeting_eligible_from_row

            eligible = _meeting_eligible_from_row(conversation)
        except Exception:
            eligible = False
    return _job_response(job, meeting_treatment_eligible=eligible)


__all__ = [
    "router",
    "finalize_conversation",
    "process_in_progress_conversation",
    "reprocess_conversation",
    "get_conversation_finalization_status",
    "process_conversation_finalization",
]
