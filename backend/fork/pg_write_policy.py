"""Keep completed accounts absent at the authoritative PG mutation boundary."""

import hashlib
from typing import Mapping

from sqlalchemy import text

from firestore_pg.codec import decode_stored_document
from firestore_pg.erasure import CONTROL_COLLECTIONS, validate_uid
from firestore_pg.migrations import collection_table_name
from firestore_pg.sql import get_sql
from . import deletion_read
from .provider_guard import ProviderOperationBusy, acquire_account_lock


class AccountWriteRejected(RuntimeError):
    pass


def _owner(address, data):
    if address.namespace.startswith('users/'):
        return validate_uid(address.namespace.split('/')[1])
    if address.namespace or address.collection in CONTROL_COLLECTIONS:
        return None
    if address.collection == 'users':
        return validate_uid(address.document_id)
    owners = {validate_uid(data[key]) for key in ('uid', 'user_uid') if key in data and data[key] is not None}
    if len(owners) > 1:
        raise AccountWriteRejected('ambiguous document ownership')
    return next(iter(owners), None)


class TerminalReceiptWrites:
    def lock(self, conn, address):
        if address.table != collection_table_name(address.collection):
            raise AccountWriteRejected('document collection identity is inconsistent')
        # Also lock absent documents: two concurrent creates must not inspect
        # an empty row and then replace each other's differently owned data.
        identity = '\x00'.join((address.table, address.namespace, address.document_id))
        key = int.from_bytes(
            hashlib.blake2b(identity.encode(), digest_size=8, person=b'omi-pg-write-v1').digest(), 'big', signed=True
        )
        if not conn.execute(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': key}).scalar_one():
            raise ProviderOperationBusy('a document mutation is in progress; retry')

    def admit(self, conn, address, data: Mapping, *, merge=False):
        self.lock(conn, address)
        row = conn.execute(
            text(get_sql(address.table) + ' FOR UPDATE'),
            {'uid': address.namespace, 'doc_id': address.document_id},
        ).fetchone()
        previous = decode_stored_document(row[0]) if row else {}
        payload = {**previous, **data} if merge else data
        owners = {_owner(address, previous), _owner(address, payload)} - {None}
        # This lock belongs to the actual SQL transaction: it survives a
        # DocumentReference call inside a transaction or batch until commit.
        for uid in sorted(owners):
            acquire_account_lock(conn, uid)
            if deletion_read.read(uid).completed:
                raise AccountWriteRejected('account deletion is complete; document mutation rejected')
