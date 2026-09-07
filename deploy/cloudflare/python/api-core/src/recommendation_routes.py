"""Authenticated original What Matters Now evaluation on the D1 product owner."""

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from candidate_db import account_generation
from candidate_kernel_policy import CandidateGenerationMismatchError, CandidateConflictError
from candidate_kernel_recommendation import EvaluationRequest
from candidate_routes import context, body, error_response
from recommendation_kernel import evaluate, get_debug_projection
from recommendation_llm import LiveJudgment, RecommendationInferenceError
from recommendation_store import RecommendationStore, EvaluationUnavailable
from task_intelligence_routes import _device

router = APIRouter()


def _bound(request, requested):
    if requested is not None or (request.headers.get('x-app-platform') and request.headers.get('x-device-id-hash')):
        return _device(request, requested)
    return None


async def _evaluate(request, value, *, enqueue=True, expected_job_id=None):
    principal = context(request)
    if not principal:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    env, uid = request.scope['env'], str(principal['uid'])
    generation = await account_generation(env, uid)
    store = RecommendationStore(env, uid, generation, value, enqueue=enqueue)
    try:
        result = await evaluate(uid, value, judgment=LiveJudgment(env), account_generation=generation, store=store)
        await store.guard(uid, generation)
        if expected_job_id is not None:
            await store.complete_dispatched_job(expected_job_id, result)
        return JSONResponse(result.model_dump(mode='json'))
    except EvaluationUnavailable as error:
        return JSONResponse(
            {
                'error': (
                    'task_intelligence_evaluation_in_progress'
                    if error.retryable
                    else 'task_intelligence_provider_unavailable'
                ),
                'job_id': error.job_id,
                'status': error.status,
                'retryable': error.retryable,
            },
            status_code=503,
        )
    except Exception as error:
        await store.fail_job('evaluation_failed')
        if isinstance(error, RecommendationInferenceError):
            return JSONResponse(
                {'error': 'task_intelligence_provider_unavailable', 'job_id': store.job_id}, status_code=503
            )
        if isinstance(error, (CandidateGenerationMismatchError, CandidateConflictError)):
            return error_response(error)
        return JSONResponse({'error': 'task_intelligence_unavailable'}, status_code=503)


@router.get('/v1/what-matters-now')
async def get_what_matters_now(request: Request):
    if not context(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    try:
        device = _bound(request, request.query_params.get('device_id'))
        if isinstance(device, JSONResponse):
            return device
        return await _evaluate(request, EvaluationRequest(device_id=device))
    except Exception as error:
        return error_response(error)


@router.post('/v1/what-matters-now/evaluate')
async def evaluate_what_matters_now(request: Request):
    if not context(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    try:
        value = await body(request, EvaluationRequest)
        device = _bound(request, value.device_id)
        if isinstance(device, JSONResponse):
            return device
        return await _evaluate(request, value.model_copy(update={'device_id': device}))
    except Exception as error:
        return error_response(error)


@router.get('/v1/task-intelligence/debug/evaluations/{evaluation_id}')
async def get_evaluation_debug(request: Request, evaluation_id: str):
    principal = context(request)
    if not principal:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    if request.headers.get('x-omi-debug', '').lower() not in {'1', 'true', 'yes'}:
        return JSONResponse({'error': 'not_found'}, status_code=404)
    try:
        device = _bound(request, request.query_params.get('device_id'))
        if isinstance(device, JSONResponse):
            return device
        env, uid = request.scope['env'], str(principal['uid'])
        generation = await account_generation(env, uid)
        store = RecommendationStore(env, uid, generation, EvaluationRequest(device_id=device))
        result = await get_debug_projection(
            uid, evaluation_id, device_id=device, account_generation=generation, store=store
        )
        await store.guard(uid, generation)
        return (
            JSONResponse(result.model_dump(mode='json'))
            if result
            else JSONResponse({'error': 'evaluation_not_found'}, status_code=404)
        )
    except Exception as error:
        return error_response(error)


@router.post('/internal/task-intelligence/evaluate')
async def process_task_intelligence_evaluation(request: Request):
    principal = context(request)
    if not principal or principal.get('authority') != 'internal':
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    try:
        data = json.loads(await request.body())
        job_id = data['job_id']
        if not isinstance(job_id, str) or not 1 <= len(job_id) <= 128:
            raise ValueError('invalid job')
        env, uid = request.scope['env'], str(principal['uid'])
        generation = await account_generation(env, uid)
        if type(data.get('account_generation')) is not int or data['account_generation'] != generation:
            raise CandidateGenerationMismatchError('job generation mismatch')
        row = (
            await env.APP_DB.prepare('SELECT * FROM cf_task_intelligence_jobs WHERE uid=? AND job_id=?')
            .bind(uid, job_id)
            .first()
        )
        if row is None:
            return JSONResponse({'error': 'evaluation_not_found'}, status_code=404)
        if row['account_generation'] != generation or row['device_id'] != data.get('device_id'):
            raise CandidateGenerationMismatchError('job scope mismatch')
        if row['status'] in {'completed', 'failed'}:
            return {'status': row['status']}
        stored = json.loads(row['input_json'])
        if 'request' not in stored:
            # Retired snapshots cannot be replayed as current canonical inputs.
            # Close the old durable job so Cron does not schedule it forever.
            from candidate_db import CandidateTransaction

            tx = CandidateTransaction(env, uid, generation)
            await tx.row('recommendation_jobs', job_id)
            tx.statements.append(
                tx.db.prepare(
                    "UPDATE cf_task_intelligence_jobs SET status='failed',lease_token=NULL,lease_until=NULL,last_error='retired_input_shape' WHERE uid=? AND job_id=?"
                ).bind(uid, job_id)
            )
            await tx.commit()
            return JSONResponse({'error': 'retired_evaluation_input'}, status_code=400)
        value = EvaluationRequest.model_validate(stored['request'])
        if (value.device_id or 'global') != row['device_id']:
            raise ValueError('invalid stored job scope')
        return await _evaluate(request, value, enqueue=False, expected_job_id=job_id)
    except Exception as error:
        return error_response(error)
