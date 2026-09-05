"""Real dynamic-path owners must run using the migration-admitted inventory."""

import pytest
import json
from types import SimpleNamespace

from database import feedback, legal_holds, users
from database.memory_collections import MemoryCollections
from fork.tests.schema_firestore import SchemaFirestore
from models.feedback import FeedbackSurface, FeedbackTargetKind


@pytest.mark.parametrize('with_receipt', [False, True])
def test_complete_export_reads_admitted_collections_including_legacy_users(monkeypatch, with_receipt):
    monkeypatch.setenv('ENCRYPTION_SECRET', 'synthetic-export-contract-key-32-bytes')
    from services.users import data_export
    from firestore_pg.migrations import SchemaNotCurrent

    rows = {
        ('users', 'owner', 'goals', 'goal'): {'title': 'Project'},
        ('users', 'owner', 'goals', 'goal', 'events', 'event'): {'state': 'accepted'},
        ('users', 'other', 'frame_vision_receipts', 'private'): {'description': 'other user'},
    }
    if with_receipt:
        rows[('users', 'owner', 'frame_vision_receipts', 'receipt')] = {'description': 'retained evidence'}
    db = SchemaFirestore(rows)
    monkeypatch.setattr(data_export.database_client, 'db', db)
    monkeypatch.setattr(data_export, 'get_user_profile', lambda uid: {})
    monkeypatch.setattr(data_export, 'get_people', lambda uid: [])
    monkeypatch.setattr(data_export, 'get_standalone_action_items', lambda uid, **kwargs: [])
    monkeypatch.setattr(data_export.conversations_db, 'iter_all_conversations', lambda uid, **kwargs: iter([]))
    monkeypatch.setattr(data_export.chat_db, 'iter_all_messages', lambda uid: iter([]))
    monkeypatch.setattr(
        data_export,
        'MemoryService',
        lambda: SimpleNamespace(iter_portability_export_memories=lambda uid, **kwargs: iter([])),
    )

    admitted = db.admitted
    db.admitted = admitted - {'frame_vision_receipts'}
    # The actual pre-v8 failure also affects a user without retained frames.
    with pytest.raises(SchemaNotCurrent, match='frame_vision_receipts'):
        data_export.iter_user_data_export('owner')
    db.admitted = admitted
    payload = json.loads(''.join(data_export.iter_user_data_export('owner')))
    assert payload.get('frame_vision_receipts', []) == (
        [{'id': 'receipt', 'description': 'retained evidence'}] if with_receipt else []
    )
    assert payload['task_data']['goal_events'] == [{'id': 'event', 'parent_id': 'goal', 'state': 'accepted'}]
    assert 'frame_vision_receipts' in db.observed
    assert 'other user' not in json.dumps(payload)


def test_existing_pending_user_gets_persistent_reusable_onboarding_admission():
    db = SchemaFirestore({('users', 'legacy'): {'onboarding': {}}})
    assert users.ensure_backend_onboarding_admission('legacy', firestore_client=db)
    token = users.get_backend_onboarding_admission('legacy', firestore_client=db)
    assert isinstance(token, str) and len(token) >= 16
    assert users.ensure_backend_onboarding_admission('legacy', firestore_client=db)
    assert users.get_backend_onboarding_admission('legacy', firestore_client=db) == token
    assert db.observed == {'users', 'onboarding_admission'}


@pytest.mark.parametrize('field', ['completed', 'device_onboarding_completed'])
def test_completed_legacy_user_cannot_gain_new_onboarding_admission(field):
    db = SchemaFirestore({('users', 'legacy'): {'onboarding': {field: True}}})
    assert users.ensure_backend_onboarding_admission('legacy', firestore_client=db) is False
    assert users.get_backend_onboarding_admission('legacy', firestore_client=db) is None
    assert len(db.rows) == 1


def test_legal_hold_dynamic_owner_uses_same_admitted_inventory():
    db = SchemaFirestore()
    legal_holds.assert_account_deletion_permitted('legacy', firestore_client=db)
    legal_holds.acquire_destructive_operation(
        'legacy', kind='account_deletion', token='synthetic-lease', firestore_client=db
    )
    legal_holds.finish_destructive_operation(
        'legacy', kind='account_deletion', token='synthetic-lease', outcome='completed', firestore_client=db
    )
    assert db.observed == {'legal_holds', 'legal_hold_deletion_gates'}
    assert db.rows[('legal_hold_deletion_gates', 'legacy')]['state'] == 'completed'


def test_memory_collection_owner_uses_the_migration_admitted_inventory():
    db = SchemaFirestore()
    collections = MemoryCollections(uid='legacy')

    for path in collections.all_collection_paths():
        assert not db.document(f'{path}/inventory-check').get().exists

    assert db.observed == {
        'daily_memory_sweep_daily_summary_staged',
        'daily_memory_sweep_model_invocations',
        'daily_memory_sweep_onboarding_sources',
        'daily_memory_sweep_onboarding_staged',
        'daily_memory_sweep_receipts',
        'daily_memory_sweep_sources',
        'jit_proactivity_candidate_turns',
        'jit_proactivity_daily_budgets',
        'jit_proactivity_events',
        'jit_trigger_feedback',
        'memory_commits',
        'memory_deletion_receipts',
        'memory_evidence',
        'memory_graph_assertions',
        'memory_historical_overrides',
        'memory_import_artifacts',
        'memory_import_candidates',
        'memory_import_runs',
        'memory_ledger_reopens',
        'memory_legacy_fallback',
        'memory_lineage',
        'memory_operations',
        'memory_outbox',
        'memory_review_queue',
        'memory_runs',
        'memory_source_replacements',
        'memory_state',
        'memory_items',
        'non_active_memory_routes',
        'short_term_lifecycle_transitions',
        'users',
    }


def test_feedback_owners_use_the_migration_admitted_inventory(monkeypatch):
    db = SchemaFirestore()
    monkeypatch.setattr(feedback, 'get_firestore_client', lambda: db)

    assert feedback.record_feedback_event(
        'legacy',
        FeedbackSurface.chat_text,
        FeedbackTargetKind.chat_message,
        'message-id',
        -1,
    )
    assert feedback.get_report('2026-09-05') is None
    assert db.observed == {'feedback_events', 'feedback_reports'}
