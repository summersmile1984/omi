"""One D1 owner for frame identity, dedupe, bounded delivery and transitions."""

from datetime import datetime, timezone
from enum import Enum
import hashlib

from fastapi import HTTPException

from frame_request_contract import FrameRequest, FrameRequestState, TERMINAL_FRAME_REQUEST_STATES
from frame_request_policy import FRAME_REQUEST_DEDUPE_WINDOW_SECONDS, request_expiry, validate_transition
from jit_authority import SNAPSHOT, authorize, bindings


def now():
    return datetime.now(timezone.utc)


def value(item):
    if isinstance(item, datetime):
        return item.timestamp()
    if isinstance(item, Enum):
        return item.value
    return item


async def get(env, uid, request_id, snapshot):
    row = (
        await env.APP_DB.prepare(
            'SELECT * FROM cf_frame_requests WHERE uid = ? AND request_id = ? AND (' + SNAPSHOT + ') = ?'
        )
        .bind(uid, request_id, *bindings(uid), snapshot)
        .first()
    )
    if not row:
        raise HTTPException(404, 'frame_request_not_found')
    return FrameRequest.model_validate(row)


async def create(env, uid, body):
    device, intent = body.device_id.strip(), body.dedupe_key.strip()
    if not device or not intent:
        raise HTTPException(400, 'uid, device_id, and dedupe_key are required')
    identity = hashlib.sha256(
        '\0'.join((intent, body.conversation_id or '', body.screenshot_id or '')).encode()
    ).hexdigest()
    for _ in range(5):
        snapshot = await authorize(env, uid, body.account_generation)
        created = now()
        existing = (
            await env.APP_DB.prepare(
                'SELECT * FROM cf_frame_requests WHERE uid = ? AND device_id = ? AND account_generation = ? '
                "AND dedupe_key = ? AND state IN ('requested','claimed','uploaded') AND expires_at > ? "
                'AND (' + SNAPSHOT + ') = ? ORDER BY attempt_number DESC LIMIT 1'
            )
            .bind(uid, device, body.account_generation, identity, created.timestamp(), *bindings(uid), snapshot)
            .first()
        )
        if existing:
            return FrameRequest.model_validate(existing), True
        attempt = (
            await env.APP_DB.prepare(
                'SELECT COALESCE(MAX(attempt_number),-1) + 1 AS number FROM cf_frame_requests '
                'WHERE uid = ? AND device_id = ? AND account_generation = ? AND dedupe_key = ?'
            )
            .bind(uid, device, body.account_generation, identity)
            .first()
        )
        number = attempt['number']
        window = int(created.timestamp()) // FRAME_REQUEST_DEDUPE_WINDOW_SECONDS
        request_id = (
            'frame-' + hashlib.sha256(f'{identity}:{body.account_generation}:{window}:{number}'.encode()).hexdigest()
        )
        frame = FrameRequest(
            uid=uid,
            request_id=request_id,
            device_id=device,
            account_generation=body.account_generation,
            dedupe_key=identity,
            dedupe_window=window,
            attempt_number=number,
            conversation_id=body.conversation_id,
            screenshot_id=body.screenshot_id,
            created_at=created,
            expires_at=request_expiry(
                created_at=created, requested_ttl_seconds=body.requested_ttl_seconds, device_retention_seconds=None
            ),
        )
        data = frame.model_dump()
        try:
            inserted = (
                await env.APP_DB.prepare(
                    'INSERT INTO cf_frame_requests ('
                    + ','.join(data)
                    + ') SELECT '
                    + ','.join('?' for _ in data)
                    + ' WHERE ('
                    + SNAPSHOT
                    + ') = ? AND NOT EXISTS (SELECT 1 FROM cf_frame_requests '
                    'WHERE uid = ? AND device_id = ? AND account_generation = ? AND dedupe_key = ? '
                    "AND state IN ('requested','claimed','uploaded') AND expires_at > ?) RETURNING *"
                )
                .bind(
                    *(value(item) for item in data.values()),
                    *bindings(uid),
                    snapshot,
                    uid,
                    device,
                    body.account_generation,
                    identity,
                    created.timestamp(),
                )
                .first()
            )
        except Exception as error:
            message = str(error)
            if 'pending_count' in message:
                raise HTTPException(400, 'frame request quota exceeded: pending_count') from error
            if 'cf_frame_requests.uid, cf_frame_requests.conversation_id' in message:
                # Re-read a possible identical winner before reporting the
                # one-conversation conflict. Both callers may have pre-read empty.
                winner = (
                    await env.APP_DB.prepare(
                        'SELECT * FROM cf_frame_requests WHERE uid = ? AND conversation_id = ? '
                        "AND state IN ('requested','claimed','uploaded','attached') AND (" + SNAPSHOT + ") = ?"
                    )
                    .bind(uid, frame.conversation_id, *bindings(uid), snapshot)
                    .first()
                )
                if (
                    winner
                    and winner['device_id'] == device
                    and winner['account_generation'] == body.account_generation
                    and winner['dedupe_key'] == identity
                    and winner['state'] != 'attached'
                    and winner['expires_at'] > created.timestamp()
                ):
                    return FrameRequest.model_validate(winner), True
                raise HTTPException(400, 'conversation already has an active frame request') from error
            if 'UNIQUE constraint failed' in message:
                continue
            raise
        if inserted:
            return FrameRequest.model_validate(inserted), False
    raise HTTPException(409, 'frame request authority changed; retry')


async def pending(env, uid, device_id, generation, limit):
    snapshot = await authorize(env, uid, generation)
    stamp = now().timestamp()
    # No external pixels are deleted by a polling request. The storage owner
    # will consume pending cleanup metadata when pixel support is qualified.
    await env.APP_DB.prepare(
        "UPDATE cf_frame_requests SET state = 'expired', terminal_reason = 'expired', "
        "cleanup_state = CASE WHEN storage_id IS NULL THEN cleanup_state ELSE 'pending' END, "
        'cleanup_next_attempt_at = CASE WHEN storage_id IS NULL THEN NULL ELSE ? END '
        'WHERE (uid,request_id) IN (SELECT uid,request_id FROM cf_frame_requests WHERE uid = ? '
        "AND account_generation = ? AND state IN ('requested','claimed','uploaded') AND expires_at <= ? "
        'ORDER BY expires_at LIMIT 32) AND (' + SNAPSHOT + ') = ?'
    ).bind(stamp, uid, generation, stamp, *bindings(uid), snapshot).run()
    rows = (
        await env.APP_DB.prepare(
            'SELECT * FROM cf_frame_requests WHERE uid = ? AND device_id = ? AND account_generation = ? '
            "AND state = 'requested' AND expires_at > ? AND ("
            + SNAPSHOT
            + ') = ? ORDER BY created_at,request_id LIMIT ?'
        )
        .bind(uid, device_id, generation, stamp, *bindings(uid), snapshot, limit)
        .all()
    )
    return [FrameRequest.model_validate(row) for row in rows['results']]


async def transition(env, uid, request_id, update):
    snapshot = await authorize(env, uid, update.account_generation)
    frame = await get(env, uid, request_id, snapshot)
    stamp = now()
    try:
        validate_transition(
            frame,
            next_state=update.state,
            uid=uid,
            device_id=update.device_id,
            account_generation=update.account_generation,
            now=stamp,
        )
    except PermissionError as error:
        raise HTTPException(403, 'frame_request_owner_mismatch') from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if update.state in {FrameRequestState.uploaded, FrameRequestState.attached}:
        # Only the pixel endpoints can atomically publish a storage receipt.
        # Client metadata alone cannot create uploaded or attached evidence.
        raise HTTPException(409, 'use_frame_upload_or_promotion_endpoint')
    terminal = update.state in TERMINAL_FRAME_REQUEST_STATES
    if update.storage_id or update.content_type or update.byte_count:
        raise HTTPException(409, 'storage metadata is only valid when uploading a frame')
    if terminal and not update.terminal_reason:
        raise HTTPException(409, 'terminal frame requests require a bounded reason')
    if not terminal and update.terminal_reason:
        raise HTTPException(409, 'active frame requests must not carry a terminal reason')
    updated = (
        await env.APP_DB.prepare(
            'UPDATE cf_frame_requests SET state = ?, terminal_reason = ?, '
            "claimed_at = CASE WHEN ? = 'claimed' THEN ? ELSE claimed_at END, "
            "cleanup_state = CASE WHEN ? AND storage_id IS NOT NULL THEN 'pending' ELSE cleanup_state END, "
            'cleanup_next_attempt_at = CASE WHEN ? AND storage_id IS NOT NULL THEN ? ELSE cleanup_next_attempt_at END '
            'WHERE uid = ? AND request_id = ? AND state = ? AND expires_at > ? AND (' + SNAPSHOT + ') = ? RETURNING *'
        )
        .bind(
            update.state.value,
            update.terminal_reason,
            update.state.value,
            stamp.timestamp(),
            int(terminal),
            int(terminal),
            stamp.timestamp(),
            uid,
            request_id,
            frame.state.value,
            stamp.timestamp(),
            *bindings(uid),
            snapshot,
        )
        .first()
    )
    if not updated:
        raise HTTPException(409, 'frame request authority changed; retry')
    return FrameRequest.model_validate(updated)
