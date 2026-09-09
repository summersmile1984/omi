"""Replay the observed real Qwen error through both admission and Server retry."""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fork.consolidation_admission import MemoryIdentity, validate_duplicate_creates
from fork import consolidation_transport
from fork.patches.consolidation import patches
from tests.unit.fixtures.strict_firestore_transaction import StrictFirestore
from tests.unit.test_canonical_consolidation import NOW, UID, _item
from models.memory_apply import MemoryControlState
from models.product_memory import MemoryTier
from utils.memory import canonical_consolidation as owner
from utils.memory.memory_system import MemorySystem

TEXT = 'User prefers drinking jasmine tea every morning as a long-term stable daily habit.'


def setup_context(monkeypatch, *, candidate_updates=None):
    pending = _item('duplicate-source', TEXT)
    candidate = _item('existing', TEXT, tier=MemoryTier.long_term)
    if candidate_updates:
        candidate = candidate.model_copy(update=candidate_updates)
    control = MemoryControlState(uid=UID, head_commit_id='head0', account_generation=1, source_generation=1)
    db = StrictFirestore(
        {
            ('users', UID, 'memory_state', 'apply_control'): control.model_dump(mode='python'),
            ('users', UID, 'memory_items', pending.memory_id): pending.model_dump(mode='python'),
            ('users', UID, 'memory_items', candidate.memory_id): candidate.model_dump(mode='python'),
        }
    )
    monkeypatch.setattr(owner, 'get_recent_rejected_memory_feedback', lambda *args, **kwargs: [])
    monkeypatch.setattr(
        owner,
        'query_memory_vector_candidates',
        lambda *args, **kwargs: SimpleNamespace(hits=[SimpleNamespace(memory_id=candidate.memory_id, score=0.8)]),
    )
    return pending, candidate, db


def captured_batch(pending):
    value = json.loads((Path(__file__).parent / 'fixtures/qwen_duplicate_create.json').read_text())
    value['decisions'][0]['source_memory_id'] = pending.memory_id
    value['decisions'][0]['evidence_ids'] = [entry.evidence_id for entry in pending.evidence]
    return owner.ConsolidationAgentBatch.model_validate(value)


def test_shared_guard_uses_full_hydrated_identity_and_preserves_prompt(monkeypatch):
    pending, candidate, db = setup_context(monkeypatch)
    original_hydrate = owner._hydrate_memory_item
    plain = owner.gather_consolidation_candidates(UID, [pending], db_client=db)
    wrapped = consolidation_transport.gather(owner.gather_consolidation_candidates)
    context = wrapped(UID, [pending], db_client=db)
    assert context.admission_memories[candidate.memory_id] == MemoryIdentity.from_item(candidate)
    assert owner._hydrate_memory_item is original_hydrate
    assert [m.content for m in owner.build_consolidation_llm_messages(context)] == [
        m.content for m in owner.build_consolidation_llm_messages(plain)
    ]
    batch = captured_batch(pending)
    assert owner._validate_agent_batch(plain, batch) is None
    validate = consolidation_transport.validate(owner._validate_agent_batch)
    assert validate(context, batch) == 'output_invalid:exact_duplicate_create'
    # Missing internal provenance cannot bypass the guard. This is not an
    # account migration gate: the next case covers an existing legacy row.
    assert validate(plain, batch) == 'output_invalid:hydrated_identity_context_missing'
    later = wrapped(UID, [_item('other-source', 'Other observation')], db_client=db)
    assert pending.memory_id not in later.admission_memories
    assert 'other-source' not in context.admission_memories


@pytest.mark.parametrize(
    'candidate_changes,expected',
    [
        ({}, 'output_invalid:exact_duplicate_create'),
        ({'subject': None}, None),
        ({'subject': 'person:alex'}, None),
        ({'content': 'User enjoys jasmine tea each morning.'}, None),
        ({'content': TEXT + ' But not on weekends.'}, None),
        ({'tier': 'archive'}, None),
        ({'active': False}, None),
        ({'uid': 'another-owner'}, 'output_invalid:candidate_identity_unavailable'),
    ],
)
def test_admission_does_not_infer_semantic_equivalence(monkeypatch, candidate_changes, expected):
    pending, candidate, db = setup_context(monkeypatch)
    context = consolidation_transport.gather(owner.gather_consolidation_candidates)(UID, [pending], db_client=db)
    identities = dict(context.admission_memories)
    identities[candidate.memory_id] = replace(identities[candidate.memory_id], **candidate_changes)
    assert validate_duplicate_creates(context, captured_batch(pending), identities) == expected


def test_existing_candidate_without_subject_provenance_remains_processable(monkeypatch):
    pending, candidate, db = setup_context(monkeypatch, candidate_updates={'promotion': {}, 'user_asserted': False})
    context = consolidation_transport.gather(owner.gather_consolidation_candidates)(UID, [pending], db_client=db)
    assert context.admission_memories[candidate.memory_id].subject is None
    assert consolidation_transport.validate(owner._validate_agent_batch)(context, captured_batch(pending)) is None


def test_observed_invalid_create_uses_upstream_retry_and_never_applies(monkeypatch):
    pending, candidate, db = setup_context(monkeypatch)
    for patch in patches():
        assert patch.applies_to({'target': 'self_hosted'})
        assert not patch.applies_to({'target': 'omi_cloud'})
        monkeypatch.setattr(owner, patch.attribute, patch.build(getattr(owner, patch.attribute)))
    monkeypatch.setattr(owner, 'resolve_memory_system', lambda *args: MemorySystem.CANONICAL)
    monkeypatch.setattr(owner, 'list_pending_consolidation_items', lambda *args, **kwargs: [pending])
    apply_route = Mock()
    monkeypatch.setattr(owner, 'apply_consolidation_decision', apply_route)
    report = owner.run_canonical_consolidation(
        UID,
        db_client=db,
        run_id='observed-qwen-error',
        now=NOW,
        llm_invoke=lambda messages: captured_batch(pending).model_dump_json(),
    )
    apply_route.assert_not_called()
    assert report.watermark_blocked
    assert report.retryable_memory_ids == [pending.memory_id]
    state = owner._read_retry_state(UID, pending, db_client=db)
    assert state.status == 'retryable' and state.attempt_count == 1
    assert state.last_error_code == 'output_invalid:exact_duplicate_create'
    assert db.document(f'users/{UID}/memory_items/{pending.memory_id}').get().to_dict() == pending.model_dump(
        mode='python'
    )
