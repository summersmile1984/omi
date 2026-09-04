"""Receipt-aware provider writes share one PG lock domain with account wipes.

Shared writer locks admit concurrent writes, while a wipe takes the exclusive
lock for its entire provider/PG lifecycle. Busy operations fail for retry; they
never wait indefinitely behind a slow provider. No distributed transaction is
claimed if the database connection or an external write outcome is unknown.
"""

from contextlib import contextmanager
from functools import wraps
import hashlib
import threading

from sqlalchemy import text

from firestore_pg.engine import get_engine
from firestore_pg.erasure import validate_uid
from .account_deletion import get_status

_held = threading.local()


class ProviderOperationBusy(RuntimeError):
    pass


@contextmanager
def account_lock(uid, *, destructive=False):
    validate_uid(uid)
    held = getattr(_held, 'accounts', {})
    if uid in held:
        if destructive and not held[uid]:
            raise ProviderOperationBusy('cannot promote an admitted writer into a wipe')
        yield
        return
    key = int.from_bytes(
        hashlib.blake2b(uid.encode(), digest_size=8, person=b'omi-provider-v1').digest(), 'big', signed=True
    )
    function = 'pg_try_advisory_xact_lock' if destructive else 'pg_try_advisory_xact_lock_shared'
    with get_engine().begin() as conn:
        if not conn.execute(text(f'SELECT {function}(:key)'), {'key': key}).scalar_one():
            raise ProviderOperationBusy('an account provider operation is in progress; retry')
        _held.accounts = {**held, uid: destructive}
        try:
            yield
        finally:
            _held.accounts = held


def fence(original):
    @wraps(original)
    @contextmanager
    def checked(uid, *, firestore_client=None):
        from database.account_deletion_policy import account_deletion_blocks_access
        from database.legal_holds import DestructiveOperationInProgress

        with account_lock(uid):
            if account_deletion_blocks_access(get_status(uid, firestore_client=firestore_client)):
                raise DestructiveOperationInProgress('external write blocked by account deletion authority')
            with original(uid, firestore_client=firestore_client):
                yield

    return checked


@contextmanager
def storage_gate(uid, bucket=None):
    from database.legal_holds import external_write_fence
    from .storage_minio import _MinioBucket

    if bucket is not None and not isinstance(bucket, _MinioBucket):
        raise ValueError('self-host storage mutation requires a MinIO bucket')
    # local self_hosted still owns real PG/MinIO data; it is not an offline fake.
    with external_write_fence(uid):
        yield


def wipe(original):
    @wraps(original)
    def run(uid, *args, **kwargs):
        from database import users

        try:
            with account_lock(uid, destructive=True):
                return original(uid, *args, **kwargs)
        except ProviderOperationBusy:
            users.mark_user_deletion_wipe_failed(uid)
            return False

    return run


def external_index():
    from database import vector_db
    from .vector_qdrant import QdrantIndex

    if not isinstance(vector_db.index, QdrantIndex):
        raise RuntimeError('self-host deletion requires the admitted Qdrant authority')
    return vector_db.index


def assert_erased(uid):
    from .provider_objects import count_owner
    from .vector_qdrant import NAMESPACES

    index = external_index()
    if any(index.count_owner(uid, namespace) for namespace in NAMESPACES) or count_owner(uid):
        raise RuntimeError('external account data remains before deletion completion')


def complete(original):
    @wraps(original)
    def run(uid):
        with account_lock(uid, destructive=True):
            assert_erased(uid)
            return original(uid)

    return run


def purge(original):
    @wraps(original)
    def run(uid):
        from .provider_objects import purge_owner

        result = original(uid)
        # Preserve every upstream failure, including metadata/provider families
        # outside this adapter. Never erase PG inventories after a partial purge.
        try:
            result['vectors_deleted'] += external_index().purge_owner(uid)
            purge_owner(uid)
            assert_erased(uid)
        except Exception:
            result['required_failures'].append(
                {'operation': 'self_host_provider_closure', 'error': 'provider erasure could not be verified'}
            )
        return result

    return run
