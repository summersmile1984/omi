"""Public memory delete coordinator; acknowledge only completed erasure."""

import time
import secrets

from fallback import record_fallback
from memory_apply_intake import encoded
from memory_privacy_apply import prepare_privacy_deletion
from memory_privacy_finalize import finalize_privacy_deletion
from memory_privacy_receipts import privacy_receipt_id


class MemoryNotFound(ValueError):
    pass


async def wake_cleanup(env, inventory, *, scope=False):
    try:
        await env.JOBS.send(
            {
                'jobId': 'memory-privacy-' + inventory['token'],
                'uid': inventory['uid'],
                'kind': 'memory_privacy_cleanup',
                'payload': {'token': inventory['token'], 'scope': scope},
            }
        )
    except Exception:
        record_fallback(
            component='other',
            from_mode='queue',
            to_mode='scheduled_reconciler',
            reason='dependency_unavailable',
            outcome='degraded',
        )


async def finish_or_schedule(env, inventory):
    try:
        await finalize_privacy_deletion(env, inventory['uid'], inventory['token'], int(time.time()))
        return True
    except Exception as error:
        if 'memory_privacy_provider_pending' not in str(error):
            raise
        await wake_cleanup(env, inventory)
        return False


async def delete_selected_memories(env, uid, memory_ids, *, expand_lineages=True):
    """False means durable cleanup is pending; exceptions never mean success."""
    if not memory_ids:
        return True
    db = env.APP_DB
    # Finish a previously admitted request before attempting another lineage.
    # This is also the retry path after a lost response or a provider outage.
    pending = await db.prepare('SELECT * FROM cf_memory_privacy_deletions WHERE uid = ?').bind(uid).first()
    if pending is not None and not await finish_or_schedule(env, pending):
        return False
    keys = [{'id': value, 'receipt_id': privacy_receipt_id(env, uid, value)} for value in memory_ids]
    result = (
        await db.prepare(
            "SELECT json_extract(t.value, '$.id') AS id, EXISTS (SELECT 1 FROM cf_memories "
            "WHERE uid = ? AND id = json_extract(t.value, '$.id')) AS present, "
            "EXISTS (SELECT 1 FROM cf_memory_privacy_receipts WHERE uid = ? "
            "AND receipt_id = json_extract(t.value, '$.receipt_id') AND expires_at > unixepoch()) AS completed "
            'FROM json_each(?) t'
        )
        .bind(uid, uid, encoded(keys))
        .all()
    )
    rows = result['results']
    if len(rows) != len(keys) or any(not row['present'] and not row['completed'] for row in rows):
        raise MemoryNotFound('memory not found')
    remaining = [row['id'] for row in rows if row['present']]
    if not remaining:
        return True
    inventory = await prepare_privacy_deletion(env, uid, remaining, int(time.time()), expand_lineages=expand_lineages)
    return await finish_or_schedule(env, inventory)


async def continue_memory_scope(env, uid, token):
    db = env.APP_DB
    state = (
        await db.prepare('SELECT * FROM cf_memory_privacy_scopes WHERE uid = ? AND token = ?').bind(uid, token).first()
    )
    if state is None:
        return True
    touched = (
        await db.prepare(
            'UPDATE cf_memory_privacy_scopes SET last_attempt_at = unixepoch() WHERE uid = ? AND token = ?'
        )
        .bind(uid, token)
        .run()
    )
    if touched['meta']['changes'] != 1:
        return True
    pending = await db.prepare('SELECT * FROM cf_memory_privacy_deletions WHERE uid = ?').bind(uid).first()
    if pending is not None and not await finish_or_schedule(env, pending):
        return False
    result = (
        await db.prepare(
            "SELECT id FROM cf_memories WHERE uid = ? AND (? = 'all' OR memory_tier != 'archive') ORDER BY id LIMIT 100"
        )
        .bind(uid, state['scope'])
        .all()
    )
    ids = [row['id'] for row in result['results']]
    # The original default wipe selects tiers. It must not expand a Short-term
    # alias into an Archive record that this scope explicitly retains.
    if ids and not await delete_selected_memories(env, uid, ids, expand_lineages=False):
        return False
    completed = (
        await db.prepare(
            'DELETE FROM cf_memory_privacy_scopes WHERE uid = ? AND token = ? AND NOT EXISTS ('
            "SELECT 1 FROM cf_memories WHERE uid = ? AND (? = 'all' OR memory_tier != 'archive')) "
            'AND NOT EXISTS (SELECT 1 FROM cf_memory_privacy_deletions WHERE uid = ?)'
        )
        .bind(uid, token, uid, state['scope'], uid)
        .run()
    )
    if completed['meta']['changes'] == 1:
        return True
    await wake_cleanup(env, state, scope=True)
    return False


async def delete_memory_scope(env, uid, scope):
    if scope not in {'all', 'default'}:
        raise ValueError('invalid memory deletion scope')
    db = env.APP_DB
    await db.prepare(
        'INSERT INTO cf_memory_privacy_scopes(uid, token, scope, created_at) VALUES (?, ?, ?, ?) ON CONFLICT(uid) DO NOTHING'
    ).bind(uid, secrets.token_hex(32), scope, int(time.time())).run()
    state = await db.prepare('SELECT * FROM cf_memory_privacy_scopes WHERE uid = ?').bind(uid).first()
    if state is None or state['scope'] != scope:
        raise ValueError('memory_privacy_scope_pending')
    return await continue_memory_scope(env, uid, state['token'])
