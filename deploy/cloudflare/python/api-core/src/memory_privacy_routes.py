"""Private continuation endpoints for the existing Jobs service binding."""

import time
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from internal_auth import verify_request_context
from memory_privacy_apply import prepare_privacy_deletion
from memory_privacy_finalize import finalize_privacy_deletion
from memory_privacy_delete import continue_memory_scope


class PrivacyRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe(request):
            try:
                return await handler(request)
            except (HTTPException, RequestValidationError):
                raise
            except Exception as error:
                code = (
                    'memory_cleanup_pending'
                    if 'memory_privacy_provider_pending' in str(error)
                    else 'memory_deletion_unavailable'
                )
                return JSONResponse({'error': code}, status_code=503, headers={'cache-control': 'no-store'})

        return safe


router = APIRouter(route_class=PrivacyRoute)


class Continuation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(pattern=r'^[0-9a-f]{64}$')


def owner(request: Request):
    context = verify_request_context(
        request.headers.get('x-omi-auth-context'),
        request.headers.get('x-omi-internal-signature'),
        request.scope['env'].INTERNAL_ASSERTION_SECRET,
        audience='api-core',
        method=request.method,
        path=request.url.path,
    )
    if not context or context.get('authority') != 'internal':
        raise HTTPException(404, 'Not found')
    return context['uid']


@router.post('/internal/memory-privacy/resume', include_in_schema=False)
async def resume(request: Request, body: Continuation, uid: str = Depends(owner)):
    env = request.scope['env']
    pending = (
        await env.APP_DB.prepare('SELECT * FROM cf_memory_privacy_deletions WHERE uid = ? AND token = ?')
        .bind(uid, body.token)
        .first()
    )
    if pending is None:
        return Response(status_code=204, headers={'cache-control': 'no-store'})
    result = await prepare_privacy_deletion(env, uid, json.loads(pending['requested_ids_json']), int(time.time()))
    return JSONResponse({'targets': json.loads(result['targets_json'])}, headers={'cache-control': 'no-store'})


@router.post('/internal/memory-privacy/finalize', include_in_schema=False)
async def finalize(request: Request, body: Continuation, uid: str = Depends(owner)):
    env = request.scope['env']
    if (
        await env.APP_DB.prepare('SELECT uid FROM cf_memory_privacy_deletions WHERE uid = ? AND token = ?')
        .bind(uid, body.token)
        .first()
        is None
    ):
        return Response(status_code=204, headers={'cache-control': 'no-store'})
    await finalize_privacy_deletion(env, uid, body.token, int(time.time()))
    return Response(status_code=204, headers={'cache-control': 'no-store'})


@router.post('/internal/memory-privacy/scope', include_in_schema=False)
async def continue_scope(request: Request, body: Continuation, uid: str = Depends(owner)):
    if await continue_memory_scope(request.scope['env'], uid, body.token):
        return Response(status_code=204, headers={'cache-control': 'no-store'})
    return JSONResponse({'error': 'memory_cleanup_pending'}, status_code=503, headers={'cache-control': 'no-store'})
