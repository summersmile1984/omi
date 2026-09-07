"""Screenshot settings, survivor reads and revocation through the D1 owner."""

from datetime import datetime, timezone
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError

from fallback import record_fallback
from assertion_path import raw_request_path
from internal_auth import verify_request_context
from screen_frame_content import HEADERS, content_url
from screen_frames_contract import (
    ConversationScreenFrame,
    ConversationScreenFrameSet,
    ScreenFrameGround,
    ScreenFrameSettingsUpdateRequest,
    ScreenFrameSharingUpdateRequest,
)
from screen_frames_selection import _apply_cap_and_roles


class ScreenshotRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            try:
                response = await handler(request)
            except HTTPException as error:
                return JSONResponse({'detail': error.detail}, status_code=error.status_code, headers=HEADERS)
            except RequestValidationError:
                raise
            except Exception:
                return JSONResponse({'detail': 'Screenshots temporarily unavailable'}, status_code=503, headers=HEADERS)
            return response

        return handle


router = APIRouter(route_class=ScreenshotRoute)
EMPTY = ConversationScreenFrameSet(revision=0)
NEUTRAL_GROUND = {'stops': ['#5A5D66', '#33363D'], 'is_neutral': True}
FENCE = (
    'NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = ?) '
    'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = ?)'
)


def owner(request: Request) -> str:
    env = request.scope['env']
    context = verify_request_context(
        request.headers.get('x-omi-auth-context'),
        request.headers.get('x-omi-internal-signature'),
        getattr(env, 'INTERNAL_ASSERTION_SECRET', None),
        audience='api-core',
        method=request.method,
        path=raw_request_path(request.scope),
    )
    if context is None:
        raise HTTPException(401, 'unauthorized')
    return context['uid']


def _response(value):
    body = value.model_dump(mode='json') if isinstance(value, ConversationScreenFrameSet) else value
    return JSONResponse(body, headers=HEADERS)


def _degraded(*, neutral=False):
    record_fallback(
        from_mode='none',
        to_mode='system_default' if neutral else 'none',
        reason='malformed_doc',
        outcome='degraded',
    )


async def _owned(env, uid: str, conversation_id: str):
    row = (
        await env.APP_DB.prepare('SELECT id FROM cf_conversations WHERE uid = ? AND id = ? AND ' + FENCE)
        .bind(uid, conversation_id, uid, uid)
        .first()
    )
    if not row:
        raise HTTPException(404, 'Conversation not found')


async def _enabled(env, uid: str) -> bool:
    row = (
        await env.APP_DB.prepare(
            'SELECT COALESCE((SELECT enabled FROM cf_screen_frame_settings WHERE uid = ?), 1) AS enabled WHERE ' + FENCE
        )
        .bind(uid, uid, uid)
        .first()
    )
    return bool(row and row['enabled'] == 1)


async def read_set(env, uid: str, conversation_id: str):
    row = (
        await env.APP_DB.prepare(
            'SELECT * FROM cf_screen_frame_sets WHERE uid = ? AND conversation_id = ? AND ' + FENCE
        )
        .bind(uid, conversation_id, uid, uid)
        .first()
    )
    return row or {'revision': 0, 'epoch': 0, 'frames_json': '[]', 'adjudicated_at': None}


def frame_set(env, uid: str, row, *, access='owner') -> ConversationScreenFrameSet:
    expires = int(time.time()) + 3600
    banner, strip = None, []
    for doc in json.loads(row['frames_json']):
        try:
            ground = doc.get('ground')
            if not ground:
                _degraded(neutral=True)
                ground = NEUTRAL_GROUND
            frame = ConversationScreenFrame(
                id=doc['id'],
                captured_at=doc['captured_at'],
                role=doc.get('role') or 'strip',
                rank=int(doc.get('rank') or 0),
                caption=doc.get('caption') or '',
                labels=list(doc.get('labels') or []),
                source_badge=doc.get('source_badge'),
                focal_region=None,
                width=int(doc.get('width') or 0),
                height=int(doc.get('height') or 0),
                ground=ScreenFrameGround.model_validate(ground),
                content_url='',
                thumbnail_url='',
                url_expires_at=datetime.fromtimestamp(expires, timezone.utc),
            )
        except (ValidationError, KeyError, TypeError, ValueError, AttributeError):
            _degraded()
            continue
        frame.content_url = content_url(env, uid, frame.id, 'content', access, expires)
        frame.thumbnail_url = content_url(env, uid, frame.id, 'thumbnail', access, expires)
        if frame.captured_at.tzinfo is None:
            frame.captured_at = frame.captured_at.replace(tzinfo=timezone.utc)
        if frame.role == 'banner':
            banner = frame
        else:
            strip.append(frame)
    strip.sort(key=lambda item: item.captured_at)
    return ConversationScreenFrameSet(
        revision=row['revision'], banner=banner, strip=strip[:6], adjudicated_at=row['adjudicated_at']
    )


@router.get('/v1/screen-frame-egress/settings')
async def get_settings(request: Request, uid: str = Depends(owner)):
    return _response({'meeting_note_screenshots_enabled': await _enabled(request.scope['env'], uid)})


@router.patch('/v1/screen-frame-egress/settings')
async def update_settings(request: Request, body: ScreenFrameSettingsUpdateRequest, uid: str = Depends(owner)):
    await request.scope['env'].APP_DB.prepare(
        'INSERT INTO cf_screen_frame_settings (uid, enabled) VALUES (?, ?) '
        'ON CONFLICT(uid) DO UPDATE SET enabled = excluded.enabled'
    ).bind(uid, int(body.meeting_note_screenshots_enabled)).run()
    return _response({'meeting_note_screenshots_enabled': body.meeting_note_screenshots_enabled})


@router.get('/v1/conversations/{conversation_id}/screenshots')
async def get_screenshots(request: Request, conversation_id: str, uid: str = Depends(owner)):
    env = request.scope['env']
    await _owned(env, uid, conversation_id)
    row = await read_set(env, uid, conversation_id)
    if not await _enabled(env, uid):
        return _response(EMPTY)
    return _response(frame_set(env, uid, row))


@router.get('/v1/conversations/{conversation_id}/shared/screenshots')
async def get_shared_screenshots(request: Request, conversation_id: str):
    env = request.scope['env']
    row = (
        await env.APP_DB.prepare(
            'SELECT s.* FROM cf_shared_conversation_index i '
            'JOIN cf_conversations c ON c.uid = i.uid AND c.id = i.conversation_id '
            'JOIN cf_screen_frame_sets s ON s.uid = c.uid AND s.conversation_id = c.id '
            "WHERE i.conversation_id = ? AND i.visibility IN ('shared', 'public') "
            "AND c.visibility IN ('shared', 'public') AND s.sharing_enabled = 1 "
            'AND COALESCE((SELECT enabled FROM cf_screen_frame_settings WHERE uid = c.uid), 1) = 1 '
            'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = c.uid) '
            'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = c.uid)'
        )
        .bind(conversation_id)
        .first()
    )
    return _response(frame_set(env, row['uid'], row, access='shared') if row else EMPTY)


@router.patch('/v1/conversations/{conversation_id}/screenshot-sharing')
async def update_sharing(
    request: Request, conversation_id: str, body: ScreenFrameSharingUpdateRequest, uid: str = Depends(owner)
):
    env = request.scope['env']
    await _owned(env, uid, conversation_id)
    row = (
        await env.APP_DB.prepare(
            'INSERT INTO cf_screen_frame_sets (uid, conversation_id, sharing_enabled) VALUES (?, ?, ?) '
            'ON CONFLICT(uid, conversation_id) DO UPDATE SET sharing_enabled = excluded.sharing_enabled RETURNING *'
        )
        .bind(uid, conversation_id, int(body.enabled))
        .first()
    )
    return _response(frame_set(env, uid, row))


@router.delete('/v1/conversations/{conversation_id}/screenshots')
async def delete_all(request: Request, conversation_id: str, uid: str = Depends(owner)):
    env = request.scope['env']
    await _owned(env, uid, conversation_id)
    # Epoch cancellation and visibility revocation commit together. R2 cleanup
    # stays with the writer's durable receipts and scheduled retry owner.
    row = (
        await env.APP_DB.prepare(
            'INSERT INTO cf_screen_frame_sets (uid, conversation_id, revision, epoch) VALUES (?, ?, 1, 1) '
            "ON CONFLICT(uid, conversation_id) DO UPDATE SET frames_json = '[]', "
            'revision = revision + 1, epoch = epoch + 1 RETURNING *'
        )
        .bind(uid, conversation_id)
        .first()
    )
    return _response(frame_set(env, uid, row))


@router.delete('/v1/conversations/{conversation_id}/screenshots/{frame_id}')
async def delete_one(request: Request, conversation_id: str, frame_id: str, uid: str = Depends(owner)):
    env = request.scope['env']
    await _owned(env, uid, conversation_id)
    for _ in range(4):
        row = await read_set(env, uid, conversation_id)
        frames = json.loads(row['frames_json'])
        if not any(doc['id'] == frame_id for doc in frames):
            raise HTTPException(404, 'Screenshot not found')
        remaining = [doc for doc in frames if doc['id'] != frame_id]
        for doc in remaining:
            captured = datetime.fromisoformat(doc['captured_at'].replace('Z', '+00:00'))
            doc['captured_at'] = captured.replace(tzinfo=timezone.utc) if captured.tzinfo is None else captured
        survivors, _ = _apply_cap_and_roles(remaining, [], len(remaining))
        for doc in survivors:
            doc['captured_at'] = doc['captured_at'].isoformat()
        changed = (
            await env.APP_DB.prepare(
                'UPDATE cf_screen_frame_sets SET frames_json = ?, revision = revision + 1, epoch = epoch + 1 '
                'WHERE uid = ? AND conversation_id = ? AND revision = ? AND epoch = ? RETURNING *'
            )
            .bind(json.dumps(survivors), uid, conversation_id, row['revision'], row['epoch'])
            .first()
        )
        if changed:
            return _response(frame_set(env, uid, changed))
    raise HTTPException(503, 'Screenshot update in progress; retry')
