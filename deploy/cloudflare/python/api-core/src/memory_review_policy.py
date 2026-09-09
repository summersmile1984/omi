"""Original canonical review accept/correct policy; consolidation owns promotion."""

import copy
import hashlib

from memory_apply_edit import _SETTLED_PROMOTION_FIELDS
from memory_kernel_admission import REQUIRED_PROCESSOR_ID, REQUIRED_PROCESSOR_VERSION
from memory_kernel_short_term_lifecycle import default_short_term_expiry


def review_resolution_patch(current, now, *, review_id, decision, correction):
    correction = correction or {}
    replacement = (
        (correction.get('memory_text') or correction.get('content')) if decision == 'correct' else current.content
    )
    if not isinstance(replacement, str) or not replacement.strip():
        replacement = current.content or ''
    replacement = replacement.strip()
    if not replacement:
        raise ValueError('canonical review resolution requires memory content')
    args = correction.get('arg_changes') if decision == 'correct' else None
    args = args if isinstance(args, dict) else {}
    promotion = {key: value for key, value in (current.promotion or {}).items() if key not in _SETTLED_PROMOTION_FIELDS}
    receipt = promotion.pop('processing_receipt', None)
    history = list(promotion.get('processing_history') or [])
    if isinstance(receipt, dict):
        history.append(receipt)
    audit = {}
    if args:
        audit['arg_changes'] = copy.deepcopy(args)
    target = correction.get('target_fact_id')
    if isinstance(target, str) and target.strip():
        audit['target_fact_id'] = target.strip()
    if decision == 'correct':
        audit['corrected_content_hash'] = hashlib.sha256(replacement.encode()).hexdigest()
    promotion.update(
        required=True,
        status='pending',
        processing_status='pending_processing',
        processor_id=REQUIRED_PROCESSOR_ID,
        processor_version=REQUIRED_PROCESSOR_VERSION,
        reason='canonical_review_' + decision,
        source_surface='memory_review_queue',
        attempt_count=0,
        processing_history=history[-10:],
        reviewed=True,
        user_review=True,
        review_resolution_id=review_id,
        review_decision=decision,
        review_correction=audit if decision == 'correct' else None,
    )
    logical = {
        'memory_text': replacement,
        'target_tier': 'short_term',
        'target_user_asserted': True,
        'clear_graph_assertion': current.graph_ready or current.kg_extracted,
    }
    if args:
        logical['arguments'] = {**current.arguments, **args}
    return (
        logical,
        {'promotion_audit': promotion, 'expires_at': default_short_term_expiry(now), 'kg_extracted': False},
        {
            'reviewed': 1,
            'user_review': 1,
        },
    )
