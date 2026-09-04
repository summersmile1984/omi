"""Self-hosted deletion authority: UID marker → minimal keyed completion receipt.

The upstream worker still owns provider cleanup and legal-hold admission. This
owner changes only its database seams; it never declares provider cleanup done.
"""

from datetime import datetime, timezone
import hashlib
import hmac
import os
import uuid

from database.account_deletion_policy import account_deletion_blocks_access, normalize_account_deletion_status
from firestore_pg.client import transactional
from firestore_pg.erasure import count_user_owned_rows, delete_user_owned_rows, validate_uid

RECEIPT_FIELDS = frozenset({'schema_version', 'wipe_status', 'wipe_job_id', 'wipe_completed_at'})


def receipt_id(uid: str) -> str:
    secret = os.environ.get('ENCRYPTION_SECRET', '')
    if len(secret.encode()) < 32:
        raise RuntimeError('completion receipts require ENCRYPTION_SECRET of at least 32 bytes')
    return hmac.new(
        secret.encode(), b'account-deletion-receipt:v1\x00' + validate_uid(uid).encode(), hashlib.sha256
    ).hexdigest()


def _client():
    from database._client import get_firestore_client

    return get_firestore_client()


def _refs(client, uid):
    return client.collection('account_deletions').document(validate_uid(uid)), client.collection(
        'account_deletion_receipts'
    ).document(receipt_id(uid))


def _read(ref, tx=None):
    snapshot = ref.get(transaction=tx)
    return snapshot.to_dict() if snapshot.exists else None


def _receipt(data):
    if data is None:
        return None
    if (
        set(data) != RECEIPT_FIELDS
        or type(data.get('schema_version')) is not int
        or data.get('schema_version') != 1
        or data.get('wipe_status') != 'completed'
        or not isinstance(data.get('wipe_job_id'), str)
        or not data['wipe_job_id']
        or not isinstance(data.get('wipe_completed_at'), datetime)
    ):
        raise RuntimeError('account deletion completion receipt is malformed')
    return data


def delete_user_data(uid):
    delete_user_owned_rows(uid)
    return {'status': 'ok'}


def get_status(uid, *, firestore_client=None):
    active, receipt = _refs(firestore_client or _client(), uid)
    data = _read(active)
    from database.firestore_read_metrics import FirestoreReadOutcome, FirestoreReadSite, record_document_read

    record_document_read(
        FirestoreReadSite.USER_DELETION_WIPE_STATUS,
        FirestoreReadOutcome.HIT if data is not None else FirestoreReadOutcome.MISS,
    )
    completed = _receipt(_read(receipt))
    if data is not None:
        status = normalize_account_deletion_status(marker_exists=True, raw_status=data.get('wipe_status'))
        return 'completed' if completed and not account_deletion_blocks_access(status) else status
    return 'completed' if completed else None


@transactional
def _complete(tx, active, receipt, uid):
    data = _read(active, tx)
    completed = _receipt(_read(receipt, tx))
    if data is None:
        if completed:
            return True
        raise RuntimeError('cannot complete deletion without an admitted marker')
    if not account_deletion_blocks_access(
        normalize_account_deletion_status(marker_exists=True, raw_status=data.get('wipe_status'))
    ):
        raise RuntimeError('cannot complete a cancelled deletion')
    if data.get('late_agent_vm_cleanup'):
        tx.set(active, {'wipe_status': 'failed', 'wipe_failed_at': datetime.now(timezone.utc)}, merge=True)
        return False
    if count_user_owned_rows(uid):
        raise RuntimeError('user-owned rows remain before deletion completion')
    job_id = data.get('wipe_job_id') or (completed or {}).get('wipe_job_id') or uuid.uuid4().hex
    if not isinstance(job_id, str):
        raise RuntimeError('deletion job identity is malformed')
    if completed and completed['wipe_job_id'] != job_id:
        raise RuntimeError('active deletion and receipt disagree on job identity')
    tx.set(
        receipt,
        {
            'schema_version': 1,
            'wipe_status': 'completed',
            'wipe_job_id': job_id,
            'wipe_completed_at': datetime.now(timezone.utc),
        },
    )
    tx.delete(active)
    return True


def complete(uid):
    client = _client()
    return _complete(client.transaction(), *_refs(client, uid), uid)


@transactional
def _late_cleanup(tx, active, receipt, pending):
    data = _read(active, tx)
    completed = _receipt(_read(receipt, tx))
    if data is None and not completed:
        return False
    if data is not None and not account_deletion_blocks_access(
        normalize_account_deletion_status(marker_exists=True, raw_status=data.get('wipe_status'))
    ):
        return False
    update = {'late_agent_vm_cleanup': pending, 'wipe_status': 'failed', 'wipe_failed_at': datetime.now(timezone.utc)}
    if data is None:
        update['wipe_job_id'] = completed['wipe_job_id']
    tx.set(active, update, merge=True)
    return True


def late_cleanup(uid, vm_name, zone, expected_instance_id=None):
    if not isinstance(vm_name, str) or not vm_name or not isinstance(zone, str) or not zone:
        raise ValueError('late cleanup requires a VM name and zone')
    pending = {'vmName': vm_name, 'zone': zone}
    if expected_instance_id is not None:
        if (
            not isinstance(expected_instance_id, str)
            or not expected_instance_id.isascii()
            or not expected_instance_id.isdigit()
        ):
            raise ValueError('late Agent VM cleanup instance identity must be numeric')
        pending['expectedInstanceId'] = expected_instance_id
    client = _client()
    return _late_cleanup(client.transaction(), *_refs(client, uid), pending)


def resolve_job(original, wipe_job_id):
    result = original(wipe_job_id)
    if result['outcome'] != 'missing':
        return result
    receipts = list(
        _client().collection('account_deletion_receipts').where('wipe_job_id', '==', wipe_job_id).limit(2).stream()
    )
    if not receipts:
        return result
    if len(receipts) != 1:
        return {'outcome': 'ambiguous', 'uid': None}
    _receipt(receipts[0].to_dict())
    return {'outcome': 'completed', 'uid': None}


def resolve_legacy(original, uid):
    result = original(uid)
    if result['outcome'] == 'missing' and _receipt(_read(_refs(_client(), uid)[1])):
        return {'outcome': 'completed', 'uid': None}
    return result


@transactional
def _guard_completed(tx, active, receipt, action, completed_result):
    data = _read(active, tx)
    completed = _receipt(_read(receipt, tx))
    if data is None and completed:
        return completed_result(completed)
    return action(tx, active)


def guard_mutation(original, uid, *args, **kwargs):
    """Simple upstream writes share this PG transaction's thread-local connection."""
    client = _client()
    return _guard_completed(
        client.transaction(), *_refs(client, uid), lambda tx, ref: original(uid, *args, **kwargs), lambda receipt: None
    )


def intent(uid):
    client = _client()
    return _guard_completed(
        client.transaction(),
        *_refs(client, uid),
        _intent,
        lambda receipt: {'wipe_job_id': receipt['wipe_job_id'], 'dispatch_claimed': False},
    )


def _intent(tx, active):
    data = _read(active, tx) or {}
    job_id = data.get('wipe_job_id')
    status = data.get('wipe_status')
    if isinstance(job_id, str) and job_id:
        if status == 'deleting_auth':
            tx.update(active, {'wipe_status': 'pending', 'wipe_queued_at': datetime.now(timezone.utc)})
            return {'wipe_job_id': job_id, 'dispatch_claimed': True}
        if status in {'pending', 'retrying', 'running', 'failed', 'completed'}:
            return {'wipe_job_id': job_id, 'dispatch_claimed': False}
    job_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    tx.set(
        active,
        {'wipe_status': 'pending', 'wipe_intent_at': now, 'wipe_queued_at': now, 'wipe_job_id': job_id},
        merge=True,
    )
    return {'wipe_job_id': job_id, 'dispatch_claimed': True}


def billing_failed(uid, subscription_id, error):
    client = _client()

    def update(tx, active):
        data = _read(active, tx) or {}
        if data.get('wipe_status') in {'pending', 'retrying', 'running', 'failed', 'completed'}:
            return False
        tx.set(
            active,
            {
                'wipe_status': 'billing_failed',
                'billing_failed_at': datetime.now(timezone.utc),
                'billing_subscription_id': subscription_id or '',
                'billing_error': error,
            },
            merge=True,
        )
        return True

    return _guard_completed(client.transaction(), *_refs(client, uid), update, lambda receipt: False)
