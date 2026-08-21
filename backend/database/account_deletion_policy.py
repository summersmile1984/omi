"""Pure policy for the durable account-deletion authority.

This module intentionally depends on no higher application layer so database,
service, and transport boundaries make the same fail-closed decision.
"""

from __future__ import annotations

import hashlib
import hmac
import os

ACCOUNT_DELETION_ACCESS_ALLOWED_STATUSES = frozenset({'cancelled', 'billing_failed'})
ACCOUNT_DELETION_INVALID_STATUS = '__invalid_account_deletion_status__'
ACCOUNT_DELETION_ACTIVE_COLLECTION = 'account_deletions'
ACCOUNT_DELETION_RECEIPT_COLLECTION = 'account_deletion_receipts'
ACCOUNT_DELETION_RECEIPT_SCHEMA_VERSION = 1


def account_deletion_receipt_id(uid: str) -> str:
    """Return a keyed pseudonymous lookup key for a completed deletion receipt.

    The receipt must remain addressable from an already-issued JWT so access
    stays fenced after the UID-keyed active marker is removed.  HMAC uses the
    deployment's durable encryption secret; the receipt contains no direct UID
    or deletion feedback. An operator holding both that secret and a candidate
    historical UID can still perform a membership test, so this is an opaque
    denial/idempotency control record rather than an anonymity claim.
    """

    if not uid.strip():
        raise ValueError('uid is required')
    secret = os.getenv('ENCRYPTION_SECRET', '').encode('utf-8')
    if len(secret) < 32:
        raise RuntimeError('ENCRYPTION_SECRET must be at least 32 bytes for account-deletion receipts')
    message = b'omi-account-deletion-receipt-v1\0' + uid.encode('utf-8')
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def normalize_account_deletion_status(*, marker_exists: bool, raw_status: object) -> str | None:
    """Normalize marker state without treating malformed markers as misses."""
    if not marker_exists:
        return None
    if isinstance(raw_status, str) and raw_status.strip():
        return raw_status.strip()
    return ACCOUNT_DELETION_INVALID_STATUS


def account_deletion_blocks_access(status: str | None) -> bool:
    """Deny every existing marker unless its state explicitly restores access."""
    return status is not None and status not in ACCOUNT_DELETION_ACCESS_ALLOWED_STATUSES


__all__ = [
    'ACCOUNT_DELETION_ACCESS_ALLOWED_STATUSES',
    'ACCOUNT_DELETION_ACTIVE_COLLECTION',
    'ACCOUNT_DELETION_INVALID_STATUS',
    'ACCOUNT_DELETION_RECEIPT_COLLECTION',
    'ACCOUNT_DELETION_RECEIPT_SCHEMA_VERSION',
    'account_deletion_receipt_id',
    'account_deletion_blocks_access',
    'normalize_account_deletion_status',
]
