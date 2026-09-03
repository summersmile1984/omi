"""Cloudflare-native task-intelligence and staged-task compatibility routes.

The released Firestore implementation joins several legacy collections and a
local executor.  This boundary owns the same user-visible surfaces for a
Cloudflare-owned Better Auth account: candidates, device snapshots,
attribution records, and evaluation receipts are all tenant and generation
scoped in D1.  Evaluations use the Workers AI binding and record a durable job
lease before publishing the result, so a provider failure cannot turn into an
unfenced write or an invented empty recommendation.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context

router = APIRouter()

MAX_BODY_BYTES = 96_000
MAX_DESCRIPTION_LENGTH = 5_000
MAX_ID_LENGTH = 128
MAX_LIST_LIMIT = 500
MAX_EVIDENCE_REFS = 50
MAX_RECOMMENDATIONS = 3
EVALUATION_TTL_SECONDS = 15 * 60
EVALUATION_LEASE_SECONDS = 5 * 60
EVALUATION_RETRY_DELAY_SECONDS = 10
MAX_JOB_ATTEMPTS = 3
WORKERS_AI_MODEL = "@cf/meta/llama-3.2-3b-instruct"
TASK_INTELLIGENCE_PROCESSOR_PATH = "/internal/task-intelligence/evaluate"
SUPPORTED_PLATFORMS = frozenset({"android", "ios", "linux", "macos", "web", "windows"})
VALID_FEEDBACK_ACTIONS = frozenset({"do_now", "later", "dismiss", "accept_candidate", "edit", "complete"})
VALID_FEEDBACK_REASONS = frozenset({"already_handled", "not_mine", "not_useful"})
VALID_OUTCOME_CODES = frozenset(
    {
        "task_completed",
        "artifact_approved",
        "artifact_delivered",
        "decision_resolved",
        "agent_output_applied",
        "workstream_advanced",
    }
)


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _error(error: str, status: int, detail: str | None = None) -> JSONResponse:
    payload: dict[str, object] = {"error": error}
    if detail is not None:
        payload["detail"] = detail
    return JSONResponse(payload, status_code=status)


def _valid_id(value: object, *, max_length: int = MAX_ID_LENGTH) -> bool:
    return isinstance(value, str) and 0 < len(value) <= max_length and "/" not in value and "\x00" not in value


def _canonical(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _dump(value: object) -> str:
    return json.dumps(_canonical(value), ensure_ascii=False, separators=(",", ":"), default=str)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: object) -> str:
    raw = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:32]}"


async def _body(request: Request) -> object:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("request body exceeds size limit")
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("request body must be an object")
    return value


def _epoch(value: object, *, nullable: bool = True) -> int | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be an ISO string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return int(parsed.astimezone(timezone.utc).timestamp())


def _iso(value: object) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _bool(value: object) -> bool:
    return bool(value) and value not in (0, "0", "false", "False", "no")


def _changes(result: object) -> int:
    meta = result.get("meta") if isinstance(result, dict) else getattr(result, "meta", None)
    if isinstance(meta, dict):
        value = meta.get("changes")
    else:
        value = getattr(meta, "changes", 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _json(value: object, default: object) -> object:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value:
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed


async def _owned_generation(env: object, uid: str) -> int | JSONResponse:
    """Return the D1 generation only for a completed Cloudflare-owned account."""

    try:
        fence = await env.APP_DB.prepare(
            "SELECT 1 AS blocked FROM cf_account_deletion_intents WHERE uid = ? "
            "UNION ALL SELECT 1 AS blocked FROM cf_account_deletion_tombstones "
            "WHERE uid = ? AND expires_at > ? LIMIT 1"
        ).bind(uid, uid, int(time.time())).first()
        if isinstance(fence, dict):
            return _error("account_deletion_in_progress", 409)
        row = await env.APP_DB.prepare(
            "SELECT state, checkpoint_phase, destination_backend_bound, account_generation "
            "FROM cf_account_cutover WHERE uid = ?"
        ).bind(uid).first()
    except Exception:
        return _error("task_intelligence_unavailable", 503)
    if not isinstance(row, dict):
        return _error("task_intelligence_unavailable", 503)
    try:
        generation = int(row.get("account_generation"))
    except (TypeError, ValueError):
        return _error("task_intelligence_unavailable", 503)
    if (
        row.get("state") != "new"
        or row.get("checkpoint_phase") != "completed"
        or int(row.get("destination_backend_bound") or 0) != 1
        or generation < 0
    ):
        return _error("task_intelligence_unavailable", 503)
    return generation


def _request_generation(request: Request) -> int | JSONResponse:
    raw = request.headers.get("x-account-generation")
    if raw is None:
        return _error("account_generation_required", 400)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _error("invalid_account_generation", 400)
    if value < 0:
        return _error("invalid_account_generation", 400)
    return value


def _staged_response(row: dict[str, object]) -> dict[str, object]:
    return {
        "id": str(row.get("candidate_id") or ""),
        "description": str(row.get("description") or "Suggested task"),
        "completed": row.get("status") != "pending",
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "due_at": _iso(row.get("due_at")),
        "source": row.get("source"),
        "priority": row.get("priority"),
        "metadata": row.get("metadata"),
        "category": row.get("category"),
        "relevance_score": row.get("relevance_score"),
    }


async def _candidate(env: object, uid: str, generation: int, candidate_id: str) -> dict[str, object] | None:
    row = await env.APP_DB.prepare(
        "SELECT uid, candidate_id, account_generation, device_id, status, description, due_at, source, priority, "
        "metadata, category, relevance_score, evidence_refs_json, request_fingerprint, resolution_reason, "
        "result_task_id, created_at, updated_at, resolved_at FROM cf_task_candidates "
        "WHERE uid = ? AND candidate_id = ? AND account_generation = ?"
    ).bind(uid, candidate_id, generation).first()
    return row if isinstance(row, dict) else None


def _candidate_input(body: dict[str, object]) -> dict[str, object]:
    description = body.get("description")
    if not isinstance(description, str) or not description.strip() or len(description.strip()) > MAX_DESCRIPTION_LENGTH:
        raise ValueError("description is invalid")
    due_at = _epoch(body.get("due_at")) if body.get("due_at") is not None else None
    relevance = body.get("relevance_score")
    if relevance is not None and (isinstance(relevance, bool) or not isinstance(relevance, int) or not 0 <= relevance <= 1000):
        raise ValueError("relevance_score is invalid")
    refs = body.get("evidence_refs", [])
    if not isinstance(refs, list) or len(refs) > MAX_EVIDENCE_REFS:
        raise ValueError("evidence_refs is invalid")
    return {
        "description": description.strip(),
        "due_at": due_at,
        "source": str(body.get("source") or "manual")[:64] or "manual",
        "priority": str(body.get("priority"))[:32] if body.get("priority") is not None else None,
        "metadata": str(body.get("metadata"))[:4_096] if body.get("metadata") is not None else None,
        "category": str(body.get("category"))[:128] if body.get("category") is not None else None,
        "relevance_score": relevance,
        "evidence_refs": refs,
    }


@router.post("/v1/staged-tasks")
async def create_staged_task(request: Request):
    context = _auth_context(request)
    if not context:
        return _error("unauthorized", 401)
    uid = str(context["uid"])
    generation = await _owned_generation(request.scope["env"], uid)
    if isinstance(generation, JSONResponse):
        return generation
    try:
        payload = _candidate_input(await _body(request))
    except (ValueError, TypeError, json.JSONDecodeError):
        return _error("invalid_staged_task", 400)
    request_fingerprint = _fingerprint({"uid": uid, "generation": generation, **payload})
    candidate_id = _stable_id("staged", uid, generation, request_fingerprint)
    now = int(time.time())
    refs = payload["evidence_refs"] or [
        {"kind": "external", "id": f"cf-staged-{candidate_id}", "scope": "canonical"}
    ]
    env = request.scope["env"]
    try:
        await env.APP_DB.prepare(
            "INSERT INTO cf_task_candidates (uid, candidate_id, account_generation, status, description, due_at, "
            "source, priority, metadata, category, relevance_score, evidence_refs_json, request_fingerprint, "
            "created_at, updated_at) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(uid, account_generation, request_fingerprint) DO NOTHING"
        ).bind(
            uid,
            candidate_id,
            generation,
            payload["description"],
            payload["due_at"],
            payload["source"],
            payload["priority"],
            payload["metadata"],
            payload["category"],
            payload["relevance_score"],
            _dump(refs),
            request_fingerprint,
            now,
            now,
        ).run()
        row = await _candidate(env, uid, generation, candidate_id)
    except Exception:
        return _error("task_intelligence_unavailable", 503)
    return _staged_response(row) if row is not None else _error("task_intelligence_unavailable", 503)


@router.get("/v1/staged-tasks")
async def list_staged_tasks(request: Request):
    context = _auth_context(request)
    if not context:
        return _error("unauthorized", 401)
    uid = str(context["uid"])
    generation = await _owned_generation(request.scope["env"], uid)
    if isinstance(generation, JSONResponse):
        return generation
    try:
        limit = min(MAX_LIST_LIMIT, max(1, int(request.query_params.get("limit", "100"))))
        offset = max(0, int(request.query_params.get("offset", "0")))
    except (TypeError, ValueError):
        return _error("invalid_pagination", 400)
    env = request.scope["env"]
    try:
        result = await env.APP_DB.prepare(
            "SELECT candidate_id, status, description, due_at, source, priority, metadata, category, "
            "relevance_score, created_at, updated_at FROM cf_task_candidates "
            "WHERE uid = ? AND account_generation = ? AND status = 'pending' "
            "ORDER BY relevance_score IS NULL ASC, relevance_score DESC, created_at DESC, candidate_id ASC "
            "LIMIT ? OFFSET ?"
        ).bind(uid, generation, limit + 1, offset).all()
    except Exception:
        return _error("task_intelligence_unavailable", 503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    rows = [row for row in rows if isinstance(row, dict)]
    return {"items": [_staged_response(row) for row in rows[:limit]], "has_more": len(rows) > limit}


@router.delete("/v1/staged-tasks")
async def clear_staged_tasks(request: Request):
    context = _auth_context(request)
    if not context:
        return _error("unauthorized", 401)
    uid = str(context["uid"])
    generation = await _owned_generation(request.scope["env"], uid)
    if isinstance(generation, JSONResponse):
        return generation
    now = int(time.time())
    try:
        row = await request.scope["env"].APP_DB.prepare(
            "UPDATE cf_task_candidates SET status = 'rejected', resolution_reason = 'legacy_clear', "
            "resolved_at = ?, updated_at = ? WHERE uid = ? AND account_generation = ? AND status = 'pending' "
            "RETURNING candidate_id"
        ).bind(now, now, uid, generation).all()
    except Exception:
        return _error("task_intelligence_unavailable", 503)
    deleted = len(row.get("results", [])) if isinstance(row, dict) else 0
    return {"status": "ok", "deleted_count": deleted}


@router.delete("/v1/staged-tasks/{task_id}")
async def delete_staged_task(request: Request, task_id: str):
    context = _auth_context(request)
    if not context:
        return _error("unauthorized", 401)
    if not _valid_id(task_id):
        return _error("invalid_staged_task_id", 400)
    uid = str(context["uid"])
    generation = await _owned_generation(request.scope["env"], uid)
    if isinstance(generation, JSONResponse):
        return generation
    try:
        await request.scope["env"].APP_DB.prepare(
            "UPDATE cf_task_candidates SET status = 'rejected', resolution_reason = 'legacy_delete', "
            "resolved_at = ?, updated_at = ? WHERE uid = ? AND account_generation = ? AND candidate_id = ? AND status = 'pending'"
        ).bind(int(time.time()), int(time.time()), uid, generation, task_id).run()
    except Exception:
        return _error("task_intelligence_unavailable", 503)
    return {"status": "ok"}


@router.patch("/v1/staged-tasks/batch-scores")
async def update_staged_scores(request: Request):
    context = _auth_context(request)
    if not context:
        return _error("unauthorized", 401)
    uid = str(context["uid"])
    generation = await _owned_generation(request.scope["env"], uid)
    if isinstance(generation, JSONResponse):
        return generation
    try:
        body = await _body(request)
        entries = body.get("scores") if isinstance(body, dict) else None
        if not isinstance(entries, list) or len(entries) > MAX_LIST_LIMIT:
            raise ValueError("scores is invalid")
        normalized: list[tuple[str, int]] = []
        for entry in entries:
            if not isinstance(entry, dict) or not _valid_id(entry.get("id")):
                raise ValueError("score id is invalid")
            score = entry.get("relevance_score")
            if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 1000:
                raise ValueError("score is invalid")
            normalized.append((str(entry["id"]), score))
    except (ValueError, TypeError, json.JSONDecodeError):
        return _error("invalid_staged_task_scores", 400)
    statements = [
        request.scope["env"].APP_DB.prepare(
            "UPDATE cf_task_candidates SET relevance_score = ?, updated_at = ? "
            "WHERE uid = ? AND account_generation = ? AND candidate_id = ? AND status = 'pending'"
        ).bind(score, int(time.time()), uid, generation, candidate_id)
        for candidate_id, score in normalized
    ]
    try:
        if statements:
            await request.scope["env"].APP_DB.batch(statements)
    except Exception:
        return _error("task_intelligence_unavailable", 503)
    return {"status": "ok"}


async def _promote(request: Request, uid: str, generation: int, candidate_id: str) -> dict[str, object] | JSONResponse:
    env = request.scope["env"]
    row = await _candidate(env, uid, generation, candidate_id)
    if row is None or row.get("status") not in {"pending", "accepted"}:
        return _error("staged_task_not_found", 404)
    task_id = str(row.get("result_task_id") or _stable_id("task", uid, generation, candidate_id))
    now = int(time.time())
    evidence = _json(row.get("evidence_refs_json"), [])
    try:
        statements = [
            env.APP_DB.prepare(
                "INSERT INTO cf_action_items (uid, id, description, status, completed, owner, due_at, source, "
                "provenance_json, priority, created_at, updated_at, idempotency_key, sync_requested, deleted) "
                "VALUES (?, ?, ?, 'active', 0, 'user', ?, ?, ?, ?, ?, ?, ?, 0, 0) ON CONFLICT(uid, id) DO NOTHING"
            ).bind(
                uid,
                task_id,
                row.get("description"),
                row.get("due_at"),
                row.get("source") or "staged_task",
                _dump(evidence),
                row.get("priority"),
                now,
                now,
                f"cloudflare-staged:{candidate_id}",
            ),
            env.APP_DB.prepare(
                "UPDATE cf_task_candidates SET status = 'accepted', result_task_id = ?, resolved_at = ?, updated_at = ? "
                "WHERE uid = ? AND candidate_id = ? AND account_generation = ? AND status = 'pending'"
            ).bind(task_id, now, now, uid, candidate_id, generation),
        ]
        await env.APP_DB.batch(statements)
        updated = await _candidate(env, uid, generation, candidate_id)
        if updated is None or updated.get("status") != "accepted":
            return _error("staged_task_not_found", 404)
        action = await env.APP_DB.prepare(
            "SELECT id, description, status, completed, owner, due_at, source, provenance_json, priority, "
            "created_at, updated_at FROM cf_action_items WHERE uid = ? AND id = ? AND deleted = 0"
        ).bind(uid, task_id).first()
    except Exception:
        return _error("task_intelligence_unavailable", 503)
    if not isinstance(action, dict):
        return _error("task_intelligence_unavailable", 503)
    return {"promoted": True, "reason": None, "promoted_task": _action_response(action)}


def _action_response(row: dict[str, object]) -> dict[str, object]:
    return {
        "id": row.get("id"),
        "task_id": row.get("id"),
        "description": row.get("description"),
        "status": row.get("status") or "active",
        "completed": _bool(row.get("completed")),
        "owner": row.get("owner") or "unknown",
        "due_at": _iso(row.get("due_at")),
        "source": row.get("source") or "staged_task",
        "provenance": _json(row.get("provenance_json"), []),
        "priority": row.get("priority"),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


@router.post("/v1/staged-tasks/promote")
async def promote_staged_task(request: Request):
    context = _auth_context(request)
    if not context:
        return _error("unauthorized", 401)
    uid = str(context["uid"])
    generation = await _owned_generation(request.scope["env"], uid)
    if isinstance(generation, JSONResponse):
        return generation
    try:
        row = await request.scope["env"].APP_DB.prepare(
            "SELECT candidate_id FROM cf_task_candidates WHERE uid = ? AND account_generation = ? AND status = 'pending' "
            "ORDER BY relevance_score IS NULL ASC, relevance_score DESC, created_at DESC, candidate_id ASC LIMIT 1"
        ).bind(uid, generation).first()
    except Exception:
        return _error("task_intelligence_unavailable", 503)
    if not isinstance(row, dict):
        return {"promoted": False, "reason": "No staged tasks available", "promoted_task": None}
    return await _promote(request, uid, generation, str(row["candidate_id"]))


@router.post("/v1/staged-tasks/{task_id}/promote")
async def promote_staged_task_by_id(request: Request, task_id: str):
    context = _auth_context(request)
    if not context:
        return _error("unauthorized", 401)
    if not _valid_id(task_id):
        return _error("invalid_staged_task_id", 400)
    uid = str(context["uid"])
    generation = await _owned_generation(request.scope["env"], uid)
    if isinstance(generation, JSONResponse):
        return generation
    return await _promote(request, uid, generation, task_id)


def _header_generation_and_auth(request: Request) -> tuple[dict[str, object], str, int] | JSONResponse:
    context = _auth_context(request)
    if not context:
        return _error("unauthorized", 401)
    generation = _request_generation(request)
    if isinstance(generation, JSONResponse):
        return generation
    return context, str(context["uid"]), generation


async def _store_idempotent(
    request: Request,
    *,
    table: str,
    uid: str,
    generation: int,
    record_id: str,
    fingerprint: str,
    columns: str,
    values: tuple[object, ...],
    select: str,
) -> dict[str, object] | JSONResponse:
    env = request.scope["env"]
    key_column = columns.split(",")[1].strip()
    try:
        existing = await env.APP_DB.prepare(
            f"SELECT {select}, request_fingerprint FROM {table} WHERE uid = ? AND {key_column} = ?"
        ).bind(uid, record_id).first()
        if isinstance(existing, dict):
            if existing.get("request_fingerprint") != fingerprint:
                return _error("idempotency_conflict", 409)
            return existing
        await env.APP_DB.prepare(
            f"INSERT INTO {table} ({columns}, request_fingerprint, created_at) VALUES ({','.join('?' for _ in values)}, ?, ?)"
        ).bind(*values, fingerprint, int(time.time())).run()
        stored = await env.APP_DB.prepare(
            f"SELECT {select}, request_fingerprint FROM {table} WHERE uid = ? AND {key_column} = ?"
        ).bind(uid, record_id).first()
    except Exception:
        return _error("task_intelligence_unavailable", 503)
    return stored if isinstance(stored, dict) else _error("task_intelligence_unavailable", 503)


def _intervention_response(row: dict[str, object]) -> dict[str, object]:
    payload = _json(row.get("payload_json"), {})
    if not isinstance(payload, dict):
        payload = {}
    return {**payload, "intervention_id": row.get("intervention_id"), "attribution_chain_id": row.get("attribution_chain_id"), "created_at": _iso(row.get("created_at"))}


@router.post("/v1/task-intelligence/interventions")
async def create_intervention(request: Request):
    parsed = _header_generation_and_auth(request)
    if isinstance(parsed, JSONResponse):
        return parsed
    _, uid, generation = parsed
    try:
        body = await _body(request)
        if not isinstance(body, dict):
            raise ValueError
        surface, subject_kind, subject_id, dedupe_key = (body.get(name) for name in ("surface", "subject_kind", "subject_id", "dedupe_key"))
        if surface not in {"suggested", "what_matters_now"} or subject_kind not in {"candidate", "task", "workstream", "artifact", "decision"}:
            raise ValueError
        if not all(_valid_id(value) for value in (subject_id, dedupe_key)):
            raise ValueError
        expires_at = _epoch(body.get("expires_at"), nullable=False)
        refs = body.get("evidence_refs", [])
        if not isinstance(refs, list) or len(refs) > MAX_EVIDENCE_REFS:
            raise ValueError
        payload = {"surface": surface, "subject_kind": subject_kind, "subject_id": subject_id, "dedupe_key": dedupe_key, "evidence_refs": refs, "expires_at": _iso(expires_at)}
        idem = request.headers.get("idempotency-key")
        if not idem or len(idem) > 512:
            raise ValueError
    except (ValueError, TypeError, json.JSONDecodeError):
        return _error("invalid_intervention", 400)
    fingerprint = _fingerprint(payload)
    intervention_id = _stable_id("intervention", uid, generation, idem)
    attribution = _stable_id("attr", uid, generation, intervention_id)
    row = await _store_idempotent(
        request,
        table="cf_task_interventions",
        uid=uid,
        generation=generation,
        record_id=intervention_id,
        fingerprint=fingerprint,
        columns="uid, intervention_id, account_generation, attribution_chain_id, payload_json",
        values=(uid, intervention_id, generation, attribution, _dump(payload)),
        select="intervention_id, attribution_chain_id, payload_json, created_at",
    )
    return _intervention_response(row) if isinstance(row, dict) else row


def _feedback_response(row: dict[str, object]) -> dict[str, object]:
    payload = _json(row.get("payload_json"), {})
    if not isinstance(payload, dict):
        payload = {}
    return {**payload, "feedback_id": row.get("feedback_id"), "attribution_chain_id": row.get("attribution_chain_id"), "created_at": _iso(row.get("created_at"))}


@router.post("/v1/task-intelligence/feedback")
async def create_feedback(request: Request):
    parsed = _header_generation_and_auth(request)
    if isinstance(parsed, JSONResponse):
        return parsed
    _, uid, generation = parsed
    try:
        body = await _body(request)
        if not isinstance(body, dict):
            raise ValueError
        action = body.get("action")
        if action not in VALID_FEEDBACK_ACTIONS or not _valid_id(body.get("subject_id")):
            raise ValueError
        subject_kind = body.get("subject_kind")
        if subject_kind not in {"candidate", "task", "workstream", "artifact", "decision"}:
            raise ValueError
        intervention_id = body.get("intervention_id")
        if action in {"do_now", "later", "dismiss"} and not _valid_id(intervention_id):
            raise ValueError
        reason = body.get("reason")
        if reason is not None and (reason not in VALID_FEEDBACK_REASONS or action != "dismiss"):
            raise ValueError
        later_until = body.get("later_until")
        later_epoch = _epoch(later_until) if later_until is not None else None
        if later_epoch is not None and action != "later":
            raise ValueError
        idem = request.headers.get("idempotency-key")
        if not idem or len(idem) > 512:
            raise ValueError
        payload = {key: body.get(key) for key in ("subject_kind", "subject_id", "intervention_id", "action", "reason", "context_snapshot_hash", "later_until") if body.get(key) is not None}
        if later_epoch is not None:
            payload["later_until"] = _iso(later_epoch)
    except (ValueError, TypeError, json.JSONDecodeError):
        return _error("invalid_feedback", 400)
    env = request.scope["env"]
    attribution = None
    if intervention_id is not None:
        intervention = await env.APP_DB.prepare(
            "SELECT attribution_chain_id FROM cf_task_interventions WHERE uid = ? AND intervention_id = ? AND account_generation = ?"
        ).bind(uid, intervention_id, generation).first()
        if not isinstance(intervention, dict):
            return _error("intervention_not_found", 404)
        attribution = intervention.get("attribution_chain_id")
    fingerprint = _fingerprint(payload)
    feedback_id = _stable_id("feedback", uid, generation, idem)
    row = await _store_idempotent(
        request,
        table="cf_task_feedback",
        uid=uid,
        generation=generation,
        record_id=feedback_id,
        fingerprint=fingerprint,
        columns="uid, feedback_id, account_generation, intervention_id, attribution_chain_id, payload_json",
        values=(uid, feedback_id, generation, intervention_id, attribution, _dump(payload)),
        select="feedback_id, attribution_chain_id, payload_json, created_at",
    )
    return _feedback_response(row) if isinstance(row, dict) else row


def _outcome_response(row: dict[str, object]) -> dict[str, object]:
    payload = _json(row.get("payload_json"), {})
    if not isinstance(payload, dict):
        payload = {}
    return {**payload, "outcome_id": row.get("outcome_id"), "occurred_at": _iso(row.get("occurred_at"))}


@router.post("/v1/task-intelligence/outcomes")
async def create_outcome(request: Request):
    parsed = _header_generation_and_auth(request)
    if isinstance(parsed, JSONResponse):
        return parsed
    _, uid, generation = parsed
    try:
        body = await _body(request)
        if not isinstance(body, dict) or not _valid_id(body.get("attribution_chain_id")):
            raise ValueError
        if body.get("subject_kind") not in {"candidate", "task", "workstream", "artifact", "decision"}:
            raise ValueError
        if not _valid_id(body.get("subject_id")) or body.get("outcome_code") not in VALID_OUTCOME_CODES:
            raise ValueError
        idem = request.headers.get("idempotency-key")
        if not idem or len(idem) > 512:
            raise ValueError
        chain = await request.scope["env"].APP_DB.prepare(
            "SELECT 1 AS found FROM cf_task_interventions WHERE uid = ? AND attribution_chain_id = ? AND account_generation = ? "
            "UNION ALL SELECT 1 AS found FROM cf_task_feedback WHERE uid = ? AND attribution_chain_id = ? AND account_generation = ? LIMIT 1"
        ).bind(uid, body["attribution_chain_id"], generation, uid, body["attribution_chain_id"], generation).first()
        if not isinstance(chain, dict):
            return _error("attribution_chain_not_found", 404)
        occurred = _epoch(body.get("occurred_at")) if body.get("occurred_at") is not None else int(time.time())
        payload = {"attribution_chain_id": body["attribution_chain_id"], "subject_kind": body["subject_kind"], "subject_id": body["subject_id"], "outcome_code": body["outcome_code"]}
    except (ValueError, TypeError, json.JSONDecodeError):
        return _error("invalid_outcome", 400)
    fingerprint = _fingerprint(payload)
    outcome_id = _stable_id("outcome", uid, generation, idem)
    env = request.scope["env"]
    try:
        row = await env.APP_DB.prepare(
            "SELECT outcome_id, attribution_chain_id, payload_json, occurred_at, request_fingerprint "
            "FROM cf_task_outcomes WHERE uid = ? AND outcome_id = ?"
        ).bind(uid, outcome_id).first()
        if isinstance(row, dict):
            if row.get("request_fingerprint") != fingerprint:
                return _error("idempotency_conflict", 409)
            return _outcome_response(row)
        await env.APP_DB.prepare(
            "INSERT INTO cf_task_outcomes (uid, outcome_id, account_generation, attribution_chain_id, "
            "request_fingerprint, payload_json, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
        ).bind(uid, outcome_id, generation, body["attribution_chain_id"], fingerprint, _dump(payload), occurred).run()
        row = await env.APP_DB.prepare(
            "SELECT outcome_id, attribution_chain_id, payload_json, occurred_at, request_fingerprint "
            "FROM cf_task_outcomes WHERE uid = ? AND outcome_id = ?"
        ).bind(uid, outcome_id).first()
    except Exception:
        return _error("task_intelligence_unavailable", 503)
    return _outcome_response(row) if isinstance(row, dict) else _error("task_intelligence_unavailable", 503)


def _device(request: Request, expected: object) -> str | JSONResponse:
    platform = request.headers.get("x-app-platform", "").strip().lower()
    device_hash = request.headers.get("x-device-id-hash", "").strip()
    if platform not in SUPPORTED_PLATFORMS or not _valid_id(device_hash, max_length=128):
        return _error("device_scope_required", 422)
    resolved = f"{platform}_{device_hash}"
    if expected is not None and expected not in {device_hash, resolved}:
        return _error("device_scope_mismatch", 403)
    return resolved


async def _save_snapshot(request: Request, *, table: str, body: dict[str, object], uid: str, generation: int) -> dict[str, object] | JSONResponse:
    device = _device(request, body.get("device_id"))
    if isinstance(device, JSONResponse):
        return device
    snapshot_id = body.get("snapshot_id")
    if not _valid_id(snapshot_id):
        return _error("invalid_snapshot", 400)
    try:
        generated = _epoch(body.get("generated_at"), nullable=False)
        expires = _epoch(body.get("expires_at"), nullable=False)
        if expires <= generated:
            raise ValueError
    except (ValueError, TypeError):
        return _error("invalid_snapshot", 400)
    payload = dict(body)
    payload["device_id"] = device
    fingerprint = _fingerprint(payload)
    env = request.scope["env"]
    try:
        existing = await env.APP_DB.prepare(
            f"SELECT snapshot_id, request_fingerprint, expires_at FROM {table} WHERE uid = ? AND device_id = ?"
        ).bind(uid, device).first()
        if isinstance(existing, dict) and existing.get("request_fingerprint") == fingerprint:
            return {"snapshot_id": existing.get("snapshot_id"), "replaced": False, "expires_at": _iso(existing.get("expires_at"))}
        await env.APP_DB.prepare(
            f"INSERT INTO {table} (uid, device_id, account_generation, snapshot_id, request_fingerprint, payload_json, generated_at, expires_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(uid, device_id) DO UPDATE SET account_generation = excluded.account_generation, snapshot_id = excluded.snapshot_id, request_fingerprint = excluded.request_fingerprint, payload_json = excluded.payload_json, generated_at = excluded.generated_at, expires_at = excluded.expires_at, updated_at = excluded.updated_at "
            "WHERE account_generation = excluded.account_generation"
        ).bind(uid, device, generation, snapshot_id, fingerprint, _dump(payload), generated, expires, int(time.time())).run()
    except Exception:
        return _error("task_intelligence_unavailable", 503)
    return {"snapshot_id": snapshot_id, "replaced": isinstance(existing, dict), "expires_at": _iso(expires)}


@router.put("/v1/task-intelligence/context-snapshot")
async def save_context_snapshot(request: Request):
    parsed = _header_generation_and_auth(request)
    if isinstance(parsed, JSONResponse):
        return parsed
    _, uid, generation = parsed
    try:
        body = await _body(request)
    except (ValueError, TypeError, json.JSONDecodeError):
        return _error("invalid_snapshot", 400)
    return await _save_snapshot(request, table="cf_task_context_snapshots", body=body, uid=uid, generation=generation)


@router.put("/v1/task-intelligence/open-loop-snapshot")
async def save_open_loop_snapshot(request: Request):
    parsed = _header_generation_and_auth(request)
    if isinstance(parsed, JSONResponse):
        return parsed
    _, uid, generation = parsed
    try:
        body = await _body(request)
    except (ValueError, TypeError, json.JSONDecodeError):
        return _error("invalid_snapshot", 400)
    return await _save_snapshot(request, table="cf_task_open_loop_snapshots", body=body, uid=uid, generation=generation)


def _provider_text(result: object) -> str:
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        for key in ("response", "result", "text", "content"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


async def _llm_why_now(env: object, descriptions: list[str]) -> tuple[str, str] | None:
    ai = getattr(env, "AI", None)
    if ai is None:
        return None
    model = str(getattr(env, "WORKERS_AI_TASK_INTELLIGENCE_MODEL", WORKERS_AI_MODEL))
    prompt = "Prioritize these tasks conservatively. Return one short reason (under 160 characters) for the first task only, and no private data beyond the supplied text.\n\n" + "\n".join(
        f"{index + 1}. {description[:500]}" for index, description in enumerate(descriptions[:12])
    )
    try:
        result = await ai.run(
            model,
            {
                "messages": [
                    {"role": "system", "content": "Return only a short plain-text reason."},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 64,
                "temperature": 0,
            },
        )
    except Exception:
        return None
    text = _provider_text(result)
    if not text:
        return None
    return text[:160], model


def _source_snapshot(source_rows: list[tuple[str, dict[str, object]]]) -> list[dict[str, object]]:
    """Serialize only the bounded provider input needed for a retry."""

    fields = {
        "candidate": ("candidate_id", "description", "due_at", "relevance_score", "evidence_refs_json"),
        "task": ("id", "description", "due_at", "provenance_json"),
    }
    return [
        {"kind": kind, "row": {field: row.get(field) for field in fields[kind]}}
        for kind, row in source_rows
    ]


def _source_rows_from_snapshot(value: object) -> list[tuple[str, dict[str, object]]] | None:
    if not isinstance(value, list) or len(value) > 12:
        return None
    fields = {
        "candidate": ("candidate_id", "description", "due_at", "relevance_score", "evidence_refs_json"),
        "task": ("id", "description", "due_at", "provenance_json"),
    }
    rows: list[tuple[str, dict[str, object]]] = []
    for item in value:
        if not isinstance(item, dict) or item.get("kind") not in fields or not isinstance(item.get("row"), dict):
            return None
        kind = str(item["kind"])
        row = item["row"]
        identity = "candidate_id" if kind == "candidate" else "id"
        if not _valid_id(row.get(identity)) or not isinstance(row.get("description"), str):
            return None
        rows.append((kind, {field: row.get(field) for field in fields[kind]}))
    return rows


async def _enqueue_evaluation(env: object, uid: str, job_id: str) -> bool:
    queue = getattr(env, "JOBS", None)
    if queue is None:
        return False
    try:
        await queue.send({"jobId": job_id, "uid": uid, "kind": "task_intelligence_evaluate", "payload": {}})
    except Exception:
        return False
    return True


async def _record_evaluation_failure(
    env: object,
    uid: str,
    job_id: str,
    generation: int,
    lease_token: str,
    error: str,
) -> str | None:
    """Release a lease into durable retry or terminal failure exactly once."""

    now = int(time.time())
    try:
        row = await env.APP_DB.prepare(
            "SELECT status, attempts FROM cf_task_intelligence_jobs "
            "WHERE uid = ? AND job_id = ? AND account_generation = ? AND lease_token = ?"
        ).bind(uid, job_id, generation, lease_token).first()
        if not isinstance(row, dict) or row.get("status") != "running":
            return None
        attempts = int(row.get("attempts") or 0)
        retryable = attempts < MAX_JOB_ATTEMPTS
        status = "queued" if retryable else "failed"
        next_attempt_at = now + EVALUATION_RETRY_DELAY_SECONDS if retryable else now
        updated = await env.APP_DB.prepare(
            "UPDATE cf_task_intelligence_jobs SET status = ?, lease_token = NULL, lease_until = NULL, "
            "next_attempt_at = ?, last_error = ?, updated_at = ? "
            "WHERE uid = ? AND job_id = ? AND account_generation = ? AND status = 'running' AND lease_token = ?"
        ).bind(status, next_attempt_at, error[:256], now, uid, job_id, generation, lease_token).run()
        if _changes(updated) != 1:
            return None
        return status
    except Exception:
        return None


async def _evaluate(
    request: Request,
    uid: str,
    generation: int,
    device_id: str | None,
    material_hint: str | None,
    *,
    expected_job_id: str | None = None,
    enqueue_on_failure: bool = True,
) -> dict[str, object] | JSONResponse:
    env = request.scope["env"]
    device_scope = device_id or "account"
    source_rows: list[tuple[str, dict[str, object]]] | None = None
    if expected_job_id is not None:
        try:
            job_input = await env.APP_DB.prepare(
                "SELECT account_generation, device_id, input_json FROM cf_task_intelligence_jobs "
                "WHERE uid = ? AND job_id = ?"
            ).bind(uid, expected_job_id).first()
        except Exception:
            return _error("task_intelligence_unavailable", 503)
        if not isinstance(job_input, dict):
            return _error("task_intelligence_job_not_found", 404)
        try:
            if int(job_input.get("account_generation")) != generation or str(job_input.get("device_id")) != device_scope:
                return _error("task_intelligence_job_mismatch", 409)
        except (TypeError, ValueError):
            return _error("task_intelligence_job_mismatch", 409)
        stored_input = _json(job_input.get("input_json"), {})
        source_rows = _source_rows_from_snapshot(
            stored_input.get("source_rows") if isinstance(stored_input, dict) else None
        )
        if source_rows is None:
            return _error("task_intelligence_job_input_invalid", 409)
        stored_hint = stored_input.get("material_hint") if isinstance(stored_input, dict) else None
        material_hint = stored_hint if isinstance(stored_hint, str) else None
    if source_rows is None:
        try:
            candidates = await env.APP_DB.prepare(
                "SELECT candidate_id, description, due_at, relevance_score, evidence_refs_json FROM cf_task_candidates "
                "WHERE uid = ? AND account_generation = ? AND status = 'pending' AND (device_id IS NULL OR device_id = ?) "
                "ORDER BY relevance_score IS NULL ASC, relevance_score DESC, due_at IS NULL ASC, due_at ASC, created_at DESC, candidate_id ASC LIMIT 12"
            ).bind(uid, generation, device_scope).all()
            tasks = await env.APP_DB.prepare(
                "SELECT id, description, due_at, provenance_json FROM cf_action_items WHERE uid = ? AND deleted = 0 AND completed = 0 "
                "ORDER BY due_at IS NULL ASC, due_at ASC, created_at DESC, id ASC LIMIT 12"
            ).bind(uid).all()
        except Exception:
            return _error("task_intelligence_unavailable", 503)
        candidate_rows = [row for row in (candidates.get("results", []) if isinstance(candidates, dict) else []) if isinstance(row, dict)]
        task_rows = [row for row in (tasks.get("results", []) if isinstance(tasks, dict) else []) if isinstance(row, dict)]
        source_rows = [("candidate", row) for row in candidate_rows] + [("task", row) for row in task_rows]
    now = int(time.time())
    expires = now + EVALUATION_TTL_SECONDS
    eval_fingerprint = _fingerprint({"uid": uid, "generation": generation, "device_id": device_scope, "material_hint": material_hint, "ids": [str(row.get("candidate_id") or row.get("id")) for _, row in source_rows]})
    evaluation_id = _stable_id("eval", uid, generation, eval_fingerprint)
    job_id = _stable_id("task-job", uid, generation, eval_fingerprint)
    if expected_job_id is not None and expected_job_id != job_id:
        return _error("task_intelligence_job_mismatch", 409)
    try:
        existing = await env.APP_DB.prepare(
            "SELECT projection_json FROM cf_task_evaluations WHERE uid = ? AND evaluation_id = ? AND expires_at > ?"
        ).bind(uid, evaluation_id, now).first()
    except Exception:
        return _error("task_intelligence_unavailable", 503)
    if isinstance(existing, dict):
        projection = _json(existing.get("projection_json"), None)
        try:
            await env.APP_DB.prepare(
                "UPDATE cf_task_intelligence_jobs SET status = 'completed', lease_token = NULL, lease_until = NULL, "
                "result_json = ?, updated_at = ? WHERE uid = ? AND job_id = ? AND account_generation = ? AND status <> 'completed'"
            ).bind(_dump(projection), now, uid, job_id, generation).run()
        except Exception:
            # The projection is already durable and can be read again.  A
            # deletion fence may intentionally reject this best-effort repair.
            pass
        return projection if isinstance(projection, dict) else _error("task_intelligence_unavailable", 503)
    lease_token = _stable_id("lease", uid, job_id, now)
    input_json = _dump(
        {
            "device_id": device_scope,
            "material_hint": material_hint,
            "source_rows": _source_snapshot(source_rows),
        }
    )
    try:
        await env.APP_DB.prepare(
            "INSERT INTO cf_task_intelligence_jobs (uid, job_id, account_generation, device_id, request_fingerprint, status, attempts, lease_token, lease_until, next_attempt_at, input_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'queued', 0, NULL, NULL, ?, ?, ?, ?) ON CONFLICT(uid, job_id) DO NOTHING"
        ).bind(uid, job_id, generation, device_scope, eval_fingerprint, now, input_json, now, now).run()
        claimed = await env.APP_DB.prepare(
            "UPDATE cf_task_intelligence_jobs SET status = 'running', attempts = attempts + 1, lease_token = ?, lease_until = ?, updated_at = ? "
            "WHERE uid = ? AND job_id = ? AND account_generation = ? AND ((status = 'queued' AND next_attempt_at <= ?) OR (status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?))"
        ).bind(lease_token, now + EVALUATION_LEASE_SECONDS, now, uid, job_id, generation, now, now).run()
        if _changes(claimed) != 1:
            current = await env.APP_DB.prepare(
                "SELECT status, attempts FROM cf_task_intelligence_jobs WHERE uid = ? AND job_id = ? AND account_generation = ?"
            ).bind(uid, job_id, generation).first()
            if isinstance(current, dict) and current.get("status") == "failed":
                return _error("task_intelligence_provider_unavailable", 503)
            return _error("task_intelligence_evaluation_in_progress", 202)
    except Exception:
        return _error("task_intelligence_unavailable", 503)
    descriptions = [str(row.get("description") or "").strip() for _, row in source_rows]
    why_now_result = await _llm_why_now(env, descriptions) if descriptions else ("No open task requires attention yet.", str(getattr(env, "WORKERS_AI_TASK_INTELLIGENCE_MODEL", WORKERS_AI_MODEL)))
    if why_now_result is None:
        state = await _record_evaluation_failure(
            env,
            uid,
            job_id,
            generation,
            lease_token,
            "workers AI unavailable",
        )
        if state is None:
            return _error("task_intelligence_evaluation_lease_lost", 409)
        retryable = state == "queued"
        queued = await _enqueue_evaluation(env, uid, job_id) if retryable and enqueue_on_failure else False
        return JSONResponse(
            {
                "error": "task_intelligence_provider_unavailable",
                "job_id": job_id,
                "status": state,
                "retryable": retryable,
                "queue_enqueued": queued,
            },
            status_code=503,
        )
    why_now, model = why_now_result
    recommendations: list[dict[str, object]] = []
    decisions: list[dict[str, object]] = []
    for index, (kind, row) in enumerate(source_rows[:MAX_RECOMMENDATIONS]):
        subject_id = str(row.get("candidate_id") or row.get("id") or "")
        intervention_id = _stable_id("intervention", uid, generation, "what_matters_now", subject_id)
        evidence = _json(row.get("evidence_refs_json") or row.get("provenance_json"), [])
        if not isinstance(evidence, list) or not evidence:
            evidence = [{"kind": "external", "id": f"cf-task-{subject_id}", "scope": "canonical"}]
        rec = {
            "intervention_id": intervention_id,
            "output_version": "what-matters-now.cf.v1",
            "subject_kind": "candidate" if kind == "candidate" else "task",
            "subject_id": subject_id,
            "feedback_subject_kind": "candidate" if kind == "candidate" else "task",
            "feedback_subject_id": subject_id,
            "destination_task_id": None if kind == "candidate" else subject_id,
            "destination_workstream_id": None,
            "headline": str(row.get("description") or "Suggested task")[:256],
            "why_now": why_now if index == 0 else "This open task is next in the due-date and priority order.",
            "goal_or_workstream_label": None,
            "recommended_action": "promote" if kind == "candidate" else "review",
            "alternative_action": "dismiss",
            "evidence_preview": str(row.get("description") or "")[:512],
            "evidence_refs": evidence[:MAX_EVIDENCE_REFS],
            "dedupe_key": _stable_id("dedupe", uid, generation, subject_id),
            "expires_at": _iso(expires),
        }
        recommendations.append(rec)
        decisions.append({"evaluation_id": evaluation_id, "subject_kind": rec["subject_kind"], "subject_id": subject_id, "model_version": model, "policy_version": "task-intelligence.cf.v1", "final_output_ref": intervention_id, "evaluated_at": _iso(now), "expires_at": _iso(expires)})
    projection = {"schema_version": 1, "evaluation_id": evaluation_id, "output_version": "what-matters-now.cf.v1", "material_version": "task-intelligence.cf.v1", "generated_at": _iso(now), "expires_at": _iso(expires), "recommendations": recommendations}
    response_hash = _fingerprint(projection)
    receipt_id = _stable_id("llm", uid, generation, evaluation_id)
    env = request.scope["env"]
    try:
        await env.APP_DB.batch(
            [
                env.APP_DB.prepare(
                    "INSERT INTO cf_task_evaluations (uid, evaluation_id, job_id, account_generation, device_id, request_fingerprint, projection_json, decisions_json, generated_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(uid, evaluation_id) DO UPDATE SET projection_json = excluded.projection_json, decisions_json = excluded.decisions_json, generated_at = excluded.generated_at, expires_at = excluded.expires_at"
                ).bind(uid, evaluation_id, job_id, generation, device_scope, eval_fingerprint, _dump(projection), _dump(decisions), now, expires),
                env.APP_DB.prepare(
                    "INSERT INTO cf_task_llm_receipts (uid, receipt_id, job_id, evaluation_id, account_generation, provider, model_version, request_fingerprint, response_fingerprint, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'workers-ai', ?, ?, ?, 'completed', ?) ON CONFLICT(uid, receipt_id) DO NOTHING"
                ).bind(uid, receipt_id, job_id, evaluation_id, generation, model, eval_fingerprint, response_hash, now),
            ]
        )
        completed = await env.APP_DB.prepare(
            "UPDATE cf_task_intelligence_jobs SET status = 'completed', lease_token = NULL, lease_until = NULL, last_error = NULL, result_json = ?, updated_at = ? "
            "WHERE uid = ? AND job_id = ? AND account_generation = ? AND status = 'running' AND lease_token = ?"
        ).bind(_dump(projection), now, uid, job_id, generation, lease_token).run()
        if _changes(completed) != 1:
            return _error("task_intelligence_evaluation_lease_lost", 409)
    except Exception:
        state = await _record_evaluation_failure(
            env,
            uid,
            job_id,
            generation,
            lease_token,
            "task intelligence result persistence unavailable",
        )
        if state is None:
            return _error("task_intelligence_unavailable", 503)
        queued = await _enqueue_evaluation(env, uid, job_id) if state == "queued" and enqueue_on_failure else False
        return JSONResponse(
            {
                "error": "task_intelligence_unavailable",
                "job_id": job_id,
                "status": state,
                "retryable": state == "queued",
                "queue_enqueued": queued,
            },
            status_code=503,
        )
    return projection


@router.post(TASK_INTELLIGENCE_PROCESSOR_PATH)
async def process_task_intelligence_evaluation(request: Request):
    context = _auth_context(request)
    if not context or context.get("authority") != "internal":
        return _error("unauthorized", 401)
    try:
        body = await _body(request)
        job_id = body.get("job_id")
        generation = int(body.get("account_generation"))
        device_id = body.get("device_id")
    except (ValueError, TypeError, json.JSONDecodeError):
        return _error("invalid_task_intelligence_job", 400)
    if not _valid_id(job_id) or not isinstance(device_id, str) or not _valid_id(device_id):
        return _error("invalid_task_intelligence_job", 400)
    uid = str(context["uid"])
    owned = await _owned_generation(request.scope["env"], uid)
    if isinstance(owned, JSONResponse):
        return owned
    if owned != generation:
        return _error("account_generation_mismatch", 409)
    return await _evaluate(
        request,
        uid,
        generation,
        device_id if device_id != "account" else None,
        None,
        expected_job_id=str(job_id),
        enqueue_on_failure=False,
    )


@router.post("/v1/what-matters-now/evaluate")
async def evaluate_what_matters_now(request: Request):
    parsed = _header_generation_and_auth(request)
    if isinstance(parsed, JSONResponse):
        return parsed
    _, uid, generation = parsed
    try:
        body = await _body(request)
        device_id = body.get("device_id") if isinstance(body, dict) else None
        material_hint = body.get("material_hint") if isinstance(body, dict) else None
        if device_id is not None and not _valid_id(device_id):
            raise ValueError
        if material_hint is not None and not _valid_id(material_hint):
            raise ValueError
    except (ValueError, TypeError, json.JSONDecodeError):
        return _error("invalid_evaluation_request", 400)
    bound = _device(request, device_id) if device_id is not None else None
    if isinstance(bound, JSONResponse):
        return bound
    return await _evaluate(request, uid, generation, bound, material_hint)


@router.get("/v1/what-matters-now")
async def get_what_matters_now(request: Request):
    context = _auth_context(request)
    if not context:
        return _error("unauthorized", 401)
    uid = str(context["uid"])
    generation = await _owned_generation(request.scope["env"], uid)
    if isinstance(generation, JSONResponse):
        return generation
    device = request.query_params.get("device_id")
    bound = _device(request, device) if device is not None else "account"
    if isinstance(bound, JSONResponse):
        return bound
    try:
        row = await request.scope["env"].APP_DB.prepare(
            "SELECT projection_json, expires_at FROM cf_task_evaluations WHERE uid = ? AND account_generation = ? AND device_id = ? AND expires_at > ? ORDER BY generated_at DESC LIMIT 1"
        ).bind(uid, generation, bound, int(time.time())).first()
    except Exception:
        return _error("task_intelligence_unavailable", 503)
    if not isinstance(row, dict):
        return _error("evaluation_not_found", 404)
    projection = _json(row.get("projection_json"), None)
    return projection if isinstance(projection, dict) else _error("task_intelligence_unavailable", 503)


@router.get("/v1/task-intelligence/debug/evaluations/{evaluation_id}")
async def get_evaluation_debug(request: Request, evaluation_id: str):
    context = _auth_context(request)
    if not context:
        return _error("unauthorized", 401)
    if request.headers.get("x-omi-debug", "").lower() not in {"1", "true", "yes"}:
        return _error("not_found", 404)
    uid = str(context["uid"])
    generation = await _owned_generation(request.scope["env"], uid)
    if isinstance(generation, JSONResponse):
        return generation
    try:
        row = await request.scope["env"].APP_DB.prepare(
            "SELECT evaluation_id, projection_json, decisions_json FROM cf_task_evaluations WHERE uid = ? AND evaluation_id = ? AND account_generation = ?"
        ).bind(uid, evaluation_id, generation).first()
    except Exception:
        return _error("task_intelligence_unavailable", 503)
    if not isinstance(row, dict):
        return _error("evaluation_not_found", 404)
    projection = _json(row.get("projection_json"), {})
    decisions = _json(row.get("decisions_json"), [])
    return {"projection": projection, "decisions": decisions}


__all__ = ["router"]
