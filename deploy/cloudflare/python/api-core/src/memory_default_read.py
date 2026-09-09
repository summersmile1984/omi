"""D1 eligibility for owner lists and processed default-memory search.

This SQL applies the upstream canonical visibility / native pending exception
before COUNT, LIMIT and OFFSET. It is a storage predicate, not a classifier or
lineage resolver. Behavioral tests compare it with the original Python policy.
"""

import json

from memory_kernel_item import RESTRICTED_SENSITIVITY_LABELS


def default_read_predicate(*, include_pending: bool):
    processed = (
        "processing_state = 'processed' AND memory_tier IN ('short_term', 'long_term') "
        "AND visibility IN ('private', 'public', 'shared') "
        "AND json_type(sensitivity_labels_json) = 'array' "
        "AND NOT EXISTS (SELECT 1 FROM json_each(sensitivity_labels_json) label "
        "WHERE label.value IN (SELECT value FROM json_each(?)))"
    )
    states = '(' + processed + ')'
    if include_pending:
        # Original _canonical_scan_item_visible: an owner's raw required input
        # remains visible in the native list, including after its nominal TTL.
        # Search never receives this exception.
        states += (
            " OR (processing_state = 'pending' AND memory_tier = 'short_term' "
            "AND json_type(canonical_metadata_json, '$.promotion.required') = 'true')"
        )
    return (
        "(deleted_at IS NULL AND invalid_at IS NULL AND status = 'active' "
        "AND source_state = 'active' AND COALESCE(user_review, 1) != 0 "
        "AND json_type(canonical_metadata_json, '$.promotion.user_review') IS NOT 'false' "
        'AND (' + states + '))',
        [json.dumps(sorted(RESTRICTED_SENSITIVITY_LABELS))],
    )
