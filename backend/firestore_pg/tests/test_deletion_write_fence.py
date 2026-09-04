"""Live PostgreSQL proof for late writers and pre-wipe transaction snapshots.

Not a CI fake: needs a disposable FIRESTORE_PG_DSN. Production account/device
writers, facade transactions/batches, receipt completion and advisory locks run
unchanged. External provider effects are counted at a controllable call seam.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get('FIRESTORE_PG_DSN'), reason='requires disposable PostgreSQL')


@pytest.fixture
def db(monkeypatch):
    from firestore_pg.compat import install

    install()
    from firestore_pg.client import Client
    from firestore_pg.migrations import migrate
    from firestore_pg import write_policy
    from fork.pg_write_policy import TerminalReceiptWrites
    from fork.patches.account_deletion import patches

    migrate()
    for patch in patches():
        module, original = patch.target()
        monkeypatch.setattr(module, patch.attribute, patch.build(original))
    monkeypatch.setattr(write_policy, 'policy', TerminalReceiptWrites())
    monkeypatch.setenv('ENCRYPTION_SECRET', 'disposable-pg-fence-receipt-secret-32-characters')
    return Client(project='synthetic-pg-write-fence')


def subject():
    return 'pg-write-fixture-' + uuid.uuid4().hex


def complete(uid):
    from database import users
    from fork.provider_guard import account_lock

    with account_lock(uid, destructive=True):
        users.mark_user_deletion_wipe_intent(uid)
        users.delete_user_data(uid)
        assert users.mark_user_deletion_wipe_completed(uid)


def test_real_request_side_effect_cannot_recreate_completed_user_or_device(db, monkeypatch):
    from database import users
    from firestore_pg import write_policy
    from firestore_pg.erasure import count_user_owned_rows, delete_user_owned_rows
    from fork.pg_write_policy import AccountWriteRejected

    uid, other = subject(), subject()
    db.collection('users').document(other).set({'preserved': True})
    complete(uid)
    monkeypatch.setattr(users, 'try_acquire_user_platform_write_lock', lambda *args: True)
    monkeypatch.setattr(users, 'try_acquire_client_device_write_lock', lambda *args: True)
    admitted = write_policy.policy
    # The old ordinary SQL path demonstrably resurrects the same completed UID.
    monkeypatch.setattr(write_policy, 'policy', write_policy.UnrestrictedWrites())
    users.record_user_platform(uid, 'android')
    users.record_client_device(uid, client_device_id='fixture-device', platform='android')
    assert count_user_owned_rows(uid) == 2
    delete_user_owned_rows(uid)
    monkeypatch.setattr(write_policy, 'policy', admitted)
    users.record_user_platform(uid, 'android')
    users.record_client_device(uid, client_device_id='fixture-device', platform='android')
    assert count_user_owned_rows(uid) == 0
    with pytest.raises(AccountWriteRejected):
        db.collection('users').document(uid).set({'late': True})
    assert db.collection('users').document(other).get().to_dict() == {'preserved': True}


def test_pre_wipe_snapshot_cannot_admit_external_or_late_sql_write(db):
    from database.legal_holds import DestructiveOperationInProgress
    from fork import account_deletion, deletion_read, provider_guard
    from fork.pg_write_policy import AccountWriteRejected
    from firestore_pg.erasure import count_user_owned_rows

    uid = subject()
    tx = db.transaction()
    tx._begin()
    try:
        assert account_deletion.get_status(uid) is None
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(complete, uid).result(timeout=15)
        assert account_deletion.get_status(uid) is None  # actual SERIALIZABLE old snapshot
        assert deletion_read.read(uid).completed
        effects = []
        fence = provider_guard.fence(lambda *args, **kwargs: nullcontext())
        with pytest.raises(DestructiveOperationInProgress):
            with fence(uid):
                effects.append('external write')
        assert effects == []
        with pytest.raises(AccountWriteRejected):
            tx.set(db.collection('users').document(uid), {'late': True})
    finally:
        tx._rollback()
    assert count_user_owned_rows(uid) == 0


def test_actual_writer_lease_survives_ref_set_until_commit_and_blocks_wipe(db):
    from fork import provider_guard

    uid = subject()
    tx = db.transaction()
    tx._begin()
    try:
        tx.set(db.collection('users').document(uid), {'in_flight': True})
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(complete, uid)
            with pytest.raises(provider_guard.ProviderOperationBusy):
                future.result(timeout=10)
        tx._commit()
    finally:
        tx._rollback()
    complete(uid)
    assert not db.collection('users').document(uid).get().exists


def test_terminal_owner_blocks_entire_real_batch_and_metadata_laundering(db, monkeypatch):
    from firestore_pg import write_policy
    from fork.pg_write_policy import AccountWriteRejected

    uid, other = subject(), subject()
    complete(uid)
    first, terminal = db.collection('users').document(other), db.collection('users').document(uid)
    batch = db.batch()
    batch.set(first, {'must_rollback': True})
    batch.set(terminal, {'late': True})
    with pytest.raises(AccountWriteRejected):
        batch.commit()
    assert not first.get().exists
    owned = db.collection('tasks').document(subject())
    # A controlled pre-existing residual cannot escape by changing its owner.
    with monkeypatch.context() as fixture:
        fixture.setattr(write_policy, 'policy', write_policy.UnrestrictedWrites())
        owned.set({'uid': uid, 'private': 'fixture'})
    for payload in ({'uid': other}, {'private': 'owner removed'}):
        with pytest.raises(AccountWriteRejected):
            owned.set(payload)
    assert owned.get().to_dict()['uid'] == uid
    owned.delete()


def test_fresh_reader_does_not_need_free_connection_in_full_writer_pool(db):
    from firestore_pg.engine import get_engine
    from fork import deletion_read

    uid = subject()
    complete(uid)
    engine = get_engine()
    # SQLAlchemy QueuePool exposes size/overflow; this disposable engine has
    # the production default finite 5 + 10 bound, with no background consumers.
    connections = [engine.connect() for _ in range(15)]
    try:
        assert deletion_read.read(uid).completed
    finally:
        for conn in connections:
            conn.close()


def test_existing_real_worker_contract_under_selected_policy_and_exclusive_lease(db, monkeypatch):
    from firestore_pg.tests.test_transaction_semantics import (
        test_real_wipe_worker_uses_pg_authority_with_isolated_provider_seams,
    )
    from fork import provider_guard
    from services.users import account_deletion as worker

    monkeypatch.setattr(worker, 'background_wipe_user_data', provider_guard.wipe(worker.background_wipe_user_data))
    test_real_wipe_worker_uses_pg_authority_with_isolated_provider_seams(db, monkeypatch)
