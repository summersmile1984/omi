"""Run the registered mutation repair through upstream feedback and apply owners."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from fork.patches import collect
from fork.patches.canonical_memory import patches
from models.jit_proactivity import JITProactivityEventReceipt
from models.memory_apply import ApplyStatus, MemoryControlState, apply_long_term_patch_transaction
from models.memory_operations import MemoryOperationType
from models.product_memory import LedgerWriteReason, MemoryAccessPolicy, MemoryKind, MemorySubjectScope, ProcessingState
from tests.unit.test_memory_apply_store import _db_with, _short_term_target, _stored_model, _target_item, store
from utils.memory import canonical_memory_adapter as owner
from utils.memory import canonical_required_processing as processing
from utils.memory.memory_system import (
    CANONICAL_MEMORY_MAINTENANCE_REGISTRY_SCHEMA_VERSION,
    canonical_memory_maintenance_registry_path,
)


def trigger_item():
    return _target_item(
        kind=MemoryKind.trigger,
        ledger_schema_version='knowledge_ledger.v1',
        subject_scope=MemorySubjectScope.primary_user,
        intent_backed=True,
        write_reason=LedgerWriteReason.standing_trigger,
        trigger_condition={'keywords': ['release'], 'action': {'type': 'agent_prompt', 'prompt': 'Next step.'}},
    )


@pytest.fixture
def registered(monkeypatch):
    expected = {patch.name for patch in patches()}
    selected = [patch for patch in collect() if patch.name in expected]
    assert len(selected) == 2
    for patch in selected:
        assert patch.applies_to({'target': 'self_hosted'})
        assert not patch.applies_to({'target': 'omi_cloud'})
        assert not patch.applies_to({'target': 'cloudflare'})
        module, original = patch.target()
        monkeypatch.setattr(module, patch.attribute, patch.build(original))


@pytest.mark.parametrize('entrypoint', ['_apply_canonical_user_mutation', 'apply_canonical_user_mutation'])
@pytest.mark.parametrize('arguments', [{'jit_trigger_feedback': {'last_action': 'useful', 'feedback_count': 1}}, {}])
def test_canonical_user_mutation_hashes_feedback_arguments_before_apply(registered, monkeypatch, entrypoint, arguments):
    item = trigger_item()
    control = MemoryControlState(uid='u1', head_commit_id='head0', account_generation=1, source_generation=2)
    observed = []

    def persist(**kwargs):
        result = apply_long_term_patch_transaction(
            control_state=control,
            operation=kwargs['proposed_operation'],
            patch_payload={**kwargs['patch_payload'], 'existing_item': item, 'evidence': item.evidence},
            allow_trigger_feedback_arguments=True,
        )
        observed.append(result)
        return result

    monkeypatch.setattr(owner, '_read_canonical_memory_item', lambda *a, **kw: item)
    monkeypatch.setattr(owner, '_ensure_control_state', lambda *a, **kw: control)
    monkeypatch.setattr(owner, 'apply_direct_user_long_term_patch_firestore', persist)
    logical = {'result_status': 'active'}
    physical = {'arguments': arguments, 'curation_weight': 1}
    before = deepcopy((logical, physical))
    _, updated = getattr(owner, entrypoint)(
        'u1',
        item.memory_id,
        mutation_kind='jit_trigger_feedback:' + 'f' * 64,
        operation_type=MemoryOperationType.ledger_mutation,
        build_patch=lambda *_: (logical, physical),
        db_client=object(),
    )
    assert len(observed) == 1 and observed[0].status == ApplyStatus.committed
    assert updated.arguments == arguments and updated.curation_weight == 1
    assert updated.item_revision == item.item_revision + 1
    assert (logical, physical) == before


def test_feedback_commits_and_replays_through_real_server_owners(registered, monkeypatch, store):
    monkeypatch.setenv('MEMORY_MODE', 'read')
    item = trigger_item()
    db = _db_with(target_items=[item])
    db.docs[canonical_memory_maintenance_registry_path('u1')] = {
        'uid': 'u1',
        'schema_version': CANONICAL_MEMORY_MAINTENANCE_REGISTRY_SCHEMA_VERSION,
    }
    now = datetime.now(timezone.utc)
    db.docs[f"users/u1/jit_proactivity_events/{'e' * 64}"] = _stored_model(
        JITProactivityEventReceipt(
            uid='u1',
            event_id='e' * 64,
            candidate_id='c' * 64,
            operation='planned_notification',
            account_generation=1,
            trigger_memory_id=item.memory_id,
            trigger_revision=item.item_revision,
            budget_day=now.date().isoformat(),
            device_id='d' * 64,
            created_at=now,
            request_hash='c' * 64,
        )
    )
    for name in ('apply_direct_user_long_term_patch_firestore', 'read_trigger_feedback_replay_firestore'):
        monkeypatch.setattr(owner, name, getattr(store, name))
    request = dict(
        event_id='e' * 64,
        expected_account_generation=1,
        expected_item_revision=item.item_revision,
        feedback={'feedback_id': 'f' * 64, 'action': 'useful', 'recorded_at': now},
        db_client=db,
    )
    first = owner.apply_canonical_trigger_feedback('u1', item.memory_id, **request)
    assert first.applied and first.item.curation_weight == 1
    assert first.item.arguments['jit_trigger_feedback']['feedback_count'] == 1
    assert first.receipt.applied_trigger_revision == item.item_revision + 1
    committed = deepcopy(db.docs)
    replay = owner.apply_canonical_trigger_feedback('u1', item.memory_id, **request)
    assert not replay.applied and replay.item.item_revision == first.item.item_revision
    assert db.docs == committed
    with pytest.raises(store.MemoryFirestoreApplyError, match='different payload'):
        owner.apply_canonical_trigger_feedback(
            'u1', item.memory_id, **{**request, 'feedback': {**request['feedback'], 'action': 'disable'}}
        )
    assert db.docs == committed


def test_repeated_review_preserves_memory_and_durable_state(registered, store):
    item = _target_item(promotion={'reviewed': True, 'user_review': True})
    db = _db_with(target_items=[item])
    db.docs[canonical_memory_maintenance_registry_path('u1')] = {
        'uid': 'u1',
        'schema_version': CANONICAL_MEMORY_MAINTENANCE_REGISTRY_SCHEMA_VERSION,
    }
    before = deepcopy(db.docs)

    updated = owner.update_canonical_memory_review('u1', item.memory_id, True, db_client=db)

    assert updated == item
    assert db.docs == before


@pytest.mark.parametrize('entrypoint', ['_apply_canonical_user_mutation', 'apply_canonical_user_mutation'])
def test_noop_mutation_preserves_memory_and_durable_state(registered, store, entrypoint):
    item = _target_item()
    db = _db_with(target_items=[item])
    db.docs[canonical_memory_maintenance_registry_path('u1')] = {
        'uid': 'u1',
        'schema_version': CANONICAL_MEMORY_MAINTENANCE_REGISTRY_SCHEMA_VERSION,
    }
    before = deepcopy(db.docs)

    previous, updated = getattr(owner, entrypoint)(
        'u1',
        item.memory_id,
        mutation_kind='noop',
        build_patch=lambda *_: None,
        db_client=db,
    )

    assert previous == updated == item
    assert db.docs == before


def test_accepted_edit_requires_receipted_processing_before_chat_visibility(registered, monkeypatch, store):
    monkeypatch.setenv('MEMORY_MODE', 'read')
    monkeypatch.setenv('MEMORY_BELIEF_MODEL_ENABLED', 'false')
    item = _short_term_target(
        user_asserted=True,
        subject_entity_id='user',
        promotion={'reviewed': True, 'user_review': True},
    )
    db = _db_with(target_items=[item])
    db.docs[canonical_memory_maintenance_registry_path('u1')] = {
        'uid': 'u1',
        'schema_version': CANONICAL_MEMORY_MAINTENANCE_REGISTRY_SCHEMA_VERSION,
    }
    monkeypatch.setattr(
        owner, 'apply_direct_user_long_term_patch_firestore', store.apply_direct_user_long_term_patch_firestore
    )
    monkeypatch.setattr(processing, 'apply_long_term_patch_firestore', store.apply_long_term_patch_firestore)
    content = 'My verification color is amber.'
    edited = owner.update_canonical_memory_content('u1', item.memory_id, content, db_client=db)
    assert edited.processing_state == ProcessingState.pending
    assert edited.promotion['user_review'] is True
    policy = MemoryAccessPolicy.for_omi_chat()
    assert owner.filter_canonical_default_visible_items([edited], policy=policy, now=datetime.now(timezone.utc)) == []
    assert owner.read_canonical_memory_item('other-account', item.memory_id, db_client=db) is None

    # This is the exact normalization/apply owner used by consolidation, not a
    # reader fast-path or an assignment to processing_state.
    processed = processing.commit_required_processing(
        edited,
        processing.ProcessedRequiredMemory(content=content),
        db_client=db,
        now=datetime.now(timezone.utc),
    )
    visible = owner.filter_canonical_default_visible_items([processed], policy=policy, now=datetime.now(timezone.utc))
    assert [memory.content for memory in visible] == [content]
    assert processed.promotion['processing_receipt']
    assert owner.read_canonical_memory_item('other-account', item.memory_id, db_client=db) is None
