"""Signed Jobs-to-Core entry point for the durable consolidation dispatcher."""

import json
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from internal_auth import verify_request_context
from memory_consolidation_dispatch import DispatchChanged, process_consolidation_dispatch

router = APIRouter()
PROCESSOR_PATH = '/internal/memory/consolidation'


@router.post(PROCESSOR_PATH)
async def process_memory_consolidation(request: Request):
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
    raw = await request.body()
    try:
        if len(raw) > 256:
            raise ValueError('body limit')
        body = json.loads(raw)
        if not isinstance(body, dict) or set(body) != {'account_generation'}:
            raise ValueError('body fields')
        generation = body['account_generation']
        if type(generation) is not int or not 0 <= generation <= 9_007_199_254_740_991:
            raise ValueError('generation')
    except (ValueError, TypeError):
        return JSONResponse({'error': 'invalid_consolidation_delivery'}, status_code=400)
    try:
        return await process_consolidation_dispatch(env, context['uid'], generation)
    except DispatchChanged:
        return JSONResponse({'error': 'consolidation_ownership_changed'}, status_code=409)
    except Exception:
        return JSONResponse({'error': 'consolidation_unavailable'}, status_code=503)
