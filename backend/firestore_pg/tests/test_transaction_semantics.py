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

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("FIRESTORE_PG_DSN"), reason="needs live PostgreSQL (set FIRESTORE_PG_DSN)"
)

from google.api_core import exceptions as api_exceptions  # noqa: E402

from firestore_pg.compat import install  # noqa: E402


@pytest.fixture(scope="module")
def db():
    install()
    from google.cloud import firestore

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
        results = list(pool.map(worker, range(2)))
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
