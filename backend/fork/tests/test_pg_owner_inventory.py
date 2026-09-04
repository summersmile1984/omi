"""Real dynamic-path owners must run using the migration-admitted inventory."""

import pytest

from database import legal_holds, users
from fork.tests.schema_firestore import SchemaFirestore


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
