"""Stage upstream consolidation decisions and normalization policy without I/O."""

from screen_frame_sources import selected_nodes


def consolidation_sources():
    common = (
        'from __future__ import annotations\n'
        'import hashlib, json, os, re\n'
        'from datetime import datetime, timezone\n'
        'from dataclasses import dataclass, field\n'
        'from typing import Any, Dict, List, Literal, Mapping, Optional, Set\n'
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
        {'RejectedMemoryFeedback'},
    )
    consolidation = selected_nodes(
        'backend/utils/memory/canonical_consolidation.py',
        {
            'CONSOLIDATION_BY',
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
    return {'memory_kernel_consolidation.py': common + normalization + belief + feedback + consolidation}
