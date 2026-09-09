"""Durable fair account scan feeding the original leased consolidation batch.

A Queue message is a wake-up hint. D1 owns the cycle boundary, captured intake
sequence, cursor, due time and exclusive account dispatch. Source/model attempts
remain with memory_consolidation_runner, not the Queue delivery retry counter.
"""

from datetime import datetime, timezone
import time
from uuid import uuid4

from memory_apply_intake import control_statement, load_memory_control
from memory_consolidation_runner import run_consolidation_batch
from memory_kernel_consolidation import ConsolidationScanCursor

PAGE_SIZE = 20
DISPATCH_LEASE_SECONDS = 900
DISPATCH_RETRY_SECONDS = 10


def clock():
    return int(time.time())


class DispatchChanged(RuntimeError):
    pass


async def _state(env, uid, generation):
    return (
        await env.APP_DB.prepare('SELECT * FROM cf_memory_consolidation_dispatch WHERE uid=? AND account_generation=?')
        .bind(uid, generation)
        .first()
    )


def _response(state, now):
    pending = bool(state and state['wake_sequence'] > state['handled_sequence'])
    due = max(state['next_attempt_at'], state['lease_until'] or 0) if pending else now
    return {'pending': pending, 'retry_after_seconds': max(0, min(DISPATCH_LEASE_SECONDS, due - now))}


async def _claim(env, uid, generation, now):
    owner = uuid4().hex
    result = (
        await env.APP_DB.prepare(
            'UPDATE cf_memory_consolidation_dispatch SET lease_owner=?,lease_until=?, '
            'cycle_sequence=COALESCE(cycle_sequence,wake_sequence) '
            'WHERE uid=? AND account_generation=? AND wake_sequence>handled_sequence '
            'AND next_attempt_at<=? AND (lease_until IS NULL OR lease_until<=?) '
            'AND account_generation=COALESCE((SELECT account_generation FROM cf_account_cutover WHERE uid=?),0) '
            'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid=?) '
            'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=?)'
        )
        .bind(owner, now + DISPATCH_LEASE_SECONDS, uid, generation, now, now, uid, uid, uid)
        .run()
    )
    state = await _state(env, uid, generation)
    return state if result['meta']['changes'] == 1 and state and state['lease_owner'] == owner else None


def _guard(db, uid, generation, prior, owner):
    return db.prepare(
        'INSERT INTO cf_memory_apply_guard (uid,expected_control_json,account_generation,new_ids_json,'
        'operation_ids_json,consolidation_dispatch_token) VALUES (?,?,?,\'[]\',\'[]\',?)'
    ).bind(uid, prior, generation, owner)


async def _materialize_control(env, state):
    """Keep legacy principals processable using the existing genesis constructor."""
    uid, generation = state['uid'], state['account_generation']
    prior, control = await load_memory_control(env, uid)
    if control.account_generation != generation:
        raise DispatchChanged('memory_consolidation_generation_changed')
    if prior is None:
        # No business commit or fabricated backfill proof is created here.
        await env.APP_DB.batch(
            [
                _guard(env.APP_DB, uid, generation, None, state['lease_owner']),
                control_statement(env.APP_DB, uid, control),
                env.APP_DB.prepare('DELETE FROM cf_memory_apply_guard WHERE uid=?').bind(uid),
            ]
        )


async def _page(env, state, now):
    cursor = None
    if state['cursor_memory_id'] is not None:
        cursor = ConsolidationScanCursor(
            uid=state['uid'],
            memory_id=state['cursor_memory_id'],
            captured_at=datetime.fromtimestamp(state['cursor_captured_at'], timezone.utc),
            updated_at=datetime.fromtimestamp(now, timezone.utc),
        )
    # Select only IDs and positions, never a whole scan window's source text.
    # The existing runner hydrates at most 20 and uses upstream eligibility.
    after = 'AND (captured_at>? OR (captured_at=? AND id>?)) ' if cursor else ''
    params = [state['uid'], state['account_generation']]
    if cursor:
        captured = int(cursor.captured_at.timestamp())
        params.extend([captured, captured, cursor.memory_id])
    params.append(PAGE_SIZE + 1)
    return (
        await env.APP_DB.prepare(
            "SELECT id,captured_at FROM cf_memories WHERE uid=? AND account_generation=? "
            "AND status='active' AND source_state='active' AND memory_tier='short_term' "
            "AND processing_state IN ('processed','pending') AND deleted_at IS NULL AND invalid_at IS NULL "
            'AND superseded_by IS NULL AND is_locked=0 ' + after + 'ORDER BY captured_at,id LIMIT ?'
        )
        .bind(*params)
        .all()
    )['results']


async def _finish(env, state, result, page, now):
    remaining = set(result.remaining) if result else set()
    crossed = []
    for row in page[:PAGE_SIZE]:
        if row['id'] in remaining:
            break
        crossed.append(row)
    if page and not crossed:
        raise DispatchChanged('memory_consolidation_scan_did_not_advance')
    more = bool(remaining or len(page) > PAGE_SIZE)
    blocked = bool(state['cycle_blocked']) or bool(
        result and any(outcome not in {'applied', 'not_pending'} for outcome in result.outcomes.values())
    )
    applied = bool(state['cycle_applied']) or bool(result and result.applied)
    due_values = [
        value
        for value in ([state['cycle_retry_at']] + list(result.retry_at.values() if result else []))
        if value is not None
    ]
    retry_at = min(due_values) if due_values else None
    next_at = now if more or not blocked else max(now + 1, retry_at or now + DISPATCH_RETRY_SECONDS)
    uid, generation = state['uid'], state['account_generation']
    prior, control = await load_memory_control(env, uid)
    if generation != control.account_generation:
        raise DispatchChanged('memory_consolidation_generation_changed')
    statements = [_guard(env.APP_DB, uid, generation, prior, state['lease_owner'])]
    if not more and not blocked and applied:
        instant = datetime.fromtimestamp(now, timezone.utc)
        statements.append(
            control_statement(
                env.APP_DB,
                uid,
                control.model_copy(
                    update={
                        'last_consolidation_run_at': instant,
                        'updated_at': instant,
                    }
                ),
            )
        )
    cursor = crossed[-1] if more else None
    statements.append(
        env.APP_DB.prepare(
            'UPDATE cf_memory_consolidation_dispatch SET '
            'handled_sequence=MAX(handled_sequence,?),cycle_sequence=?,cursor_captured_at=?,cursor_memory_id=?, '
            'cycle_blocked=?,cycle_applied=?,cycle_retry_at=?,lease_owner=NULL,lease_until=NULL, '
            'next_attempt_at=CASE WHEN wake_sequence>? THEN MIN(?,?) ELSE ? END '
            'WHERE uid=? AND account_generation=? AND lease_owner=?'
        ).bind(
            state['handled_sequence'] if more or blocked else state['cycle_sequence'],
            state['cycle_sequence'] if more else None,
            cursor['captured_at'] if cursor else None,
            cursor['id'] if cursor else None,
            int(blocked) if more else 0,
            int(applied) if more else 0,
            retry_at if more else None,
            state['cycle_sequence'],
            now,
            next_at,
            next_at,
            uid,
            generation,
            state['lease_owner'],
        )
    )
    statements.append(env.APP_DB.prepare('DELETE FROM cf_memory_apply_guard WHERE uid=?').bind(uid))
    await env.APP_DB.batch(statements)
    return _response(await _state(env, uid, generation), now)


async def process_consolidation_dispatch(env, uid, generation):
    now = clock()
    _, control = await load_memory_control(env, uid)
    if control.account_generation != generation:
        raise DispatchChanged('memory_consolidation_generation_changed')
    state = await _claim(env, uid, generation, now)
    if not state:
        return _response(await _state(env, uid, generation), now)
    try:
        await _materialize_control(env, state)
        page = await _page(env, state, now)
        result = (
            await run_consolidation_batch(
                env,
                uid,
                [row['id'] for row in page[:PAGE_SIZE]],
                run_id='dispatch:' + state['lease_owner'],
            )
            if page
            else None
        )
        return await _finish(env, state, result, page, clock())
    except Exception:
        # Cursor/cycle are left at the last acknowledged batch. A lost apply
        # response is safe: reloading finds already-routed sources non-pending.
        await env.APP_DB.prepare(
            'UPDATE cf_memory_consolidation_dispatch SET lease_owner=NULL,lease_until=NULL,next_attempt_at=? '
            'WHERE uid=? AND account_generation=? AND lease_owner=? AND lease_until>unixepoch()'
        ).bind(clock() + DISPATCH_RETRY_SECONDS, uid, generation, state['lease_owner']).run()
        raise
