"""Transactional erasure of registered user namespaces and top-level ownership.

Control authorities survive erasure: completion must retain the deletion fence,
legal hold checks and the worker lease until its owner finishes the operation.
"""

from contextlib import contextmanager

from sqlalchemy import text

from .engine import get_engine, get_tx_conn
from .migrations import COLLECTION_TABLE, SchemaNotCurrent, collection_table_name

CONTROL_COLLECTIONS = frozenset(
    {'account_deletions', 'account_deletion_receipts', 'legal_holds', 'legal_hold_deletion_gates'}
)


def validate_uid(uid: str) -> str:
    if not isinstance(uid, str) or not uid or '/' in uid or '\x00' in uid:
        raise ValueError('a single nonempty UID segment is required')
    return uid


@contextmanager
def _connection(engine=None):
    active = get_tx_conn() if engine is None else None
    if active is not None:
        yield active
    else:
        with (engine or get_engine()).begin() as conn:
            yield conn


def _owned_rows(conn, uid: str, *, erase: bool) -> int:
    namespace = f'users/{validate_uid(uid)}'
    prefix = namespace + '/'
    params = {'owner': uid, 'namespace': namespace, 'prefix': prefix, 'prefix_length': len(prefix)}
    rows = conn.execute(text(f'SELECT collection_id, table_name FROM {COLLECTION_TABLE}')).fetchall()
    # Validate the whole inventory before the first write; identifiers never
    # come from account data, and a corrupt registry cannot execute arbitrary SQL.
    for logical, physical in rows:
        if physical != collection_table_name(logical):
            raise SchemaNotCurrent('invalid collection registry mapping during user erasure')
    for logical, physical in rows:
        if logical in CONTROL_COLLECTIONS or logical == 'users':
            continue
        ambiguous = conn.execute(
            text(
                f"SELECT count(*) FROM {physical} WHERE uid = '' "
                "AND data->>'uid' IS NOT NULL AND data->>'user_uid' IS NOT NULL "
                "AND data->>'uid' != data->>'user_uid' "
                "AND (data->>'uid' = :owner OR data->>'user_uid' = :owner)"
            ),
            params,
        ).scalar_one()
        if ambiguous:
            raise RuntimeError('ambiguous top-level row ownership prevents erasure')
    total = 0
    for logical, physical in rows:
        predicate = '(uid = :namespace OR substr(uid, 1, :prefix_length) = :prefix)'
        if logical not in CONTROL_COLLECTIONS:
            top = "data->>'uid' = :owner OR data->>'user_uid' = :owner"
            if logical == 'users':
                top = 'doc_id = :owner'
            predicate += f" OR (uid = '' AND ({top}))"
        if erase:
            result = conn.execute(text(f'DELETE FROM {physical} WHERE {predicate}'), params)
            total += result.rowcount
        else:
            total += conn.execute(text(f'SELECT count(*) FROM {physical} WHERE {predicate}'), params).scalar_one()
    return total


def count_user_owned_rows(uid: str, *, engine=None) -> int:
    with _connection(engine) as conn:
        return _owned_rows(conn, uid, erase=False)


def delete_user_owned_rows(uid: str, *, engine=None) -> int:
    """Delete all known rows atomically, including orphaned nested documents."""
    with _connection(engine) as conn:
        return _owned_rows(conn, uid, erase=True)
