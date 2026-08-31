"""D1-backed canonical memory review queue.

The queue is deliberately small and deterministic.  A review candidate is
created only from a canonical ``cf_memories`` write, and every read rechecks
the source row's revision and content hash before exposing the candidate.
That keeps a stale review from becoming an alternate memory authority.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from internal_auth import decode_context

router = APIRouter()

MAX_REVIEW_ID_LENGTH = 512
MAX_STATUS_LENGTH = 32
MAX_LIMIT = 500
MAX_CORRECTION_BYTES = 64_000
REVIEW_TTL_SECONDS = 72 * 60 * 60
_ACTIVE_STATUSES = {"pending", "pending_review"}
_VALID_STATUSES = _ACTIVE_STATUSES | {"accepted", "rejected", "dropped", "tombstoned"}

_MEMORY_SOURCE_SELECT = (
    "SELECT uid, id, content, category, visibility, tags_json, headline, predicate, arguments_json, "
    "subject_entity_id, subject_attribution, object_entity_ids_json, qualifiers_json, capture_confidence, veracity, "
    "uncertainty_reasons_json, durability, conversation_id, reviewed, user_review, manually_added, edited, scoring, "
    "app_id, data_protection_level, is_locked, is_read, is_dismissed, kg_extracted, is_baseline, memory_tier, "
    "valid_at, invalid_at, superseded_by, primary_capture_device, capture_device_ids_json, created_at, updated_at "
    "FROM cf_memories "
)


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


def _bool(value: object) -> bool:
    return bool(value) and value not in ("0", "false", "False", "no")


def _candidate(row: dict[str, object]) -> dict[str, object]:
    """Serialize the same stable fields that memory clients use for a fact."""

    memory_id = str(row.get("id") or "")
    tier = str(row.get("memory_tier") or "short_term")
    return {
        "id": memory_id,
        "memory_id": memory_id,
        "uid": str(row.get("uid") or ""),
        "content": str(row.get("content") or ""),
        "category": str(row.get("category") or "interesting"),
        "visibility": str(row.get("visibility") or "private"),
        "tags": _json_list(row.get("tags_json")),
        "headline": row.get("headline"),
        "predicate": row.get("predicate"),
        "arguments": _json_dict(row.get("arguments_json")),
        "subject_entity_id": row.get("subject_entity_id"),
        "subject_attribution": str(row.get("subject_attribution") or "unknown"),
        "object_entity_ids": _json_list(row.get("object_entity_ids_json")),
        "qualifiers": _json_dict(row.get("qualifiers_json")),
        "capture_confidence": row.get("capture_confidence"),
        "veracity": row.get("veracity"),
        "uncertainty_reasons": _json_list(row.get("uncertainty_reasons_json")),
        "durability": row.get("durability"),
        "conversation_id": row.get("conversation_id"),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "reviewed": _bool(row.get("reviewed")),
        "user_review": None if row.get("user_review") is None else _bool(row.get("user_review")),
        "manually_added": _bool(row.get("manually_added")),
        "edited": _bool(row.get("edited")),
        "scoring": row.get("scoring"),
        "app_id": row.get("app_id"),
        "data_protection_level": row.get("data_protection_level"),
        "is_locked": _bool(row.get("is_locked")),
        "is_read": _bool(row.get("is_read")),
        "is_dismissed": _bool(row.get("is_dismissed")),
        "kg_extracted": _bool(row.get("kg_extracted")),
        "is_baseline": _bool(row.get("is_baseline")),
        "evidence": [],
        "memory_tier": tier,
        "layer": tier,
        "valid_at": _iso(row.get("valid_at")),
        "invalid_at": _iso(row.get("invalid_at")),
        "superseded_by": row.get("superseded_by"),
        "primary_capture_device": row.get("primary_capture_device"),
        "capture_device_ids": _json_list(row.get("capture_device_ids_json")),
    }


def _source_hash(row: dict[str, object]) -> str:
    return hashlib.sha256(str(row.get("content") or "").encode("utf-8")).hexdigest()


def _source_commit_id(uid: str, memory_id: str, revision: int) -> str:
    return f"d1-memory:{uid}:{memory_id}:{revision}"


def _structural_conflict(candidate: dict[str, object], existing: dict[str, object]) -> bool:
    predicate = candidate.get("predicate")
    if not predicate or predicate != existing.get("predicate"):
        return False
    left_subject = candidate.get("subject_entity_id")
    right_subject = existing.get("subject_entity_id")
    if left_subject and right_subject and left_subject != right_subject:
        return False
    left_args = _json_dict(candidate.get("arguments_json"))
    right_args = _json_dict(existing.get("arguments_json"))
    return any(key in right_args and left_args[key] != right_args[key] for key in left_args)


def _importance(row: dict[str, object]) -> float:
    qualifiers = _json_dict(row.get("qualifiers_json"))
    raw = qualifiers.get("importance", 0.5)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.5
    return value if math.isfinite(value) else 0.5


def _veracity(row: dict[str, object]) -> float:
    raw = row.get("veracity")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _review_id(memory_id: str, revision: int, conflict_ids: list[str]) -> str:
    return f"review:{memory_id}:r{revision}:{','.join(sorted(set(conflict_ids)))}"


async def build_review_queue_statements(
    env: object,
    *,
    uid: str,
    candidate_rows: list[dict[str, object]],
    now: int,
) -> list[object]:
    """Build queue writes for a canonical memory batch before it is committed.

    The existing active rows are read once per predicate.  The statements can
    then be appended to the enclosing memory transaction, so a memory cannot
    commit without its conflict projection.
    """

    normalized = [row for row in candidate_rows if isinstance(row, dict) and row.get("predicate")]
    if not normalized:
        return []
    predicates = sorted({str(row["predicate"]) for row in normalized})
    placeholders = ",".join("?" for _ in predicates)
    existing_result = (
        await env.APP_DB.prepare(
            _MEMORY_SOURCE_SELECT
            + "WHERE uid = ? AND deleted_at IS NULL AND invalid_at IS NULL AND predicate IN ("
            + placeholders
            + ")"
        )
        .bind(uid, *predicates)
        .all()
    )
    raw_existing = existing_result.get("results", []) if isinstance(existing_result, dict) else []
    existing = [row for row in raw_existing if isinstance(row, dict)]
    all_rows = [*existing, *normalized]
    statements: list[object] = []
    for row in normalized:
        memory_id = str(row.get("id") or "")
        if not memory_id:
            continue
        conflicts = [
            str(other.get("id"))
            for other in all_rows
            if str(other.get("id") or "") != memory_id and _structural_conflict(row, other)
        ]
        conflicts = sorted(set(conflicts))
        if not conflicts:
            continue
        revision = max(1, int(row.get("updated_at") or now))
        review_id = _review_id(memory_id, revision, conflicts)
        candidate = _candidate(row)
        referenced = sorted({memory_id, *conflicts})
        impact = _importance(row) * abs(
            _veracity(row)
            - max((_veracity(item) for item in all_rows if str(item.get("id") or "") in conflicts), default=0.0)
        )
        statements.append(
            env.APP_DB.prepare(
                "INSERT INTO cf_memory_review_queue "
                "(uid, review_id, fact_id, candidate_json, conflict_with_json, referenced_memory_ids_json, "
                "veracity, impact, status, authority, source_commit_id, source_item_revision, source_content_hash, "
                "permitted_uses_json, created_at, updated_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'canonical_memory', ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(uid, review_id) DO NOTHING"
            ).bind(
                uid,
                review_id,
                memory_id,
                json.dumps(candidate, ensure_ascii=False, separators=(",", ":")),
                json.dumps(conflicts, ensure_ascii=False, separators=(",", ":")),
                json.dumps(referenced, ensure_ascii=False, separators=(",", ":")),
                _veracity(row),
                impact,
                _source_commit_id(uid, memory_id, revision),
                revision,
                _source_hash(row),
                '["answers_with_disclaimer"]',
                now,
                now,
                now + REVIEW_TTL_SECONDS,
            )
        )
    return statements


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
    revision = int(raw.get("source_item_revision") or 0)
    stale_reason: str | None = None
    if source is None or source.get("deleted_at") is not None or source.get("invalid_at") is not None:
        stale_reason = "canonical_review_source_missing"
    elif int(source.get("updated_at") or 0) != revision or _source_hash(source) != raw.get("source_content_hash"):
        stale_reason = "canonical_review_source_stale"
    if stale_reason is None:
        return item, False
    now = int(time.time())
    redacted = {
        **item,
        "candidate": {"id": memory_id} if memory_id else {},
        "permitted_uses": [],
        "status": "tombstoned",
        "reason": stale_reason,
    }
    await env.APP_DB.prepare(
        "UPDATE cf_memory_review_queue SET status = 'tombstoned', previous_status = status, reason = ?, "
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


async def _resolve_memory(request: Request, uid: str, item: dict[str, object], resolution: ReviewResolution):
    decision = resolution.decision.strip().lower()
    if decision not in {"accept", "reject", "correct", "timeout"}:
        return JSONResponse({"error": "invalid review decision"}, status_code=400)
    effective = decision
    if decision == "timeout":
        effective = "accept" if (resolution.current_veracity or float(item.get("veracity") or 0)) >= 0.75 else "drop"
    if effective not in {"accept", "reject", "correct", "drop"}:
        return JSONResponse({"error": "invalid review decision"}, status_code=400)
    env = request.scope["env"]
    fact_id = str(item.get("fact_id") or "")
    conflicts = [str(value) for value in item.get("conflict_with", []) if isinstance(value, str)]
    now = int(time.time())
    statements: list[object] = []
    correction = resolution.correction
    if (
        correction is not None
        and len(json.dumps(correction, ensure_ascii=False).encode("utf-8")) > MAX_CORRECTION_BYTES
    ):
        return JSONResponse({"error": "correction exceeds size limit"}, status_code=413)
    if effective == "accept":
        statements.append(
            env.APP_DB.prepare(
                "UPDATE cf_memories SET reviewed = 1, user_review = 1, updated_at = ? "
                "WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
            ).bind(now, uid, fact_id)
        )
        for conflict_id in conflicts:
            statements.append(
                env.APP_DB.prepare(
                    "UPDATE cf_memories SET invalid_at = ?, superseded_by = ?, updated_at = ? "
                    "WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
                ).bind(now, fact_id, now, uid, conflict_id)
            )
    elif effective == "reject":
        statements.append(
            env.APP_DB.prepare(
                "UPDATE cf_memories SET reviewed = 1, user_review = 0, updated_at = ? "
                "WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
            ).bind(now, uid, fact_id)
        )
    elif effective == "correct":
        correction_dict = correction or {}
        target_id = str(correction_dict.get("target_fact_id") or fact_id)
        if target_id not in {fact_id, *conflicts}:
            return JSONResponse({"error": "invalid correction target"}, status_code=400)
        target = await _source_row(env, uid, target_id)
        if target is None or target.get("deleted_at") is not None or target.get("invalid_at") is not None:
            return JSONResponse({"error": "memory not found"}, status_code=404)
        assignments: list[str] = []
        values: list[object] = []
        arg_changes = correction_dict.get("arg_changes")
        if arg_changes is not None:
            if not isinstance(arg_changes, dict):
                return JSONResponse({"error": "invalid correction"}, status_code=400)
            arguments = _json_dict(target.get("arguments_json"))
            arguments.update(arg_changes)
            assignments.append("arguments_json = ?")
            values.append(json.dumps(arguments, ensure_ascii=False, separators=(",", ":")))
        if "content" in correction_dict:
            content = correction_dict.get("content")
            if not isinstance(content, str) or not content.strip() or len(content) > 50_000:
                return JSONResponse({"error": "invalid correction content"}, status_code=400)
            assignments.extend(("content = ?", "edited = 1"))
            values.append(content)
        if not assignments:
            return JSONResponse({"error": "correction is empty"}, status_code=400)
        assignments.extend(("reviewed = 1", "user_review = 1", "updated_at = ?"))
        values.extend((now, uid, target_id))
        statements.append(
            env.APP_DB.prepare(
                "UPDATE cf_memories SET "
                + ", ".join(assignments)
                + " WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
            ).bind(*values)
        )
    commit_id = f"d1-review:{item['review_id']}:{effective}"
    status = {"accept": "accepted", "reject": "rejected", "correct": "accepted", "drop": "dropped"}[effective]
    permitted = {"accepted": '["answers", "actions"]', "rejected": '["audit_debug"]', "dropped": "[]"}[status]
    statements.append(
        env.APP_DB.prepare(
            "UPDATE cf_memory_review_queue SET status = ?, previous_status = status, decision = ?, reason = ?, "
            "resolution_commit_id = ?, correction_json = ?, permitted_uses_json = ?, resolved_at = ?, updated_at = ? "
            "WHERE uid = ? AND review_id = ? AND status IN ('pending', 'pending_review')"
        ).bind(
            status,
            effective,
            resolution.reason,
            commit_id,
            json.dumps(correction, ensure_ascii=False, separators=(",", ":")) if correction is not None else None,
            permitted,
            now,
            now,
            uid,
            str(item["review_id"]),
        )
    )
    try:
        await env.APP_DB.batch(statements)
    except Exception:
        return JSONResponse({"error": "memory review resolution unavailable"}, status_code=503)
    resolved_raw = await _raw_item(env, uid, str(item["review_id"]))
    resolved = _decode_queue_row(resolved_raw) if resolved_raw else {**item, "status": status}
    return {
        "status": "resolved",
        "decision": effective,
        "commit": {"commit_id": commit_id},
        "correction": correction,
        "item": resolved,
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
    except Exception:
        return JSONResponse({"error": "memory review resolution unavailable"}, status_code=503)


__all__ = ["build_review_queue_statements", "router"]
