"""Exercise the installed fork clock seam through the unchanged operation owner."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from fork import memory_operation_clock
from fork.patches import collect, collect_memory_projection
from models.memory_operations import MemoryOperation, MemoryOperationStatus, MemoryOperationType


@pytest.mark.parametrize('backwards', [timedelta(microseconds=1), timedelta(seconds=2)])
def test_registered_operation_transition_remains_monotonic_without_relaxing_decoding(monkeypatch, backwards):
    patch = next(p for p in collect() if p.name == 'canonical-memory.operation-clock')
    assert patch.name in {p.name for p in collect_memory_projection()}
    assert patch.applies_to({'target': 'self_hosted'})
    assert not patch.applies_to({'target': 'omi_cloud'})
    module, original = patch.target()
    # Restore the class method after this test, including already-captured users.
    monkeypatch.setattr(original, '_transition', original._transition)
    monkeypatch.setattr(module, patch.attribute, patch.build(original))
    operation = MemoryOperation.new(
        uid='clock-owner',
        operation_type=MemoryOperationType.source_candidate,
        source_packet_id='packet',
        target_memory_id=None,
        evidence_ids=[],
        logical_payload={'decision': 'add', 'memory_text': 'Synthetic capture'},
        account_generation=0,
        source_generation=0,
    )
    now = operation.created_at + timedelta(seconds=1)
    monkeypatch.setattr(memory_operation_clock, 'wall_now', lambda tz: now)
    retry = operation.mark_retryable('transaction_conflict')
    now -= backwards
    committed = retry.mark_committed('next-head', committed_sequence=1)
    assert committed.updated_at == retry.updated_at
    assert committed.created_at == operation.created_at
    assert committed.operation_id == operation.operation_id
    assert committed.status == MemoryOperationStatus.committed
    with pytest.raises(ValueError, match='terminal'):
        committed.mark_retryable('must-not-reopen')
    invalid = committed.model_dump(mode='python')
    invalid['updated_at'] = operation.created_at - timedelta(microseconds=1)
    with pytest.raises(ValidationError, match='updated_at must be >= created_at'):
        MemoryOperation.model_validate(invalid)
