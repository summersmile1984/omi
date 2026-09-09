"""MCP/Developer explicit submissions join the canonical intake transaction.

Identical active rows are read-only duplicate results, as in upstream
_existing_identical_add_row. Privacy-deleted identities stay retired; a new
explicit submission receives a fresh identity, as write_canonical_external_memory
requires. The existing batch guard validates every duplicate alongside new writes.
"""

import secrets

from account_routes import usage_source_statement
from memory_apply_intake import create_native_memories, encoded, load_memory_control
from memory_apply_item import read_item
from memory_privacy_receipts import privacy_receipt_id
from vector_search import publish_vector_projection

RETRY_CONFLICTS = (
    'memory_apply_head_changed',
    'memory_apply_target_exists',
    'memory_apply_operation_changed',
    'memory_apply_observation_changed',
)


async def create_external_memories(env, uid, rows, *, source_surface):
    if source_surface not in {'mcp', 'developer_api'} or not 1 <= len(rows) <= 25:
        raise ValueError('invalid external memory submission')
    if any(row['uid'] != uid for row in rows):
        raise ValueError('invalid external memory owner')
    # Reuse allocated identities when a known rolled-back transaction retries.
    reissued = {}
    for attempt in range(3):
        try:
            return await _create(env, uid, rows, source_surface, reissued)
        except Exception as exc:
            if attempt == 2 or not any(code in str(exc) for code in RETRY_CONFLICTS):
                raise


async def _create(env, uid, rows, source_surface, reissued):
    db = env.APP_DB
    prior, control = await load_memory_control(env, uid)
    planned, existing, ordered = {}, {}, []
    transferred = 0
    for original in rows:
        row = dict(original)
        identity = row['id']
        receipt = (
            await db.prepare('SELECT 1 FROM cf_memory_privacy_receipts WHERE uid=? AND receipt_id=?')
            .bind(uid, privacy_receipt_id(env, uid, identity))
            .first()
        )
        if receipt:
            identity = reissued.setdefault(identity, 'mem_' + secrets.token_hex(16))
            row['id'] = identity
        ordered.append(identity)
        if identity in planned or identity in existing:
            prior_row = planned.get(identity) or existing[identity]
            if prior_row['content'].strip() != row['content'].strip():
                raise ValueError('external memory identity content conflict')
            continue
        current = await db.prepare('SELECT * FROM cf_memories WHERE uid=? AND id=?').bind(uid, identity).first()
        if current is None:
            planned[identity] = row
            continue
        size = len(encoded(current).encode())
        transferred += size
        if size > 1_000_000 or transferred > 4_000_000:
            raise ValueError('external memory duplicate read budget exhausted')
        read_item(current)
        if (
            current['status'] != 'active'
            or current['source_state'] != 'active'
            or current['deleted_at'] is not None
            or current['invalid_at'] is not None
            or current['superseded_by'] is not None
            or current['account_generation'] != control.account_generation
            or (current['content'] or '').strip() != row['content'].strip()
        ):
            raise ValueError('external memory identity is no longer an identical active row')
        existing[identity] = current
    observed = list(existing.values())
    if planned:
        fresh = list(planned.values())
        usage = [
            usage_source_statement(
                env,
                uid=uid,
                source_kind='memory',
                source_id=row['id'],
                occurred_at=row['created_at'],
                memories_created=1,
                updated_at=row['updated_at'],
            )
            for row in fresh
        ]
        await create_native_memories(
            env,
            uid,
            fresh,
            usage,
            source_surface=source_surface,
            observed_items=observed,
        )
    else:
        # Duplicate-only submissions recheck authority atomically without a
        # new memory, usage record, canonical commit or head update.
        await db.batch(
            [
                db.prepare(
                    'INSERT INTO cf_memory_apply_guard (uid,expected_control_json,account_generation,'
                    'new_ids_json,operation_ids_json,observed_items_json) VALUES (?,?,?,?,?,?)'
                ).bind(uid, prior, control.account_generation, '[]', '[]', encoded(observed)),
                db.prepare('DELETE FROM cf_memory_apply_guard WHERE uid=?').bind(uid),
            ]
        )
    for identity in planned:
        await publish_vector_projection(env, uid=uid, source_kind='memory', source_id=identity)
    result = {}
    transferred = 0
    for identity in dict.fromkeys(ordered):
        current = (
            await db.prepare(
                "SELECT * FROM cf_memories WHERE uid=? AND id=? AND deleted_at IS NULL AND invalid_at IS NULL "
                "AND status='active' AND source_state='active' AND account_generation=?"
            )
            .bind(uid, identity, control.account_generation)
            .first()
        )
        if current is None:
            raise ValueError('external memory write readback unavailable')
        size = len(encoded(current).encode())
        transferred += size
        if size > 1_000_000 or transferred > 4_000_000:
            raise ValueError('external memory readback budget exhausted')
        read_item(current)
        result[identity] = current
    return [result[identity] for identity in ordered]
