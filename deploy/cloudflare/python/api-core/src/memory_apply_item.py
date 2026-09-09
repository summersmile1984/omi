"""Read the single physical memory authority as an upstream canonical item."""

import hashlib
import json
from memory_apply_intake import MODEL_COLUMNS, encoded
from memory_kernel_evidence import MemoryEvidence
from memory_kernel_item import MemoryItem


def read_item(row):
    metadata = json.loads(row['canonical_metadata_json'])
    if set(metadata) & set(MODEL_COLUMNS):
        raise ValueError('memory metadata duplicates physical authority')
    fields = dict(metadata)
    for field, column in MODEL_COLUMNS.items():
        fields[field] = json.loads(row[column]) if column.endswith('_json') else row[column]
    if not metadata:
        # Pre-journal rows have no fabricated durable commit. This deterministic
        # historical marker exists only in the read projection; apply replaces
        # it with its actual committed head before writing the item.
        fingerprint = hashlib.sha256(encoded(row).encode()).hexdigest()
        fields.update(ledger_commit_id='historical_' + fingerprint, ledger_sequence=0)
        if not fields['evidence'] and fields['source_state'] == 'active':
            fields['evidence'] = [
                MemoryEvidence(
                    evidence_id='historical_' + fingerprint,
                    source_type='legacy_memory',
                    source_id=row['id'],
                    source_version=fingerprint,
                    content_hash=hashlib.sha256(row['content'].encode()).hexdigest(),
                    artifact_preservation='preserved',
                )
            ]
        fields['promotion'] = {'historical_materialization': True}
    return MemoryItem.model_validate(fields)
