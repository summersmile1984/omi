"""Execute the staged upstream apply rules; this does not simulate persistence."""

from datetime import datetime, timedelta, timezone

import pytest

from memory_kernel_apply import (
    ApplyStatus,
    MemoryControlState,
    apply_long_term_patch_transaction,
    memory_content_hash,
)
from memory_kernel_evidence import ArtifactPreservationState, MemoryEvidence, SourceState, SourceStateReason
from memory_kernel_item import MemoryItem, MemoryItemStatus, MemoryTier, ProcessingState
from memory_kernel_operations import MemoryOperation, MemoryOperationType
from memory_kernel_promotion import PromotionGraphPlan, build_promotion_admission_receipt


def control(**changes):
    return MemoryControlState(
        **{'uid': 'owner', 'head_commit_id': 'head0', 'account_generation': 1, 'source_generation': 2, **changes}
    )


def evidence():
    return MemoryEvidence(
        evidence_id='ev1',
        source_type='conversation',
        source_id='conversation1',
        source_version='v1',
        artifact_preservation=ArtifactPreservationState.preserved,
    )


def operation(*, logical=None, **changes):
    return MemoryOperation.new(
        **{
            'uid': 'owner',
            'operation_type': MemoryOperationType.long_term_apply,
            'source_packet_id': 'packet1',
            'target_memory_id': None,
            'evidence_ids': ['ev1'],
            'account_generation': 1,
            'source_generation': 2,
            'observed_head_commit_id': 'head0',
            'logical_payload': {
                'decision': 'add',
                'memory_text': 'User prefers concise updates.',
                'target_memory_id': None,
                'result_status': 'active',
                'supersedes': [],
                'subject_entity_id': 'user',
                'predicate': None,
                'arguments': {},
                'target_tier': None,
                **(logical or {}),
            },
            **changes,
        }
    )


def patch(**changes):
    return {
        'patch_id': 'patch1',
        'packet_id': 'packet1',
        'run_id': 'run1',
        'observed_head_commit_id': 'head0',
        'idempotency_key': 'intent1',
        'decision': 'add',
        'result_status': 'active',
        'evidence_ids': ['ev1'],
        'memory_text': 'User prefers concise updates.',
        'confidence': 'medium',
        'relationship_to_user': 'self',
        'subject_entity_id': 'user',
        'subject_label': 'the user',
        'aboutness': 'primary_user',
        **changes,
    }


def promotion_case(*, stale=False, missing=False):
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    item = MemoryItem(
        memory_id='memory1',
        uid='owner',
        version=1,
        tier=MemoryTier.short_term,
        status=MemoryItemStatus.active,
        processing_state=ProcessingState.processed,
        content='Short-term fact.',
        evidence=[evidence()],
        source_state=SourceState.active,
        sensitivity_labels=[],
        visibility='private',
        user_asserted=False,
        captured_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=2),
        ledger_commit_id='head0',
        ledger_sequence=1,
        source_commit_id='head0',
        source_commit_sequence=1,
        content_hash='old-hash',
        account_generation=1,
    )
    graph = PromotionGraphPlan(
        subject_entity_id='user', predicate='prefers_update_style', arguments={'style': 'concise'}
    )
    receipt = build_promotion_admission_receipt(
        memory_id=item.memory_id,
        source_item_revision=item.item_revision + int(stale),
        output_content_hash=memory_content_hash(content='User prefers concise updates.', evidence_ids=['ev1']),
        evidence_ids=['ev1'],
        graph_plan=graph,
        supersedes=[],
    )
    audit = (
        None
        if missing
        else {'graph_plan': graph.model_dump(mode='json'), 'admission_receipt': receipt.model_dump(mode='json')}
    )
    op = operation(
        operation_type=MemoryOperationType.synthesis,
        target_memory_id=item.memory_id,
        logical={
            'decision': 'update',
            'target_memory_id': item.memory_id,
            'predicate': 'prefers_update_style',
            'arguments': {'style': 'concise'},
            'target_tier': 'long_term',
        },
    )
    value = patch(
        decision='update',
        target_memory_id=item.memory_id,
        target_tier='long_term',
        predicate='prefers_update_style',
        arguments={'style': 'concise'},
        promotion_audit=audit,
        existing_item=item.model_dump(mode='json'),
    )
    return op, value


def test_intake_retries_preserve_ids_and_committed_replay_creates_no_second_work():
    op, value = operation(), patch()
    first = apply_long_term_patch_transaction(control_state=control(), operation=op, patch_payload=value)
    repeated = apply_long_term_patch_transaction(control_state=control(), operation=op, patch_payload=value)
    assert first.status == repeated.status == ApplyStatus.committed
    assert first.memory_items[0].tier == MemoryTier.short_term
    assert first.graph_assertions == []
    assert first.control_state.head_commit_id == repeated.control_state.head_commit_id
    assert first.operation.committed_memory_item_ids == repeated.operation.committed_memory_item_ids
    assert first.operation.committed_outbox_event_ids == repeated.operation.committed_outbox_event_ids
    assert {event.event_type.value for event in first.outbox_events} == {'projection_sync', 'vector_sync'}
    assert all(event.commit_id == first.control_state.head_commit_id for event in first.outbox_events)
    replay = apply_long_term_patch_transaction(
        control_state=first.control_state, operation=first.operation, patch_payload=value
    )
    assert replay.status == ApplyStatus.idempotent_skip
    assert replay.memory_items == replay.graph_assertions == replay.outbox_events == []
    changed = apply_long_term_patch_transaction(
        control_state=first.control_state,
        operation=first.operation,
        patch_payload=patch(memory_text='Different fact.'),
    )
    assert changed.status == ApplyStatus.payload_mismatch


@pytest.mark.parametrize(
    'changes,status',
    [
        ({'head_commit_id': 'head-new'}, ApplyStatus.retryable_head_mismatch),
        ({'account_generation': 2}, ApplyStatus.generation_mismatch),
        ({'source_generation': 3}, ApplyStatus.generation_mismatch),
    ],
)
def test_current_head_and_generations_fence_the_entire_result(changes, status):
    result = apply_long_term_patch_transaction(
        control_state=control(**changes), operation=operation(), patch_payload=patch()
    )
    assert result.status == status
    assert result.memory_items == result.graph_assertions == result.outbox_events == []


def test_tombstoned_evidence_cannot_create_memory_or_projection():
    deleted = evidence().model_copy(
        update={
            'source_state': SourceState.tombstoned,
            'source_state_reason': SourceStateReason.deleted_by_user,
            'artifact_preservation': ArtifactPreservationState.deleted_by_user,
        }
    )
    result = apply_long_term_patch_transaction(
        control_state=control(), operation=operation(), patch_payload=patch(evidence=[deleted])
    )
    assert result.status == ApplyStatus.source_not_active
    assert result.memory_items == result.outbox_events == []


def test_restricted_intake_is_delete_only_at_both_projection_boundaries():
    result = apply_long_term_patch_transaction(
        control_state=control(),
        operation=operation(),
        patch_payload=patch(sensitivity_labels=['credential']),
    )
    assert result.status == ApplyStatus.committed
    assert result.memory_items[0].sensitivity_labels == ['credential']
    assert [event.payload['action'] for event in result.outbox_events] == ['delete', 'delete']


@pytest.mark.parametrize('state', ['valid', 'stale', 'missing'])
def test_long_term_promotion_requires_the_original_receipt_and_bundles_graph(state):
    op, value = promotion_case(stale=state == 'stale', missing=state == 'missing')
    result = apply_long_term_patch_transaction(control_state=control(), operation=op, patch_payload=value)
    if state != 'valid':
        assert result.status == ApplyStatus.invalid_patch
        assert result.memory_items == result.graph_assertions == result.outbox_events == []
        return
    assert result.status == ApplyStatus.committed
    item, assertion = result.memory_items[0], result.graph_assertions[0]
    assert item.tier == MemoryTier.long_term and item.graph_ready
    assert item.graph_assertion_id == assertion.assertion_id
    assert item.item_revision == assertion.item_revision
    assert item.content_hash == assertion.content_hash
    assert assertion.commit_id == result.control_state.head_commit_id
    assert assertion.graph_records()['edges']


def test_new_intake_cannot_request_long_term_directly():
    result = apply_long_term_patch_transaction(
        control_state=control(),
        operation=operation(),
        patch_payload=patch(initial_tier='long_term'),
    )
    assert result.status == ApplyStatus.invalid_patch
    assert result.memory_items == result.graph_assertions == result.outbox_events == []
