"""Transaction semantics for the firestore_pg shim, aligned with the
StrictFirestore fixture contract (tests/unit/fixtures/strict_firestore_transaction.py).

StrictFirestore guards: (1) reads may be interleaved with writes but the fixture
rejects reads-after-writes as a discipline guard; (2) transaction lifecycle
hooks (_clean_up/_begin/_commit/_rollback) drive the @firestore.transactional
decorator; (3) create on an existing doc errors; (4) update on a missing doc
errors. The real SDK/emulator enforce (2)-(4) and leave (1) to the server at
commit time — so this suite asserts the behavior the business code actually
depends on, against a real PostgreSQL.

These are integration tests: they need a live ``FIRESTORE_PG_DSN``. Without one
they skip (CI stays hermetic; run locally with the dev stack up).
"""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("FIRESTORE_PG_DSN"), reason="needs live PostgreSQL (set FIRESTORE_PG_DSN)"
)

from google.api_core import exceptions as api_exceptions  # noqa: E402

from firestore_pg.compat import install  # noqa: E402
from firestore_pg.field_path import UnsupportedFirestoreQuery  # noqa: E402


@pytest.fixture(scope="module")
def db():
    install()
    from google.cloud import firestore
    from firestore_pg.migrations import migrate, provision_collections

    migrate()
    provision_collections(
        {
            'account_deletion_receipts',
            'account_deletions',
            'future_collection',
            'future_nested',
            'future_nested_data',
            'pg_add_surface',
            'pg_batch_atomic',
            'pg_cas',
            'pg_create_race',
            'pg_cursor_numeric',
            'pg_field_path_security',
            'pg_global_jobs',
            'pg_global_tasks',
            'pg_mixed_transform',
            'pg_nested_inventory',
            'pg_query_semantics',
            'pg_query_types',
            'pg_top_level_inventory',
            'pg_tx_create',
            'photos',
            'txn_semantics',
        }
    )

    client = firestore.Client(project="demo-omi-local")
    yield client
    # best-effort cleanup of this suite's namespace
    for ref in [client.collection("txn_semantics").document("d1"), client.collection("txn_semantics").document("d2")]:
        try:
            ref.delete()
        except Exception:
            pass


def _reset(db, doc_id):
    db.collection("txn_semantics").document(doc_id).delete()


def test_read_write_commit_visibility(db):
    _reset(db, "d1")
    from google.cloud import firestore

    @firestore.transactional
    def run(tx, doc_id: str) -> None:
        ref = db.collection("txn_semantics").document(doc_id)
        snap = ref.get(transaction=tx)
        cur = snap.to_dict().get("n", 0) if snap.exists else 0
        tx.set(ref, {"n": cur + 1})

    run(db.transaction(), "d1")
    run(db.transaction(), "d1")
    assert db.collection("txn_semantics").document("d1").get().to_dict()["n"] == 2


def test_abort_on_conflict(db):
    """Two transactions incrementing the same doc: one must abort (serialization)."""
    _reset(db, "d1")
    from google.cloud import firestore

    @firestore.transactional
    def run(tx, doc_id: str) -> None:
        ref = db.collection("txn_semantics").document(doc_id)
        snap = ref.get(transaction=tx)
        cur = snap.to_dict().get("n", 0) if snap.exists else 0
        tx.set(ref, {"n": cur + 1})

    def worker(_):
        return run(db.transaction(), "d1")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(worker, range(2)))
    final = db.collection("txn_semantics").document("d1").get().to_dict()["n"]
    # both increments must land (retry on serialization) OR one raises and the
    # other lands — either way the final count is exactly 2 (atomicity, no lost update)
    assert final == 2, f"lost update: final n={final}"


def test_create_existing_raises(db):
    _reset(db, "d2")
    ref = db.collection("txn_semantics").document("d2")
    ref.create({"v": 1})
    with pytest.raises((api_exceptions.AlreadyExists, RuntimeError)):
        ref.create({"v": 2})


def test_update_missing_doc_raises(db):
    _reset(db, "d2")
    ref = db.collection("txn_semantics").document("d2")
    with pytest.raises(Exception):
        ref.update({"v": 1})


def test_tx_create_existing_raises(db):
    _reset(db, "d2")
    ref = db.collection("txn_semantics").document("d2")
    ref.create({"v": 1})
    from google.cloud import firestore

    @firestore.transactional
    def run(tx, doc_id: str) -> None:
        tx.create(db.collection("txn_semantics").document(doc_id), {"v": 2})

    with pytest.raises(Exception):
        run(db.transaction(), "d2")


def test_rollback_leaves_no_partial_write(db):
    """A failed transaction must not leave partial writes visible."""
    _reset(db, "d2")
    ref = db.collection("txn_semantics").document("d2")
    ref.set({"a": 1, "b": 2})
    from google.cloud import firestore

    @firestore.transactional
    def run(tx, doc_id: str) -> None:
        r = db.collection("txn_semantics").document(doc_id)
        snap = r.get(transaction=tx)
        tx.set(r, {"a": 99, "b": 99})
        if snap.exists:
            raise RuntimeError("boom after write")

    with pytest.raises(RuntimeError):
        run(db.transaction(), "d2")
    got = db.collection("txn_semantics").document("d2").get().to_dict()
    assert got == {"a": 1, "b": 2}, f"partial write visible: {got}"


def test_nested_subcollections_keep_complete_parent_namespace(db):
    u1 = db.collection("users").document("pg-ns-u1")
    u2 = db.collection("users").document("pg-ns-u2")
    c1_photo = u1.collection("conversations").document("c1").collection("photos").document("p")
    c2_photo = u1.collection("conversations").document("c2").collection("photos").document("p")
    other_user_photo = u2.collection("conversations").document("c1").collection("photos").document("p")
    for ref, marker in ((c1_photo, "u1-c1"), (c2_photo, "u1-c2"), (other_user_photo, "u2-c1")):
        ref.set({"marker": marker})

    assert [snap.to_dict()["marker"] for snap in c1_photo._parent.stream()] == ["u1-c1"]
    assert [snap.to_dict()["marker"] for snap in c2_photo._parent.stream()] == ["u1-c2"]
    assert [snap.to_dict()["marker"] for snap in other_user_photo._parent.stream()] == ["u2-c1"]
    assert c1_photo.path == "users/pg-ns-u1/conversations/c1/photos/p"

    grouped = {snap.reference.path for snap in db.collection_group("photos").stream() if snap.id == "p"}
    assert c1_photo.path in grouped
    assert c2_photo.path in grouped
    assert other_user_photo.path in grouped


def test_client_collections_only_enumerates_live_top_level_namespaces(db):
    top = db.collection('pg_top_level_inventory').document('one')
    nested = db.collection('users').document('pg-inventory-user').collection('pg_nested_inventory').document('one')
    top.set({'value': 1})
    nested.set({'value': 2})

    collection_ids = {collection.id for collection in db.collections()}

    assert 'pg_top_level_inventory' in collection_ids
    assert 'pg_nested_inventory' not in collection_ids


def test_account_deletion_reconciles_user_tree_and_top_level_owned_rows(db):
    from database import users as users_db

    uid = 'pg-account-delete-user'
    other_uid = f'{uid}-other'
    db.collection('users').document(uid).set({'email': 'private@example.invalid'})
    db.collection('users').document(uid).collection('future_nested_data').document('one').set({'private': True})
    db.collection('pg_global_jobs').document('owned').set({'uid': uid, 'state': 'pending'})
    db.collection('pg_global_tasks').document('owned').set({'user_uid': uid, 'state': 'pending'})
    db.collection('pg_global_jobs').document('other').set({'uid': other_uid, 'state': 'pending'})
    db.collection('account_deletions').document(uid).set({'wipe_status': 'running'})

    assert users_db.count_user_owned_rows(uid) == 4
    assert users_db.delete_user_data(uid)['status'] == 'ok'
    assert users_db.count_user_owned_rows(uid) == 0
    assert db.collection('pg_global_jobs').document('other').get().exists
    assert db.collection('account_deletions').document(uid).get().exists


def test_account_deletion_completion_replaces_private_marker_atomically(db, monkeypatch):
    from google.cloud import firestore

    from database import users as users_db
    from database.account_deletion_policy import account_deletion_receipt_id
    from database.account_deletion_transitions import mark_wipe_completed, record_late_agent_vm_cleanup

    monkeypatch.setenv('ENCRYPTION_SECRET', 'test-account-deletion-receipt-secret-32-bytes')
    uid = 'pg-account-delete-receipt-user'
    active = db.collection('account_deletions').document(uid)
    receipt = db.collection('account_deletion_receipts').document(account_deletion_receipt_id(uid))
    active.delete()
    receipt.delete()
    active.set(
        {
            'uid': uid,
            'reason': 'private reason',
            'reason_details': 'private details',
            'wipe_status': 'running',
            'wipe_job_id': 'opaque-job-id',
        }
    )

    assert mark_wipe_completed(db.transaction(), active, receipt, 'unused-generated-id') is True
    assert not active.get().exists
    receipt_data = receipt.get().to_dict()
    assert set(receipt_data) == {'schema_version', 'wipe_status', 'wipe_job_id', 'wipe_completed_at'}
    assert receipt_data['wipe_status'] == 'completed'
    assert receipt_data['wipe_job_id'] == 'opaque-job-id'
    assert uid not in repr(receipt_data)
    assert 'private reason' not in repr(receipt_data)
    assert users_db.resolve_deletion_wipe_job_id('opaque-job-id') == {'outcome': 'completed', 'uid': None}
    assert users_db.resolve_legacy_deletion_wipe_uid(uid) == {'outcome': 'completed', 'uid': None}

    # A provider resource arriving after completion reopens only the active
    # cleanup authority. Once that work is cleared, redelivery returns to the
    # same minimal receipt and removes the UID-keyed row again.
    assert record_late_agent_vm_cleanup(db.transaction(), active, receipt, 'omi-agent-late', 'us-central1-a', '707')
    reopened = active.get().to_dict()
    assert reopened['wipe_status'] == 'failed'
    assert reopened['wipe_job_id'] == 'opaque-job-id'
    assert {'uid', 'reason', 'reason_details'}.isdisjoint(reopened)
    active.update({'late_agent_vm_cleanup': firestore.DELETE_FIELD})
    assert mark_wipe_completed(db.transaction(), active, receipt, 'unused-generated-id') is True
    assert not active.get().exists
    assert set(receipt.get().to_dict()) == {'schema_version', 'wipe_status', 'wipe_job_id', 'wipe_completed_at'}

    receipt.delete()


def test_write_batch_is_one_transaction(db):
    first = db.collection("pg_batch_atomic").document("first")
    missing = db.collection("pg_batch_atomic").document("missing")
    first.delete()
    missing.delete()
    batch = db.batch()
    batch.set(first, {"landed": True})
    batch.update(missing, {"must": "fail"})
    with pytest.raises(api_exceptions.NotFound):
        batch.commit()
    assert not first.get().exists


def test_document_add_timeout_and_transaction_create_surface(db):
    direct = db.document("users/pg-surface/conversations/c1")
    direct.set({"ok": True})
    assert direct.get(timeout=0.01).to_dict() == {"ok": True}

    db.collection("pg_add_surface").document("stable-id").delete()
    update_time, added = db.collection("pg_add_surface").add({"v": 1}, "stable-id")
    assert isinstance(update_time, datetime)
    assert added.id == "stable-id"

    created = db.collection("pg_tx_create").document("created")
    created.delete()
    from google.cloud import firestore

    @firestore.transactional
    def run(tx):
        tx.create(created, {"via": "transaction"})

    run(db.transaction())
    assert created.get().to_dict() == {"via": "transaction"}


def test_start_after_snapshot_and_mapping_with_numeric_order(db):
    collection = db.collection("pg_cursor_numeric")
    for doc_id, score in (("two", 2), ("ten", 10), ("hundred", 100)):
        collection.document(doc_id).set({"score": score})
    collection.document("missing").set({"other": True})

    ordered = list(collection.order_by("score").stream())
    assert [(snap.id, snap.to_dict()["score"]) for snap in ordered] == [
        ("two", 2),
        ("ten", 10),
        ("hundred", 100),
    ]
    assert [snap.id for snap in collection.order_by("score").start_after(ordered[0]).stream()] == [
        "ten",
        "hundred",
    ]
    assert [
        snap.id
        for snap in collection.order_by("score")
        .order_by("__name__")
        .start_after({"score": 10, "__name__": collection.document("ten")})
        .stream()
    ] == ["hundred"]


def test_not_in_excludes_missing_and_null_and_count_honors_limit(db):
    collection = db.collection("pg_query_semantics")
    for doc_id, payload in (
        ("x", {"state": "x"}),
        ("y", {"state": "y"}),
        ("null", {"state": None}),
        ("missing", {"other": True}),
    ):
        collection.document(doc_id).set(payload)
    assert [snap.id for snap in collection.where("state", "not-in", ["x"]).stream()] == ["y"]
    assert list(collection.where("state", "not-in", [None]).stream()) == []
    assert [snap.id for snap in collection.where("state", "==", None).stream()] == ["null"]
    assert collection.order_by("__name__").limit(2).count().get()[0][0].value == 2


def test_equality_preserves_firestore_value_types(db):
    collection = db.collection("pg_query_types")
    collection.document("number").set({"value": 2})
    collection.document("text").set({"value": "2"})
    assert [snap.id for snap in collection.where("value", "==", 2).stream()] == ["number"]
    assert [snap.id for snap in collection.where("value", "==", "2").stream()] == ["text"]


def test_malicious_query_field_path_cannot_escape_user_namespace(db):
    attacker = db.collection('users').document('pg-field-attacker').collection('pg_field_path_security')
    victim = db.collection('users').document('pg-field-victim').collection('pg_field_path_security')
    attacker.document('visible').set({'x': 'attacker'})
    victim.document('secret').set({'x': 'victim-secret'})

    assert [snapshot.to_dict()['x'] for snapshot in attacker.where('x', '==', 'attacker').stream()] == ['attacker']
    malicious = "x' IS NULL OR TRUE --"
    with pytest.raises(UnsupportedFirestoreQuery, match='field path'):
        list(attacker.where(malicious, '==', 'ignored').stream())
    with pytest.raises(UnsupportedFirestoreQuery, match='field path'):
        list(attacker.order_by(malicious).stream())

    assert victim.document('secret').get().to_dict() == {'x': 'victim-secret'}
    attacker.document('visible').delete()
    victim.document('secret').delete()


def test_mixed_transform_update_keeps_plain_fields_and_float_precision(db):
    from google.cloud import firestore

    ref = db.collection("pg_mixed_transform").document("doc")
    ref.set({"counter": 1})
    ref.update({"counter": firestore.Increment(0.5), "marker": "kept"})
    assert ref.get().to_dict() == {"counter": 1.5, "marker": "kept"}


def test_update_time_precondition_rejects_stale_update_and_delete(db):
    ref = db.collection("pg_cas").document("doc")
    ref.set({"version": 1})
    original = ref.get()
    assert original.update_time is not None
    ref.update({"version": 2}, option=db.write_option(last_update_time=original.update_time))
    with pytest.raises(api_exceptions.FailedPrecondition):
        ref.update({"version": 3}, option=db.write_option(last_update_time=original.update_time))
    with pytest.raises(api_exceptions.FailedPrecondition):
        ref.delete(option=db.write_option(last_update_time=original.update_time))
    assert ref.get().to_dict()["version"] == 2


def test_concurrent_create_has_one_winner(db):
    ref = db.collection("pg_create_race").document("same")
    ref.delete()

    def create(value):
        try:
            ref.create({"value": value})
            return "created"
        except api_exceptions.AlreadyExists:
            return "exists"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(create, (1, 2)))
    assert outcomes == ["created", "exists"]


def test_collections_discovers_every_direct_child_for_recursive_delete(db):
    from database._client import delete_collection_recursive

    user = db.collection("users").document("pg-delete-user")
    user.set({"exists": True})
    child = user.collection("future_collection").document("child")
    nested = child.collection("future_nested").document("nested")
    child.set({"v": 1})
    nested.set({"v": 2})

    assert [collection.id for collection in user.collections()] == ["future_collection"]
    for collection in user.collections():
        delete_collection_recursive(collection, client=db)
    user.delete()
    assert not child.get().exists
    assert not nested.get().exists


def test_explicit_provision_rejects_populated_unknown_legacy_collection(db):
    from sqlalchemy import text

    from firestore_pg.engine import get_engine
    from firestore_pg.migrations import SchemaNotCurrent, provision_collections

    with get_engine().begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS pg_legacy_future"))
        conn.execute(
            text(
                "CREATE TABLE pg_legacy_future ("
                "uid TEXT NOT NULL DEFAULT '', doc_id TEXT NOT NULL DEFAULT '', "
                "data JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                "PRIMARY KEY (uid, doc_id))"
            )
        )
        conn.execute(
            text(
                "INSERT INTO pg_legacy_future (uid, doc_id, data) "
                "VALUES ('pg-legacy-user', 'doc', CAST(:data AS jsonb))"
            ),
            {"data": '{"legacy":true}'},
        )
    with pytest.raises(SchemaNotCurrent, match='authoritative import into a fresh target'):
        provision_collections(['pg_legacy_future'])
    with get_engine().begin() as conn:
        conn.execute(text("DROP TABLE pg_legacy_future"))
