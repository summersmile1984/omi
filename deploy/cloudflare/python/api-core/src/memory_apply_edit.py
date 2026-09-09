"""Explicit corrections return to pending Short-term through canonical apply."""

import hashlib
from memory_apply_mutation import apply_user_memory_mutation
from memory_kernel_admission import REQUIRED_PROCESSOR_ID, REQUIRED_PROCESSOR_VERSION
from memory_kernel_short_term_lifecycle import default_short_term_expiry

_SETTLED_PROMOTION_FIELDS = {
    'route',
    'reconciliation',
    'target_memory_id',
    'relationship_to_user',
    'aboutness',
    'basis_for_memory',
    'confidence',
    'rationale',
    'processed_at',
    'processed_by',
    'from_tier',
    'to_tier',
    'promoted_at',
    'graph_plan',
    'admission_receipt',
}


def content_edit_patch(item, content, now):
    promotion = {key: value for key, value in (item.promotion or {}).items() if key not in _SETTLED_PROMOTION_FIELDS}
    receipt = promotion.pop('processing_receipt', None)
    processing_history = list(promotion.get('processing_history') or [])
    if isinstance(receipt, dict):
        processing_history.append(receipt)
    submission_history = list(promotion.get('submission_history') or [])
    if isinstance(promotion.get('submission'), dict):
        submission_history.append(promotion['submission'])
    promotion.update(
        required=True,
        status='pending',
        processing_status='pending_processing',
        processor_id=REQUIRED_PROCESSOR_ID,
        processor_version=REQUIRED_PROCESSOR_VERSION,
        reason='manual_user_correction',
        source_surface='memory_edit',
        attempt_count=0,
        reviewed=True,
        user_review=True,
        processing_history=processing_history[-10:],
        submission_history=submission_history[-10:],
        submission={
            'submission_id': f'{item.memory_id}:revision:{item.item_revision + 1}',
            'source_surface': 'memory_edit',
            'source_type': 'manual_edit',
            'source_id': item.memory_id,
            'content_hash': hashlib.sha256(content.encode()).hexdigest(),
            'submitted_at': now.isoformat(),
        },
    )
    return {
        'memory_text': content,
        'target_tier': 'short_term',
        'target_user_asserted': True,
        'clear_graph_assertion': True,
        'promotion_audit': promotion,
        'expires_at': default_short_term_expiry(now).isoformat(),
        'kg_extracted': False,
        'updated_at': now.isoformat(),
    }


async def edit_native_memory(env, uid, memory_id, content, now):
    if not isinstance(content, str) or not content.strip():
        raise ValueError('invalid memory correction')
    content = content.strip()

    def build(item, instant):
        updates = content_edit_patch(item, content, instant)
        logical = {
            'memory_text': content,
            'target_tier': 'short_term',
            'target_user_asserted': True,
            'clear_graph_assertion': True,
        }
        return logical, updates, {'edited': 1, 'reviewed': 1, 'user_review': 1}

    return await apply_user_memory_mutation(env, uid, memory_id, now, kind='content_edit', build_patch=build)
