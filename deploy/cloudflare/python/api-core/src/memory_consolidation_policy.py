"""Build the upstream planner's two canonical applies; never decide an L2 route."""

from memory_kernel_apply import build_patch_mutation_identity
from memory_kernel_contracts import deterministic_contract_id
from memory_kernel_operations import MemoryOperation, MemoryOperationType
from memory_kernel_required_promotion import REQUIRED_PROMOTION_STATUS_PENDING, REQUIRED_PROCESSING_STATUS_PROCESSED
from memory_kernel_consolidation import (
    ConsolidationApplySkipped,
    HALF_LIFE_DAYS_BY_CLASS,
    _bind_required_promote_memory_text,
    _conserved_processed_source_attribution,
    _consolidation_decision_identity,
    _new_consolidation_operation,
    _ordered_route_evidence_ids,
    _processing_receipt,
    _promotion_audit,
    _route_logical_payload,
    belief_model_enabled,
)


def required_processing_patch(item, processed, control, now, *, attempt_count=1):
    evidence_ids = [record.evidence_id for record in item.evidence]
    attribution = _conserved_processed_source_attribution(item, processed)
    logical = {
        'decision': 'update',
        'target_memory_id': item.memory_id,
        'memory_text': processed.content,
        'result_status': 'active',
        'subject_entity_id': processed.subject_entity_id,
        'predicate': processed.predicate,
        'arguments': processed.arguments,
    }
    receipt = _processing_receipt(item, processed, now=now)
    promotion = {
        **(item.promotion or {}),
        'status': REQUIRED_PROMOTION_STATUS_PENDING,
        'processing_status': REQUIRED_PROCESSING_STATUS_PROCESSED,
        'processing_receipt': receipt,
        'attempt_count': attempt_count,
        'last_processing_error': None,
        'next_processing_attempt_at': None,
        'source_attribution': attribution,
    }
    key = deterministic_contract_id(
        'canonical-required-processing',
        {
            'uid': item.uid,
            'memory_id': item.memory_id,
            'input_item_revision': item.item_revision,
            'output_hash': receipt['output_hash'],
        },
    )
    patch = {
        'patch_id': f'patch_process_{key[:24]}',
        'packet_id': f'required_processing:{item.memory_id}',
        'run_id': f'required_processing:{item.memory_id}',
        'observed_head_commit_id': control.head_commit_id,
        'idempotency_key': key,
        **logical,
        'evidence_ids': evidence_ids,
        'expected_item_revision': item.item_revision,
        'expected_content_hash': item.content_hash,
        'promotion_audit': promotion,
        'sensitivity_labels': sorted(set(item.sensitivity_labels).union(processed.sensitivity_labels)),
    }
    identity = build_patch_mutation_identity(patch)
    patch['mutation_metadata'] = logical['mutation_metadata'] = identity
    operation = MemoryOperation.new(
        uid=item.uid,
        operation_type=MemoryOperationType.synthesis,
        source_packet_id=f'required_processing:{item.memory_id}:r{item.item_revision}:{receipt["output_hash"]}',
        target_memory_id=item.memory_id,
        evidence_ids=evidence_ids,
        logical_payload=logical,
        account_generation=control.account_generation,
        source_generation=control.source_generation,
        observed_head_commit_id=control.head_commit_id,
    )
    return operation, patch


def consolidation_route_patch(source, decision, control, run_id, now, *, quarantine=False):
    decision = _bind_required_promote_memory_text(source, decision)
    if (
        source.tier.value != 'short_term'
        or source.status.value != 'active'
        or source.processing_state.value != 'processed'
        or source.source_state.value != 'active'
    ):
        raise ConsolidationApplySkipped('source is no longer promotable')
    evidence_ids = _ordered_route_evidence_ids(source, decision)
    key = deterministic_contract_id(
        'canonical-promotion-route',
        _consolidation_decision_identity(
            uid=source.uid,
            source=source,
            decision=decision,
            quarantine=quarantine,
        ),
    )
    logical = _route_logical_payload(source=source, decision=decision, quarantine=quarantine)
    patch = {
        'patch_id': f'patch_cons_{key[:24]}',
        'packet_id': f'consolidation_{run_id}',
        'run_id': run_id,
        'observed_head_commit_id': control.head_commit_id,
        'idempotency_key': key,
        **logical,
        'evidence_ids': evidence_ids,
        'expected_item_revision': source.item_revision,
        'expected_content_hash': source.content_hash,
        'promotion_audit': _promotion_audit(
            source=source, decision=decision, evidence_ids=evidence_ids, now=now, quarantine=quarantine
        ),
    }
    if belief_model_enabled() and decision.route == 'reject':
        carried = getattr(decision, 'belief_class', None)
        patch['belief_class'] = carried if carried in HALF_LIFE_DAYS_BY_CLASS else 'meta_residue'
    identity = build_patch_mutation_identity(patch)
    patch['mutation_metadata'] = logical['mutation_metadata'] = identity
    operation = _new_consolidation_operation(
        uid=source.uid,
        source=source,
        decision=decision,
        control=control,
        evidence_ids=evidence_ids,
        logical_payload=logical,
        quarantine=quarantine,
    )
    return operation, patch
