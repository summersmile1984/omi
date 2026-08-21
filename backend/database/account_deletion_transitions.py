"""Account-deletion state transitions and deletion-scoped resource reads."""

from datetime import datetime, timezone
from typing import Any

from database._client import get_firestore_client
from google.cloud.firestore_v1 import transactional

from database.account_deletion_policy import (
    ACCOUNT_DELETION_RECEIPT_SCHEMA_VERSION,
    account_deletion_blocks_access,
    normalize_account_deletion_status,
)


def read_agent_vm_migration_journals(uid: str) -> list[dict[str, Any]]:
    """Read migration journals before deleting the user's Firestore subtree.

    The journal is the durable source of truth for provider resources created
    during an Agent VM migration.  Callers must validate each returned record
    against the provider before issuing destructive requests.
    """
    if not uid.strip():
        raise ValueError('uid is required')
    client = get_firestore_client()
    migration_ref = client.collection('users').document(uid).collection('agentVmMigrations')
    journals: list[dict[str, Any]] = []
    for snapshot in migration_ref.stream():
        data = snapshot.to_dict()
        if not isinstance(data, dict):
            raise RuntimeError('Agent VM migration journal is malformed')
        journal = dict(data)
        if journal.get('migrationId') not in (None, snapshot.id):
            raise RuntimeError('Agent VM migration journal identity is ambiguous')
        journal['migrationId'] = snapshot.id
        journals.append(journal)
    journals.sort(key=lambda journal: str(journal.get('migrationId') or ''))
    return journals


@transactional
def mark_wipe_completed(transaction, active_ref, receipt_ref, generated_job_id: str) -> bool:
    """Atomically replace the UID-keyed active marker with an opaque receipt."""

    active_snapshot = active_ref.get(transaction=transaction)
    receipt_snapshot = receipt_ref.get(transaction=transaction)
    if not active_snapshot.exists:
        receipt_data = (receipt_snapshot.to_dict() or {}) if receipt_snapshot.exists else {}
        return receipt_data.get('wipe_status') == 'completed'

    data = active_snapshot.to_dict() or {}
    if data.get('late_agent_vm_cleanup'):
        transaction.set(
            active_ref,
            {'wipe_status': 'failed', 'wipe_failed_at': datetime.now(timezone.utc)},
            merge=True,
        )
        return False
    prior_receipt = (receipt_snapshot.to_dict() or {}) if receipt_snapshot.exists else {}
    wipe_job_id = data.get('wipe_job_id') or prior_receipt.get('wipe_job_id') or generated_job_id
    if not isinstance(wipe_job_id, str) or not wipe_job_id:
        raise RuntimeError('account deletion completion requires an opaque wipe job id')
    transaction.set(
        receipt_ref,
        {
            'schema_version': ACCOUNT_DELETION_RECEIPT_SCHEMA_VERSION,
            'wipe_status': 'completed',
            'wipe_job_id': wipe_job_id,
            'wipe_completed_at': datetime.now(timezone.utc),
        },
    )
    transaction.delete(active_ref)
    return True


@transactional
def record_late_agent_vm_cleanup(
    transaction,
    active_ref,
    receipt_ref,
    vm_name: str,
    zone: str,
    expected_instance_id: str | None = None,
) -> bool:
    active_snapshot = active_ref.get(transaction=transaction)
    receipt_snapshot = receipt_ref.get(transaction=transaction)
    active_data = (active_snapshot.to_dict() or {}) if active_snapshot.exists else {}
    receipt_data = (receipt_snapshot.to_dict() or {}) if receipt_snapshot.exists else {}
    authority = active_data if active_snapshot.exists else receipt_data
    authority_exists = active_snapshot.exists or receipt_snapshot.exists
    status = normalize_account_deletion_status(
        marker_exists=authority_exists,
        raw_status=authority.get('wipe_status'),
    )
    if not account_deletion_blocks_access(status):
        return False
    if expected_instance_id is not None and (not expected_instance_id.isascii() or not expected_instance_id.isdigit()):
        raise ValueError('late Agent VM cleanup instance identity must be numeric')
    pending = {'vmName': vm_name, 'zone': zone}
    if expected_instance_id is not None:
        pending['expectedInstanceId'] = expected_instance_id
    update = {
        'late_agent_vm_cleanup': pending,
        'wipe_status': 'failed',
        'wipe_failed_at': datetime.now(timezone.utc),
    }
    if active_snapshot.exists:
        transaction.set(active_ref, update, merge=True)
    else:
        wipe_job_id = receipt_data.get('wipe_job_id')
        if not isinstance(wipe_job_id, str) or not wipe_job_id:
            raise RuntimeError('completed deletion receipt is missing its opaque wipe job id')
        # Reopen the active state only while provider cleanup is outstanding.
        # The next successful completion atomically removes this UID-keyed row
        # and restores the minimal receipt.
        transaction.set(active_ref, {**update, 'wipe_job_id': wipe_job_id})
    return True


@transactional
def adopt_legacy_late_agent_vm_cleanup(
    transaction,
    doc_ref,
    vm_name: str,
    zone: str,
    expected_instance_id: str,
) -> bool:
    """Add a provider identity fence to an exact pre-fence cleanup record."""
    if not expected_instance_id.isascii() or not expected_instance_id.isdigit():
        raise ValueError('late Agent VM cleanup instance identity must be numeric')
    snapshot = doc_ref.get(transaction=transaction)
    data = (snapshot.to_dict() or {}) if snapshot.exists else {}
    raw_status = data.get('wipe_status')
    status = normalize_account_deletion_status(marker_exists=snapshot.exists, raw_status=raw_status)
    pending = data.get('late_agent_vm_cleanup')
    if not account_deletion_blocks_access(status) or not isinstance(pending, dict):
        return False
    if pending.get('vmName') != vm_name or pending.get('zone') != zone:
        return False
    current_id = pending.get('expectedInstanceId')
    if current_id is not None:
        return current_id == expected_instance_id
    transaction.update(doc_ref, {'late_agent_vm_cleanup.expectedInstanceId': expected_instance_id})
    return True
