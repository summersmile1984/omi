"""Prepare the canonical privacy result; persistence and provider cleanup own IO.

The caller must supply an authoritative, transaction-fenced lineage and a fresh
server-generated epoch nonce. This result is not a deletion acknowledgement.
"""

from datetime import datetime
import re
from typing import Sequence

from memory_kernel_apply import ApplyResult, ApplyStatus, MemoryControlState
from memory_kernel_contracts import deterministic_contract_id
from memory_kernel_item import MemoryItem, MemoryItemStatus
from memory_kernel_lineage import canonical_lineage_root
from memory_kernel_operations import MemoryOperation, MemoryOperationType
from memory_kernel_privacy import (
    _privacy_delete_events,
    _privacy_tombstoned_evidence,
    _privacy_tombstoned_memory_item,
)


def privacy_lineage_ids(uid: str, requested_ids: Sequence[str], items: Sequence[MemoryItem]) -> list[str]:
    """Include incoming aliases, cycles and references to a missing survivor."""
    by_id = {item.memory_id: item for item in items}
    if not uid or len(by_id) != len(items) or any(item.uid != uid for item in items):
        raise ValueError('invalid privacy lineage authority')
    if not requested_ids or len(set(requested_ids)) != len(requested_ids):
        raise ValueError('invalid privacy deletion identities')
    if any(memory_id not in by_id for memory_id in requested_ids):
        raise ValueError('privacy target not found')
    roots = {canonical_lineage_root(by_id[memory_id], items_by_id=by_id) for memory_id in requested_ids}
    return sorted(item.memory_id for item in items if canonical_lineage_root(item, items_by_id=by_id) in roots)


def build_privacy_result(
    control: MemoryControlState,
    items: Sequence[MemoryItem],
    *,
    epoch_nonce: str,
    now: datetime,
    reason: str = 'explicit_memory_deletion',
) -> ApplyResult:
    """Use the original scrubbers and content-free privacy commit identity.

    Writer transition/payment locks do not gate explicit privacy deletion.
    Account/legal-hold, live item/evidence and control checks belong inside the
    storage transaction, before any part of this proposed result is persisted.
    """
    if not re.fullmatch(r'[0-9a-f]{64}', epoch_nonce):
        raise ValueError('privacy epoch requires a server-generated 256-bit nonce')
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('privacy timestamp requires a timezone')
    if not items or len({item.memory_id for item in items}) != len(items):
        raise ValueError('privacy deletion requires unique items')
    if any(item.uid != control.uid or item.status == MemoryItemStatus.tombstoned for item in items):
        raise ValueError('privacy item authority changed')
    ordered = sorted(items, key=lambda item: item.memory_id)
    if reason not in {'explicit_memory_deletion', 'canonical_review_reject', 'canonical_review_drop'}:
        raise ValueError('invalid memory privacy reason')
    operation = MemoryOperation.new(
        uid=control.uid,
        operation_type=MemoryOperationType.deletion,
        source_packet_id='privacy_delete:' + reason,
        target_memory_id=None,
        evidence_ids=[],
        logical_payload={
            'decision': 'delete',
            'reason': reason,
            'items': [{'memory_id': item.memory_id, 'item_revision': item.item_revision} for item in ordered],
        },
        account_generation=control.account_generation,
        source_generation=control.source_generation,
        observed_head_commit_id=None,
    )
    commit_id = (
        'commit_'
        + deterministic_contract_id(
            'memory-privacy-epoch',
            {
                'uid': control.uid,
                'deletion_gate_token': epoch_nonce,
                'commit_sequence': control.commit_sequence + 1,
            },
        )[:32]
    )
    committed = control.advance_head(commit_id).model_copy(
        update={
            'projection_watermark_commit_id': None,
            'vector_watermark_commit_id': None,
        }
    )
    tombstones, events = [], []
    for item in ordered:
        tombstone = _privacy_tombstoned_memory_item(
            item,
            embedded_evidence=[_privacy_tombstoned_evidence(e, scrub_source_identity=True) for e in item.evidence],
            now=now,
            commit_id=commit_id,
            commit_sequence=committed.commit_sequence,
            account_generation=committed.account_generation,
        )
        tombstones.append(tombstone)
        events.extend(
            _privacy_delete_events(
                uid=control.uid,
                item=tombstone,
                parent_control=control,
                committed_control=committed,
                operation_id=operation.operation_id,
                reason=reason,
                now=now,
            )
        )
    return ApplyResult(
        status=ApplyStatus.committed,
        control_state=committed,
        operation=operation.mark_committed(
            commit_id,
            committed_sequence=committed.commit_sequence,
            committed_memory_item_ids=[item.memory_id for item in tombstones],
            committed_outbox_event_ids=[event.event_id for event in events],
        ),
        memory_items=tombstones,
        outbox_events=events,
    )
