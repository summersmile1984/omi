"""Signed Jobs-to-Core delivery for one durable recurrence receipt."""

import json
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from internal_auth import verify_request_context
from candidate_kernel_policy import CandidateGenerationMismatchError, CandidateNotFoundError
from recurrence_inbox import process_receipt

router = APIRouter()
PROCESSOR_PATH = '/internal/task-intelligence/recurrence'


@router.post(PROCESSOR_PATH)
async def process_recurrence(request: Request):
    env = request.scope['env']
    context = verify_request_context(
        request.headers.get('x-omi-auth-context'),
        request.headers.get('x-omi-internal-signature'),
        getattr(env, 'INTERNAL_ASSERTION_SECRET', None),
        audience='api-core',
        method=request.method,
        path=PROCESSOR_PATH,
    )
    if context is None or context.get('authority') != 'internal':
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    try:
        raw = await request.body()
        if len(raw) > 256:
            raise ValueError('body limit')
        body = json.loads(raw)
        if not isinstance(body, dict) or set(body) != {'receipt_id', 'account_generation'}:
            raise ValueError('body fields')
        identity, generation = body['receipt_id'], body['account_generation']
        if not isinstance(identity, str) or not 1 <= len(identity) <= 128 or '/' in identity:
            raise ValueError('receipt identity')
        if type(generation) is not int or not 0 <= generation <= 9_007_199_254_740_991:
            raise ValueError('generation')
    except (ValueError, TypeError):
        return JSONResponse({'error': 'invalid_recurrence_delivery'}, status_code=400)
    try:
        receipt = await process_receipt(env, context['uid'], generation, identity)
        return JSONResponse({'status': receipt.status.value})
    except CandidateNotFoundError:
        return JSONResponse({'error': 'recurrence_not_found'}, status_code=404)
    except CandidateGenerationMismatchError:
        return JSONResponse({'error': 'recurrence_generation_changed'}, status_code=409)
    except Exception:
        return JSONResponse({'error': 'recurrence_unavailable'}, status_code=503)
