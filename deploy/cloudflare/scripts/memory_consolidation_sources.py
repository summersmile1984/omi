"""Stage upstream consolidation decisions and normalization policy without I/O."""

import json

from screen_frame_sources import selected_nodes, source


def consolidation_sources():
    common = (
        'from __future__ import annotations\n'
        'import hashlib, json, os, re\n'
        'from datetime import datetime, timedelta, timezone\n'
        'from dataclasses import dataclass, field\n'
        'from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence, Set, Tuple, cast\n'
        'from pydantic import BaseModel, Field, field_validator, model_validator\n'
        'from memory_kernel_item import (MemoryItem, MemoryItemStatus, MemoryLayer, '
        'ProcessingState, RESTRICTED_SENSITIVITY_LABELS, effective_short_term_expiry)\n'
        'from memory_kernel_evidence import SourceState\n'
        'from memory_kernel_apply import MemoryControlState, memory_content_hash\n'
        'from memory_kernel_contracts import LifecycleState, DurablePatchDecision, deterministic_contract_id\n'
        'from memory_kernel_operations import MemoryOperation, MemoryOperationType\n'
        'from memory_kernel_promotion import PromotionGraphPlan, build_promotion_admission_receipt\n'
        'from memory_kernel_recurrence import CanonicalRecurrenceSignal\n'
        'from memory_kernel_admission import REQUIRED_PROCESSING_RECEIPT_VERSION\n'
        'from memory_kernel_required_promotion import (REQUIRED_PROCESSOR_ID, REQUIRED_PROCESSOR_VERSION, '
        'REQUIRED_PROCESSING_STATUS_PENDING, REQUIRED_PROCESSING_STATUS_FAILED_RETRYABLE)\n'
        'from memory_kernel_sanitizer import sanitize_pii\n'
    )
    normalization = selected_nodes(
        'backend/utils/memory/canonical_required_processing.py',
        {
            '_PREDICATE_RE',
            'ProcessedRequiredMemory',
            'RequiredProcessingSubjectContradiction',
            'is_pending_required_processing',
            '_processing_receipt',
            '_subject_kind_from_id',
            '_conserved_processed_source_attribution',
        },
    )
    belief = selected_nodes(
        'backend/utils/memory/belief_model.py',
        {'MEMORY_BELIEF_MODEL_ENABLED_ENV', 'HALF_LIFE_DAYS_BY_CLASS', 'belief_model_enabled'},
    )
    feedback = selected_nodes(
        'backend/utils/memory/rejected_memory_feedback.py',
        {
            'RejectedMemoryFeedback',
            'REJECTED_MEMORY_FEEDBACK_QUERY_LIMIT',
            'REJECTED_MEMORY_FEEDBACK_SCAN_LIMIT',
            'REJECTED_MEMORY_FEEDBACK_ITEM_MAX_CHARS',
            'REJECTED_MEMORY_FEEDBACK_TOTAL_MAX_CHARS',
            'REJECTED_MEMORY_FEEDBACK_MAX_AGE',
            'bound_rejected_memory_examples',
            '_is_prompt_eligible_rejection',
        },
    )
    consolidation = selected_nodes(
        'backend/utils/memory/canonical_consolidation.py',
        {
            'CONSOLIDATION_BY',
            'CONSOLIDATION_ATTEMPT_LEASE_SECONDS',
            'MAX_CONSOLIDATION_FAILURE_ATTEMPTS',
            'CONSOLIDATION_RETRY_STATE_SCHEMA_VERSION',
            'ConsolidationRetryState',
            '_coerce_aware_utc',
            '_is_promotable_for_consolidation',
            '_safe_consolidation_failure_code',
            '_terminal_review_decision',
            'CONSOLIDATION_CONTEXT_MEMORY_CONTENT_MAX_CHARS',
            'CONSOLIDATION_CONTEXT_CANDIDATE_CONTENT_MAX_CHARS',
            'CONSOLIDATION_CONTEXT_EVIDENCE_QUOTES_MAX_COUNT',
            'CONSOLIDATION_CONTEXT_EVIDENCE_QUOTE_MAX_CHARS',
            'CONSOLIDATION_CONTEXT_EVIDENCE_IDS_MAX_COUNT',
            'CONSOLIDATION_CONTEXT_EVIDENCE_SOURCE_IDS_MAX_COUNT',
            'CONSOLIDATION_CONTEXT_QUOTE_FIELDS_MAX_COUNT',
            'CONSOLIDATION_CONTEXT_QUOTE_COLLECTION_MAX_COUNT',
            'CONSOLIDATION_CONTEXT_QUOTE_MAX_DEPTH',
            'CONSOLIDATION_CONTEXT_ARGUMENTS_MAX_CHARS',
            'CONSOLIDATION_CONTEXT_PROMOTION_MAX_CHARS',
            'CONSOLIDATION_CONTEXT_METADATA_TEXT_MAX_CHARS',
            'CONSOLIDATION_CONTEXT_METADATA_COLLECTION_MAX_COUNT',
            'CONSOLIDATION_CONTEXT_METADATA_MAX_DEPTH',
            'CONSOLIDATION_CONTEXT_CANDIDATES_PER_ANCHOR_MAX_COUNT',
            'CONSOLIDATION_CONTEXT_REDACTED_TEXT',
            'CONSOLIDATION_CONTEXT_TRUNCATION_SUFFIX',
            'CONSOLIDATION_AGENT_PROMPT',
            'CONSOLIDATION_CACHE_KEY',
            '_has_restricted_sensitivity',
            '_truncate_context_text',
            '_bounded_json_value',
            '_bounded_quote_value',
            '_bounded_metadata',
            '_bounded_evidence_quotes',
            'format_consolidation_llm_context',
            'build_consolidation_llm_messages',
            'Payload',
            '_empty_candidate_map',
            '_empty_str_list',
            '_empty_consolidation_decisions',
            '_empty_recurrence_signals',
            'ConsolidationCandidate',
            'ConsolidationContext',
            'ConsolidationAgentDecision',
            'ConsolidationAgentBatch',
            'ConsolidationApplySkipped',
            '_source_attribution_metadata',
            '_validate_agent_batch',
            '_agent_batch_blocks_watermark',
            '_consolidation_decision_identity',
            '_ordered_route_evidence_ids',
            '_route_result_status',
            '_route_target_tier',
            '_route_logical_payload',
            '_new_consolidation_operation',
            '_promotion_audit',
            '_processed_from_consolidation_decision',
            '_bind_required_promote_memory_text',
        },
    )
    cache = selected_nodes(
        'backend/utils/llm/prompt_cache.py',
        {'EXPLICIT_CACHE_MINIMUM_TOKENS', 'EXPLICIT_CACHE_MINIMUM_CHARACTERS', 'has_cacheable_prefix'},
    )
    # Freeze the original backend parser's schema text across Pydantic versions.
    # The Core suite compares this data contract to the current upstream model.
    schema = json.loads(source('deploy/cloudflare/contracts/consolidation-output-schema.json'))
    return {
        'memory_kernel_duplicate_admission.py': source('backend/fork/consolidation_admission.py'),
        'memory_kernel_consolidation.py': common + normalization + belief + feedback + cache + consolidation,
        'memory_kernel_consolidation_schema.py': 'CONSOLIDATION_OUTPUT_SCHEMA = ' + repr(schema) + '\n',
    }
