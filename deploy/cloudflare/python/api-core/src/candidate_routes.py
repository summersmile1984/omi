"""Canonical Candidate lifecycle endpoints for authenticated Cloudflare accounts."""

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

import candidate_accept as acceptance
import candidate_create as creation
import candidate_read as reads
import candidate_resolve as resolution
from candidate_db import account_generation
from candidate_kernel_models import (
    CandidateCreate,
    CandidateMigrationRequest,
    CandidateResolutionRequest,
    CandidateStatus,
)
from candidate_kernel_policy import CandidateConflictError, CandidateGenerationMismatchError, CandidateNotFoundError
from internal_auth import decode_context

router = APIRouter()
MAX_BODY_BYTES = 96_000


def context(request):
    env = request.scope['env']
    return decode_context(
        request.headers.get('x-omi-auth-context'),
        request.headers.get('x-omi-internal-signature'),
        getattr(env, 'INTERNAL_ASSERTION_SECRET', None),
    )


def error_response(error):
    if isinstance(error, CandidateNotFoundError):
        return JSONResponse({'detail': 'Candidate or task not found'}, status_code=404)
    if isinstance(error, CandidateGenerationMismatchError):
        return JSONResponse({'detail': 'Account generation mismatch'}, status_code=409)
    if isinstance(error, CandidateConflictError):
        return JSONResponse({'detail': str(error)}, status_code=409)
    if isinstance(error, (ValueError, TypeError, ValidationError)):
        return JSONResponse({'detail': 'Invalid Candidate request'}, status_code=422)
    return JSONResponse({'detail': 'Candidate service unavailable'}, status_code=503)


async def body(request, model):
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError('Candidate request is too large')
    return model.model_validate_json(raw)


def generation_header(request):
    value = int(request.headers['x-account-generation']) if 'x-account-generation' in request.headers else -1
    if value < 0:
        raise ValueError('X-Account-Generation is required')
    return value


def idempotency_header(request):
    value = request.headers.get('idempotency-key', '')
    if not value.strip() or len(value) > 512:
        raise ValueError('Idempotency-Key is required')
    return value


@router.get('/v1/candidates')
async def list_candidates(request: Request):
    principal = context(request)
    if not principal:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    try:
        params = request.query_params
        limit, offset = int(params.get('limit', '100')), int(params.get('offset', '0'))
        surface = params.get('surface')
        status = CandidateStatus(params['status']) if 'status' in params else None
        if not 1 <= limit <= 500 or offset < 0 or surface not in {None, 'suggested'}:
            raise ValueError('invalid Candidate query')
        records, more = await reads.list_candidates(
            request.scope['env'], str(principal['uid']), status=status, limit=limit, offset=offset, surface=surface
        )
        return JSONResponse({'candidates': [creation.stored_record(record) for record in records], 'has_more': more})
    except Exception as error:
        return error_response(error)


@router.get('/v1/candidates/{candidate_id}')
async def get_candidate(request: Request, candidate_id: str):
    principal = context(request)
    if not principal:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    try:
        record = await reads.get_owned_candidate(request.scope['env'], str(principal['uid']), candidate_id)
        if record is None:
            raise CandidateNotFoundError(candidate_id)
        return JSONResponse(creation.stored_record(record))
    except Exception as error:
        return error_response(error)


@router.post('/v1/candidates')
async def create_candidate(request: Request):
    principal = context(request)
    if not principal:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    try:
        generation, key = generation_header(request), idempotency_header(request)
        proposal = await body(request, CandidateCreate)
        record = await creation.create_candidate(
            request.scope['env'], str(principal['uid']), proposal, idempotency_key=key, generation=generation
        )
        return JSONResponse(creation.stored_record(record))
    except Exception as error:
        return error_response(error)


@router.post('/v1/candidates/migrate-staged')
async def migrate_staged_candidates(request: Request):
    principal = context(request)
    if not principal:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    try:
        query = await body(request, CandidateMigrationRequest)
        env, uid = request.scope['env'], str(principal['uid'])
        generation = await account_generation(env, uid)
        rows = (
            await env.APP_DB.prepare(
                'SELECT candidate_id FROM cf_task_candidates WHERE uid=? AND candidate_id>? '
                'ORDER BY candidate_id LIMIT ?'
            )
            .bind(uid, query.after_id or '', query.limit)
            .all()
        )
        values = rows['results']
        if await account_generation(env, uid) != generation:
            raise CandidateGenerationMismatchError('account generation changed')
        return JSONResponse(
            {
                'workflow_mode': 'read',
                'account_generation': generation,
                'dry_run': True,
                'scanned': len(values),
                'created': 0,
                'reconciled': 0,
                'unchanged': len(values),
                'failed': 0,
                'failure_ids': [],
                'checkpoint': values[-1]['candidate_id'] if values else query.after_id,
            }
        )
    except Exception as error:
        return error_response(error)


@router.post('/v1/candidates/integrations/drain')
async def drain_candidate_integrations(request: Request):
    principal = context(request)
    if not principal:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    try:
        from candidate_integrations import drain

        limit = int(request.query_params.get('limit', '100'))
        if not 1 <= limit <= 500:
            raise ValueError('limit')
        count = await drain(request.scope['env'], principal['uid'], generation_header(request), limit)
        return JSONResponse({'scheduled': count})
    except Exception as error:
        return error_response(error)


@router.post('/v1/candidates/{candidate_id}/accept')
async def accept_candidate(request: Request, candidate_id: str):
    principal = context(request)
    if not principal:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    try:
        receipt = await acceptance.accept_candidate(
            request.scope['env'], str(principal['uid']), candidate_id, generation=generation_header(request)
        )
        return JSONResponse(receipt.model_dump(mode='json', exclude_none=True))
    except Exception as error:
        return error_response(error)


async def terminal_decision(request, candidate_id, status):
    principal = context(request)
    if not principal:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    try:
        generation = generation_header(request)
        resolution_request = await body(request, CandidateResolutionRequest)
        receipt = await resolution.resolve_candidate_without_mutation(
            request.scope['env'],
            str(principal['uid']),
            candidate_id,
            status=status,
            reason=resolution_request.reason,
            generation=generation,
        )
        return JSONResponse(receipt.model_dump(mode='json', exclude_none=True))
    except Exception as error:
        return error_response(error)


@router.post('/v1/candidates/{candidate_id}/reject')
async def reject_candidate(request: Request, candidate_id: str):
    return await terminal_decision(request, candidate_id, CandidateStatus.rejected)


@router.post('/v1/candidates/{candidate_id}/expire')
async def expire_candidate(request: Request, candidate_id: str):
    return await terminal_decision(request, candidate_id, CandidateStatus.expired)
