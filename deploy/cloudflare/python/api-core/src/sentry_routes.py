"""Cloudflare-native Sentry feedback ingestion.

Sentry feedback is an internal webhook/polling integration.  The legacy route
stored feedback as Firestore action items; this worker keeps the same external
envelopes while writing the canonical D1 action-item projection.  Delivery is
idempotent on the Sentry issue id so webhook retries cannot create duplicate
tasks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import uuid
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

try:
    from workers import fetch as worker_fetch
except ModuleNotFoundError as error:  # CPython tests do not provide Pyodide's js module.
    if error.name != "js":
        raise
    worker_fetch = None  # type: ignore[assignment]


router = APIRouter()

MAX_BODY_BYTES = 512_000
MAX_ISSUE_ID_LENGTH = 256
MAX_TEXT_LENGTH = 4_096
MAX_METADATA_BYTES = 60_000
SENTRY_ISSUES_URL = (
    "https://sentry.io/api/0/organizations/mediar-n5/issues/" "?query=issue.category:feedback&limit=25&sort=date"
)


def _bounded_text(value: object, maximum: int = MAX_TEXT_LENGTH) -> str:
    return value.strip()[:maximum] if isinstance(value, str) else ""


def _signature_matches(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature or not re.fullmatch(r"[0-9a-fA-F]{64}", signature):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _request_body(request: Request) -> bytes:
    body_reader = getattr(request, "body", None)
    if callable(body_reader):
        body = await body_reader()
    else:
        body = body_reader
        if body is None:
            body = await request.body()
    if not isinstance(body, bytes) or len(body) > MAX_BODY_BYTES:
        raise ValueError("request body too large")
    return body


def _object(value: object) -> dict[str, object] | None:
    return value if isinstance(value, dict) else None


def _issue_id(issue: dict[str, object]) -> str | None:
    issue_id = _bounded_text(issue.get("id"), MAX_ISSUE_ID_LENGTH)
    return issue_id or None


def _existing_issue_ids(rows: object) -> set[str]:
    if not isinstance(rows, dict):
        return set()
    result = rows.get("results")
    if not isinstance(result, list):
        return set()
    issue_ids: set[str] = set()
    for row in result:
        if not isinstance(row, dict):
            continue
        key = row.get("idempotency_key")
        if isinstance(key, str) and key.startswith("sentry-feedback:"):
            issue_ids.add(key.removeprefix("sentry-feedback:"))
            continue
        provenance = row.get("provenance_json")
        if not isinstance(provenance, str):
            continue
        try:
            parsed = json.loads(provenance)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, list):
            continue
        for entry in parsed:
            if isinstance(entry, dict) and isinstance(entry.get("sentry_issue_id"), str):
                issue_ids.add(entry["sentry_issue_id"])
    return issue_ids


async def _existing_issue_ids_for_uid(env: object, uid: str) -> set[str]:
    rows = (
        await env.APP_DB.prepare(
            "SELECT idempotency_key, provenance_json FROM cf_action_items "
            "WHERE uid = ? AND source = 'sentry_feedback' AND deleted = 0 LIMIT 500"
        )
        .bind(uid)
        .all()
    )
    return _existing_issue_ids(rows)


async def _fetch_json(url: str, headers: dict[str, str]) -> tuple[int, object | None]:
    if worker_fetch is None:
        return 0, None
    try:
        response = await worker_fetch(url, method="GET", headers=headers)
        status = int(response.status)
        if hasattr(response, "json"):
            payload = await response.json()
        else:
            raw = await response.arrayBuffer()
            payload = json.loads(bytes(raw).decode("utf-8"))
        return status, payload
    except Exception:
        return 0, None


async def _event_details(issue_id: str, token: str) -> tuple[str, str, str, dict[str, object]]:
    status, payload = await _fetch_json(
        f"https://sentry.io/api/0/issues/{issue_id}/events/latest/",
        {"Authorization": f"Bearer {token}"},
    )
    if status < 200 or status >= 300:
        return "", "", "", {}
    event = _object(payload)
    if event is None:
        return "", "", "", {}
    contexts = _object(event.get("contexts")) or _object(event.get("context")) or {}
    feedback = _object(contexts.get("feedback")) or {}
    message = _bounded_text(feedback.get("message"))
    name = _bounded_text(feedback.get("name"), 256)
    email = _bounded_text(feedback.get("contact_email"), 512)
    if not email:
        user = _object(event.get("user")) or {}
        email = _bounded_text(user.get("email"), 512)
    tags = event.get("tags")
    metadata: dict[str, object] = {}
    if isinstance(tags, list):
        metadata["tags"] = [item for item in tags[:50] if isinstance(item, (str, dict))]
    encoded = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        metadata = {}
    return message, name, email, metadata


async def _create_feedback(
    env: object,
    uid: str,
    issue: dict[str, object],
    existing_ids: set[str],
    token: str | None,
) -> str:
    issue_id = _issue_id(issue)
    if issue_id is None:
        return "ignored"
    if issue_id in existing_ids:
        return "duplicate"
    short_id = _bounded_text(issue.get("shortId"), 128) or "unknown"
    title = _bounded_text(issue.get("title"))
    message = name = email = ""
    event_metadata: dict[str, object] = {}
    if token:
        message, name, email, event_metadata = await _event_details(issue_id, token)
    description = f"[Sentry Feedback] {short_id}: {message or title}".rstrip(": ")
    if not description:
        return "ignored"
    metadata: dict[str, object] = {
        "sentry_issue_id": issue_id,
        "sentry_short_id": short_id,
        "sentry_url": f"https://mediar-n5.sentry.io/issues/{issue_id}/",
        "tags": ["bug"],
    }
    if name:
        metadata["reporter_name"] = name
    if email:
        metadata["reporter_email"] = email
    metadata.update(event_metadata)
    provenance = json.dumps([metadata], ensure_ascii=False, separators=(",", ":"))
    now = int(time.time())
    idempotency_key = f"sentry-feedback:{issue_id}"
    item_id = uuid.uuid4().hex
    result = (
        await env.APP_DB.prepare(
            "INSERT INTO cf_action_items "
            "(uid, id, description, status, completed, owner, source, provenance_json, priority, "
            "sort_order, indent_level, created_at, updated_at, idempotency_key, sync_requested, deleted) "
            "SELECT ?, ?, ?, 'active', 0, 'user', 'sentry_feedback', ?, 'high', 0, 0, ?, ?, ?, 0, 0 "
            "WHERE NOT EXISTS (SELECT 1 FROM cf_action_items "
            "WHERE uid = ? AND idempotency_key = ? AND deleted = 0)"
        )
        .bind(
            uid,
            item_id,
            description,
            provenance,
            now,
            now,
            idempotency_key,
            uid,
            idempotency_key,
        )
        .run()
    )
    changes = result.get("meta", {}).get("changes", 0) if isinstance(result, dict) else 0
    if int(changes or 0) != 1:
        return "duplicate"
    existing_ids.add(issue_id)
    return "created"


def _poll_skip(reason: str, sentry_status: int | None = None) -> dict[str, object]:
    return {
        "status": "skipped",
        "reason": reason,
        "sentry_status": sentry_status,
        "created": 0,
        "skipped": 0,
        "total_fetched": 0,
    }


@router.post("/v1/webhooks/sentry")
async def sentry_webhook(request: Request):
    if request.headers.get("sentry-hook-resource") == "installation":
        return {"status": "ok"}
    secret = _bounded_text(getattr(request.scope["env"], "SENTRY_WEBHOOK_SECRET", None), 512)
    if not secret:
        return JSONResponse({"detail": "Sentry webhook is not configured"}, status_code=503)
    try:
        body = await _request_body(request)
    except (TypeError, ValueError):
        return JSONResponse({"detail": "Invalid Sentry webhook"}, status_code=400)
    if not _signature_matches(secret, body, request.headers.get("sentry-hook-signature")):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    try:
        payload = json.loads(body)
        payload_object = _object(payload)
        data = _object(payload_object.get("data")) if payload_object else None
        issue = _object(data.get("issue")) if data else None
    except (TypeError, ValueError, json.JSONDecodeError):
        issue = None
        payload_object = None
    if payload_object is None or issue is None:
        return JSONResponse({"detail": "Invalid Sentry webhook"}, status_code=400)
    if payload_object.get("action") != "created" or issue.get("issueCategory") != "feedback":
        return {"status": "ignored"}
    uid = _bounded_text(getattr(request.scope["env"], "SENTRY_ADMIN_UID", None), 256)
    if not uid:
        return JSONResponse({"detail": "Sentry admin is not configured"}, status_code=500)
    env = request.scope["env"]
    try:
        existing_ids = await _existing_issue_ids_for_uid(env, uid)
        result = await _create_feedback(
            env,
            uid,
            issue,
            existing_ids,
            _bounded_text(getattr(env, "SENTRY_AUTH_TOKEN", None), 4_096) or None,
        )
    except Exception:
        return JSONResponse({"detail": "Sentry feedback unavailable"}, status_code=503)
    return {"status": result}


@router.post("/v1/webhooks/sentry/poll")
async def sentry_poll(request: Request):
    env = request.scope["env"]
    uid = _bounded_text(getattr(env, "SENTRY_ADMIN_UID", None), 256)
    token = _bounded_text(getattr(env, "SENTRY_AUTH_TOKEN", None), 4_096)
    if not uid or not token:
        return JSONResponse({"detail": "Sentry polling is not configured"}, status_code=500)
    status, payload = await _fetch_json(SENTRY_ISSUES_URL, {"Authorization": f"Bearer {token}"})
    if status == 0:
        return _poll_skip("sentry_unreachable")
    if status < 200 or status >= 300:
        reason = (
            "sentry_auth_error"
            if status in {401, 403}
            else "sentry_rate_limited" if status == 429 else "sentry_upstream_error"
        )
        return _poll_skip(reason, status)
    if not isinstance(payload, list):
        return _poll_skip("sentry_bad_response")
    try:
        existing_ids = await _existing_issue_ids_for_uid(env, uid)
        created = 0
        skipped = 0
        for issue in payload:
            if not isinstance(issue, dict):
                skipped += 1
                continue
            result = await _create_feedback(env, uid, issue, existing_ids, token)
            if result == "created":
                created += 1
            else:
                skipped += 1
    except Exception:
        return JSONResponse({"detail": "Sentry feedback unavailable"}, status_code=503)
    return {"status": "ok", "created": created, "skipped": skipped, "total_fetched": len(payload)}


__all__ = ["router", "sentry_poll", "sentry_webhook"]
