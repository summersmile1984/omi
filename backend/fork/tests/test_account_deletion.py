"""Behavioral guards for the deletion gaps reproduced by the main PG audit.

SQL erasure runs against real hermetic SQLite transactions. Lifecycle tests use
an extension of the sanctioned strict fixture for delete/rollback only; actual
PostgreSQL serialization and the upstream public seams run in the live suite.
"""

from copy import deepcopy
from datetime import datetime, timezone
import json
from unittest import mock

import pytest
from sqlalchemy import create_engine, event, text

from firestore_pg.erasure import count_user_owned_rows, delete_user_owned_rows
from firestore_pg.migrations import COLLECTION_TABLE, SchemaNotCurrent, collection_table_name
from fork import account_deletion as owner
from tests.unit.fixtures.strict_firestore_transaction import StrictFirestore, StrictFirestoreTransaction


class Transaction(StrictFirestoreTransaction):
    def _begin(self, retry_id=None):
        super()._begin(retry_id)
        self.before = deepcopy(self._database.rows)

    def _rollback(self):
        self._database.rows = self.before

    def set(self, ref, data, merge=False):
        if merge:
            data = {**self._database.rows.get(ref.path, {}), **data}
        super().set(ref, data)

    def delete(self, ref):
        self._assert_reference_belongs(ref)
        self.has_written = True
        self._database.rows.pop(ref.path, None)


@pytest.fixture
def authority(monkeypatch):
    db = StrictFirestore()
    monkeypatch.setattr(db, 'transaction', lambda: Transaction(db))
    monkeypatch.setattr(owner, '_client', lambda: db)
    monkeypatch.setattr(owner, 'count_user_owned_rows', lambda uid: 0)
    monkeypatch.setenv('ENCRYPTION_SECRET', 'synthetic-receipt-secret-that-is-stable-32')
    return db


def seed(db, uid='owner', **data):
    db.rows[('account_deletions', uid)] = {'wipe_status': 'running', 'wipe_job_id': 'opaque-job', **data}


def test_completion_receipt_retries_and_late_cleanup(authority):
    db = authority
    seed(db, uid='owner', reason='private feedback')
    assert owner.complete('owner')
    assert owner.complete('owner')
    assert ('account_deletions', 'owner') not in db.rows
    receipt = db.rows[('account_deletion_receipts', owner.receipt_id('owner'))]
    assert set(receipt) == owner.RECEIPT_FIELDS
    assert 'private' not in repr(receipt) and 'owner' not in repr(receipt)
    assert owner.get_status('owner') == 'completed'
    assert owner.intent('owner') == {'wipe_job_id': 'opaque-job', 'dispatch_claimed': False}
    stale_write = mock.Mock()
    owner.guard_mutation(stale_write, 'owner', 'private feedback')
    stale_write.assert_not_called()
    assert owner.billing_failed('owner', 'subscription', 'stale failure') is False
    assert owner.late_cleanup('owner', 'vm', 'zone', '123')
    assert db.rows[('account_deletions', 'owner')]['wipe_job_id'] == 'opaque-job'
    assert set(db.rows[('account_deletions', 'owner')]) == {
        'wipe_status',
        'wipe_job_id',
        'wipe_failed_at',
        'late_agent_vm_cleanup',
    }
    assert owner.complete('owner') is False
    del db.rows[('account_deletions', 'owner')]['late_agent_vm_cleanup']
    assert owner.complete('owner')


def test_receipt_publish_and_private_marker_removal_roll_back_together(authority, monkeypatch):
    seed(authority, reason='private feedback')
    before = deepcopy(authority.rows)
    monkeypatch.setattr(Transaction, 'delete', mock.Mock(side_effect=RuntimeError('synthetic write fault')))
    with pytest.raises(RuntimeError, match='synthetic'):
        owner.complete('owner')
    assert authority.rows == before


def test_residual_rows_and_cancelled_marker_cannot_complete(authority, monkeypatch):
    seed(authority)
    before = deepcopy(authority.rows)
    monkeypatch.setattr(owner, 'count_user_owned_rows', lambda uid: 1)
    with pytest.raises(RuntimeError, match='rows remain'):
        owner.complete('owner')
    assert authority.rows == before
    seed(authority, wipe_status='cancelled')
    with pytest.raises(RuntimeError, match='cancelled'):
        owner.complete('owner')


def test_existing_unmigrated_principal_and_legacy_marker(authority):
    assert owner.get_status('owner') is None
    assert not owner.late_cleanup('owner', 'vm', 'zone')
    with pytest.raises(RuntimeError, match='admitted marker'):
        owner.complete('owner')
    authority.rows[('account_deletions', 'owner')] = {'wipe_status': 'running'}
    assert owner.complete('owner')
    assert owner.get_status('owner') == 'completed'
    assert len(owner.intent('owner')['wipe_job_id']) == 32


def test_new_intent_and_retry_preserve_job_and_running_state(authority):
    first = owner.intent('owner')
    assert first['dispatch_claimed']
    authority.rows[('account_deletions', 'owner')]['wipe_status'] = 'running'
    assert owner.intent('owner') == {'wipe_job_id': first['wipe_job_id'], 'dispatch_claimed': False}
    assert owner.get_status('owner') == 'running'


def test_malformed_receipt_identity_and_weak_key_fail_closed(authority, monkeypatch):
    authority.rows[('account_deletion_receipts', owner.receipt_id('owner'))] = {'wipe_status': 'completed'}
    with pytest.raises(RuntimeError, match='malformed'):
        owner.get_status('owner')
    with pytest.raises(ValueError):
        owner.late_cleanup('owner', 'vm', 'zone', 'not-numeric')
    with pytest.raises(ValueError):
        owner.receipt_id('nested/owner')
    monkeypatch.setenv('ENCRYPTION_SECRET', 'short')
    with pytest.raises(RuntimeError, match='32 bytes'):
        owner.get_status('owner')


@pytest.fixture
def rows():
    engine = create_engine('sqlite://')
    names = [
        'users',
        'orphan_data',
        'global_jobs',
        'global_tasks',
        'account_deletions',
        'legal_hold_deletion_gates',
        'legal_holds',
    ]
    with engine.begin() as conn:
        conn.execute(text(f'CREATE TABLE {COLLECTION_TABLE} (collection_id TEXT, table_name TEXT)'))
        for name in names:
            physical = collection_table_name(name)
            conn.execute(text(f'CREATE TABLE {physical} (uid TEXT, doc_id TEXT, data JSON)'))
            conn.execute(
                text(f'INSERT INTO {COLLECTION_TABLE} VALUES (:name, :physical)'), {'name': name, 'physical': physical}
            )

    def add(name, parent, doc, data):
        with engine.begin() as conn:
            conn.execute(
                text(f'INSERT INTO {collection_table_name(name)} VALUES (:parent, :doc, :data)'),
                {'parent': parent, 'doc': doc, 'data': json.dumps(data)},
            )

    uid = 'owner_%'
    add('users', '', uid, {})
    add('orphan_data', f'users/{uid}/missing/parent', 'one', {'private': True})
    add('global_jobs', '', 'one', {'uid': uid})
    add('global_tasks', '', 'one', {'user_uid': uid})
    add('users', '', 'other', {})
    add('orphan_data', 'users/owner_AB/missing/parent', 'other', {})
    for name in ['account_deletions', 'legal_hold_deletion_gates', 'legal_holds']:
        add(name, '', uid, {'uid': uid, 'state': 'running'})
    yield engine, uid
    engine.dispose()


def test_erasure_uses_full_parent_and_owner_scope_retains_control_authorities(rows):
    engine, uid = rows
    assert count_user_owned_rows(uid, engine=engine) == 4
    assert delete_user_owned_rows(uid, engine=engine) == 4
    assert count_user_owned_rows(uid, engine=engine) == 0
    with engine.connect() as conn:
        total = sum(
            conn.execute(text(f'SELECT count(*) FROM {physical}')).scalar_one()
            for physical, in conn.execute(text(f'SELECT table_name FROM {COLLECTION_TABLE}')).fetchall()
        )
    assert total == 5  # two other-principal rows + three control authorities


def test_erasure_rolls_back_all_collections_on_statement_failure(rows):
    engine, uid = rows

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith(f'DELETE FROM {collection_table_name("global_jobs")}'):
            raise RuntimeError('synthetic SQL fault')

    event.listen(engine, 'before_cursor_execute', fail)
    with pytest.raises(RuntimeError, match='SQL fault'):
        delete_user_owned_rows(uid, engine=engine)
    event.remove(engine, 'before_cursor_execute', fail)
    assert count_user_owned_rows(uid, engine=engine) == 4


def test_corrupt_registry_refuses_erasure_before_any_write(rows):
    engine, uid = rows
    with engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {COLLECTION_TABLE} SET table_name = 'users; DROP TABLE users' WHERE collection_id = 'orphan_data'"
            )
        )
    with pytest.raises(SchemaNotCurrent):
        delete_user_owned_rows(uid, engine=engine)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM users')).scalar_one() == 2


def test_conflicting_top_level_owner_fields_stop_before_delete(rows):
    engine, uid = rows
    with engine.begin() as conn:
        conn.execute(
            text(f'UPDATE {collection_table_name("global_jobs")} SET data = :data'),
            {'data': json.dumps({'uid': uid, 'user_uid': 'other'})},
        )
    with pytest.raises(RuntimeError, match='ambiguous'):
        delete_user_owned_rows(uid, engine=engine)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM users')).scalar_one() == 2
