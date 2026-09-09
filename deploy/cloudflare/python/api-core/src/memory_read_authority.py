"""Shared read-only memory head snapshot and deterministic row fingerprints."""

import hashlib
import json

HEAD = '''SELECT COALESCE(account.account_generation,0) AS trusted_generation,
control.control_json,control.account_generation,control.head_commit_id,
control.commit_sequence,control.source_generation,
EXISTS(SELECT 1 FROM cf_memories WHERE uid=owner.uid) AS has_memory,
(EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid=owner.uid) OR
EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=owner.uid)) AS deleted
FROM (SELECT ? AS uid) owner
LEFT JOIN cf_account_cutover account ON account.uid=owner.uid
LEFT JOIN cf_memory_apply_control control ON control.uid=owner.uid'''


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
