"""Fresh reads of the existing PG deletion authority, independent of callers.

A SERIALIZABLE caller may have a snapshot older than a completed wipe. Reads
here use a small separate pool to the exact same database, never the caller's
thread-local transaction or a writer pool exhausted by waiting transactions.
This is a reader of the existing marker/receipt, not another lifecycle store.
"""

from dataclasses import dataclass
import threading

from sqlalchemy import create_engine, text

from firestore_pg.codec import decode_stored_document
from firestore_pg.engine import get_engine
from firestore_pg.migrations import collection_table_name
from .account_deletion import _receipt, receipt_id, status_from_records

_pool_lock = threading.Lock()
_primary = None
_reader = None


class DeletionAuthorityUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class DeletionState:
    status: str | None
    completed: bool


def _engine():
    global _primary, _reader
    primary = get_engine()
    with _pool_lock:
        if primary is not _primary:
            if _reader is not None:
                _reader.dispose()
            _reader = create_engine(
                primary.url,
                isolation_level='READ COMMITTED',
                pool_size=2,
                max_overflow=0,
                pool_timeout=2,
                pool_pre_ping=True,
                connect_args={'connect_timeout': 2},
            )
            _primary = primary
        return _reader


def read(uid):
    try:
        receipt_key = receipt_id(uid)
        active_table = collection_table_name('account_deletions')
        receipt_table = collection_table_name('account_deletion_receipts')
        with _engine().begin() as conn:
            conn.execute(text('SET LOCAL transaction_read_only = on'))
            conn.execute(text('SET LOCAL statement_timeout = 2000'))
            records = {
                kind: decode_stored_document(data)
                for kind, data in conn.execute(
                    text(
                        f"SELECT 'active', data FROM {active_table} WHERE uid = '' AND doc_id = :uid "
                        f"UNION ALL SELECT 'receipt', data FROM {receipt_table} WHERE uid = '' AND doc_id = :receipt"
                    ),
                    {'uid': uid, 'receipt': receipt_key},
                )
            }
        completed = _receipt(records.get('receipt'))
        return DeletionState(status_from_records(records.get('active'), completed), completed is not None)
    except Exception:
        # Driver failures can contain a DSN or raw account parameters. A failed
        # read is never permission to write, and none of that data is exposed.
        raise DeletionAuthorityUnavailable('account deletion authority could not be read') from None
