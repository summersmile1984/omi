"""Apply an explicit content correction through the native memory journal.

The patch follows update_canonical_memory_content in the upstream adapter:
corrected content returns to pending Short-term, with its old graph admission
cleared. The historical projection is read-only; the only durable write is the
same guarded D1 transaction used by native intake.
"""

from datetime import datetime, timezone
import hashlib
import json

from memory_apply_intake import (
    MODEL_COLUMNS,
    _insert_rows,
    _stored_item,
    append_journal_records,
    control_statement,
    encoded,
    load_memory_control,
)
from memory_kernel_admission import REQUIRED_PROCESSOR_ID, REQUIRED_PROCESSOR_VERSION
from memory_kernel_apply import ApplyStatus, apply_long_term_patch_transaction, build_patch_mutation_identity
from memory_kernel_contracts import deterministic_contract_id
from memory_kernel_evidence import MemoryEvidence
from memory_kernel_item import MemoryItem
from memory_kernel_operations import MemoryOperation, MemoryOperationType
from memory_kernel_short_term_lifecycle import default_short_term_expiry

_SETTLED_PROMOTION_FIELDS = {
    'route',
    'reconciliation',
    'target_memory_id',
    'relationship_to_user',
    'aboutness',
    'basis_for_memory',
    'confidence',
    'rationale',
    'processed_at',
    'processed_by',
    'from_tier',
    'to_tier',
    'promoted_at',
    'graph_plan',
    'admission_receipt',
}


def read_item(row):
    metadata = json.loads(row['canonical_metadata_json'])
    if set(metadata) & set(MODEL_COLUMNS):
        raise ValueError('memory metadata duplicates physical authority')
    fields = dict(metadata)
    for field, column in MODEL_COLUMNS.items():
        fields[field] = json.loads(row[column]) if column.endswith('_json') else row[column]
    if not metadata:
        # Pre-journal rows have no fabricated durable commit. This deterministic
        # historical marker exists only in the read projection; apply replaces
        # it with its actual committed head before writing the item.
        fingerprint = hashlib.sha256(encoded(row).encode()).hexdigest()
        fields.update(ledger_commit_id='historical_' + fingerprint, ledger_sequence=0)
        if not fields['evidence'] and fields['source_state'] == 'active':
            fields['evidence'] = [
                MemoryEvidence(
                    evidence_id='historical_' + fingerprint,
                    source_type='legacy_memory',
                    source_id=row['id'],
                    source_version=fingerprint,
                    content_hash=hashlib.sha256(row['content'].encode()).hexdigest(),
                    artifact_preservation='preserved',
                )
            ]
        fields['promotion'] = {'historical_materialization': True}
    return MemoryItem.model_validate(fields)


def content_edit_patch(item, content, now):
    promotion = {key: value for key, value in (item.promotion or {}).items() if key not in _SETTLED_PROMOTION_FIELDS}
    receipt = promotion.pop('processing_receipt', None)
    processing_history = list(promotion.get('processing_history') or [])
    if isinstance(receipt, dict):
        processing_history.append(receipt)
    submission_history = list(promotion.get('submission_history') or [])
    if isinstance(promotion.get('submission'), dict):
        submission_history.append(promotion['submission'])
    promotion.update(
        required=True,
        status='pending',
        processing_status='pending_processing',
        processor_id=REQUIRED_PROCESSOR_ID,
        processor_version=REQUIRED_PROCESSOR_VERSION,
        reason='manual_user_correction',
        source_surface='memory_edit',
        attempt_count=0,
        reviewed=True,
        user_review=True,
        processing_history=processing_history[-10:],
        submission_history=submission_history[-10:],
        submission={
            'submission_id': f'{item.memory_id}:revision:{item.item_revision + 1}',
            'source_surface': 'memory_edit',
            'source_type': 'manual_edit',
            'source_id': item.memory_id,
            'content_hash': hashlib.sha256(content.encode()).hexdigest(),
            'submitted_at': now.isoformat(),
        },
    )
    return {
        'memory_text': content,
        'target_tier': 'short_term',
        'target_user_asserted': True,
        'clear_graph_assertion': True,
        'promotion_audit': promotion,
        'expires_at': default_short_term_expiry(now).isoformat(),
        'kg_extracted': False,
        'updated_at': now.isoformat(),
    }


async def edit_native_memory(env, uid, memory_id, content, now):
    """Return False only for an absent/deleted target; every write is atomic."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError('invalid memory correction')
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
    if item.uid != uid or item.account_generation != control.account_generation or item.source_state.value != 'active':
        raise ValueError('memory_apply_generation_or_source_changed')
    instant = max(datetime.fromtimestamp(now, timezone.utc), item.captured_at, item.updated_at)
    updates = content_edit_patch(item, content.strip(), instant)
    evidence_ids = [evidence.evidence_id for evidence in item.evidence]
    patch = {
        'decision': 'update',
        'target_memory_id': memory_id,
        'result_status': 'active',
        'evidence_ids': evidence_ids,
        'expected_item_revision': item.item_revision,
        'expected_content_hash': item.content_hash,
        **updates,
    }
    identity = build_patch_mutation_identity(patch)
    key = deterministic_contract_id(
        'canonical-memory-user-mutation',
        {
            'uid': uid,
            'memory_id': memory_id,
            'item_revision': item.item_revision,
            'mutation_kind': 'content_edit',
            'mutation_metadata': identity,
            'content': content.strip(),
        },
    )
    logical = {
        'decision': 'update',
        'target_memory_id': memory_id,
        'result_status': 'active',
        'memory_text': content.strip(),
        'target_tier': 'short_term',
        'target_user_asserted': True,
        'clear_graph_assertion': True,
        'mutation_metadata': identity,
    }
    operation = MemoryOperation.new(
        uid=uid,
        operation_type=MemoryOperationType.user_mutation,
        source_packet_id=f'user_mutation:content_edit:{memory_id}:r{item.item_revision}:{key[:16]}',
        target_memory_id=memory_id,
        evidence_ids=evidence_ids,
        logical_payload=logical,
        account_generation=control.account_generation,
        source_generation=control.source_generation,
        observed_head_commit_id=control.head_commit_id,
    )
    patch.update(
        patch_id='patch_user_' + key[:24],
        packet_id='user_mutation:content_edit:' + memory_id,
        run_id='user_mutation:content_edit:' + memory_id,
        observed_head_commit_id=control.head_commit_id,
        idempotency_key=key,
        mutation_metadata=identity,
        existing_item=item,
        evidence=item.evidence,
    )
    result = apply_long_term_patch_transaction(control_state=control, operation=operation, patch_payload=patch)
    if result.status != ApplyStatus.committed or len(result.memory_items) != 1 or result.graph_assertions:
        raise ValueError('memory content edit was not admitted')
    updated = MemoryItem.model_validate(result.memory_items[0].model_dump())
    stored = _stored_item(row, updated)
    stored.update(edited=1, reviewed=1, user_review=1)
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
    columns = sorted(
        (set(MODEL_COLUMNS.values()) | {'canonical_metadata_json', 'edited', 'reviewed', 'user_review'}) - {'uid', 'id'}
    )
    statements.append(
        db.prepare(
            'UPDATE cf_memories SET ' + ', '.join(column + ' = ?' for column in columns) + ' WHERE uid = ? AND id = ?'
        ).bind(*(stored[column] for column in columns), uid, memory_id)
    )
    records = {table: [] for table in ('cf_memory_operations', 'cf_memory_commits', 'cf_memory_outbox')}
    append_journal_records(records, uid, result, control, int(instant.timestamp()))
    for table, values in records.items():
        statements.extend(_insert_rows(db, table, values))
    statements.append(control_statement(db, uid, result.control_state))
    statements.append(db.prepare('DELETE FROM cf_memory_apply_guard WHERE uid = ?').bind(uid))
    await db.batch(statements)
    return True
