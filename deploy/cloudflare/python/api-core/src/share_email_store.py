"""D1 transactions for at-most-once mail dispatch and reversible publication."""

from datetime import datetime, timezone
import json
import time
from uuid import uuid4

from fastapi import HTTPException
from share_email_contract import SHARE_EMAIL_CLAIM_TTL_SECONDS
from fallback import record_fallback


def statement(env, sql, *values):
    return env.APP_DB.prepare(sql).bind(*values)


async def expire(env, uid, now):
    results = await env.APP_DB.batch(
        [
            statement(
                env,
                "UPDATE cf_share_email_dispatches SET phase='rejected',payload_json=NULL "
                "WHERE uid=? AND phase='prepared' AND expires_at<=?",
                uid,
                now,
            ),
            statement(
                env,
                "UPDATE cf_share_email_dispatches SET phase='ambiguous',payload_json=NULL "
                "WHERE uid=? AND phase='dispatching' AND expires_at<=?",
                uid,
                now,
            ),
        ]
    )
    if results[1].get('meta', {}).get('changes', 0) > 0:
        record_fallback(
            component='other',
            from_mode='cloudflare_email',
            to_mode='conservative_no_action',
            reason='dependency_unavailable',
            outcome='degraded',
        )


async def get_conversation(env, uid, conversation_id):
    row = await statement(
        env,
        'SELECT id,structured_json,visibility,is_locked,share_email_revision FROM cf_conversations c '
        'WHERE uid=? AND id=? '
        'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid=c.uid) '
        'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=c.uid)',
        uid,
        conversation_id,
    ).first()
    if not row:
        raise HTTPException(404, 'Conversation not found')
    if row['is_locked']:
        raise HTTPException(402, 'A paid plan is required to access this conversation.')
    return row


async def prepare(env, uid, row, recipients, payload, *, now=None):
    now = int(time.time()) if now is None else now
    identifier, day = str(uuid4()), datetime.fromtimestamp(now, timezone.utc).strftime('%Y%m%d')
    cid, revision = row['id'], row['share_email_revision']
    sql = [
        statement(
            env,
            'INSERT INTO cf_share_email_dispatches '
            '(id,uid,conversation_id,phase,quota_day,was_private,created_at,expires_at,payload_json) '
            "SELECT ?,uid,id,'prepared',?,visibility NOT IN ('shared','public'),?,?,? FROM cf_conversations c "
            'WHERE uid=? AND id=? AND share_email_revision=? AND is_locked=0 '
            'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid=c.uid) '
            'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=c.uid) RETURNING id',
            identifier,
            day,
            now,
            now + SHARE_EMAIL_CLAIM_TTL_SECONDS,
            json.dumps(payload),
            uid,
            cid,
            revision,
        ),
        statement(
            env,
            'INSERT INTO cf_share_email_recipients (uid,conversation_id,email,dispatch_id) '
            'SELECT d.uid,d.conversation_id,j.value,d.id FROM cf_share_email_dispatches d,json_each(?) j '
            'WHERE d.id=? ON CONFLICT(uid,conversation_id,email) DO NOTHING',
            json.dumps(recipients),
            identifier,
        ),
        statement(
            env,
            'UPDATE cf_share_email_dispatches SET recipient_count=(SELECT COUNT(*) FROM cf_share_email_recipients '
            'WHERE dispatch_id=?) WHERE id=?',
            identifier,
            identifier,
        ),
        statement(
            env,
            'INSERT INTO cf_share_email_quota(uid,day,used) '
            'SELECT uid,quota_day,recipient_count FROM cf_share_email_dispatches WHERE id=? AND recipient_count>0 '
            'ON CONFLICT(uid,day) DO UPDATE SET used=used+excluded.used',
            identifier,
        ),
        # Even an existing shared/public link gets a fresh write revision. A
        # previous failed send must not revoke a later send's accepted link.
        statement(
            env,
            "UPDATE cf_conversations SET visibility=CASE WHEN visibility IN ('shared','public') "
            "THEN visibility ELSE 'shared' END,updated_at=MAX(updated_at+1,?) WHERE uid=? AND id=? "
            'AND EXISTS (SELECT 1 FROM cf_share_email_dispatches WHERE id=? AND recipient_count>0)',
            now,
            uid,
            cid,
            identifier,
        ),
        statement(
            env,
            'UPDATE cf_share_email_dispatches SET publish_revision=(SELECT share_email_revision '
            'FROM cf_conversations WHERE uid=? AND id=?) WHERE id=?',
            uid,
            cid,
            identifier,
        ),
        statement(
            env,
            'INSERT INTO cf_shared_conversation_index(conversation_id,uid,visibility,updated_at) '
            'SELECT c.id,c.uid,c.visibility,c.updated_at FROM cf_conversations c '
            'JOIN cf_share_email_dispatches d ON d.uid=c.uid AND d.conversation_id=c.id '
            'WHERE d.id=? AND d.recipient_count>0 ON CONFLICT(conversation_id) DO UPDATE SET '
            'visibility=excluded.visibility,updated_at=excluded.updated_at '
            'WHERE cf_shared_conversation_index.uid=excluded.uid',
            identifier,
        ),
        statement(env, 'DELETE FROM cf_share_email_dispatches WHERE id=? AND recipient_count=0', identifier),
        statement(env, 'SELECT id,recipient_count FROM cf_share_email_dispatches WHERE id=?', identifier),
        statement(
            env,
            'SELECT r.email,d.phase FROM cf_share_email_recipients r '
            'JOIN cf_share_email_dispatches d ON d.id=r.dispatch_id '
            'WHERE r.uid=? AND r.conversation_id=? AND r.email IN (SELECT value FROM json_each(?))',
            uid,
            cid,
            json.dumps(recipients),
        ),
    ]
    try:
        results = await env.APP_DB.batch(sql)
    except Exception as error:
        if 'share_email_daily_quota_exceeded' in str(error):
            raise HTTPException(429, 'Daily share-email limit reached') from None
        raise
    if not results[0]['results']:
        await get_conversation(env, uid, cid)
        raise HTTPException(409, 'Conversation changed. Try again.')
    ledger = results[-1]['results']
    sent = {item['email'] for item in ledger if item['phase'] in ('sent', 'ambiguous')}
    if results[-2]['results']:
        return {'dispatch_id': identifier, 'already_sent': sorted(sent)}
    if any(item['phase'] in ('prepared', 'dispatching') for item in ledger):
        raise HTTPException(409, 'That summary is already being sent to this recipient — check back in a moment')
    return {'sent_to': [email for email in recipients if email in sent]}


async def claim(env, uid, identifier):
    now = int(time.time())
    result = await statement(
        env,
        "UPDATE cf_share_email_dispatches SET phase='dispatching',expires_at=? WHERE id=? AND uid=? "
        "AND phase='prepared' AND expires_at>? AND EXISTS (SELECT 1 FROM cf_conversations c "
        'WHERE c.uid=cf_share_email_dispatches.uid AND c.id=cf_share_email_dispatches.conversation_id '
        'AND c.share_email_revision=cf_share_email_dispatches.publish_revision AND c.is_locked=0 '
        "AND c.visibility IN ('shared','public')) "
        'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid=?) '
        'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=?) RETURNING payload_json',
        now + SHARE_EMAIL_CLAIM_TTL_SECONDS,
        identifier,
        uid,
        now,
        uid,
        uid,
    ).first()
    if not result:
        # Only a still-prepared attempt can be cancelled here. An in-flight
        # provider call keeps its claim and may never be dispatched again.
        await statement(
            env,
            "UPDATE cf_share_email_dispatches SET phase='rejected',payload_json=NULL "
            "WHERE uid=? AND id=? AND phase='prepared'",
            uid,
            identifier,
        ).run()
        raise HTTPException(409, 'Share email dispatch is unavailable or already started')
    emails = (
        await statement(
            env,
            'SELECT email FROM cf_share_email_recipients ' 'WHERE uid=? AND dispatch_id=? ORDER BY email',
            uid,
            identifier,
        ).all()
    )['results']
    return {**json.loads(result['payload_json']), 'to': [item['email'] for item in emails]}


async def finish(env, uid, identifier, phase, message_id=None):
    if phase not in ('sent', 'ambiguous', 'rejected'):
        raise ValueError('invalid dispatch result')
    await statement(
        env,
        'UPDATE cf_share_email_dispatches SET phase=?,payload_json=NULL,provider_message_id=? '
        "WHERE uid=? AND id=? AND phase='dispatching'",
        phase,
        message_id,
        uid,
        identifier,
    ).run()
    result = await statement(
        env, 'SELECT phase,conversation_id FROM cf_share_email_dispatches WHERE uid=? AND id=?', uid, identifier
    ).first()
    if not result:
        raise HTTPException(404, 'Share email dispatch not found')
    if result['phase'] != phase:
        raise HTTPException(409, 'Share email dispatch already finalized')
    return {'phase': phase}
