"""Server-owned screenshot admission, exact-byte judging and publication."""

import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request

from screen_frame_adjudication_store import current, failure, publish, reserve
from screen_frame_content import _encode, signing_secret
from screen_frame_image import canonicalize_screen_frame
from screen_frame_judge import SCREEN_FRAME_MODEL, JudgeFailure, judge
from screen_frame_views import FENCE, ScreenshotRoute, _enabled, _response, owner
from screen_frames_admission import (
    IDEMPOTENCY_TTL_SECONDS,
    MAX_CANDIDATE_DECODED_BYTES,
    _ensure_aware,
    _request_fingerprint,
    _validate_capture_window,
)
from screen_frames_canonical import ScreenFrameCanonicalizationError
from screen_frames_contract import ScreenFrameAdjudicationRequest
from screen_frames_palette import compute_ground
from screen_frames_policy import get_purpose_policy
from screen_frames_transport import ScreenFrameDigestMismatch, decode_and_verify_transport_digest

router = APIRouter(route_class=ScreenshotRoute)


async def available(env):
    if str(getattr(env, 'SCREEN_FRAME_EGRESS_ENABLED', '')).strip().lower() != 'true':
        return False
    if any(getattr(env, name, None) is None for name in ('AI', 'IMAGES', 'SCREEN_FRAME_WRITER')):
        return False
    try:
        signing_secret(env)
        response = await asyncio.wait_for(
            env.SCREEN_FRAME_WRITER.fetch('https://screen-frame-writer/ready'), timeout=15
        )
        return response.status == 200
    except Exception:
        return False


def approval(env, attempt, candidate, canonical, judgement, policy):
    now = int(time.time())
    metadata = {
        'captured_at': _ensure_aware(candidate.captured_at).isoformat(),
        'caption': judgement.caption,
        'labels': list(judgement.labels),
        'source_badge': judgement.source_badge,
        'banner_suitability': judgement.banner_suitability,
        'width': canonical.width,
        'height': canonical.height,
        'ground': compute_ground(canonical.jpeg_bytes).model_dump(),
    }
    claims = {
        'version': 1,
        'iss': 'omi-screen-frame-adjudicator',
        'aud': 'omi-screen-frame-writer',
        'jti': str(uuid4()),
        'uid': attempt['uid'],
        'conversation_id': attempt['conversation_id'],
        'attempt_id': attempt['attempt_id'],
        'epoch': attempt['epoch'],
        'purpose': 'meeting_note_v1',
        'retention': policy.retention.value,
        'decision': judgement.outcome,
        'model': SCREEN_FRAME_MODEL,
        'policy_version': policy.policy_version,
        'prompt_version': policy.prompt_version,
        'issued_at': now,
        'expires_at': now + 600,
        'canonical_sha256': canonical.sha256_hex,
        'thumbnail_sha256': hashlib.sha256(canonical.thumbnail_jpeg_bytes).hexdigest(),
        'metadata': metadata,
    }
    message = 'screen-frame-approval-v1.' + _encode(json.dumps(claims, separators=(',', ':')).encode())
    token = message + '.' + _encode(hmac.new(signing_secret(env), message.encode(), hashlib.sha256).digest())
    return claims, token


async def write(env, claims, token, canonical):
    try:
        response = await asyncio.wait_for(
            env.SCREEN_FRAME_WRITER.fetch(
                'https://screen-frame-writer/internal/screen-frames/write',
                method='POST',
                headers={'content-type': 'application/json'},
                body=json.dumps(
                    {
                        'approval': token,
                        'jpeg_base64': base64.b64encode(canonical.jpeg_bytes).decode('ascii'),
                        'thumbnail_base64': base64.b64encode(canonical.thumbnail_jpeg_bytes).decode('ascii'),
                    }
                ),
            ),
            timeout=30,
        )
        if response.status != 201 or (await response.json()).get('frame_id') != claims['jti']:
            raise ValueError('writer rejected approved frame')
    except Exception as error:
        raise failure('writer_unavailable') from error


@router.post('/v1/screen-frame-egress/adjudications')
async def adjudicate(request: Request, body: ScreenFrameAdjudicationRequest, uid: str = Depends(owner)):
    env = request.scope['env']
    if not await available(env):
        raise failure('screen_frame_egress_unavailable', 409)
    policy = get_purpose_policy(body.purpose)
    if policy is None:
        raise failure('unknown_purpose', 400)
    if body.subject.kind != policy.subject_kind:
        raise failure('unsupported_subject_kind', 400)
    conversation = (
        await env.APP_DB.prepare(
            'SELECT status, started_at, finished_at FROM cf_conversations WHERE uid = ? AND id = ? AND ' + FENCE
        )
        .bind(uid, body.subject.id, uid, uid)
        .first()
    )
    if not conversation:
        raise HTTPException(404, 'Conversation not found')
    if conversation['status'] != 'completed':
        raise failure('conversation_not_completed', 409)
    if not await _enabled(env, uid):
        raise failure('meeting_note_screenshots_disabled', 409)
    _validate_capture_window(
        {
            name: datetime.fromtimestamp(conversation[name], timezone.utc) if conversation[name] is not None else None
            for name in ('started_at', 'finished_at')
        },
        body.candidates,
    )
    for candidate in body.candidates:
        if len(candidate.bytes_base64) * 3 // 4 > MAX_CANDIDATE_DECODED_BYTES:
            raise failure_with_candidate('candidate_too_large', candidate, 413)
    # Verify every candidate before the first external model call. Decode again
    # when processing to avoid retaining a second complete binary batch. The
    # original duplicate-client-id behavior uses the last candidate's bytes.
    by_id = {}
    for candidate in body.candidates:
        try:
            decode_and_verify_transport_digest(candidate)
        except ScreenFrameDigestMismatch as error:
            raise failure_with_candidate(str(error), candidate, 400) from error
        by_id[candidate.client_frame_id] = candidate
    attempt, replay = await reserve(env, uid, body, _request_fingerprint(body), IDEMPOTENCY_TTL_SECONDS)
    if replay is not None:
        return _response(replay.model_dump(mode='json'))
    new_docs = []
    for candidate in body.candidates:
        if not await current(env, attempt):
            raise failure('screen_frame_adjudication_cancelled', 409)
        try:
            canonical = await canonicalize_screen_frame(
                env, decode_and_verify_transport_digest(by_id[candidate.client_frame_id])
            )
        except ScreenFrameCanonicalizationError:
            continue
        try:
            judgement = await judge(env, attempt, canonical.jpeg_bytes)
        except JudgeFailure:
            continue
        if judgement.outcome != 'approved_clean':
            continue
        if not await current(env, attempt):
            raise failure('screen_frame_adjudication_cancelled', 409)
        claims, token = approval(env, attempt, candidate, canonical, judgement, policy)
        await write(env, claims, token, canonical)
        new_docs.append(
            {
                **claims['metadata'],
                'captured_at': _ensure_aware(candidate.captured_at),
                'id': claims['jti'],
                'focal_region': None,
                'canonical_sha256': canonical.sha256_hex,
                'created_at': datetime.now(timezone.utc).isoformat(),
            }
        )
    result = await publish(env, attempt, new_docs, policy.max_persisted)
    return _response(result.model_dump(mode='json'))


def failure_with_candidate(code, candidate, status):
    return HTTPException(status, {'code': code, 'client_frame_id': candidate.client_frame_id})
