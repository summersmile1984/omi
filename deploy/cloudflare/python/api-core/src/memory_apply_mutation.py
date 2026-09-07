"""One transaction owner for ordinary canonical user mutations and their graph."""

from datetime import datetime, timezone
import json

from memory_apply_item import read_item
from memory_apply_intake import (
    MODEL_COLUMNS,
    _insert_rows,
    _stored_item,
    append_journal_records,
    control_statement,
    encoded,
    load_memory_control,
)
from memory_kernel_apply import ApplyStatus, apply_long_term_patch_transaction, build_patch_mutation_identity
from memory_kernel_contracts import deterministic_contract_id
from memory_kernel_item import MemoryItem
from memory_kernel_operations import MemoryOperation, MemoryOperationType
from memory_kernel_promotion import MemoryGraphAssertion
from memory_review_store import CanonicalReviewResolution
from vector_search import publish_vector_projection

PRODUCT_COLUMNS = {
    'edited',
    'reviewed',
    'user_review',
    'is_read',
    'is_dismissed',
    'is_baseline',
    'category',
    'tags_json',
}


async def apply_user_memory_mutation(
    env,
    uid,
    memory_id,
    now,
    *,
    kind,
    build_patch,
    extra_statements=(),
    review_resolution: CanonicalReviewResolution | None = None,
):
    """False is an absent target; receipts, graph, feedback and row commit together."""
    db = env.APP_DB
    prior, control = await load_memory_control(env, uid)
    row = (
        await db.prepare(
            "SELECT * FROM cf_memories WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL "
            "AND status = 'active' AND superseded_by IS NULL"
        )
        .bind(uid, memory_id)
        .first()
    )
    if row is None:
        return False
    if row['is_locked']:
        raise ValueError('memory_locked_for_mutation')
    item = read_item(row)
    if review_resolution is not None:
        review_resolution.validate(item)
    if item.uid != uid or item.account_generation != control.account_generation or item.source_state.value != 'active':
        raise ValueError('memory_apply_generation_or_source_changed')
    # Legacy product columns remain the read authority until their writer is
    # adopted. Preserve them when another product field joins canonical apply.
    metadata = {
        key: row[key] for key in ('category', 'reviewed', 'user_review', 'is_read', 'is_dismissed', 'is_baseline')
    }
    metadata['tags'] = json.loads(row['tags_json'])
    for key in ('reviewed', 'user_review', 'is_read', 'is_dismissed', 'is_baseline'):
        if metadata[key] is not None:
            metadata[key] = bool(metadata[key])
    item = item.model_copy(update={'promotion': {**(item.promotion or {}), **metadata}})
    instant = max(datetime.fromtimestamp(now, timezone.utc), item.captured_at, item.updated_at)
    logical_updates, patch_updates, physical = build_patch(item, instant)
    if not set(physical).issubset(PRODUCT_COLUMNS):
        raise ValueError('unsupported memory product mutation')
    logical = {'decision': 'update', 'target_memory_id': memory_id, 'result_status': 'active', **logical_updates}
    evidence_ids = [evidence.evidence_id for evidence in item.evidence]
    identity = build_patch_mutation_identity(
        {
            **logical,
            'evidence_ids': evidence_ids,
            'expected_item_revision': item.item_revision,
            'expected_content_hash': item.content_hash,
            **patch_updates,
        }
    )
    logical['mutation_metadata'] = identity
    key = deterministic_contract_id(
        'canonical-memory-user-mutation',
        {
            'uid': uid,
            'memory_id': memory_id,
            'item_revision': item.item_revision,
            'mutation_kind': kind,
            'logical_payload': logical,
        },
    )
    operation = MemoryOperation.new(
        uid=uid,
        operation_type=MemoryOperationType.user_mutation,
        source_packet_id=f'user_mutation:{kind}:{memory_id}:r{item.item_revision}:{key[:16]}',
        target_memory_id=memory_id,
        evidence_ids=evidence_ids,
        logical_payload=logical,
        account_generation=control.account_generation,
        source_generation=control.source_generation,
        observed_head_commit_id=control.head_commit_id,
    )
    patch = {
        'patch_id': 'patch_user_' + key[:24],
        'packet_id': f'user_mutation:{kind}:{memory_id}',
        'run_id': f'user_mutation:{kind}:{memory_id}',
        'observed_head_commit_id': control.head_commit_id,
        'idempotency_key': key,
        **logical,
        'evidence_ids': evidence_ids,
        'expected_item_revision': item.item_revision,
        'expected_content_hash': item.content_hash,
        **patch_updates,
        'mutation_metadata': identity,
        'existing_item': item,
        'evidence': item.evidence,
    }
    result = apply_long_term_patch_transaction(control_state=control, operation=operation, patch_payload=patch)
    if result.status != ApplyStatus.committed or len(result.memory_items) != 1:
        raise ValueError('memory user mutation was not admitted')
    updated = MemoryItem.model_validate(result.memory_items[0].model_dump())
    stored = {**_stored_item(row, updated), **physical}
    expected = {
        key: row[key]
        for key in (
            'id',
            'item_revision',
            'version',
            'canonical_metadata_json',
            'capture_device_ids_json',
            'primary_capture_device',
        )
    }
    statements = [
        db.prepare(
            'INSERT INTO cf_memory_apply_guard (uid, expected_control_json, account_generation, '
            'new_ids_json, operation_ids_json, expected_items_json) VALUES (?, ?, ?, ?, ?, ?)'
        ).bind(uid, prior, control.account_generation, '[]', encoded([operation.operation_id]), encoded([expected]))
    ]
    if review_resolution is not None:
        statements.append(review_resolution.admission(db, uid))
    columns = sorted((set(MODEL_COLUMNS.values()) | {'canonical_metadata_json'} | set(physical)) - {'uid', 'id'})
    statements.append(
        db.prepare(
            'UPDATE cf_memories SET ' + ', '.join(column + ' = ?' for column in columns) + ' WHERE uid = ? AND id = ?'
        ).bind(*(stored[column] for column in columns), uid, memory_id)
    )
    statements.extend(extra_statements)
    for assertion in result.graph_assertions:
        assertion = MemoryGraphAssertion.model_validate(assertion.model_dump())
        statements.extend(
            _insert_rows(
                db,
                'cf_memory_graph_assertions',
                [
                    {
                        'uid': uid,
                        'memory_id': assertion.memory_id,
                        'item_revision': assertion.item_revision,
                        'account_generation': control.account_generation,
                        'assertion_json': assertion.model_dump_json(),
                    }
                ],
            )
        )
    records = {table: [] for table in ('cf_memory_operations', 'cf_memory_commits', 'cf_memory_outbox')}
    append_journal_records(records, uid, result, control, int(instant.timestamp()))
    for table, values in records.items():
        statements.extend(_insert_rows(db, table, values))
    statements.append(control_statement(db, uid, result.control_state))
    if review_resolution is not None:
        statements.extend(
            review_resolution.resolution_statements(
                db, uid, result.control_state.head_commit_id, int(instant.timestamp())
            )
        )
    statements.append(db.prepare('DELETE FROM cf_memory_apply_guard WHERE uid = ?').bind(uid))
    await db.batch(statements)
    await publish_vector_projection(env, uid=uid, source_kind='memory', source_id=memory_id)
    return True
