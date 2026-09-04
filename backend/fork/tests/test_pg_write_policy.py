"""Execute real facade mutations through the selected write-policy boundary.

The connection is a controllable seam; PostgreSQL snapshot/lock behavior is
separately exercised against a disposable database, not simulated by this fake.
"""

from copy import deepcopy
from datetime import datetime, timezone
from unittest import mock

import pytest

from firestore_pg import client, write_policy
from firestore_pg.migrations import collection_table_name
from fork import deletion_read, pg_write_policy, provider_guard


class Result:
    def __init__(self, value=None):
        self.value = value

    def fetchone(self):
        return self.value

    def scalar_one(self):
        return self.value


class Connection:
    def __init__(self, previous=None):
        self.previous = previous
        self.locks = []
        self.writes = []

    def execute(self, query, params=None):
        sql = str(query)
        if sql.startswith('SELECT pg_try_advisory'):
            self.locks.append((sql, params['key']))
            return Result(True)
        if sql.startswith('SELECT data,'):
            row = None if self.previous is None else (deepcopy(self.previous), datetime.now(timezone.utc), 1)
            return Result(row)
        self.writes.append(sql)
        return mock.Mock(rowcount=1)


def address(collection='users', namespace='', document_id='owner'):
    return write_policy.DocumentAddress(collection, collection_table_name(collection), namespace, document_id)


@pytest.fixture
def selected(monkeypatch):
    policy = pg_write_policy.TerminalReceiptWrites()
    monkeypatch.setattr(write_policy, 'policy', policy)
    monkeypatch.setattr(client, 'require_table', lambda table: None)
    monkeypatch.setattr(client, 'get_engine', mock.Mock())
    return policy


@pytest.mark.parametrize(
    'operation', ['set', 'merge', 'transform-merge', 'update', 'transform-update', 'create', 'tx', 'batch']
)
def test_completed_owner_never_reaches_any_facade_mutation(selected, monkeypatch, operation):
    conn = Connection(None if operation in {'create', 'transform-merge'} else {'v': 1})
    monkeypatch.setattr(client, 'get_tx_conn', lambda: conn)
    fresh = mock.Mock(return_value=deletion_read.DeletionState('completed', True))
    monkeypatch.setattr(deletion_read, 'read', fresh)
    ref = client.DocumentReference('users', '', 'owner')
    with pytest.raises(pg_write_policy.AccountWriteRejected):
        if operation == 'set':
            ref.set({'v': 2})
        elif operation == 'merge':
            ref.set({'v': 2}, merge=True)
        elif operation == 'transform-merge':
            ref.set({'v': client.Increment(1)}, merge=True)
        elif operation == 'update':
            ref.update({'v': 2})
        elif operation == 'transform-update':
            ref.update({'v': client.Increment(1)})
        elif operation == 'create':
            ref.create({'v': 2})
        elif operation == 'tx':
            client.Transaction(None).set(ref, {'v': 2})
        else:
            batch = client.WriteBatch(None)
            batch.set(ref, {'v': 2})
            batch.commit()
    fresh.assert_called_once_with('owner')
    assert conn.writes == []
    assert any('pg_try_advisory_xact_lock_shared' in sql for sql, _ in conn.locks)


@pytest.mark.parametrize('previous,payload', [({'uid': 'owner'}, {}), ({'user_uid': 'owner'}, {'uid': 'other'})])
def test_removing_or_transferring_old_owner_cannot_launder_terminal_data(selected, monkeypatch, previous, payload):
    conn = Connection(previous)
    monkeypatch.setattr(deletion_read, 'read', lambda uid: deletion_read.DeletionState('completed', uid == 'owner'))
    with pytest.raises(pg_write_policy.AccountWriteRejected):
        selected.admit(conn, address('tasks'), payload)
    assert conn.writes == []


def test_existing_principal_merge_uses_old_owner_and_checks_same_sql_connection(selected, monkeypatch):
    conn = Connection({'uid': 'legacy-owner', 'private': 'fixture'})
    fresh = mock.Mock(return_value=deletion_read.DeletionState(None, False))
    acquire = mock.Mock()
    monkeypatch.setattr(deletion_read, 'read', fresh)
    monkeypatch.setattr(pg_write_policy, 'acquire_account_lock', acquire)
    selected.admit(conn, address('tasks'), {'state': 'done'}, merge=True)
    acquire.assert_called_once_with(conn, 'legacy-owner')
    fresh.assert_called_once_with('legacy-owner')


def test_control_exemption_is_exact_and_cannot_override_namespace_or_physical_owner(selected, monkeypatch):
    read = mock.Mock(return_value=deletion_read.DeletionState('completed', True))
    monkeypatch.setattr(deletion_read, 'read', read)
    for collection in ('account_deletions', 'account_deletion_receipts', 'legal_holds', 'legal_hold_deletion_gates'):
        selected.admit(Connection(), address(collection), {'uid': 'owner'})
    read.assert_not_called()
    with pytest.raises(pg_write_policy.AccountWriteRejected):
        selected.admit(Connection(), address('account_deletions', 'users/owner'), {})
    read.assert_called_once_with('owner')
    with pytest.raises(pg_write_policy.AccountWriteRejected, match='identity'):
        selected.admit(Connection(), write_policy.DocumentAddress('account_deletions', 'users', '', 'owner'), {})


def test_ambiguous_payload_and_authority_outage_fail_before_sql(selected, monkeypatch):
    with pytest.raises(pg_write_policy.AccountWriteRejected, match='ambiguous'):
        selected.admit(Connection(), address('tasks'), {'uid': 'one', 'user_uid': 'two'})
    monkeypatch.setattr(deletion_read, 'read', mock.Mock(side_effect=deletion_read.DeletionAuthorityUnavailable()))
    with pytest.raises(deletion_read.DeletionAuthorityUnavailable):
        selected.admit(Connection(), address(), {})


def test_provider_uses_ambient_sql_lock_but_rejects_wipe_in_older_transaction(monkeypatch):
    conn = Connection()
    monkeypatch.setattr(provider_guard, 'get_tx_conn', lambda: conn)
    monkeypatch.setattr(provider_guard, 'get_engine', mock.Mock(side_effect=AssertionError('second writer connection')))
    with provider_guard.account_lock('owner'):
        pass
    assert len(conn.locks) == 1
    with pytest.raises(provider_guard.ProviderOperationBusy, match='earlier SQL transaction'):
        with provider_guard.account_lock('owner', destructive=True):
            pytest.fail('wipe admitted')


@pytest.mark.parametrize('receipt', [None, {'wipe_status': 'completed'}])
def test_fresh_reader_missing_principal_and_malformed_receipt(monkeypatch, receipt):
    monkeypatch.setenv('ENCRYPTION_SECRET', 'synthetic-receipt-authority-32-characters')
    conn = mock.MagicMock()
    conn.execute.side_effect = [None, None, [] if receipt is None else [('receipt', receipt)]]
    engine = mock.Mock()
    engine.begin.return_value.__enter__ = mock.Mock(return_value=conn)
    engine.begin.return_value.__exit__ = mock.Mock(return_value=False)
    monkeypatch.setattr(deletion_read, '_engine', lambda: engine)
    if receipt is None:
        assert deletion_read.read('legacy') == deletion_read.DeletionState(None, False)
    else:
        with pytest.raises(deletion_read.DeletionAuthorityUnavailable):
            deletion_read.read('legacy')


def test_reader_outage_is_bounded_and_does_not_expose_driver_parameters(monkeypatch):
    monkeypatch.setenv('ENCRYPTION_SECRET', 'synthetic-receipt-authority-32-characters')
    monkeypatch.setattr(deletion_read, '_engine', mock.Mock(side_effect=RuntimeError('dsn-password-and-private-uid')))
    with pytest.raises(deletion_read.DeletionAuthorityUnavailable) as error:
        deletion_read.read('private-uid')
    assert 'private' not in str(error.value) and 'password' not in str(error.value)
