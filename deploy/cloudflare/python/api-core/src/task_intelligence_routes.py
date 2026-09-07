"""Device snapshots, outcomes, interventions and feedback for Cloudflare accounts.

Canonical recommendation evaluation lives in recommendation_routes; staged-task
operations use staged_candidate_routes and the shared Candidate lifecycle.
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
from candidate_attention import register_intervention, record_feedback
from candidate_kernel_recommendation import InterventionCreate, FeedbackCreate
from candidate_routes import error_response, generation_header, idempotency_header

router = APIRouter()

MAX_BODY_BYTES = 96_000
MAX_ID_LENGTH = 128
MAX_EVIDENCE_REFS = 50
SUPPORTED_PLATFORMS = frozenset({"android", "ios", "linux", "macos", "web", "windows"})
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
        fence = (
            await env.APP_DB.prepare(
                "SELECT 1 AS blocked FROM cf_account_deletion_intents WHERE uid = ? "
                "UNION ALL SELECT 1 AS blocked FROM cf_account_deletion_tombstones "
                "WHERE uid = ? AND expires_at > ? LIMIT 1"
            )
            .bind(uid, uid, int(time.time()))
            .first()
        )
        if isinstance(fence, dict):
            return _error("account_deletion_in_progress", 409)
        row = (
            await env.APP_DB.prepare(
                "SELECT state, checkpoint_phase, destination_backend_bound, account_generation "
                "FROM cf_account_cutover WHERE uid = ?"
            )
            .bind(uid)
            .first()
        )
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
        existing = (
            await env.APP_DB.prepare(
                f"SELECT {select}, request_fingerprint FROM {table} WHERE uid = ? AND {key_column} = ?"
            )
            .bind(uid, record_id)
            .first()
        )
        if isinstance(existing, dict):
            if existing.get("request_fingerprint") != fingerprint:
                return _error("idempotency_conflict", 409)
            return existing
        await env.APP_DB.prepare(
            f"INSERT INTO {table} ({columns}, request_fingerprint, created_at) VALUES ({','.join('?' for _ in values)}, ?, ?)"
        ).bind(*values, fingerprint, int(time.time())).run()
        stored = (
            await env.APP_DB.prepare(
                f"SELECT {select}, request_fingerprint FROM {table} WHERE uid = ? AND {key_column} = ?"
            )
            .bind(uid, record_id)
            .first()
        )
    except Exception:
        return _error("task_intelligence_unavailable", 503)
    return stored if isinstance(stored, dict) else _error("task_intelligence_unavailable", 503)


@router.post("/v1/task-intelligence/interventions")
async def create_intervention(request: Request):
    principal = _auth_context(request)
    if not principal:
        return _error("unauthorized", 401)
    try:
        generation, key = generation_header(request), idempotency_header(request)
        value = InterventionCreate.model_validate(await _body(request))
        record = await register_intervention(
            request.scope['env'],
            str(principal['uid']),
            value,
            idempotency_key=key,
            generation=generation,
        )
        return JSONResponse(record.model_dump(mode='json', exclude_none=True))
    except Exception as error:
        return error_response(error)


@router.post("/v1/task-intelligence/feedback")
async def create_feedback(request: Request):
    principal = _auth_context(request)
    if not principal:
        return _error("unauthorized", 401)
    try:
        generation, key = generation_header(request), idempotency_header(request)
        value = FeedbackCreate.model_validate(await _body(request))
        record = await record_feedback(
            request.scope['env'],
            str(principal['uid']),
            value,
            idempotency_key=key,
            generation=generation,
        )
        return JSONResponse(record.model_dump(mode='json', exclude_none=True))
    except Exception as error:
        return error_response(error)


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
        chain = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT 1 AS found FROM cf_task_interventions WHERE uid = ? AND attribution_chain_id = ? AND account_generation = ? "
                "UNION ALL SELECT 1 AS found FROM cf_task_feedback WHERE uid = ? AND attribution_chain_id = ? AND account_generation = ? LIMIT 1"
            )
            .bind(uid, body["attribution_chain_id"], generation, uid, body["attribution_chain_id"], generation)
            .first()
        )
        if not isinstance(chain, dict):
            return _error("attribution_chain_not_found", 404)
        occurred = _epoch(body.get("occurred_at")) if body.get("occurred_at") is not None else int(time.time())
        payload = {
            "attribution_chain_id": body["attribution_chain_id"],
            "subject_kind": body["subject_kind"],
            "subject_id": body["subject_id"],
            "outcome_code": body["outcome_code"],
        }
    except (ValueError, TypeError, json.JSONDecodeError):
        return _error("invalid_outcome", 400)
    fingerprint = _fingerprint(payload)
    outcome_id = _stable_id("outcome", uid, generation, idem)
    env = request.scope["env"]
    try:
        row = (
            await env.APP_DB.prepare(
                "SELECT outcome_id, attribution_chain_id, payload_json, occurred_at, request_fingerprint "
                "FROM cf_task_outcomes WHERE uid = ? AND outcome_id = ?"
            )
            .bind(uid, outcome_id)
            .first()
        )
        if isinstance(row, dict):
            if row.get("request_fingerprint") != fingerprint:
                return _error("idempotency_conflict", 409)
            return _outcome_response(row)
        await env.APP_DB.prepare(
            "INSERT INTO cf_task_outcomes (uid, outcome_id, account_generation, attribution_chain_id, "
            "request_fingerprint, payload_json, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
        ).bind(uid, outcome_id, generation, body["attribution_chain_id"], fingerprint, _dump(payload), occurred).run()
        row = (
            await env.APP_DB.prepare(
                "SELECT outcome_id, attribution_chain_id, payload_json, occurred_at, request_fingerprint "
                "FROM cf_task_outcomes WHERE uid = ? AND outcome_id = ?"
            )
            .bind(uid, outcome_id)
            .first()
        )
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


async def _ingest_snapshot(request: Request, *, open_loop: bool):
    from candidate_routes import body as typed_body
    from candidate_kernel_recommendation import NormalizedContextSnapshot, OpenLoopSnapshot
    from recommendation_snapshots import save_snapshot

    principal = _auth_context(request)
    if not principal:
        return _error('unauthorized', 401)
    try:
        generation, key = generation_header(request), idempotency_header(request)
        snapshot = await typed_body(request, OpenLoopSnapshot if open_loop else NormalizedContextSnapshot)
        device = _device(request, snapshot.device_id)
        if isinstance(device, JSONResponse):
            return device
        receipt = await save_snapshot(
            request.scope['env'],
            str(principal['uid']),
            generation,
            snapshot.model_copy(update={'device_id': device}),
            idempotency_key=key,
        )
        return JSONResponse(receipt.model_dump(mode='json'))
    except Exception as error:
        return error_response(error)


@router.put('/v1/task-intelligence/context-snapshot')
async def save_context_snapshot(request: Request):
    return await _ingest_snapshot(request, open_loop=False)


@router.put('/v1/task-intelligence/open-loop-snapshot')
async def save_open_loop_snapshot(request: Request):
    return await _ingest_snapshot(request, open_loop=True)


__all__ = ['router']
