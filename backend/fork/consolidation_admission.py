"""Shared admission for the exact duplicate/create failure observed with Qwen.

This only rejects a contradictory model decision. It never generates a route,
compares similarity scores, or equates paraphrases. Both deployment adapters
supply full hydrated identities before the first business mutation.
"""

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class MemoryIdentity:
    uid: str
    memory_id: str
    subject: str | None
    content: str
    tier: str
    active: bool

    @classmethod
    def from_item(cls, item):
        attribution = (item.promotion or {}).get('source_attribution') or {}
        subject = item.subject_entity_id
        known = item.user_asserted or (
            isinstance(attribution, dict)
            and attribution.get('subject_attribution') in {'user', 'third_party'}
            and attribution.get('subject_entity_id') == subject
        )
        return cls(
            uid=item.uid,
            memory_id=item.memory_id,
            subject=subject if known and subject else None,
            content=item.content or '',
            tier=item.tier.value,
            active=item.status.value == 'active' and item.source_state.value == 'active' and item.superseded_by is None,
        )


def validate_duplicate_creates(context, batch, identities: Mapping[str, MemoryIdentity]) -> str | None:
    for decision in batch.decisions:
        if decision.route != 'promote' or decision.reconciliation != 'create':
            continue
        source = identities.get(decision.source_memory_id)
        if source is None or source.uid != context.uid:
            return 'output_invalid:source_identity_unavailable'
        if not source.subject or not source.content or not source.active:
            continue
        for candidate in context.candidates_by_anchor.get(source.memory_id, ()):
            other = identities.get(candidate.memory_id)
            if other is None or other.uid != context.uid:
                return 'output_invalid:candidate_identity_unavailable'
            if (
                other.memory_id != source.memory_id
                and other.active
                and other.tier == 'long_term'
                and other.subject == source.subject
                and other.content == source.content
            ):
                return 'output_invalid:exact_duplicate_create'
    return None
