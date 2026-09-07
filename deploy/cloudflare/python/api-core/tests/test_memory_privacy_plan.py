"""Exercise the upstream privacy scrubbers and complete-lineage planning.

These tests verify a proposed canonical result. They do not substitute for
the D1 transaction, anti-resurrection receipts or observed provider erasure.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from memory_kernel_apply import MemoryControlState, WriterMode
from memory_kernel_evidence import MemoryEvidence
from memory_kernel_item import MemoryItem
from memory_kernel_operations import MemoryOperation
from memory_privacy_plan import build_privacy_result, privacy_lineage_ids

NOW = datetime(2026, 9, 7, 4, tzinfo=timezone.utc)
NONCE = 'a' * 64


def item(memory_id='memory', **changes):
    return MemoryItem.model_validate(
        {
            'memory_id': memory_id,
            'uid': 'owner',
            'version': 2,
            'tier': 'short_term',
            'status': 'active',
            'processing_state': 'processed',
            'content': 'Private content',
            'evidence': [
                MemoryEvidence(
                    evidence_id='evidence',
                    source_type='conversation',
                    source_id='private-conversation',
                    source_version='private-source-version',
                    content_hash='private-source-hash',
                    artifact_preservation='preserved',
                    conversation_id='private-conversation',
                    quote_refs=[{'quote': 'private quote'}],
                    client_device_id='private-device',
                    lineage_id='private-source-lineage',
                    artifact_refs=[{'uri': 'r2://private-object', 'preservation': 'preserved'}],
                )
            ],
            'source_state': 'active',
            'sensitivity_labels': ['health'],
            'visibility': 'private',
            'user_asserted': True,
            'captured_at': NOW,
            'updated_at': NOW,
            'expires_at': NOW + timedelta(days=2),
            'ledger_commit_id': 'old-content-derived-head',
            'ledger_sequence': 3,
            'source_commit_id': 'old-content-derived-source',
            'source_commit_sequence': 2,
            'content_hash': 'private-memory-hash',
            'account_generation': 1,
            'item_revision': 4,
            'promotion': {'submission': {'private': 'old submission'}},
            'capture_device_ids': ['private-device'],
            'primary_capture_device': 'private-device',
            'subject_entity_id': 'private-person',
            'predicate': 'private_predicate',
            'arguments': {'secret': 'value'},
            **changes,
        }
    )


def control(**changes):
    return MemoryControlState(
        **{
            'uid': 'owner',
            'head_commit_id': 'old-content-derived-head',
            'account_generation': 1,
            'source_generation': 2,
            'commit_sequence': 3,
            'projection_watermark_commit_id': 'old-projection',
            'vector_watermark_commit_id': 'old-vector',
            **changes,
        }
    )


@pytest.mark.parametrize(
    'kind,extra',
    [
        ('fact', {'slot': 'private-slot'}),
        ('document', {'body': 'Private playbook body'}),
        ('trigger', {'trigger_condition': {'action': 'Private action prompt'}}),
    ],
)
def test_tombstone_scrubs_all_semantic_and_provenance_fields(kind, extra):
    original = item(kind=kind, **extra)
    before = original.model_dump()
    result = build_privacy_result(control(), [original], epoch_nonce=NONCE, now=NOW)
    deleted = MemoryItem.model_validate(result.memory_items[0].model_dump())
    assert original.model_dump() == before
    assert deleted.status.value == deleted.source_state.value == 'tombstoned'
    for field in [
        'content',
        'normalized_content_key',
        'content_hash',
        'promotion',
        'body',
        'slot',
        'subject_entity_id',
        'predicate',
        'primary_capture_device',
        'graph_assertion_id',
        'graph_plan_hash',
    ]:
        assert getattr(deleted, field) is None, field
    assert deleted.arguments == deleted.trigger_condition == {}
    assert deleted.capture_device_ids == deleted.sensitivity_labels == []
    assert not deleted.graph_ready and not deleted.kg_extracted
    assert deleted.item_revision == original.item_revision + 1 and deleted.version == original.version + 1
    assert deleted.source_commit_id == deleted.ledger_commit_id == result.control_state.head_commit_id
    evidence = deleted.evidence[0]
    assert evidence.source_state.value == 'tombstoned'
    assert evidence.artifact_preservation.value == 'deleted_by_user'
    assert evidence.artifact_refs == evidence.quote_refs == []
    assert all(
        getattr(evidence, field) is None
        for field in [
            'source_id',
            'source_version',
            'content_hash',
            'conversation_id',
            'lineage_id',
            'client_device_id',
            'patch_id',
            'commit_id',
        ]
    )
    assert 'Private' not in deleted.model_dump_json()


def test_privacy_operation_and_epoch_do_not_depend_on_deleted_content_or_old_head():
    original = item()
    first = build_privacy_result(control(), [original], epoch_nonce=NONCE, now=NOW)
    changed = original.model_copy(
        update={'content': 'Different sensitive claim', 'content_hash': 'another-secret-hash'}
    )
    second = build_privacy_result(control(head_commit_id='different-prior-head'), [changed], epoch_nonce=NONCE, now=NOW)
    assert first.control_state.head_commit_id == second.control_state.head_commit_id
    assert first.operation.operation_id == second.operation.operation_id
    assert first.operation.observed_head_commit_id is None and first.operation.evidence_ids == []
    assert first.control_state.projection_watermark_commit_id is None
    assert first.control_state.vector_watermark_commit_id is None
    MemoryOperation.model_validate_json(first.operation.model_dump_json())
    for event in first.outbox_events:
        assert event.parent_commit_id == event.commit_id == first.control_state.head_commit_id
        assert event.payload['action'] == 'delete' and event.payload['content_hash'] is None
        assert event.payload['item_revision'] == first.memory_items[0].item_revision
    assert len(first.outbox_events) == 2
    third = build_privacy_result(control(), [original], epoch_nonce='b' * 64, now=NOW)
    assert third.control_state.head_commit_id != first.control_state.head_commit_id


@pytest.mark.parametrize('mode', list(WriterMode))
def test_writer_transitions_do_not_block_privacy_planning(mode):
    owner = 'transition-owner' if mode.value.startswith('transitioning_') else None
    result = build_privacy_result(
        control(writer_mode=mode, writer_transition_owner=owner), [item()], epoch_nonce=NONCE, now=NOW
    )
    assert result.memory_items[0].status.value == 'tombstoned'


def test_complete_lineage_includes_incoming_aliases_cycles_and_missing_survivors():
    values = [
        item('tail', canonical_memory_id='b'),
        item('b', canonical_memory_id='c'),
        item('c', superseded_by='b'),
        item('separate'),
        item('missing-a', canonical_memory_id='missing'),
        item('missing-b', superseded_by='missing'),
    ]
    for requested in ['tail', 'b', 'c']:
        assert privacy_lineage_ids('owner', [requested], values) == ['b', 'c', 'tail']
    assert privacy_lineage_ids('owner', ['missing-a'], values) == ['missing-a', 'missing-b']
    values[0] = values[0].model_copy(update={'status': 'tombstoned', 'content': None})
    assert privacy_lineage_ids('owner', ['tail'], values) == ['b', 'c', 'tail']  # Retry inventory.


@pytest.mark.parametrize('problem', ['foreign', 'duplicate', 'missing'])
def test_lineage_authority_rejects_unowned_ambiguous_and_absent_targets(problem):
    values = [item()]
    requested = ['memory']
    if problem == 'foreign':
        values.append(item('other', uid='other-owner'))
    elif problem == 'duplicate':
        values.append(item())
    else:
        requested = ['missing']
    with pytest.raises(ValueError):
        privacy_lineage_ids('owner', requested, values)


@pytest.mark.parametrize('problem', ['foreign', 'duplicate', 'tombstoned', 'nonce', 'clock'])
def test_invalid_privacy_proposals_are_rejected(problem):
    values, nonce, now = [item()], NONCE, NOW
    if problem == 'foreign':
        values = [item(uid='other')]
    elif problem == 'duplicate':
        values *= 2
    elif problem == 'tombstoned':
        values = [item(status='tombstoned')]
    elif problem == 'nonce':
        nonce = 'user-content'
    else:
        now = NOW.replace(tzinfo=None)
    with pytest.raises(ValueError):
        build_privacy_result(control(), values, epoch_nonce=nonce, now=now)
