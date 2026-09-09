"""Internal Jobs transport for the canonical integration state owner."""

import json
from assertion_path import raw_request_path
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from internal_auth import verify_request_context
from candidate_kernel_policy import CandidateGenerationMismatchError
import candidate_integrations as integrations

router = APIRouter()
PATH = '/internal/candidates/integrations'


@router.post(PATH)
async def dispatch(request: Request):
    env = request.scope['env']
    context = verify_request_context(
        request.headers.get('x-omi-auth-context'),
        request.headers.get('x-omi-internal-signature'),
        getattr(env, 'INTERNAL_ASSERTION_SECRET', None),
        audience='api-core',
        method=request.method,
        path=raw_request_path(request.scope),
    )
    if context is None or context.get('authority') != 'internal':
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    try:
        raw = await request.body()
        if len(raw) > 2048:
            raise ValueError('body limit')
        value = json.loads(raw)
        action, identity, generation = value['action'], value['outbox_id'], value['account_generation']
        keys = {'action', 'outbox_id', 'account_generation'}
        if action in {'prepare', 'settle'}:
            keys |= {'lease_token', 'platform'}
            token, platform = value['lease_token'], value['platform']
            if not isinstance(token, str) or len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
                raise ValueError('token')
            if platform is not None and platform not in integrations.PLATFORMS:
                raise ValueError('platform')
        if action == 'settle':
            keys |= {'succeeded', 'external_id'}
            if type(value['succeeded']) is not bool or (
                value['external_id'] is not None
                and (not isinstance(value['external_id'], str) or len(value['external_id']) > 512)
            ):
                raise ValueError('result')
        if action not in {'schedule', 'prepare', 'settle'} or set(value) != keys:
            raise ValueError('fields')
        if not isinstance(identity, str) or not 1 <= len(identity) <= 128 or '/' in identity:
            raise ValueError('identity')
        if type(generation) is not int or not 0 <= generation <= 9_007_199_254_740_991:
            raise ValueError('generation')
    except (ValueError, TypeError, KeyError):
        return JSONResponse({'error': 'invalid_integration_delivery'}, status_code=400)
    try:
        args = (env, context['uid'], generation, identity)
        if action == 'schedule':
            result = {'scheduled': await integrations.schedule(*args)}
        elif action == 'prepare':
            result = await integrations.prepare(*args, token, platform)
        else:
            result = await integrations.settle(
                *args, token, platform=platform, succeeded=value['succeeded'], external_id=value['external_id']
            )
        return JSONResponse(result)
    except CandidateGenerationMismatchError:
        return JSONResponse({'error': 'integration_generation_changed'}, status_code=409)
    except Exception:
        return JSONResponse({'error': 'integration_unavailable'}, status_code=503)
