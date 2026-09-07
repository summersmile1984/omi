"""Device snapshots, outcomes, interventions and feedback for Cloudflare accounts.

Canonical recommendation evaluation lives in recommendation_routes; staged-task
operations use staged_candidate_routes and the shared Candidate lifecycle.
"""

from __future__ import annotations

import json

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


@router.post('/v1/task-intelligence/outcomes')
async def create_outcome(request: Request):
    from candidate_routes import body as typed_body
    from candidate_kernel_recommendation import OutcomeCreate
    from recommendation_outcomes import record_outcome, AttributionChainNotFoundError

    principal = _auth_context(request)
    if not principal:
        return _error('unauthorized', 401)
    try:
        value = await typed_body(request, OutcomeCreate)
        record = await record_outcome(
            request.scope['env'],
            str(principal['uid']),
            generation_header(request),
            value,
            idempotency_key=idempotency_header(request),
        )
        return JSONResponse(record.model_dump(mode='json'))
    except AttributionChainNotFoundError:
        return JSONResponse({'detail': 'Attribution chain not found'}, status_code=404)
    except Exception as error:
        return error_response(error)


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
