"""Private mail transaction API; Jobs owns the native provider call."""

import asyncio
import json
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from typing import Literal

from brand_runtime import load_brand_runtime, load_share_origin
from assertion_path import raw_request_path
from internal_auth import verify_request_context
from share_email_contract import SendShareEmailRequest, _sender_display_name, build_summary_email
from share_recipient_contract import normalized_recipient_emails
from share_recipient_routes import owner_profile
import share_email_store as store

HEADERS = {'cache-control': 'private, no-store'}


class MailRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            try:
                return await handler(request)
            except HTTPException as error:
                return JSONResponse({'detail': error.detail}, status_code=error.status_code, headers=HEADERS)
            except RequestValidationError:
                raise
            except Exception:
                return JSONResponse({'detail': 'Share email temporarily unavailable'}, status_code=503, headers=HEADERS)

        return handle


router = APIRouter(route_class=MailRoute)


def owner(request: Request, response: Response):
    response.headers.update(HEADERS)
    context = verify_request_context(
        request.headers.get('x-omi-auth-context'),
        request.headers.get('x-omi-internal-signature'),
        getattr(request.scope['env'], 'INTERNAL_ASSERTION_SECRET', None),
        audience='api-core',
        method=request.method,
        path=raw_request_path(request.scope),
    )
    if context is None or context.get('authority') != 'internal':
        raise HTTPException(401, 'unauthorized')
    return context['uid']


@router.post('/internal/share-email/conversations/{conversation_id}/prepare')
async def prepare(request: Request, conversation_id: str, body: SendShareEmailRequest, uid=Depends(owner)):
    env = request.scope['env']
    await store.expire(env, uid, int(time.time()))
    row = await store.get_conversation(env, uid, conversation_id)
    recipients = normalized_recipient_emails(body.recipient_emails)
    if not recipients:
        raise HTTPException(400, 'A valid recipient email is required')
    profile = await asyncio.wait_for(owner_profile(env, uid), 15)
    brand, origin = load_brand_runtime(env), load_share_origin(env)
    structured = json.loads(row['structured_json'])
    sender = _sender_display_name({'display_name': profile.get('name'), 'email': profile.get('email')})
    content = build_summary_email(
        sender_name=sender,
        conversation_title=(structured.get('title') or '').strip(),
        overview=(structured.get('overview') or '').strip(),
        share_url=origin + '/conversations/' + quote(conversation_id, safe=''),
        brand_name=brand.display_name,
        brand_url=origin,
    )
    emails = normalized_recipient_emails([profile.get('email') or ''])
    return await store.prepare(
        env,
        uid,
        row,
        recipients,
        {
            **content,
            'sender_name': sender,
            'brand_name': brand.display_name,
            'reply_to': emails[0] if emails else None,
        },
    )


@router.post('/internal/share-email/dispatches/{dispatch_id}/claim')
async def claim(request: Request, dispatch_id: str, uid=Depends(owner)):
    return await store.claim(request.scope['env'], uid, dispatch_id)


class DeliveryResult(BaseModel):
    phase: Literal['sent', 'ambiguous', 'rejected']
    message_id: str | None = Field(default=None, max_length=512)


@router.post('/internal/share-email/dispatches/{dispatch_id}/finish')
async def finish(request: Request, dispatch_id: str, body: DeliveryResult, uid=Depends(owner)):
    return await store.finish(request.scope['env'], uid, dispatch_id, body.phase, body.message_id)
