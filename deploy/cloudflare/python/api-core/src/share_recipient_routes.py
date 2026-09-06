"""Calendar recipient suggestions from owned conversations and the Auth profile."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from fallback import record_fallback
from internal_auth import create_request_context, verify_request_context
from share_recipient_contract import ShareRecipientsResponse, extract_share_recipients, normalized_recipient_emails

HEADERS = {'cache-control': 'private, no-store'}


class ShareRecipientRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            try:
                return await handler(request)
            except HTTPException as error:
                return JSONResponse({'detail': error.detail}, status_code=error.status_code, headers=HEADERS)
            except Exception:
                return JSONResponse(
                    {'detail': 'Share recipients temporarily unavailable'}, status_code=503, headers=HEADERS
                )

        return handle


router = APIRouter(route_class=ShareRecipientRoute)


async def conversation(env, uid, conversation_id):
    row = (
        await env.APP_DB.prepare(
            'SELECT external_data_json, calendar_event_json, is_locked FROM cf_conversations c '
            'WHERE c.uid = ? AND c.id = ? '
            'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = c.uid) '
            'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = c.uid)'
        )
        .bind(uid, conversation_id)
        .first()
    )
    if not isinstance(row, dict):
        raise HTTPException(404, 'Conversation not found')
    if row['is_locked']:
        raise HTTPException(402, 'A paid plan is required to access this conversation.')
    return row


async def owner_profile(env, uid):
    signed = create_request_context(
        uid,
        getattr(env, 'INTERNAL_ASSERTION_SECRET', None),
        audience='auth',
        method='GET',
        path='/internal/profile',
        request_id='share-recipients',
    )
    if signed is None or getattr(env, 'AUTH', None) is None:
        raise ValueError('owner profile unavailable')
    response = await env.AUTH.fetch(
        'https://auth.internal/internal/profile',
        method='GET',
        headers={'x-omi-auth-context': signed[0], 'x-omi-internal-signature': signed[1]},
    )
    if int(response.status) == 410:
        raise HTTPException(401, 'unauthorized')
    if int(response.status) != 200:
        raise ValueError('owner profile unavailable')
    profile = await response.json()
    if not isinstance(profile, dict) or profile.get('uid') != uid:
        raise ValueError('owner profile identity mismatch')
    return profile


@router.get('/v1/conversations/{conversation_id}/share-recipients', response_model=ShareRecipientsResponse)
async def get_conversation_share_recipients(request: Request, conversation_id: str, response: Response):
    response.headers.update(HEADERS)
    env = request.scope['env']
    context = verify_request_context(
        request.headers.get('x-omi-auth-context'),
        request.headers.get('x-omi-internal-signature'),
        getattr(env, 'INTERNAL_ASSERTION_SECRET', None),
        audience='api-core',
        method=request.method,
        path=request.url.path,
    )
    if context is None:
        raise HTTPException(401, 'unauthorized')
    uid = context['uid']
    row = await conversation(env, uid, conversation_id)
    profile = await asyncio.wait_for(owner_profile(env, uid), 15)
    # Auth enrichment can yield while the user locks, deletes or unlinks the
    # conversation. Return only calendar data still present after that await.
    current = await conversation(env, uid, conversation_id)
    if current != row:
        raise HTTPException(409, 'Conversation changed. Try again.')
    owner_emails = normalized_recipient_emails([profile.get('email') or ''])
    if not owner_emails:
        record_fallback(
            component='auth',
            from_mode='share_recipients_proposed',
            to_mode='share_recipients_suppressed',
            reason='auth',
            outcome='degraded',
        )
        return {'recipients': []}
    data = {
        'external_data': json.loads(row['external_data_json']) if row['external_data_json'] else None,
        'calendar_event': json.loads(row['calendar_event_json']) if row['calendar_event_json'] else None,
    }
    return {'recipients': extract_share_recipients(data, owner_emails)}
