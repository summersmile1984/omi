"""Strict transaction fixture that also enforces the PG collection inventory.

Dynamic document paths caused both legal-hold and onboarding startup defects.
Execute their real owners against this shared seam; do not scrape source strings
or add inferred collections while a production request is serving.
"""

from copy import deepcopy

from firestore_pg.migrations import SchemaNotCurrent, known_collections
from tests.unit.fixtures.strict_firestore_transaction import (
    StrictFirestore,
    StrictFirestoreCollection,
    StrictFirestoreDocument,
    StrictFirestoreTransaction,
)


class Document(StrictFirestoreDocument):
    def __init__(self, database, path):
        database.require(path)
        super().__init__(database, path)

    def collection(self, name):
        return Collection(self._database, (*self.path, name))

    def set(self, data):
        self._database.rows[self.path] = deepcopy(data)


class Collection(StrictFirestoreCollection):
    def document(self, name):
        return Document(self._database, (*self._path, name))

    def stream(self):
        # Export queries empty collections too. Enforce schema before reading.
        self._database.require(self._path)
        for path in sorted(self._database.rows):
            if path[:-1] == self._path:
                ref = Document(self._database, path)
                snapshot = ref.get()
                snapshot.id, snapshot.reference = path[-1], ref
                yield snapshot


class Transaction(StrictFirestoreTransaction):
    def get(self, ref):
        return ref.get(transaction=self)


class SchemaFirestore(StrictFirestore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.admitted = frozenset(known_collections())
        self.observed = set()

    def require(self, path):
        collections = set(path[::2])
        missing = collections - self.admitted
        if missing:
            raise SchemaNotCurrent('production owner needs unprovisioned collections: ' + ', '.join(sorted(missing)))
        self.observed.update(collections)

    def collection(self, name):
        return Collection(self, (name,))

    def document(self, path):
        return Document(self, super().document(path).path)

    def transaction(self):
        tx = Transaction(self)
        self.transactions.append(tx)
        return tx
