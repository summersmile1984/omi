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
from uuid import uuid4

import pytest
from sqlalchemy import text

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

    from fork.patches.account_deletion import patches
    from fork.registry import build_registry

    build_registry(patches()).apply({'target': 'self_hosted'})
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


def test_csat_create_only_owner_and_erasure_use_real_postgres(db, monkeypatch):
    from types import SimpleNamespace
    from database import csat
    from firestore_pg.erasure import delete_user_owned_rows

    uid = 'pg-csat-' + uuid4().hex
    monkeypatch.setattr(csat, 'get_firestore_client', lambda: db)
    monkeypatch.setattr(
        csat, 'get_memory_cache', lambda: SimpleNamespace(get_or_fetch=lambda key, fetch, **kw: fetch())
    )
    assert csat.get_product_config() == csat.DEFAULT_CONFIG

    def submit(score):
        return csat.submit_rating(
            uid=uid, platform='macos', app_version='1', score=score, comment='private', revision=0
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(submit, (2, 3)))
    assert sorted(created for _, created in receipts) == [False, True]
    ref = db.collection('csat_ratings').document('macos_' + uid)
    stored = ref.get().to_dict()
    assert stored['uid'] == uid and stored['score'] in (2, 3) and stored['comment'] == 'private'
    assert submit(5) == ('macos_' + uid, False)
    assert ref.get().to_dict() == stored
    delete_user_owned_rows(uid)
    assert not ref.get().exists


def test_v7_upgrade_adds_receipt_reads_and_preserves_existing_user_data(db):
    from firestore_pg import migrations

    uid = f'pg-v7-upgrade-{uuid4().hex}'
    user = db.collection('users').document(uid)
    user.set({'state': 'retained-from-v7'})
    engine = migrations.get_engine()
    table = migrations.collection_table_name('frame_vision_receipts')
    with engine.begin() as conn:
        conn.execute(text(f'DROP TABLE {table}'))
        conn.execute(
            text(f'DELETE FROM {migrations.COLLECTION_TABLE} WHERE collection_id = :name'),
            {'name': 'frame_vision_receipts'},
        )
        conn.execute(text(f'DELETE FROM {migrations.MIGRATION_TABLE} WHERE version = 8'))
    with pytest.raises(migrations.SchemaNotCurrent, match='1..9'):
        migrations.check_schema(engine)
    assert migrations.migrate(engine).current_version == 9
    assert migrations.migrate(engine).current_version == 9
    assert user.get().to_dict() == {'state': 'retained-from-v7'}
    receipts = user.collection('frame_vision_receipts')
    assert list(receipts.stream()) == []
    receipts.document('receipt').set({'description': 'retained vision evidence'})
    rows = list(receipts.stream())
    assert len(rows) == 1 and rows[0].to_dict() == {'description': 'retained vision evidence'}
    assert list(db.collection('users').document(f'other-{uid}').collection('frame_vision_receipts').stream()) == []
    receipts.document('receipt').delete()
    user.delete()


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


def test_transform_update_missing_doc_raises_and_does_not_create(db):
    """Transform updates retain Firestore's missing-document contract.

    The read-modify-write path used to treat a missing row as ``{}``, so an
    ``Increment`` (or any other transform) accidentally created a document.
    Besides diverging from Firestore, that could resurrect data during a
    retrying worker after the authoritative document had been deleted.
    """
    _reset(db, "d2")
    ref = db.collection("txn_semantics").document("d2")
    from google.cloud import firestore

    with pytest.raises(Exception):
        ref.update({"count": firestore.Increment(1)})

    assert not ref.get().exists


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
    from firestore_pg.erasure import count_user_owned_rows

    uid = 'pg-account-delete-user'
    other_uid = f'{uid}-other'
    db.collection('users').document(uid).set({'email': 'private@example.invalid'})
    db.collection('users').document(uid).collection('future_nested_data').document('one').set({'private': True})
    db.collection('pg_global_jobs').document('owned').set({'uid': uid, 'state': 'pending'})
    db.collection('pg_global_tasks').document('owned').set({'user_uid': uid, 'state': 'pending'})
    db.collection('pg_global_jobs').document('other').set({'uid': other_uid, 'state': 'pending'})
    db.collection('account_deletions').document(uid).set({'wipe_status': 'running'})

    assert count_user_owned_rows(uid) == 4
    assert users_db.delete_user_data(uid)['status'] == 'ok'
    assert count_user_owned_rows(uid) == 0
    assert db.collection('pg_global_jobs').document('other').get().exists
    assert db.collection('account_deletions').document(uid).get().exists


def test_existing_user_onboarding_admission_persists_and_stops_after_completion(db):
    from database import users

    uid = 'pg-onboarding-owner'
    root = db.collection('users').document(uid)
    admission = db.document(f'users/{uid}/{users.ONBOARDING_ADMISSION_PATH}')
    admission.delete()
    root.set({'onboarding': {}, 'preserved': 'legacy-principal'})
    assert users.ensure_backend_onboarding_admission(uid, firestore_client=db)
    token = users.get_backend_onboarding_admission(uid, firestore_client=db)
    assert isinstance(token, str) and len(token) >= 16
    assert users.ensure_backend_onboarding_admission(uid, firestore_client=db)
    assert users.get_backend_onboarding_admission(uid, firestore_client=db) == token
    root.set({'onboarding': {'completed': True}}, merge=True)
    assert users.ensure_backend_onboarding_admission(uid, firestore_client=db) is False
    assert users.get_backend_onboarding_admission(uid, firestore_client=db) is None
    assert root.get().to_dict()['preserved'] == 'legacy-principal'
    admission.delete()
    root.delete()


def test_v5_upgrade_registers_memory_collections_without_rewriting_existing_rows(db):
    """The forward v6 admission must preserve data that was valid under v5."""
    from firestore_pg.migrations import (
        COLLECTION_TABLE,
        MIGRATION_TABLE,
        STATIC_HASHED_COLLECTION_IDS_V6,
        SchemaNotCurrent,
        check_schema,
        collection_table_name,
        get_engine,
        migrate,
    )

    uid = f'pg-v5-upgrade-{uuid4().hex}'
    legacy = db.collection('users').document(uid)
    legacy.set({'state': 'created-under-v5'})
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(f'DELETE FROM {COLLECTION_TABLE} WHERE collection_id = ANY(:collection_ids)'),
            {'collection_ids': list(STATIC_HASHED_COLLECTION_IDS_V6)},
        )
        conn.execute(text(f'DELETE FROM {MIGRATION_TABLE} WHERE version = 6'))

    with pytest.raises(SchemaNotCurrent, match='1..9'):
        check_schema(engine)

    status = migrate(engine)

    assert status.current_version == status.latest_version == 9
    assert legacy.get().to_dict() == {'state': 'created-under-v5'}
    with engine.connect() as conn:
        registered = dict(
            conn.execute(
                text(
                    f'SELECT collection_id, table_name FROM {COLLECTION_TABLE} '
                    'WHERE collection_id = ANY(:collection_ids)'
                ),
                {'collection_ids': list(STATIC_HASHED_COLLECTION_IDS_V6)},
            ).fetchall()
        )
    assert registered == {
        collection_id: collection_table_name(collection_id) for collection_id in STATIC_HASHED_COLLECTION_IDS_V6
    }
    legacy.delete()


def test_v6_upgrade_registers_feedback_collections_without_rewriting_existing_rows(db):
    """The v7 ledger admission preserves rows already valid under v6."""
    from firestore_pg.migrations import (
        COLLECTION_TABLE,
        MIGRATION_TABLE,
        STATIC_HASHED_COLLECTION_IDS_V7,
        SchemaNotCurrent,
        check_schema,
        collection_table_name,
        get_engine,
        migrate,
    )

    event_id = f'pg-v6-upgrade-{uuid4().hex}'
    event = db.collection('feedback_events').document(event_id)
    event.set({'uid': 'pg-v6-owner', 'state': 'created-under-v6'})
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(f'DELETE FROM {COLLECTION_TABLE} WHERE collection_id = ANY(:collection_ids)'),
            {'collection_ids': list(STATIC_HASHED_COLLECTION_IDS_V7)},
        )
        conn.execute(text(f'DELETE FROM {MIGRATION_TABLE} WHERE version = 7'))

    with pytest.raises(SchemaNotCurrent, match='1..9'):
        check_schema(engine)

    status = migrate(engine)

    assert status.current_version == status.latest_version == 9
    assert event.get().to_dict() == {'uid': 'pg-v6-owner', 'state': 'created-under-v6'}
    with engine.connect() as conn:
        registered = dict(
            conn.execute(
                text(
                    f'SELECT collection_id, table_name FROM {COLLECTION_TABLE} '
                    'WHERE collection_id = ANY(:collection_ids)'
                ),
                {'collection_ids': list(STATIC_HASHED_COLLECTION_IDS_V7)},
            ).fetchall()
        )
    assert registered == {
        collection_id: collection_table_name(collection_id) for collection_id in STATIC_HASHED_COLLECTION_IDS_V7
    }


def test_canonical_source_replacement_reads_the_admitted_privacy_receipt_collection(db, monkeypatch):
    """A v5 database could persist a recording but failed when replacement read its receipt.

    This runs the production replacement transaction against PostgreSQL after the
    migration fixture has admitted the v6 inventory.  It proves that a normal new
    candidate may read the anti-resurrection receipt and commit atomically; it is
    deliberately not a synthetic table-read assertion.
    """
    from database import memory_apply_store as store
    from models.memory_apply import MemoryControlState
    from tests.unit import test_memory_apply_store as memory_test

    monkeypatch.setenv('MEMORY_MODE', 'write')
    monkeypatch.setenv('ENCRYPTION_SECRET', 'test-memory-inventory-receipt-secret-32-bytes')
    suffix = uuid4().hex
    uid = 'u1'
    control = MemoryControlState(uid=uid, head_commit_id=f'head-{suffix}', account_generation=1, source_generation=2)
    old_evidence = memory_test._evidence(evidence_id=f'old-evidence-{suffix}')
    old = memory_test._short_term_target(memory_id=f'old-memory-{suffix}', evidence=[old_evidence])
    replacement_id, replacement_digest, replacement_operation, write = memory_test._replacement_operation_and_write(
        store,
        control,
        memory_id=f'new-memory-{suffix}',
        replacement_id=f'replace-{suffix}',
        replacement_digest=f'digest-{suffix}',
        evidence_id=f'new-evidence-{suffix}',
    )
    collection = 'memory_deletion_receipts'
    receipt_ref = db.collection('users').document(uid).collection(collection).document(f'probe-{suffix}')
    for ref in (
        db.document(f'users/{uid}/memory_state/apply_control'),
        db.document(f'users/{uid}/memory_items/{old.memory_id}'),
        db.document(f'users/{uid}/memory_evidence/{old_evidence.evidence_id}'),
        db.document(f'users/{uid}/memory_items/{write.patch_payload["new_memory_id"]}'),
        db.document(f'users/{uid}/memory_evidence/{write.evidence[0].evidence_id}'),
        db.document(f'users/{uid}/memory_operations/{replacement_operation.operation_id}'),
        db.document(f'users/{uid}/memory_operations/{write.operation.operation_id}'),
        db.document(f'users/{uid}/memory_source_replacements/{replacement_id}'),
        receipt_ref,
    ):
        ref.delete()
    db.document(f'users/{uid}/memory_state/apply_control').set(control.model_dump(mode='json'))
    db.document(f'users/{uid}/memory_items/{old.memory_id}').set(old.model_dump(mode='json'))
    db.document(f'users/{uid}/memory_evidence/{old_evidence.evidence_id}').set(old_evidence.model_dump(mode='json'))

    result = store.replace_conversation_source_firestore(
        uid=uid,
        conversation_id='conv1',
        replacement_id=replacement_id,
        replacement_digest=replacement_digest,
        replacement_operation=replacement_operation,
        observed_control=control,
        expected_source_items=[old],
        expected_reactivation_items=[],
        writes=[write],
        db_client=db,
    )

    assert result.committed_memory_ids == [write.patch_payload['new_memory_id']]
    assert db.document(f'users/{uid}/memory_items/{old.memory_id}').get().to_dict()['status'] == 'tombstoned'
    assert db.document(f'users/{uid}/memory_items/{write.patch_payload["new_memory_id"]}').get().exists


def test_account_deletion_completion_replaces_private_marker_atomically(db, monkeypatch):
    from google.cloud import firestore

    from database import users as users_db
    from fork.account_deletion import receipt_id

    monkeypatch.setenv('ENCRYPTION_SECRET', 'test-account-deletion-receipt-secret-32-bytes')
    uid = 'pg-account-delete-receipt-user'
    active = db.collection('account_deletions').document(uid)
    receipt = db.collection('account_deletion_receipts').document(receipt_id(uid))
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

    assert users_db.mark_user_deletion_wipe_completed(uid) is True
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
    assert users_db.record_late_agent_vm_cleanup(uid, 'omi-agent-late', 'us-central1-a', '707')
    reopened = active.get().to_dict()
    assert reopened['wipe_status'] == 'failed'
    assert reopened['wipe_job_id'] == 'opaque-job-id'
    assert {'uid', 'reason', 'reason_details'}.isdisjoint(reopened)
    active.update({'late_agent_vm_cleanup': firestore.DELETE_FIELD})
    assert users_db.mark_user_deletion_wipe_completed(uid) is True
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


def test_account_deletion_preserves_legal_hold_lease_and_rejects_stale_reopening(db, monkeypatch):
    from database import legal_holds, users
    from firestore_pg.erasure import count_user_owned_rows
    from fork.account_deletion import receipt_id

    monkeypatch.setenv('ENCRYPTION_SECRET', 'test-account-deletion-receipt-secret-32-bytes')
    uid = 'pg-legal-gated-delete'
    active = db.collection('account_deletions').document(uid)
    receipt = db.collection('account_deletion_receipts').document(receipt_id(uid))
    gate = db.collection('legal_hold_deletion_gates').document(uid)
    for ref in (active, receipt, gate):
        ref.delete()
    active.set({'wipe_status': 'running', 'wipe_job_id': 'pg-legal-job'})
    # A missing intermediate document must not hide its descendants from erasure.
    db.document(f'users/{uid}/future_nested/missing/future_nested_data/orphan').set({'private': True})
    legal_holds.acquire_destructive_operation(
        uid, kind='account_deletion', token='opaque-worker-lease', firestore_client=db
    )
    assert count_user_owned_rows(uid) == 1
    assert users.delete_user_data(uid)['status'] == 'ok'
    assert gate.get().to_dict()['state'] == 'running'
    assert users.mark_user_deletion_wipe_completed(uid)
    legal_holds.finish_destructive_operation(
        uid, kind='account_deletion', token='opaque-worker-lease', outcome='completed', firestore_client=db
    )
    assert gate.get().to_dict()['state'] == 'completed'
    users.mark_user_deletion_wipe_running(uid)
    users.mark_user_deletion_wipe_failed(uid)
    users.set_user_deletion_feedback(uid, 'stale private feedback')
    users.cancel_user_deletion_wipe(uid)
    assert users.mark_user_deletion_billing_failed(uid, 'subscription', 'stale failure') is False
    assert users.mark_user_deletion_wipe_intent(uid) == {'wipe_job_id': 'pg-legal-job', 'dispatch_claimed': False}
    assert users.get_user_deletion_wipe_status(uid) == 'completed'
    assert not active.get().exists
    for ref in (receipt, gate):
        ref.delete()


def test_account_deletion_pg_fault_rolls_back_receipt_and_keeps_private_authority(db, monkeypatch):
    from database import users
    from firestore_pg.engine import get_engine
    from firestore_pg.migrations import collection_table_name
    from fork.account_deletion import receipt_id
    from sqlalchemy import event

    monkeypatch.setenv('ENCRYPTION_SECRET', 'test-account-deletion-receipt-secret-32-bytes')
    uid = 'pg-delete-receipt-fault'
    active = db.collection('account_deletions').document(uid)
    receipt = db.collection('account_deletion_receipts').document(receipt_id(uid))
    receipt.delete()
    original = {'wipe_status': 'running', 'wipe_job_id': 'fault-job', 'reason': 'synthetic private feedback'}
    active.set(original)

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith(f'DELETE FROM {collection_table_name("account_deletions")}'):
            raise RuntimeError('synthetic marker deletion failure')

    engine = get_engine()
    event.listen(engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError, match='synthetic marker'):
            users.mark_user_deletion_wipe_completed(uid)
    finally:
        event.remove(engine, 'before_cursor_execute', fail)
    assert active.get().to_dict() == original
    assert not receipt.get().exists
    active.delete()


def test_real_wipe_worker_uses_pg_authority_with_isolated_provider_seams(db, monkeypatch):
    from database import users
    from fork.account_deletion import receipt_id
    from services.users import account_deletion as worker

    monkeypatch.setenv('ENCRYPTION_SECRET', 'test-account-deletion-receipt-secret-32-bytes')
    uid = 'pg-worker-delete-fixture'
    active = db.collection('account_deletions').document(uid)
    receipt = db.collection('account_deletion_receipts').document(receipt_id(uid))
    gate = db.collection('legal_hold_deletion_gates').document(uid)
    for ref in (active, receipt, gate):
        ref.delete()
    active.set({'wipe_status': 'pending', 'wipe_job_id': 'pg-worker-job', 'reason': 'synthetic feedback'})
    db.collection('users').document(uid).set({'email': 'synthetic@example.invalid'})
    called = []
    for name in (
        '_cancel_subscription_for_account_deletion',
        'delete_agent_vm_for_account',
        'delete_account_credentials',
        'delete_user_caller_ids',
        '_delete_memory_maintenance_registry',
    ):
        monkeypatch.setattr(worker, name, lambda current_uid, name=name: called.append(name))
    monkeypatch.setattr(worker.auth, 'delete_account', lambda current_uid: called.append('identity'))
    monkeypatch.setattr(
        worker, 'purge_derived_user_data', lambda current_uid: {'required_failures': [], 'best_effort_failures': []}
    )
    monkeypatch.setattr(worker, '_emit_deletion_telemetry', lambda *args, **kwargs: None)
    assert worker.background_wipe_user_data(uid) is True
    assert len(called) == 6
    assert users.get_user_deletion_wipe_status(uid) == 'completed'
    assert not active.get().exists and not db.collection('users').document(uid).get().exists
    assert gate.get().to_dict()['state'] == 'completed'
    for ref in (receipt, gate):
        ref.delete()
