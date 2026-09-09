"""Multipart pixel ownership and atomic metadata publication in App D1."""

import hashlib
import re
import time
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

import frame_request_store as frames
from frame_request_contract import FrameRequestState
from frame_request_policy import validate_transition
from jit_authority import SNAPSHOT, authorize, bindings
from screen_frame_content import HEADERS, _chunks


def bucket(env, tier):
    value = getattr(env, 'FRAME_REQUESTS_TEMPORARY' if tier == 'temporary' else 'FRAME_REQUESTS', None)
    if value is None:
        raise HTTPException(503, 'frame_request_storage_unavailable')
    return value


def key(row):
    uid, object_id = row['uid'], row['object_id']
    if not uid or '/' in uid or '\\' in uid or not re.fullmatch('[a-f0-9]{32}', object_id):
        raise ValueError('invalid frame storage identity')
    return f'frame-requests/{uid}/{object_id}.jpg'


async def owned(env, uid, request_id, device, generation):
    snapshot = await authorize(env, uid, generation)
    frame = await frames.get(env, uid, request_id, snapshot)
    if frame.device_id != device or frame.account_generation != generation:
        raise HTTPException(403, 'frame_request_owner_mismatch')
    return frame, snapshot


async def reserve(env, frame, snapshot, payload, tier):
    stamp = int(time.time())
    object_id = uuid4().hex
    row = {
        'object_id': object_id,
        'uid': frame.uid,
        'request_id': frame.request_id,
        'storage_id': (
            'temporary-' + uuid4().hex
            if tier == 'temporary'
            else 'permanent-' + frame.request_id.removeprefix('frame-')
        ),
        'tier': tier,
        'account_generation': frame.account_generation,
        'authority_snapshot': snapshot,
        'sha256': hashlib.sha256(payload).hexdigest(),
        'byte_count': len(payload),
        'created_at': stamp,
        'write_expires_at': stamp + 120,
    }
    key(row)
    result = (
        await env.APP_DB.prepare(
            'INSERT INTO cf_frame_objects ('
            + ','.join(row)
            + ') SELECT '
            + ','.join('?' for _ in row)
            + ' WHERE ('
            + SNAPSHOT
            + ') = ? RETURNING object_id'
        )
        .bind(*row.values(), *bindings(frame.uid), snapshot)
        .first()
    )
    if not result:
        raise HTTPException(409, 'frame request authority changed; retry')
    return row


async def alive(env, row, phase='writing'):
    result = (
        await env.APP_DB.prepare(
            'SELECT 1 AS active FROM cf_frame_objects o JOIN cf_frame_requests f '
            'ON f.uid=o.uid AND f.request_id=o.request_id WHERE o.object_id=? AND o.phase=? '
            'AND o.write_expires_at>unixepoch() AND (' + SNAPSHOT + ') = o.authority_snapshot '
            "AND ((o.tier='temporary' AND f.state='claimed' AND f.expires_at>unixepoch()) "
            "OR (o.tier='permanent' AND f.state='uploaded' AND EXISTS (SELECT 1 FROM cf_conversations c "
            'WHERE c.uid=f.uid AND c.id=f.conversation_id)))'
        )
        .bind(row['object_id'], phase, *bindings(row['uid']))
        .first()
    )
    if not result:
        raise HTTPException(409, 'frame request authority changed; retry')


async def discard(env, row):
    # Never convert an ambiguously committed live image to cleanup. D1 owns
    # publication; its frame/conversation deletion triggers own live erasure.
    await env.APP_DB.prepare(
        "UPDATE cf_frame_objects SET phase='cleanup',next_attempt_at=0 WHERE object_id=? AND phase IN ('writing','ready')"
    ).bind(row['object_id']).run()


async def write(env, row, payload):
    target = bucket(env, row['tier'])
    await alive(env, row)
    upload = await target.createMultipartUpload(key(row), {'httpMetadata': {'contentType': 'image/jpeg'}})
    upload_id = upload.uploadId
    try:
        registered = (
            await env.APP_DB.prepare(
                "UPDATE cf_frame_objects SET upload_id=? WHERE object_id=? AND phase='writing' AND upload_id IS NULL RETURNING object_id"
            )
            .bind(upload_id, row['object_id'])
            .first()
        )
        if not registered:
            await upload.abort()
            raise HTTPException(409, 'frame request authority changed; retry')
        # The registration response may be lost after commit. Do not abort or
        # delete blindly; the durable handle and Jobs cleanup converge it.
        await alive(env, row)
        part = await upload.uploadPart(1, payload)
        await alive(env, row)
        etag = part['etag'] if isinstance(part, dict) else part.etag
        await upload.complete([{'partNumber': 1, 'etag': etag}])
        await alive(env, row)
        ready = (
            await env.APP_DB.prepare(
                "UPDATE cf_frame_objects SET phase='ready' WHERE object_id=? AND phase='writing' "
                'AND write_expires_at>unixepoch() AND (' + SNAPSHOT + ') = authority_snapshot RETURNING object_id'
            )
            .bind(row['object_id'], *bindings(row['uid']))
            .first()
        )
        if not ready:
            raise HTTPException(409, 'frame request authority changed; retry')
    except Exception:
        await discard(env, row)
        raise


async def publish(env, row):
    try:
        await alive(env, row, 'ready')
        result = (
            await env.APP_DB.prepare(
                "UPDATE cf_frame_objects SET phase='live' WHERE object_id=? AND phase='ready' "
                'AND write_expires_at>unixepoch() AND (' + SNAPSHOT + ') = authority_snapshot RETURNING object_id'
            )
            .bind(row['object_id'], *bindings(row['uid']))
            .first()
        )
        if not result:
            raise HTTPException(409, 'frame request authority changed; retry')
    except Exception as error:
        # An error can follow a committed transaction. Only the live reference
        # proves publication; retain it and return the original request below.
        snapshot = await authorize(env, row['uid'], row['account_generation'])
        frame = await frames.get(env, row['uid'], row['request_id'], snapshot)
        if frame.storage_id != row['storage_id'] or frame.state not in {
            FrameRequestState.uploaded,
            FrameRequestState.attached,
        }:
            await discard(env, row)
            if 'quota exceeded' in str(error) or 'photo id is already used' in str(error):
                raise HTTPException(409, 'frame evidence quota or photo identity conflict') from error
            raise
        await discard(env, row)  # A concurrent deterministic promotion may own another object.
        return frame
    snapshot = await authorize(env, row['uid'], row['account_generation'])
    return await frames.get(env, row['uid'], row['request_id'], snapshot)


async def upload(env, uid, request_id, device, generation, payload):
    frame, snapshot = await owned(env, uid, request_id, device, generation)
    try:
        validate_transition(
            frame,
            next_state=FrameRequestState.uploaded,
            uid=uid,
            device_id=device,
            account_generation=generation,
            now=frames.now(),
        )
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    row = await reserve(env, frame, snapshot, payload, 'temporary')
    await write(env, row, payload)
    return await publish(env, row)


async def live_object(env, frame):
    row = (
        await env.APP_DB.prepare(
            "SELECT * FROM cf_frame_objects WHERE uid=? AND request_id=? AND storage_id=? AND phase='live'"
        )
        .bind(frame.uid, frame.request_id, frame.storage_id)
        .first()
    )
    if not row:
        raise HTTPException(404, 'frame_request_pixels_unavailable')
    return row


async def promote(env, uid, request_id, promotion):
    frame, snapshot = await owned(env, uid, request_id, promotion.device_id, promotion.account_generation)
    if frame.conversation_id != promotion.conversation_id:
        raise HTTPException(403, 'frame_request_owner_mismatch')
    if frame.state == FrameRequestState.attached:
        return frame
    if frame.state != FrameRequestState.uploaded or not frame.storage_id:
        raise HTTPException(409, 'only uploaded frame requests may be promoted')
    source = await live_object(env, frame)
    stored = await bucket(env, 'temporary').get(key(source))
    if not stored:
        raise HTTPException(404, 'frame_request_pixels_unavailable')
    payload = bytes(await stored.arrayBuffer())
    if len(payload) != source['byte_count'] or hashlib.sha256(payload).hexdigest() != source['sha256']:
        raise HTTPException(503, 'frame_request_pixels_unavailable')
    frame, snapshot = await owned(env, uid, request_id, promotion.device_id, promotion.account_generation)
    if frame.state == FrameRequestState.attached:
        return frame
    row = await reserve(env, frame, snapshot, payload, 'permanent')
    try:
        await write(env, row, payload)
        return await publish(env, row)
    except Exception:
        # The other promoter can attach while this copy is in flight. Its
        # deterministic storage ID has one live object and never gets overwritten.
        current, _ = await owned(env, uid, request_id, promotion.device_id, promotion.account_generation)
        if current.state == FrameRequestState.attached and current.storage_id == row['storage_id']:
            await discard(env, row)
            return current
        raise


async def temporary_image(env, uid, request_id, generation):
    snapshot = await authorize(env, uid, generation)
    frame = await frames.get(env, uid, request_id, snapshot)
    if frame.account_generation != generation or frame.conversation_id is not None:
        raise HTTPException(404, 'frame_request_not_found')
    if frame.expires_at <= frames.now():
        raise HTTPException(410, 'frame_request_expired')
    if frame.state != FrameRequestState.uploaded or not frame.storage_id:
        raise HTTPException(409, 'frame_request_' + frame.state.value)
    row = await live_object(env, frame)
    stored = await bucket(env, 'temporary').get(key(row))
    if not stored:
        raise HTTPException(404, 'frame_request_pixels_unavailable')
    try:
        snapshot = await authorize(env, uid, generation)
        current = await frames.get(env, uid, request_id, snapshot)
        if (
            current.state != FrameRequestState.uploaded
            or current.storage_id != frame.storage_id
            or current.expires_at <= frames.now()
        ):
            raise HTTPException(404, 'frame_request_pixels_unavailable')
    except Exception:
        await stored.body.cancel()
        raise
    return StreamingResponse(_chunks(stored), media_type='image/jpeg', headers=HEADERS)


async def conversation_image(env, uid, conversation_id, photo_id):
    sql = (
        'SELECT o.* FROM cf_frame_objects o JOIN cf_frame_requests f ON f.uid=o.uid AND f.request_id=o.request_id '
        'JOIN cf_conversations c ON c.uid=f.uid AND c.id=f.conversation_id WHERE f.uid=? AND f.conversation_id=? '
        "AND f.request_id=? AND f.state='attached' AND o.phase='live' AND o.storage_id=f.storage_id "
        "AND o.tier='permanent' AND EXISTS (SELECT 1 FROM json_each(c.photos_json) p WHERE json_extract(p.value,'$.id')=f.request_id "
        "AND json_extract(p.value,'$.storage_id')=f.storage_id) "
        'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid=f.uid) '
        'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=f.uid)'
    )
    row = await env.APP_DB.prepare(sql).bind(uid, conversation_id, photo_id).first()
    if not row:
        raise HTTPException(404, 'Photo not found')
    stored = await bucket(env, 'permanent').get(key(row))
    if not stored:
        raise HTTPException(404, 'Photo not found')
    current = await env.APP_DB.prepare(sql).bind(uid, conversation_id, photo_id).first()
    if not current or current['object_id'] != row['object_id']:
        await stored.body.cancel()
        raise HTTPException(404, 'Photo not found')
    return StreamingResponse(_chunks(stored), media_type='image/jpeg', headers=HEADERS)
