"""One D1 attempt owns atomic screenshot publication and its replay response."""

from datetime import datetime, timezone
import json

from fastapi import HTTPException

from screen_frame_views import FENCE, frame_set, read_set
from screen_frames_contract import ScreenFrameAdjudicationResponse
from screen_frames_selection import _apply_cap_and_roles


def failure(code, status=503):
    return HTTPException(status, {'code': code})


async def reserve(env, uid, request, fingerprint, ttl):
    conversation_id, attempt_id = request.subject.id, str(request.attempt_id)
    await env.APP_DB.batch(
        [
            env.APP_DB.prepare(
                'DELETE FROM cf_screen_frame_attempts WHERE uid = ? AND attempt_id = ? AND expires_at <= unixepoch()'
            ).bind(uid, attempt_id),
            env.APP_DB.prepare(
                'INSERT INTO cf_screen_frame_sets (uid, conversation_id) SELECT ?, ? '
                'WHERE NOT EXISTS (SELECT 1 FROM cf_screen_frame_attempts WHERE uid = ? AND attempt_id = ?) '
                'ON CONFLICT(uid, conversation_id) DO NOTHING'
            ).bind(uid, conversation_id, uid, attempt_id),
        ]
    )
    inserted = (
        await env.APP_DB.prepare(
            'INSERT INTO cf_screen_frame_attempts (uid, conversation_id, attempt_id, fingerprint, epoch, expires_at) '
            'SELECT uid, conversation_id, ?, ?, epoch, unixepoch() + ? FROM cf_screen_frame_sets '
            'WHERE uid = ? AND conversation_id = ? ON CONFLICT(uid, attempt_id) DO NOTHING RETURNING *'
        )
        .bind(attempt_id, fingerprint, ttl, uid, conversation_id)
        .first()
    )
    if inserted:
        return inserted, None
    row = (
        await env.APP_DB.prepare('SELECT * FROM cf_screen_frame_attempts WHERE uid = ? AND attempt_id = ?')
        .bind(uid, attempt_id)
        .first()
    )
    if row and row['fingerprint'] != fingerprint:
        raise failure('attempt_id_reused_with_different_request', 409)
    if row and row['response_json'] is not None:
        return row, ScreenFrameAdjudicationResponse.model_validate_json(row['response_json'])
    raise failure('adjudication_in_progress_retry')


async def current(env, attempt):
    return bool(
        await env.APP_DB.prepare(
            'SELECT 1 FROM cf_screen_frame_attempts a JOIN cf_screen_frame_sets s '
            'ON s.uid = a.uid AND s.conversation_id = a.conversation_id '
            'JOIN cf_conversations c ON c.uid = s.uid AND c.id = s.conversation_id '
            "WHERE a.uid = ? AND a.attempt_id = ? AND a.epoch = ? AND s.epoch = a.epoch AND c.status = 'completed' "
            'AND a.response_json IS NULL AND a.expires_at > unixepoch() '
            'AND COALESCE((SELECT enabled FROM cf_screen_frame_settings WHERE uid = a.uid), 1) = 1 '
            'AND ' + FENCE
        )
        .bind(attempt['uid'], attempt['attempt_id'], attempt['epoch'], attempt['uid'], attempt['uid'])
        .first()
    )


async def publish(env, attempt, new_docs, max_persisted):
    uid, conversation_id, attempt_id = attempt['uid'], attempt['conversation_id'], attempt['attempt_id']
    new_ids = {doc['id'] for doc in new_docs}
    for _ in range(5):
        if not await current(env, attempt):
            raise failure('screen_frame_adjudication_cancelled', 409)
        row = await read_set(env, uid, conversation_id)
        existing = json.loads(row['frames_json'])
        for doc in existing:
            captured = datetime.fromisoformat(doc['captured_at'].replace('Z', '+00:00'))
            doc['captured_at'] = captured.replace(tzinfo=timezone.utc) if captured.tzinfo is None else captured
        survivors, _ = _apply_cap_and_roles(existing, new_docs, max_persisted)
        stored = [{**doc, 'captured_at': doc['captured_at'].isoformat()} for doc in survivors]
        timestamp = datetime.now(timezone.utc).isoformat()
        frames_json = json.dumps(stored, separators=(',', ':'))
        next_row = {
            **row,
            'frames_json': frames_json,
            'revision': row['revision'] + bool(new_docs),
            'adjudicated_at': timestamp,
        }
        response = ScreenFrameAdjudicationResponse(
            attempt_id=attempt_id,
            outcome='committed' if any(doc['id'] in new_ids for doc in survivors) else 'no_approved_frames',
            frame_set=frame_set(env, uid, next_row),
        )
        response_json = response.model_dump_json()
        # The attempt receipt is the transaction's publication owner. A failed
        # frame trigger rolls back the receipt as well; concurrent publications
        # must still match the revision/epoch snapshot before acquiring it.
        result = await env.APP_DB.batch(
            [
                env.APP_DB.prepare(
                    'UPDATE cf_screen_frame_attempts SET response_json = ? '
                    'WHERE uid = ? AND attempt_id = ? AND epoch = ? AND response_json IS NULL AND expires_at > unixepoch() '
                    'AND EXISTS (SELECT 1 FROM cf_screen_frame_sets s JOIN cf_conversations c '
                    'ON c.uid = s.uid AND c.id = s.conversation_id '
                    "WHERE s.uid = ? AND s.conversation_id = ? AND s.revision = ? AND s.epoch = ? AND c.status = 'completed') "
                    'AND COALESCE((SELECT enabled FROM cf_screen_frame_settings WHERE uid = ?), 1) = 1 '
                    'AND ' + FENCE
                ).bind(
                    response_json,
                    uid,
                    attempt_id,
                    attempt['epoch'],
                    uid,
                    conversation_id,
                    row['revision'],
                    attempt['epoch'],
                    uid,
                    uid,
                    uid,
                ),
                env.APP_DB.prepare(
                    'UPDATE cf_screen_frame_sets SET frames_json = ?, revision = ?, adjudicated_at = ? '
                    'WHERE uid = ? AND conversation_id = ? AND revision = ? AND epoch = ? '
                    'AND EXISTS (SELECT 1 FROM cf_screen_frame_attempts '
                    'WHERE uid = ? AND attempt_id = ? AND response_json = ?)'
                ).bind(
                    frames_json,
                    next_row['revision'],
                    timestamp,
                    uid,
                    conversation_id,
                    row['revision'],
                    attempt['epoch'],
                    uid,
                    attempt_id,
                    response_json,
                ),
                env.APP_DB.prepare(
                    "UPDATE cf_screen_frame_writes SET phase = 'cleanup' WHERE uid = ? AND attempt_id = ? AND phase = 'ready' "
                    'AND EXISTS (SELECT 1 FROM cf_screen_frame_attempts WHERE uid = ? AND attempt_id = ? AND response_json = ?)'
                ).bind(uid, attempt_id, uid, attempt_id, response_json),
            ]
        )
        if result[0]['meta']['changes'] == 1:
            return response
    raise failure('adjudication_in_progress_retry')
