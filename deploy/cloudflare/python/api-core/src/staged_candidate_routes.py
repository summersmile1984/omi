"""Released staged-task routes over canonical Candidates and read-only history."""

from datetime import datetime, timezone
import hashlib
import json

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from candidate_accept import accept_candidate
from candidate_create import create_candidate, parse_record, stored_record, get_candidate
from candidate_db import CandidateTransaction, CandidateSnapshotChanged, account_generation
from candidate_kernel_models import CandidateCompatibilityMetadata, CandidateStatus
from candidate_kernel_policy import CandidateConflictError, CandidateGenerationMismatchError, CandidateNotFoundError
from candidate_kernel_staged import (
    CreateStagedTaskRequest,
    BatchUpdateScoresRequest,
    proposal_from_legacy_staged,
    _candidate_as_staged,
    _candidate_legacy_row_ids,
    _is_staged_compatibility_candidate,
    _legacy_as_staged,
    _staged_sort_key,
)
from candidate_read import parse_read
from candidate_resolve import resolve_candidate_without_mutation
from candidate_routes import body, context, error_response

router = APIRouter()


async def _scope(request):
    principal = context(request)
    if not principal:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    env, uid = request.scope['env'], str(principal['uid'])
    return env, uid, await account_generation(env, uid)


async def _generation_unchanged(env, uid, generation):
    if await account_generation(env, uid) != generation:
        raise CandidateGenerationMismatchError('task account generation changed')


def _historical_value(row):
    value = dict(row)
    value['id'] = value.pop('candidate_id')
    value['completed'] = value['status'] != 'pending'
    for field in ('created_at', 'updated_at', 'due_at'):
        if value.get(field) is not None:
            value[field] = datetime.fromtimestamp(value[field], timezone.utc)
    return value


async def _historical(env, uid, row_id=None):
    query = "SELECT * FROM cf_task_candidates WHERE uid=? AND status='pending'"
    args = [uid]
    if row_id is not None:
        query += ' AND candidate_id=?'
        args.append(row_id)
    result = await env.APP_DB.prepare(query + ' ORDER BY candidate_id').bind(*args).all()
    return [_historical_value(row) for row in result['results']]


async def _candidates(env, uid, generation):
    # Read all compatibility records, including terminal records that suppress
    # their historical evidence row even when cleanup is retried later.
    result, cursor = [], ''
    while True:
        page = (
            await env.APP_DB.prepare(
                'SELECT candidate_id,record_json FROM cf_candidates WHERE uid=? AND account_generation=? '
                'AND candidate_id>? ORDER BY candidate_id LIMIT 500'
            )
            .bind(uid, generation, cursor)
            .all()
        )
        rows = page['results']
        result.extend(
            record
            for row in rows
            if (record := parse_read(row['record_json'])) is not None and _is_staged_compatibility_candidate(record)
        )
        if len(rows) < 500:
            return result
        cursor = rows[-1]['candidate_id']


async def _projection(env, uid, generation):
    candidates = await _candidates(env, uid, generation)
    represented = set().union(*(_candidate_legacy_row_ids(candidate) for candidate in candidates))
    historical = [_legacy_as_staged(row) for row in await _historical(env, uid) if row['id'] not in represented]
    current = [
        _candidate_as_staged(candidate) for candidate in candidates if candidate.status == CandidateStatus.pending
    ]
    by_id = {str(item['id']): item for item in [*historical, *current]}
    await _generation_unchanged(env, uid, generation)
    return sorted(by_id.values(), key=_staged_sort_key)


async def _materialize(env, uid, row, generation):
    return await create_candidate(
        env, uid, proposal_from_legacy_staged(row), idempotency_key=f'legacy-staged:{row["id"]}', generation=generation
    )


async def _public_candidate(env, uid, candidate_id, generation):
    candidate = await get_candidate(env, uid, candidate_id)
    if (
        candidate is None
        or candidate.account_generation != generation
        or not _is_staged_compatibility_candidate(candidate)
    ):
        return None
    return candidate


async def _for_mutation(env, uid, public_id, generation):
    candidate = await _public_candidate(env, uid, public_id, generation)
    if candidate is not None:
        return candidate
    rows = await _historical(env, uid, public_id)
    return await _materialize(env, uid, rows[0], generation) if rows else None


async def _reject(env, uid, candidate, generation, reason):
    if candidate.status == CandidateStatus.pending:
        return await resolve_candidate_without_mutation(
            env, uid, candidate.candidate_id, status=CandidateStatus.rejected, reason=reason, generation=generation
        )


async def _retire(env, uid, candidate, generation):
    tx = CandidateTransaction(env, uid, generation)
    current = parse_record(await tx.record('candidates', candidate.candidate_id))
    if current is None or current.account_generation != generation:
        raise CandidateGenerationMismatchError('candidate unavailable during historical cleanup')
    if current.status == CandidateStatus.pending:
        raise CandidateConflictError('historical task needs a canonical decision before cleanup')
    for row_id in sorted(_candidate_legacy_row_ids(current)):
        tx.statements.append(
            tx.db.prepare('DELETE FROM cf_task_candidates WHERE uid=? AND candidate_id=?').bind(uid, row_id)
        )
    await tx.commit()


async def _score(env, uid, candidate_id, score, generation):
    for attempt in range(3):
        tx = CandidateTransaction(env, uid, generation)
        candidate = parse_record(await tx.record('candidates', candidate_id))
        if candidate is None:
            return
        if candidate.account_generation != generation:
            raise CandidateGenerationMismatchError('candidate account generation changed')
        if candidate.status != CandidateStatus.pending or not _is_staged_compatibility_candidate(candidate):
            return
        metadata = candidate.compatibility or CandidateCompatibilityMetadata()
        updated = candidate.model_copy(update={'compatibility': metadata.model_copy(update={'relevance_score': score})})
        tx.put('candidates', candidate_id, stored_record(updated))
        try:
            await tx.commit()
            return
        except CandidateSnapshotChanged:
            if attempt == 2:
                raise


async def _promote(env, uid, public_id, generation):
    candidate = await _for_mutation(env, uid, public_id, generation)
    if candidate is None or candidate.status in {CandidateStatus.rejected, CandidateStatus.expired}:
        raise CandidateNotFoundError('staged task not found or already closed')
    receipt = await accept_candidate(env, uid, candidate.candidate_id, generation=generation)
    await _retire(env, uid, candidate, generation)
    task = await CandidateTransaction(env, uid, generation).task(receipt.task_id)
    await _generation_unchanged(env, uid, generation)
    return {
        'promoted': True,
        'reason': None,
        'promoted_task': task or {'id': receipt.task_id, 'candidate_id': candidate.candidate_id},
    }


@router.post('/v1/staged-tasks')
async def create_staged_task(request: Request):
    try:
        scope = await _scope(request)
        if isinstance(scope, JSONResponse):
            return scope
        env, uid, generation = scope
        value = await body(request, CreateStagedTaskRequest)
        # Exact upstream identity payload: requests can retry without a new
        # client header while the resulting Candidate still owns idempotency.
        identity = value.model_dump(mode='python')
        identity['description'] = value.description.strip()
        identity['due_at'] = value.due_at.isoformat() if value.due_at else None
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(',', ':')).encode('utf-8')
        ).hexdigest()[:24]
        row = value.model_dump(mode='python') | {'id': f'compat-{digest}'}
        candidate = await _materialize(env, uid, row, generation)
        return JSONResponse(jsonable_encoder(_candidate_as_staged(candidate)))
    except Exception as error:
        return error_response(error)


@router.get('/v1/staged-tasks')
async def list_staged_tasks(request: Request):
    try:
        scope = await _scope(request)
        if isinstance(scope, JSONResponse):
            return scope
        limit, offset = int(request.query_params.get('limit', '100')), int(request.query_params.get('offset', '0'))
        if not 1 <= limit <= 1000 or offset < 0:
            raise ValueError('invalid staged task pagination')
        items = await _projection(*scope)
        return JSONResponse(
            jsonable_encoder({'items': items[offset : offset + limit], 'has_more': len(items) > offset + limit})
        )
    except Exception as error:
        return error_response(error)


@router.delete('/v1/staged-tasks')
async def clear_staged_tasks(request: Request):
    try:
        scope = await _scope(request)
        if isinstance(scope, JSONResponse):
            return scope
        env, uid, generation = scope
        candidates = await _candidates(*scope)
        by_legacy_id = {
            row_id: candidate for candidate in candidates for row_id in _candidate_legacy_row_ids(candidate)
        }
        counted = set()
        for candidate in candidates:
            if candidate.status == CandidateStatus.pending:
                await _reject(env, uid, candidate, generation, 'legacy_clear')
                counted.add(candidate.candidate_id)
        for row in await _historical(env, uid):
            candidate = by_legacy_id.get(row['id'])
            if candidate is None:
                candidate = await _materialize(env, uid, row, generation)
                await _reject(env, uid, candidate, generation, 'legacy_clear')
            await _retire(env, uid, candidate, generation)
            counted.add(candidate.candidate_id)
        await _generation_unchanged(*scope)
        return {'status': 'ok', 'deleted_count': len(counted)}
    except Exception as error:
        return error_response(error)


@router.delete('/v1/staged-tasks/{task_id}')
async def delete_staged_task(request: Request, task_id: str):
    try:
        scope = await _scope(request)
        if isinstance(scope, JSONResponse):
            return scope
        env, uid, generation = scope
        candidate = await _for_mutation(env, uid, task_id, generation)
        if candidate is not None:
            await _reject(env, uid, candidate, generation, 'legacy_delete')
            await _retire(env, uid, candidate, generation)
        await _generation_unchanged(*scope)
        return {'status': 'ok'}
    except Exception as error:
        return error_response(error)


@router.patch('/v1/staged-tasks/batch-scores')
async def update_staged_scores(request: Request):
    try:
        scope = await _scope(request)
        if isinstance(scope, JSONResponse):
            return scope
        env, uid, generation = scope
        value = await body(request, BatchUpdateScoresRequest)
        for entry in value.scores:
            candidate = await _for_mutation(env, uid, entry.id, generation)
            if candidate is not None:
                await _score(env, uid, candidate.candidate_id, entry.relevance_score, generation)
        await _generation_unchanged(*scope)
        return {'status': 'ok'}
    except Exception as error:
        return error_response(error)


@router.post('/v1/staged-tasks/promote')
async def promote_staged_task(request: Request):
    try:
        scope = await _scope(request)
        if isinstance(scope, JSONResponse):
            return scope
        env, uid, generation = scope
        items = await _projection(*scope)
        if not items:
            return {'promoted': False, 'reason': 'No staged tasks available', 'promoted_task': None}
        return JSONResponse(jsonable_encoder(await _promote(env, uid, items[0]['id'], generation)))
    except Exception as error:
        return error_response(error)


@router.post('/v1/staged-tasks/{task_id}/promote')
async def promote_staged_task_by_id(request: Request, task_id: str):
    try:
        scope = await _scope(request)
        if isinstance(scope, JSONResponse):
            return scope
        env, uid, generation = scope
        return JSONResponse(jsonable_encoder(await _promote(env, uid, task_id, generation)))
    except Exception as error:
        return error_response(error)
