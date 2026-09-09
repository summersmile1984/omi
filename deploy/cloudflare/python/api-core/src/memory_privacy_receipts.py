"""Opaque deletion identities shared by every memory creator and privacy apply.

The HMAC domain and output match upstream privacy_deletion_receipt_id. A
dedicated deployment secret keeps receipt lifetime independent of auth rotation.
The D1 receipt gate owns the final, transaction-time admission decision.
"""

import hashlib
import hmac


def privacy_receipt_id(env, uid: str, memory_id: str) -> str:
    secret = getattr(env, 'MEMORY_PRIVACY_SECRET', None)
    if not isinstance(secret, str) or len(secret.encode('utf-8')) < 32:
        raise ValueError('memory privacy receipt secret is unavailable')
    if not uid or not memory_id or '\n' in uid or '\n' in memory_id:
        raise ValueError('invalid memory privacy identity')
    digest = hmac.new(
        secret.encode('utf-8'),
        f'memory-privacy-receipt.v2\n{uid}\n{memory_id}'.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    return f'receipt_{digest}'
