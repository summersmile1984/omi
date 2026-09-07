"""Commit native intake through the upstream rules and one guarded D1 batch.

cf_memories remains row authority. Only model fields without physical columns
live in canonical_metadata_json; content/evidence/lifecycle are never copied
into a second memory document. No public caller supplies control, evidence or
an apply result. Other writer families still require migration to this owner.
"""

from datetime import datetime, timezone
import hashlib
from itertools import chain
import json

from memory_kernel_apply import (
    ApplyStatus,
    MemoryControlState,
    MemoryWriterClass,
    WriterMode,
    apply_long_term_patch_transaction,
    build_patch_mutation_identity,
    require_writer_admitted,
)
from memory_kernel_evidence import ArtifactPreservationState, MemoryEvidence
from memory_kernel_operations import MemoryOperation, MemoryOperationStatus, MemoryOperationType
from memory_kernel_short_term_lifecycle import default_short_term_expiry

MODEL_COLUMNS = {
    'memory_id': 'id',
    'uid': 'uid',
    'version': 'version',
    'tier': 'memory_tier',
    'status': 'status',
    'processing_state': 'processing_state',
    'content': 'content',
    'evidence': 'evidence_json',
    'source_state': 'source_state',
    'sensitivity_labels': 'sensitivity_labels_json',
    'visibility': 'visibility',
    'user_asserted': 'user_asserted',
    'captured_at': 'captured_at',
    'updated_at': 'updated_at',
    'expires_at': 'expires_at',
    'item_revision': 'item_revision',
    'account_generation': 'account_generation',
    'capture_device_ids': 'capture_device_ids_json',
    'primary_capture_device': 'primary_capture_device',
    'subject_entity_id': 'subject_entity_id',
    'predicate': 'predicate',
    'arguments': 'arguments_json',
    'kg_extracted': 'kg_extracted',
    'superseded_by': 'superseded_by',
}
INTAKE_COLUMNS = {
    'uid',
    'id',
    'content',
    'category',
    'visibility',
    'tags_json',
    'headline',
    'predicate',
    'arguments_json',
    'subject_entity_id',
    'subject_attribution',
    'object_entity_ids_json',
    'qualifiers_json',
    'capture_confidence',
    'veracity',
    'uncertainty_reasons_json',
    'durability',
    'manually_added',
    'memory_tier',
    'valid_at',
    'created_at',
    'updated_at',
}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def _instant(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _operation(control, row):
    memory_id, content = row['id'], row['content']
    source_version = hashlib.sha256(content.encode()).hexdigest()
    evidence = MemoryEvidence(
        evidence_id='native_' + hashlib.sha256(encoded([row['uid'], memory_id, source_version]).encode()).hexdigest(),
        source_type='explicit_submission',
        source_id=memory_id,
        source_version=source_version,
        content_hash=source_version,
        artifact_preservation=ArtifactPreservationState.preserved,
    )
    patch = {
        'patch_id': 'native_' + memory_id,
        'packet_id': memory_id,
        'run_id': 'native_' + memory_id,
        'observed_head_commit_id': control.head_commit_id,
        'idempotency_key': memory_id,
        'decision': 'add',
        'result_status': 'active',
        'evidence_ids': [evidence.evidence_id],
        'new_memory_id': memory_id,
        'memory_text': content,
        'confidence': 'medium',
        'relationship_to_user': 'self' if row.get('subject_attribution') == 'user' else 'unclear',
        'initial_tier': 'short_term',
        'visibility': row['visibility'],
        'user_asserted': bool(row.get('manually_added')),
        'subject_entity_id': row.get('subject_entity_id'),
        'predicate': row.get('predicate'),
        'arguments': json.loads(row.get('arguments_json') or '{}'),
        'captured_at': _instant(row['valid_at']),
        'updated_at': _instant(row['updated_at']),
        'expires_at': default_short_term_expiry(datetime.fromtimestamp(row['valid_at'], timezone.utc)).isoformat(),
        'promotion': {'intake_payload_digest': hashlib.sha256(encoded(row).encode()).hexdigest()},
    }
    # Product metadata also participates in retry identity, even though its
    # physical columns are not part of the upstream canonical MemoryItem.
    identity = build_patch_mutation_identity(patch)
    patch['mutation_metadata'] = identity
    operation = MemoryOperation.new(
        uid=control.uid,
        operation_type=MemoryOperationType.source_candidate,
        source_packet_id=memory_id,
        target_memory_id=None,
        evidence_ids=[evidence.evidence_id],
        logical_payload={
            'decision': 'add',
            'memory_text': content,
            'result_status': 'active',
            'subject_entity_id': patch['subject_entity_id'],
            'predicate': patch['predicate'],
            'arguments': patch['arguments'],
            'supersedes': [],
            'mutation_metadata': identity,
        },
        account_generation=control.account_generation,
        source_generation=control.source_generation,
        observed_head_commit_id=control.head_commit_id,
    )
    patch['evidence'] = [evidence]
    return operation, patch


def _stored_item(row, item):
    stored = dict(row)
    values = item.model_dump(mode='json')
    for field, column in MODEL_COLUMNS.items():
        value = values[field]
        if column.endswith('_json'):
            value = encoded(value)
        elif field in {'captured_at', 'updated_at', 'expires_at'} and value is not None:
            value = int(datetime.fromisoformat(value).timestamp())
        elif isinstance(value, bool):
            value = int(value)
        stored[column] = value
    stored['canonical_metadata_json'] = encoded(
        {key: value for key, value in values.items() if key not in MODEL_COLUMNS}
    )
    return stored


def _operation_identity(control, row):
    operation, _ = _operation(control, row)
    return operation.operation_id, operation.logical_payload_digest


def _insert_rows(db, table, rows):
    remaining = iter(rows)
    first = next(remaining, None)
    if first is None:
        return
    columns = sorted(first)
    expressions = {key: f"json_extract(source.value, '$.{key}')" for key in columns}
    if table == 'cf_memory_operations':
        # The item was inserted earlier in this same guarded batch. Transport
        # its text once, then restore the exact complete receipt in D1. A
        # missing owned item deliberately produces invalid JSON and aborts.
        expressions['operation_json'] = (
            "json_set(json_extract(source.value, '$.operation_json'), '$.logical_payload.memory_text', "
            "json_extract(COALESCE((SELECT json_array(content) FROM cf_memories "
            "WHERE uid = json_extract(source.value, '$.uid') AND id = "
            "json_extract(json_extract(source.value, '$.operation_json'), '$.committed_memory_item_ids[0]')), "
            "'missing owned intake item'), '$[0]'))"
        )
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) SELECT "
        + ', '.join(expressions[key] for key in columns)
        + ' FROM json_each(?) AS source'
    )
    parts, size = [], 2
    for row in chain((first,), remaining):
        value = encoded(row)
        length = len(value.encode()) + bool(parts)
        if length + 2 > 1_800_000:
            raise ValueError('memory apply record exceeds D1 bind limit')
        if size + length > 1_800_000:
            yield db.prepare(sql).bind('[' + ','.join(parts) + ']')
            parts, size = [], 2
        parts.append(value)
        size += length
    if parts:
        yield db.prepare(sql).bind('[' + ','.join(parts) + ']')


async def create_native_memories(env, uid, rows, extra_statements):
    """The native single/batch routes supply validated rows and owned side effects.

    Admission is rechecked inside the same transaction as every result write.
    A transient CAS conflict is returned to the caller rather than silently
    rebasing an accepted user operation against an unobserved generation.
    """
    if not rows or len(rows) > 100 or len({row['id'] for row in rows}) != len(rows):
        raise ValueError('invalid native memory batch')
    for row in rows:
        if set(row) - INTAKE_COLUMNS or row['uid'] != uid or row['memory_tier'] != 'short_term':
            raise ValueError('invalid native memory authority')
    db = env.APP_DB
    snapshot = (
        await db.prepare(
            'SELECT COALESCE((SELECT account_generation FROM cf_account_cutover WHERE uid = ?), 0) AS generation, '
            '(SELECT control_json FROM cf_memory_apply_control WHERE uid = ?) AS control_json, '
            '(EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = ?) OR '
            'EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = ?)) AS deleted'
        )
        .bind(uid, uid, uid, uid)
        .first()
    )
    if not snapshot or snapshot['deleted']:
        raise ValueError('memory_apply_account_deleted')
    prior = snapshot['control_json']
    control = (
        MemoryControlState.model_validate_json(prior)
        if prior is not None
        else MemoryControlState(
            uid=uid,
            head_commit_id='genesis_'
            + hashlib.sha256(encoded([uid, snapshot['generation']]).encode()).hexdigest()[:32],
            account_generation=snapshot['generation'],
            source_generation=0,
            writer_mode=WriterMode.compatibility,
        )
    )
    if control.uid != uid or control.account_generation != snapshot['generation']:
        raise ValueError('memory_apply_generation_changed')
    require_writer_admitted(control, MemoryWriterClass.user)
    identities = [_operation_identity(control, row) for row in rows]
    operations = (
        await db.prepare(
            'SELECT operation_id, operation_json FROM cf_memory_operations '
            'WHERE uid = ? AND operation_id IN (SELECT value FROM json_each(?))'
        )
        .bind(uid, encoded([operation_id for operation_id, _ in identities]))
        .all()
    )
    existing = {row['operation_id']: row['operation_json'] for row in operations['results']}
    # Native POST allocates fresh IDs. An exact internal retry may replay the
    # whole committed batch; a mixed replay requires its caller to reconcile
    # side effects and is not silently treated as a new transaction.
    if existing:
        if len(existing) != len(rows):
            raise ValueError('memory_apply_partial_replay')
        for row, (operation_id, digest) in zip(rows, identities):
            receipt = MemoryOperation.model_validate_json(existing[operation_id])
            if (
                receipt.logical_payload_digest != digest
                or receipt.uid != uid
                or receipt.status != MemoryOperationStatus.committed
                or receipt.committed_memory_item_ids != [row['id']]
            ):
                raise ValueError('memory_apply_payload_changed')
        live = (
            await db.prepare(
                'SELECT count(*) AS count FROM cf_memories WHERE uid = ? AND id IN (SELECT value FROM json_each(?)) '
                'AND deleted_at IS NULL AND invalid_at IS NULL AND account_generation = ? '
                'AND ? = COALESCE((SELECT account_generation FROM cf_account_cutover WHERE uid = ?), 0) '
                'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = ?) '
                'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = ?)'
            )
            .bind(
                uid,
                encoded([row['id'] for row in rows]),
                control.account_generation,
                control.account_generation,
                uid,
                uid,
                uid,
            )
            .first()
        )
        if not live or live['count'] != len(rows):
            raise ValueError('memory_apply_replay_source_unavailable')
        return
    statements = [
        db.prepare(
            'INSERT INTO cf_memory_apply_guard '
            '(uid, expected_control_json, account_generation, new_ids_json, operation_ids_json) VALUES (?, ?, ?, ?, ?)'
        ).bind(
            uid,
            prior,
            control.account_generation,
            encoded([row['id'] for row in rows]),
            encoded([operation_id for operation_id, _ in identities]),
        )
    ]
    records = {table: [] for table in ('cf_memory_operations', 'cf_memory_commits', 'cf_memory_outbox')}

    def item_records():
        nonlocal control
        for row, expected_identity in zip(rows, identities):
            operation, patch = _operation(control, row)
            if (operation.operation_id, operation.logical_payload_digest) != expected_identity:
                raise ValueError('native intake operation identity changed')
            result = apply_long_term_patch_transaction(control_state=control, operation=operation, patch_payload=patch)
            if result.status != ApplyStatus.committed or len(result.memory_items) != 1 or result.graph_assertions:
                raise ValueError('native memory apply was not admitted: ' + str(result.reason))
            item = result.memory_items[0]
            receipt = result.operation.model_dump(mode='json')
            if receipt['logical_payload'].pop('memory_text') != item.content:
                raise ValueError('native intake receipt does not match the committed item')
            records['cf_memory_operations'].append(
                {
                    'uid': uid,
                    'operation_id': result.operation.operation_id,
                    'logical_payload_digest': result.operation.logical_payload_digest,
                    'operation_json': encoded(receipt),
                    'created_at': row['created_at'],
                }
            )
            records['cf_memory_commits'].append(
                {
                    'uid': uid,
                    'commit_id': result.control_state.head_commit_id,
                    'parent_commit_id': control.head_commit_id,
                    'commit_sequence': result.control_state.commit_sequence,
                    'account_generation': control.account_generation,
                    'source_generation': control.source_generation,
                    'operation_id': operation.operation_id,
                    'memory_ids_json': encoded(result.operation.committed_memory_item_ids),
                    'outbox_ids_json': encoded(result.operation.committed_outbox_event_ids),
                    'created_at': row['created_at'],
                }
            )
            for event in result.outbox_events:
                records['cf_memory_outbox'].append(
                    {
                        'uid': uid,
                        'event_id': event.event_id,
                        'event_type': event.event_type.value,
                        'status': event.status.value,
                        'memory_id': event.memory_id,
                        'commit_id': event.commit_id,
                        'commit_sequence': event.commit_sequence,
                        'account_generation': event.account_generation,
                        'event_json': event.model_dump_json(),
                        'available_at': int(event.available_at.timestamp()),
                    }
                )
            control = result.control_state
            yield _stored_item(row, item)

    # Materialize one item at a time; only its bounded serialized chunk survives
    # until the atomic batch. Receipts carry no duplicate text in transport.
    statements.extend(_insert_rows(db, 'cf_memories', item_records()))
    for table, values in records.items():
        statements.extend(_insert_rows(db, table, values))
        # D1 statements own their serialized bindings now. Do not retain a
        # second full operation payload while the SDK serializes the batch.
        values.clear()
    statements.append(
        db.prepare(
            'INSERT INTO cf_memory_apply_control '
            '(uid, head_commit_id, account_generation, source_generation, commit_sequence, control_json) '
            'VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(uid) DO UPDATE SET head_commit_id = excluded.head_commit_id, '
            'account_generation = excluded.account_generation, source_generation = excluded.source_generation, '
            'commit_sequence = excluded.commit_sequence, control_json = excluded.control_json'
        ).bind(
            uid,
            control.head_commit_id,
            control.account_generation,
            control.source_generation,
            control.commit_sequence,
            control.model_dump_json(),
        )
    )
    statements.extend(extra_statements)
    statements.append(db.prepare('DELETE FROM cf_memory_apply_guard WHERE uid = ?').bind(uid))
    await db.batch(statements)
