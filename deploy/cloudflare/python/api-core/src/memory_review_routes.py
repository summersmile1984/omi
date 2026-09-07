"""D1-backed canonical memory review queue.

Every active review must match the canonical commit, item revision, content
hash and consolidation review route. Resolutions join the item/privacy owner;
historical timestamp-based projections cannot authorize a new mutation.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from memory_mutation_errors import memory_mutation_error
from pydantic import BaseModel, Field, ValidationError

from internal_auth import decode_context
from memory_apply_item import read_item
from memory_apply_mutation import apply_user_memory_mutation
from memory_privacy_apply import prepare_privacy_deletion
from memory_privacy_delete import finish_or_schedule
from memory_review_policy import review_resolution_patch
from memory_review_store import CanonicalReviewResolution, review_source_matches

router = APIRouter()

MAX_REVIEW_ID_LENGTH = 512
MAX_STATUS_LENGTH = 32
MAX_LIMIT = 500
MAX_CORRECTION_BYTES = 64_000
_ACTIVE_STATUSES = {"pending", "pending_review"}
_VALID_STATUSES = _ACTIVE_STATUSES | {"accepted", "rejected", "dropped", "tombstoned"}

_MEMORY_SOURCE_SELECT = "SELECT * FROM cf_memories "


class ReviewResolution(BaseModel):
    model_config = {"extra": "forbid"}

    decision: str
    correction: dict[str, Any] | None = None
    reason: str = Field(default="", max_length=2_000)
    current_veracity: float | None = Field(default=None, ge=0, le=1)


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _json(value: object, fallback: object) -> object:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_CORRECTION_BYTES:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _json_dict(value: object) -> dict[str, Any]:
    parsed = _json(value, {})
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: object) -> list[Any]:
    parsed = _json(value, [])
    return parsed if isinstance(parsed, list) else []


def _iso(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _decode_queue_row(row: dict[str, object]) -> dict[str, object]:
    candidate = _json_dict(row.get("candidate_json"))
    conflict_with = _json_list(row.get("conflict_with_json"))
    referenced = _json_list(row.get("referenced_memory_ids_json"))
    permitted = _json_list(row.get("permitted_uses_json"))
    item: dict[str, object] = {
        "review_id": str(row.get("review_id") or ""),
        "fact_id": str(row.get("fact_id") or ""),
        "candidate": candidate,
        "conflict_with": [str(item) for item in conflict_with if isinstance(item, str)],
        "referenced_memory_ids": [str(item) for item in referenced if isinstance(item, str)],
        "veracity": row.get("veracity"),
        "impact": row.get("impact"),
        "status": str(row.get("status") or "pending"),
        "authority": str(row.get("authority") or "canonical_memory"),
        "source_commit_id": row.get("source_commit_id"),
        "source_item_revision": row.get("source_item_revision"),
        "source_content_hash": row.get("source_content_hash"),
        "source_short_term_id": row.get("source_short_term_id"),
        "permitted_uses": [str(item) for item in permitted if isinstance(item, str)],
        "reason": row.get("reason"),
        "decision": row.get("decision"),
        "resolution_commit_id": row.get("resolution_commit_id"),
        "correction": _json(row.get("correction_json"), None),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "expires_at": _iso(row.get("expires_at")),
        "resolved_at": _iso(row.get("resolved_at")),
    }
    if row.get('source_commit_id') == '':
        item.update(source_commit_id=None, source_item_revision=None, source_content_hash=None, impact=None)
    if row.get("previous_status") is not None:
        item["previous_status"] = row.get("previous_status")
    return item


async def _raw_item(env: object, uid: str, review_id: str) -> dict[str, object] | None:
    row = (
        await env.APP_DB.prepare(
            "SELECT uid, review_id, fact_id, candidate_json, conflict_with_json, referenced_memory_ids_json, veracity, "
            "impact, status, authority, previous_status, source_commit_id, source_item_revision, source_content_hash, "
            "source_short_term_id, permitted_uses_json, reason, decision, resolution_commit_id, correction_json, "
            "created_at, updated_at, expires_at, resolved_at FROM cf_memory_review_queue WHERE uid = ? AND review_id = ?"
        )
        .bind(uid, review_id)
        .first()
    )
    return row if isinstance(row, dict) else None


async def _source_row(env: object, uid: str, memory_id: str) -> dict[str, object] | None:
    row = await env.APP_DB.prepare(_MEMORY_SOURCE_SELECT + "WHERE uid = ? AND id = ?").bind(uid, memory_id).first()
    return row if isinstance(row, dict) else None


async def _project(env: object, uid: str, raw: dict[str, object]) -> tuple[dict[str, object], bool]:
    item = _decode_queue_row(raw)
    if item["status"] not in _ACTIVE_STATUSES:
        return item, False
    memory_id = str(item.get("fact_id") or "")
    source = await _source_row(env, uid, memory_id) if memory_id else None
    stale_reason: str | None = None
    if source is None or source.get("deleted_at") is not None or source.get("invalid_at") is not None:
        stale_reason = "canonical_review_source_missing"
    else:
        try:
            valid_source = review_source_matches(raw, read_item(source))
        except (TypeError, ValueError):
            valid_source = False
        if not valid_source:
            stale_reason = 'canonical_review_source_stale'
    if stale_reason is None:
        return item, False
    now = int(time.time())
    redacted = {
        **item,
        "candidate": {},
        "source_commit_id": None,
        "source_item_revision": None,
        "source_content_hash": None,
        "source_short_term_id": None,
        "correction": None,
        "veracity": None,
        "impact": None,
        "resolved_at": _iso(now),
        "updated_at": _iso(now),
        "permitted_uses": [],
        "status": "tombstoned",
        "reason": stale_reason,
    }
    await env.APP_DB.prepare(
        "UPDATE cf_memory_review_queue SET status = 'tombstoned', previous_status = status, reason = ?, "
        "source_commit_id = '', source_item_revision = 1, source_content_hash = '', "
        "source_short_term_id = NULL, correction_json = NULL, veracity = NULL, impact = 0, "
        "candidate_json = ?, permitted_uses_json = '[]', resolved_at = COALESCE(resolved_at, ?), updated_at = ? "
        "WHERE uid = ? AND review_id = ? AND status IN ('pending', 'pending_review')"
    ).bind(
        stale_reason,
        json.dumps(redacted["candidate"], ensure_ascii=False, separators=(",", ":")),
        now,
        now,
        uid,
        str(item["review_id"]),
    ).run()
    return redacted, True


def _query_value(request: Request, name: str) -> str | None:
    value = request.query_params.get(name)
    return value if isinstance(value, str) else None


@router.get("/v3/memories/review-queue")
async def list_memory_review_queue(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    status = (_query_value(request, "status") or "pending").strip()
    if len(status) > MAX_STATUS_LENGTH or status not in _VALID_STATUSES:
        return JSONResponse({"error": "invalid review status"}, status_code=400)
    try:
        limit = int(_query_value(request, "limit") or "100")
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid pagination"}, status_code=400)
    if limit < 1 or limit > MAX_LIMIT:
        return JSONResponse({"error": "invalid pagination"}, status_code=400)
    uid = str(context["uid"])
    where = "uid = ?"
    args: list[object] = [uid]
    if status:
        where += " AND status = ?"
        args.append(status)
    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT uid, review_id, fact_id, candidate_json, conflict_with_json, referenced_memory_ids_json, veracity, "
                "impact, status, authority, previous_status, source_commit_id, source_item_revision, source_content_hash, "
                "source_short_term_id, permitted_uses_json, reason, decision, resolution_commit_id, correction_json, "
                "created_at, updated_at, expires_at, resolved_at FROM cf_memory_review_queue WHERE "
                + where
                + " ORDER BY impact DESC, created_at DESC, review_id DESC LIMIT ?"
            )
            .bind(*args, limit)
            .all()
        )
        rows = result.get("results", []) if isinstance(result, dict) else []
        projected: list[dict[str, object]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item, _ = await _project(request.scope["env"], uid, row)
            if item.get("status") == status:
                projected.append(item)
        return projected
    except Exception:
        return JSONResponse({"error": "memory review queue unavailable"}, status_code=503)


@router.get("/v3/memories/review-queue/{review_id}")
async def get_memory_review_item(request: Request, review_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not review_id or len(review_id) > MAX_REVIEW_ID_LENGTH:
        return JSONResponse({"error": "review item not found"}, status_code=404)
    uid = str(context["uid"])
    try:
        raw = await _raw_item(request.scope["env"], uid, review_id)
        if raw is None:
            return JSONResponse({"error": "review item not found"}, status_code=404)
        item, _ = await _project(request.scope["env"], uid, raw)
        return item
    except Exception:
        return JSONResponse({"error": "memory review queue unavailable"}, status_code=503)


def _pending_cleanup():
    return JSONResponse({'error': 'memory_cleanup_pending'}, status_code=503, headers={'Retry-After': '2'})


async def _resume_review_privacy(env, uid, item):
    if item.get('decision') not in {'reject', 'drop'}:
        return True
    pending = await env.APP_DB.prepare('SELECT * FROM cf_memory_privacy_deletions WHERE uid = ?').bind(uid).first()
    if pending is not None and item['fact_id'] in json.loads(pending['requested_ids_json']):
        return await finish_or_schedule(env, pending)
    return True


async def _resolve_memory(request: Request, uid: str, item: dict[str, object], resolution: ReviewResolution):
    decision = resolution.decision.strip().lower()
    if decision not in {'accept', 'reject', 'correct', 'timeout'}:
        return JSONResponse({'error': 'invalid review decision'}, status_code=400)
    if decision == 'timeout':
        veracity = resolution.current_veracity if resolution.current_veracity is not None else item.get('veracity') or 0
        decision = 'accept' if veracity >= 0.75 else 'drop'
    correction = resolution.correction
    if correction is not None and len(json.dumps(correction, ensure_ascii=False).encode()) > MAX_CORRECTION_BYTES:
        return JSONResponse({'error': 'correction exceeds size limit'}, status_code=413)
    if decision == 'correct':
        value = correction or {}
        content = value.get('memory_text') or value.get('content')
        args = value.get('arg_changes')
        if (
            content is not None
            and (not isinstance(content, str) or not content.strip() or len(content) > 50_000)
            or args is not None
            and not isinstance(args, dict)
            or not (isinstance(content, str) and content.strip() or isinstance(args, dict) and args)
        ):
            return JSONResponse({'error': 'invalid correction'}, status_code=400)
    elif correction:
        return JSONResponse({'error': 'decision does not accept correction data'}, status_code=400)
    env = request.scope['env']
    participant = CanonicalReviewResolution(item, decision, resolution.reason)
    now = int(time.time())
    if decision in {'reject', 'drop'}:
        inventory = await prepare_privacy_deletion(env, uid, [item['fact_id']], now, review_resolution=participant)
        raw = await _raw_item(env, uid, item['review_id'])
        resolved = _decode_queue_row(raw)
        if not await finish_or_schedule(env, inventory):
            return _pending_cleanup()
    else:

        def build(current, instant):
            return review_resolution_patch(
                current, instant, review_id=item['review_id'], decision=decision, correction=correction
            )

        applied = await apply_user_memory_mutation(
            env,
            uid,
            item['fact_id'],
            now,
            kind=f"review_resolution:{item['review_id']}:{decision}",
            build_patch=build,
            review_resolution=participant,
        )
        if not applied:
            raise ValueError('memory_review_source_changed')
        resolved = _decode_queue_row(await _raw_item(env, uid, item['review_id']))
    return {
        'status': 'resolved',
        'decision': decision,
        'commit': {'commit_id': resolved['resolution_commit_id']},
        'correction': None,
        'item': resolved,
    }


@router.post("/v3/memories/review-queue/{review_id}/resolve")
async def resolve_memory_review_item(request: Request, review_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not review_id or len(review_id) > MAX_REVIEW_ID_LENGTH:
        return JSONResponse({"error": "review item not found"}, status_code=404)
    try:
        payload = ReviewResolution.model_validate_json(await request.body())
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid review resolution"}, status_code=400)
    uid = str(context["uid"])
    try:
        raw = await _raw_item(request.scope["env"], uid, review_id)
        if raw is None:
            return JSONResponse({"error": "review item not found"}, status_code=404)
        item, _ = await _project(request.scope["env"], uid, raw)
        if item.get("status") not in _ACTIVE_STATUSES:
            if not await _resume_review_privacy(request.scope['env'], uid, item):
                return _pending_cleanup()
            if str(item.get("reason") or "").startswith("canonical_review_source_"):
                return {"status": "stale_review", "decision": None, "commit": None, "correction": None, "item": item}
            return {
                "status": "already_resolved",
                "decision": item.get("decision"),
                "commit": None,
                "correction": None,
                "item": item,
            }
        return await _resolve_memory(request, uid, item, payload)
    except Exception as error:
        if 'memory_review_source_changed' in str(error):
            current = await _raw_item(request.scope['env'], uid, review_id)
            if current is not None:
                latest, _ = await _project(request.scope['env'], uid, current)
                if latest.get('status') not in _ACTIVE_STATUSES:
                    if not await _resume_review_privacy(request.scope['env'], uid, latest):
                        return _pending_cleanup()
                    status = (
                        'stale_review'
                        if str(latest.get('reason') or '').startswith('canonical_review_source_')
                        else 'already_resolved'
                    )
                    return {
                        'status': status,
                        'decision': latest.get('decision'),
                        'commit': None,
                        'correction': None,
                        'item': latest,
                    }
        return memory_mutation_error(error, unavailable='memory review resolution unavailable')


__all__ = ["router"]
