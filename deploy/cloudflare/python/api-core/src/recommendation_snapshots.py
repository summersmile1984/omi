"""Original device snapshot admission, receipts and expiry on the D1 owner."""

from datetime import datetime, timezone
import json

from pydantic import ValidationError
from candidate_db import CandidateTransaction, CandidateSnapshotChanged, account_generation, encoded
from candidate_kernel_attention import _request_hash
from candidate_kernel_policy import CandidateConflictError, CandidateGenerationMismatchError
from candidate_kernel_recommendation import NormalizedContextSnapshot, OpenLoopSnapshot, SnapshotReceipt
from recommendation_kernel import _stable_id, _validate_snapshot_window, SnapshotValidationError
from fallback import record_fallback


def scope_key(snapshot, generation):
    parts = [generation, snapshot.device_id]
    if isinstance(snapshot, OpenLoopSnapshot):
        parts += [snapshot.runtime_id, snapshot.workstream_id]
    return encoded(parts)


async def _generation(env, uid, generation):
    if await account_generation(env, uid) != generation:
        raise CandidateGenerationMismatchError('snapshot account generation changed')


async def cleanup_receipts(env, uid, generation, now):
    # Upstream deletes at most 50 expired request receipts per cleanup pass.
    for attempt in range(3):
        tx = CandidateTransaction(env, uid, generation)
        rows = (
            await tx.db.prepare(
                'SELECT receipt_id FROM cf_task_snapshot_receipts WHERE uid=? AND expires_at<=? ORDER BY expires_at,receipt_id LIMIT 50'
            )
            .bind(uid, int(now.timestamp()))
            .all()
        )['results']
        for row in rows:
            raw = await tx.record('snapshot_receipts', row['receipt_id'])
            if raw is not None and datetime.fromisoformat(json.loads(raw)['expires_at']) <= now:
                tx.statements.append(
                    tx.db.prepare('DELETE FROM cf_task_snapshot_receipts WHERE uid=? AND receipt_id=?').bind(
                        uid, row['receipt_id']
                    )
                )
        try:
            await tx.commit()
            return
        except CandidateSnapshotChanged:
            if attempt == 2:
                raise


async def save_snapshot(env, uid, generation, snapshot, *, idempotency_key, now=None):
    now = now or datetime.now(timezone.utc)
    _validate_snapshot_window(snapshot.generated_at, snapshot.expires_at, now)
    open_loop = isinstance(snapshot, OpenLoopSnapshot)
    if open_loop and snapshot.owner != uid:
        raise SnapshotValidationError('snapshot owner must match authenticated user')
    group = 'open_loop_snapshots' if open_loop else 'context_snapshots'
    table = 'cf_task_' + group
    identity = scope_key(snapshot, generation)
    snapshot_id = (
        _stable_id('loop-snapshot', generation, snapshot.device_id, snapshot.runtime_id, snapshot.workstream_id)
        if open_loop
        else snapshot.snapshot_id
    )
    receipt_id = _stable_id('snapshot-receipt', generation, 'open-loop' if open_loop else 'context', idempotency_key)
    request_hash = _request_hash(snapshot.model_dump(mode='json'))
    for attempt in range(3):
        await _generation(env, uid, generation)
        tx = CandidateTransaction(env, uid, generation)
        if open_loop:
            stream = await tx.row('workstreams', snapshot.workstream_id)
            if not stream or stream['account_generation'] != generation or stream['status'] != 'open':
                raise SnapshotValidationError(
                    'snapshot workstream must be canonical and owned by the authenticated user'
                )
        previous_receipt = await tx.record('snapshot_receipts', receipt_id)
        if previous_receipt is not None:
            previous = json.loads(previous_receipt)
            if previous['request_hash'] != request_hash:
                raise CandidateConflictError('idempotency key was used for a different snapshot')
            result = SnapshotReceipt.model_validate(previous['receipt'])
        else:
            old = await tx.row(group, identity)
            if old is not None:
                stored = type(snapshot).model_validate_json(old['payload_json'])
                if snapshot.generated_at < stored.generated_at:
                    raise CandidateConflictError('snapshot is older than stored state')
                if snapshot.generated_at == stored.generated_at and snapshot != stored:
                    raise CandidateConflictError('snapshot timestamp was reused for different state')
            result = SnapshotReceipt(snapshot_id=snapshot_id, replaced=old is not None, expires_at=snapshot.expires_at)
            tx.statements.append(
                tx.db.prepare(
                    f'INSERT INTO {table}(uid,device_id,account_generation,snapshot_id,request_fingerprint,payload_json,generated_at,expires_at,updated_at,receipt_id) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(uid,scope_key) DO UPDATE SET '
                    'snapshot_id=excluded.snapshot_id,request_fingerprint=excluded.request_fingerprint,payload_json=excluded.payload_json,'
                    'generated_at=excluded.generated_at,expires_at=excluded.expires_at,updated_at=excluded.updated_at,receipt_id=excluded.receipt_id'
                ).bind(
                    uid,
                    snapshot.device_id,
                    generation,
                    snapshot_id,
                    request_hash,
                    snapshot.model_dump_json(),
                    int(snapshot.generated_at.timestamp()),
                    int(snapshot.expires_at.timestamp()),
                    int(now.timestamp()),
                    receipt_id,
                )
            )
            tx.put(
                'snapshot_receipts',
                receipt_id,
                {
                    'account_generation': generation,
                    'request_hash': request_hash,
                    'receipt': result.model_dump(mode='json'),
                    'expires_at': snapshot.expires_at.isoformat(),
                },
            )
        try:
            await tx.commit()
            await cleanup_receipts(env, uid, generation, snapshot.generated_at)
            return result
        except CandidateSnapshotChanged:
            if attempt == 2:
                raise


async def read_snapshots(env, uid, generation, device, *, now, open_loop):
    await _generation(env, uid, generation)
    group = 'open_loop_snapshots' if open_loop else 'context_snapshots'
    table = 'cf_task_' + group
    model = OpenLoopSnapshot if open_loop else NormalizedContextSnapshot
    rows = (
        await env.APP_DB.prepare(
            f'SELECT scope_key FROM {table} WHERE uid=? AND account_generation=? AND device_id=? ORDER BY scope_key'
        )
        .bind(uid, generation, device)
        .all()
    )['results']
    values = []
    for identity in rows:
        for attempt in range(3):
            tx = CandidateTransaction(env, uid, generation)
            row = await tx.row(group, identity['scope_key'])
            if row is None:
                break
            try:
                value = model.model_validate_json(row['payload_json'])
            except ValidationError:
                record_fallback(from_mode='none', to_mode='none', reason='malformed_doc', outcome='degraded')
                break
            if value.expires_at > now:
                values.append(value)
                break
            if row['receipt_id']:
                await tx.record('snapshot_receipts', row['receipt_id'])
                tx.statements.append(
                    tx.db.prepare('DELETE FROM cf_task_snapshot_receipts WHERE uid=? AND receipt_id=?').bind(
                        uid, row['receipt_id']
                    )
                )
            tx.statements.append(
                tx.db.prepare(f'DELETE FROM {table} WHERE uid=? AND scope_key=?').bind(uid, identity['scope_key'])
            )
            try:
                await tx.commit()
                break
            except CandidateSnapshotChanged:
                if attempt == 2:
                    raise
    await cleanup_receipts(env, uid, generation, now)
    await _generation(env, uid, generation)
    return sorted(values, key=lambda value: (value.workstream_id, value.runtime_id)) if open_loop else values
